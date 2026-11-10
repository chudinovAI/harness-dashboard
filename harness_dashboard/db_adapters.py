"""Read-only adapters for the local SQLite-backed harnesses.

Only metadata is read during a scan. Conversation records are loaded on demand;
credential, configuration, instruction and system-prompt tables are never read.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import stat
import sqlite3
import tempfile
from pathlib import Path


MAX_SESSIONS = 20000
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024 * 1024


def _id(source, *parts):
    encoded = base64.urlsafe_b64encode(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).decode().rstrip('=')
    return source + ':' + encoded


def _text(value):
    if value is None:
        return ''
    return value if isinstance(value, str) else str(value)


def _timestamp(value):
    if isinstance(value, (int, float)):
        return value / 1000 if abs(value) > 100_000_000_000 else value
    return _text(value)


def _safe_root(home, relative):
    home = Path(home).resolve()
    root = home / relative
    # The source root itself must not redirect into another location.
    if root.resolve() != root:
        raise ValueError('source root contains a symlink')
    return root


def _open_directory(path):
    """Traverse from the filesystem root without following any symlink."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('unsafe absolute directory')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_beneath(root, path):
    candidate = Path(path)
    if not candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError('unsafe source path')
    relative = candidate.relative_to(root)
    if not relative.parts:
        raise ValueError('source path is a directory')
    directory = _open_directory(root)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError('source path is not a regular file')
        return fd
    finally:
        os.close(directory)


def _safe_path(root, path, must_exist=True):
    try:
        candidate = Path(path)
        fd = _open_beneath(root, candidate)
        os.close(fd)
        return candidate
    except (OSError, ValueError, RuntimeError, TypeError):
        return None


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def _stat_at(directory, name):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('database or sidecar is not a regular file')
    return _signature(info)


def _copy_snapshot(fd, destination, directory_fd, filename):
    # APFS copy-on-write cloning avoids copying a large database on every read.
    # On other filesystems the portable copy remains subject to the size limit.
    try:
        clone = ctypes.CDLL(None, use_errno=True).fclonefileat
        clone.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
        clone.restype = ctypes.c_int
        if clone(fd, directory_fd, os.fsencode(filename), 0) == 0:
            return
    except (AttributeError, OSError):
        pass
    with os.fdopen(os.dup(fd), 'rb') as source, open(destination, 'xb') as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)


class _SnapshotConnection(sqlite3.Connection):
    def close(self):
        try:
            super().close()
        finally:
            self._snapshot.cleanup()


def _db(root, relative):
    """Open a stable private database/WAL snapshot; never open SQLite on source."""
    path = root / relative
    directory = _open_directory(path.parent)
    temp = None
    handles = []
    try:
        names = (path.name, path.name + '-wal', path.name + '-journal')
        before = {name: _stat_at(directory, name) for name in names}
        if before[names[0]] is None:
            raise FileNotFoundError('database not found')
        if before[names[2]] is not None:
            raise ValueError('database has a rollback journal (including possibly inactive PERSIST journals); safe snapshot unavailable')
        present = [name for name in names[:2] if before[name] is not None]
        if sum(before[name][2] for name in present) > MAX_SNAPSHOT_BYTES:
            raise ValueError('database and WAL exceed the snapshot size limit')
        for name in present:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            handles.append(fd)
            if _signature(os.fstat(fd)) != before[name]:
                raise ValueError('database changed while preparing snapshot')
        temp = tempfile.TemporaryDirectory(prefix='harness-db-')
        temp_path = Path(temp.name)
        dest_dir = os.open(temp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name, fd in zip(present, handles):
                _copy_snapshot(fd, temp_path / name, dest_dir, name)
        finally:
            os.close(dest_dir)
        after = {name: _stat_at(directory, name) for name in names}
        if before != after or any(_signature(os.fstat(fd)) != before[name] for name, fd in zip(present, handles)):
            raise ValueError('database changed while taking snapshot; try again')
        conn = sqlite3.connect((temp_path / path.name).as_uri() + '?mode=rw', uri=True, timeout=2, factory=_SnapshotConnection)
        conn._snapshot = temp
        temp = None
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
        except BaseException:
            conn.close()
            raise
        return conn, path
    finally:
        for fd in handles:
            os.close(fd)
        os.close(directory)
        if temp is not None:
            temp.cleanup()


def _rows(conn, query, params=(), limit=MAX_SESSIONS):
    cursor = conn.execute(query, params)
    result = cursor.fetchmany(limit + 1)
    return result[:limit], len(result) > limit




def _session(source, ident, dataset, path, **fields):
    return {'id': ident, 'source': source, 'title': _text(fields.get('title')),
            'project': _text(fields.get('project')), 'path': _text(path),
            'updated': _timestamp(fields.get('updated')), 'parent_id': _text(fields.get('parent_id')),
            'status': _text(fields.get('status')), 'model': _text(fields.get('model')),
            '_dataset': dataset, '_key': fields.get('key'), '_root': _text(fields.get('root'))}


def _status(name, state, detail, root):
    return {'name': name, 'status': state, 'detail': detail, 'root': _text(root)}


def _scan_zcode(home):
    name = 'ZCode'
    try:
        root = _safe_root(home, '.zcode')
    except (ValueError, OSError, RuntimeError) as exc:
        return [], _status(name, 'error', str(exc), Path(home) / '.zcode')
    sessions, notes, states = [], [], []
    try:
        con, dbpath = _db(root, 'v2/tasks-index.sqlite')
        try:
            rows, truncated = _rows(con, 'SELECT workspace_key, workspace_path, task_id, title, task_status, model, updated_at, forked_from_task_id FROM tasks ORDER BY updated_at DESC LIMIT ?', (MAX_SESSIONS + 1,))
            for r in rows:
                key = (_text(r['workspace_key']), _text(r['task_id']))
                ident = _id(name, 'task', *key)
                parent = _id(name, 'task', key[0], r['forked_from_task_id']) if r['forked_from_task_id'] else ''
                s = _session(name, ident, 'zcode_task', dbpath, title=r['title'] or 'ZCode task', project=r['workspace_path'], updated=r['updated_at'], status=r['task_status'], model=r['model'], parent_id=parent, key=key, root=root)
                sessions.append(s)
            states.append('ok' if rows else 'empty')
            notes.append('Task index: ' + ('limited to %d records' % MAX_SESSIONS if truncated else '%d records' % len(rows)))
        finally:
            con.close()
    except FileNotFoundError:
        states.append('missing'); notes.append('Task index: missing')
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        states.append('error'); notes.append('Task index: ' + str(exc))
    try:
        con, dbpath = _db(root, 'cli/db/db.sqlite')
        try:
            rows, truncated = _rows(con, 'SELECT id, parent_id, directory, title, time_updated, last_outcome FROM session ORDER BY time_updated DESC LIMIT ?', (MAX_SESSIONS + 1,))
            for r in rows:
                raw_id = _text(r['id'])
                parent = _id(name, 'cli', r['parent_id']) if r['parent_id'] else ''
                sessions.append(_session(name, _id(name, 'cli', raw_id), 'zcode_cli', dbpath, title=r['title'] or 'ZCode CLI session', project=r['directory'], updated=r['time_updated'], status=r['last_outcome'], parent_id=parent, key=raw_id, root=root))
            states.append('ok' if rows else 'empty')
            notes.append('CLI database: ' + ('limited to %d records' % MAX_SESSIONS if truncated else '%d records' % len(rows)))
        finally:
            con.close()
    except FileNotFoundError:
        states.append('missing'); notes.append('CLI database: missing')
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        states.append('error'); notes.append('CLI database: ' + str(exc))
    state = 'error' if 'error' in states else ('ok' if sessions else ('missing' if all(x == 'missing' for x in states) else 'empty'))
    return sessions, _status(name, state, '; '.join(notes), root)


def _scan_single(home, name, relative, dbname, dataset, query, convert):
    stage = 'opening snapshot'
    try:
        root = _safe_root(home, relative)
        con, dbpath = _db(root, dbname)
        stage = 'reading metadata'
        try:
            rows, truncated = _rows(con, query, (MAX_SESSIONS + 1,))
            sessions = [convert(r, root, dbpath) for r in rows]
        finally:
            con.close()
        detail = '%d records' % len(sessions)
        if truncated:
            detail += '; limited to %d records' % MAX_SESSIONS
        return sessions, _status(name, 'ok' if sessions else 'empty', detail, root)
    except FileNotFoundError:
        return [], _status(name, 'missing', 'Database not found', Path(home) / relative)
    except (sqlite3.Error, OSError, ValueError, RuntimeError, TypeError) as exc:
        return [], _status(name, 'error', f'{stage}: {exc}', Path(home) / relative)


def _codex_row(r, root, dbpath):
    path = None
    # Rollout paths from the index may only select files under these two roots.
    for sub in ('sessions', 'archived_sessions'):
        allowed = root / sub
        if allowed.resolve() != allowed:
            continue
        path = _safe_path(allowed, r['rollout_path'])
        if path:
            break
    return _session('Codex', _id('Codex', r['id']), 'codex', path or '', title='Codex thread ' + _text(r['id'])[:12], project=r['cwd'], updated=r['updated_at'], model=r['model'], parent_id=_id('Codex', r['parent_id']) if r['parent_id'] else '', key=_text(r['id']), root=root)


def _opencode_row(r, root, dbpath):
    return _session('OpenCode', _id('OpenCode', r['id']), 'opencode', dbpath, title=r['title'] or 'OpenCode session', project=r['directory'], updated=r['time_updated'], parent_id=_id('OpenCode', r['parent_id']) if r['parent_id'] else '', status=r['idle_outcome'], model=r['model'], key=_text(r['id']), root=root)


def _hermes_row(r, root, dbpath):
    return _session('Hermes', _id('Hermes', r['id']), 'hermes', dbpath, title=r['display_name'] or 'Hermes session', project=r['cwd'] or r['git_repo_root'], updated=r['ended_at'] or r['started_at'], parent_id=_id('Hermes', r['parent_session_id']) if r['parent_session_id'] else '', status=r['end_reason'], model=r['model'], key=_text(r['id']), root=root)


def scan(home):
    """Return public metadata plus private keys needed for subsequent detail."""
    all_sessions, statuses = [], []
    scanners = [lambda: _scan_zcode(home),
                lambda: _scan_single(home, 'Codex', '.codex', 'state_5.sqlite', 'codex', 'SELECT id, rollout_path, cwd, updated_at, model, (SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id=threads.id LIMIT 1) AS parent_id FROM threads ORDER BY updated_at DESC LIMIT ?', _codex_row),
                lambda: _scan_single(home, 'OpenCode', '.local/share/opencode', 'opencode.db', 'opencode', 'SELECT id, parent_id, directory, title, time_updated, idle_outcome, model FROM session_v2 ORDER BY time_updated DESC LIMIT ?', _opencode_row),
                lambda: _scan_single(home, 'Hermes', '.hermes', 'state.db', 'hermes', 'SELECT id, display_name, cwd, git_repo_root, ended_at, started_at, parent_session_id, end_reason, model FROM sessions ORDER BY COALESCE(ended_at, started_at) DESC LIMIT ?', _hermes_row)]
    for scanner in scanners:
        sessions, status = scanner()
        all_sessions.extend(sessions)
        statuses.append(status)
    return all_sessions, statuses


def detail(session, offset=0, limit=80):
    """Read one page of the selected conversation from its permitted source."""
    # Imported lazily: the detail reader uses the shared secure snapshot helpers.
    from .db_details import detail as read_detail
    return read_detail(session, offset, limit)
