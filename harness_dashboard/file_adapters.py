"""Read-only adapters for local JSONL harness histories.

Only the three documented roots are traversed. Session contents are read on
request; scans examine bounded prefixes for recorded metadata only.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone


_SCAN_BYTES = 512 * 1024
_LINE_BYTES = 1024 * 1024
_MAX_FILES = 5000
_TEXT_PAGE = 32 * 1024
_TIMEOUT = 5.0


def _string(value):
    return value if isinstance(value, str) else ''


def _time(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ''
    return ''

def _open_root(root):
    """Open an absolute root from the filesystem anchor without following links."""
    if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY'):
        raise OSError('safe directory traversal is unavailable on this platform')
    root = root.absolute()
    if not root.is_absolute() or any(part in ('.', '..') for part in root.parts[1:]):
        raise OSError('invalid source root')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    directory = os.open(root.anchor, flags)
    try:
        for part in root.parts[1:]:
            next_directory = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        return directory
    except BaseException:
        os.close(directory)
        raise


def _open_source(path, root, root_identity, file_identity=None):
    root = root.absolute()
    path = path.absolute()
    relative = path.relative_to(root)
    if not relative.parts or any(part in ('.', '..') for part in relative.parts):
        raise OSError('source path is outside its root')
    if not root_identity:
        raise OSError('source root identity is unavailable; rescan required')
    flags = os.O_RDONLY | os.O_NOFOLLOW
    directory = _open_root(root)
    try:
        root_stat = os.fstat(directory)
        if (root_stat.st_dev, root_stat.st_ino) != tuple(root_identity):
            raise OSError('source root changed since scan; rescan required')
        for part in relative.parts[:-1]:
            next_directory = os.open(part, flags | getattr(os, 'O_DIRECTORY', 0), dir_fd=directory)
            os.close(directory)
            directory = next_directory
        fd = os.open(relative.parts[-1], flags | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
        try:
            import stat
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError('source is not a regular file')
            if file_identity and (file_stat.st_dev, file_stat.st_ino) != tuple(file_identity):
                raise OSError('session file changed since scan; rescan required')
            return os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(directory)


def _lines(path, root, compressed, limit, state, root_identity):
    """Yield complete bounded lines; report truncation and decoder errors in state."""
    proc = None
    source = None
    selector = None
    stopped = False
    try:
        source = _open_source(path, root, root_identity)
        if compressed:
            executable = shutil.which('zstd')
            if not executable:
                state.append('zstd executable is unavailable')
                return
            proc = subprocess.Popen([executable, '-dc'], stdin=source, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, close_fds=True)
            stream = proc.stdout
            selector = selectors.DefaultSelector()
            selector.register(stream, selectors.EVENT_READ)
            deadline = time.monotonic() + _TIMEOUT
        else:
            stream = source
        pending = b''
        read_bytes = 0
        while read_bytes < limit:
            if proc:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    state.append('decompression timed out')
                    stopped = True
                    break
                chunk = os.read(stream.fileno(), min(65536, limit - read_bytes))
            else:
                chunk = stream.read(min(65536, limit - read_bytes))
            if not chunk:
                break
            read_bytes += len(chunk)
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if len(line) > _LINE_BYTES:
                    state.append('oversized JSONL record omitted')
                    continue
                yield line
            if len(pending) > _LINE_BYTES:
                state.append('oversized JSONL record; remainder not read')
                stopped = True
                break
        else:
            state.append('reading stopped at byte limit; more content may exist')
            stopped = True
        if pending and not stopped:
            state.append('incomplete final JSONL line omitted')
        if proc and not stopped:
            try:
                code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                if code:
                    state.append('zstd could not fully decode the file')
            except subprocess.TimeoutExpired:
                state.append('decompression timed out')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        state.append('source read error: ' + str(exc))
    finally:
        if selector:
            selector.close()
        if proc:
            if proc.poll() is None:
                proc.kill()
            if proc.stdout:
                proc.stdout.close()
            proc.wait()
        if source:
            source.close()


def _records(path, root, compressed, limit, state, root_identity):
    for line in _lines(path, root, compressed, limit, state, root_identity):
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError('not an object')
        except (ValueError, UnicodeError, RecursionError):
            state.append('malformed JSONL record omitted')
            continue
        yield record


def _metadata_string(line, key):
    """Read one JSON string field without decoding a conversation payload."""
    pattern = rb'(?<!\\)"' + re.escape(key.encode('ascii')) + rb'"\s*:\s*("(?:\\.|[^"\\])*")'
    match = re.search(pattern, line)
    if not match:
        return ''
    try:
        value = json.loads(match.group(1))
        return _string(value)
    except (ValueError, UnicodeError):
        return ''


def _paths(root, source, notices):
    count = 0
    directory_count = 0
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: notices.append(str(e))):
        directory_count += 1
        if directory_count > _MAX_FILES * 2:
            notices.append('directory discovery limit reached')
            return
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            if source == 'DSH':
                match = name == 'session.v4.jsonl.zstd'
                if not match and name.startswith('session.') and name.endswith('.jsonl.zstd'):
                    notices.append('unsupported DSH session file version omitted')
            else:
                match = name.endswith('.jsonl')
            if not match:
                continue
            path = Path(directory) / name
            if path.is_symlink():
                notices.append('symlinked source omitted')
                continue
            count += 1
            if count > _MAX_FILES:
                notices.append('file discovery limit reached')
                return
            yield path


def _id(source, path, root):
    rel = str(path.relative_to(root))
    return source + ':' + hashlib.sha256(rel.encode('utf-8', 'surrogateescape')).hexdigest()


def _summarize_notices(notices):
    return '; '.join(text + (f' ({count} occurrences)' if count > 1 else '')
                     for text, count in Counter(notices).items())


def scan(home: Path):
    sessions = []
    sources = []
    roots = [('OMP', 'OMP', home / '.omp/agent/sessions'),
             ('DSH', 'DSH', home / '.dsh/sessions'),
             ('Claude', 'Claude', home / '.claude/projects')]
    configured = os.environ.get('CLAUDE_CONFIG_DIR', '').strip()
    if configured:
        configured_root = Path(configured).expanduser().absolute() / 'projects'
        if configured_root != roots[-1][2].absolute():
            roots.append(('Claude · configured', 'Claude', configured_root))
    for source, kind, root in roots:
        root = root.absolute()
        if not root.exists():
            sources.append({'name': source, 'status': 'missing', 'detail': 'Source directory not found.', 'root': str(root)})
            continue
        if not root.is_dir() or root.is_symlink():
            sources.append({'name': source, 'status': 'error', 'detail': 'Source root is not a real directory.', 'root': str(root)})
            continue
        if kind == 'DSH' and not shutil.which('zstd'):
            sources.append({'name': source, 'status': 'error', 'detail': 'zstd executable is unavailable.', 'root': str(root)})
            continue
        try:
            root_fd = _open_root(root)
            try:
                root_stat = os.fstat(root_fd)
                root_identity = (root_stat.st_dev, root_stat.st_ino)
            finally:
                os.close(root_fd)
        except OSError as exc:
            sources.append({'name': source, 'status': 'error',
                            'detail': 'Cannot safely open source root: ' + str(exc), 'root': str(root)})
            continue
        notices = []
        found = []
        unreadable = 0
        paths = list(_paths(root, kind, notices))
        for path in paths:
            state = []
            session = {'id': _id(source, path, root), 'source': source,
                       'title': path.stem, 'project': '', 'path': str(path),
                       'updated': '', 'parent_id': '', 'status': '', 'model': '',
                       '_adapter': 'file', '_kind': kind,
                       '_root': str(root), '_root_identity': root_identity,
                       '_compressed': kind == 'DSH', '_notices': []}
            try:
                with _open_source(path, root, root_identity) as source_file:
                    file_stat = os.fstat(source_file.fileno())
                    session['updated'] = file_stat.st_mtime
                    session['_file_identity'] = (file_stat.st_dev, file_stat.st_ino)
                    session['_scan_size'] = file_stat.st_size
            except OSError as exc:
                unreadable += 1
                notices.append('file metadata unavailable: ' + str(exc))
                continue
            header = False
            # A bounded prefix is enough for headers and titles. Avoid parsing
            # message bodies for list metadata, including large tool outputs.
            for index, line in enumerate(_lines(path, root, kind == 'DSH', _SCAN_BYTES, state, root_identity)):
                if index >= 128:
                    state.append('metadata scan stopped at record limit')
                    break
                typ = _metadata_string(line, 'type')
                if kind == 'Claude':
                    recorded_id = _metadata_string(line, 'sessionId')
                    if recorded_id and not header:
                        header = True
                        session['_recorded_id'] = recorded_id
                        session['project'] = _metadata_string(line, 'cwd')
                        session['model'] = _metadata_string(line, 'model')
                    if typ == 'custom-title':
                        session['title'] = _metadata_string(line, 'customTitle') or session['title']
                    continue
                allowed = ({'session', 'title', 'title_change', 'model_change'} if kind == 'OMP'
                           else {'session', 'session/title', 'model/selection',
                                 'subagent/descriptor', 'turn/end'})
                if typ not in allowed:
                    if not typ:
                        state.append('record type missing or malformed in metadata scan')
                    continue
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise ValueError('not an object')
                except (ValueError, UnicodeError, RecursionError):
                    state.append('malformed JSONL metadata record omitted')
                    continue
                if typ == 'session' and kind in ('OMP', 'DSH'):
                    header = True
                    session['_recorded_id'] = _string(obj.get('id'))
                    session['project'] = _string(obj.get('cwd'))
                    session['title'] = _string(obj.get('title')) or session['title']
                    session['_parent_ref'] = _string(obj.get('parentSession'))
                elif kind == 'OMP' and typ in ('title', 'title_change'):
                    session['title'] = _string(obj.get('title')) or session['title']
                elif kind == 'OMP' and typ == 'model_change':
                    session['model'] = _string(obj.get('model')) or session['model']
                elif kind == 'DSH':
                    data = obj.get('data') if isinstance(obj.get('data'), dict) else {}
                    if typ == 'session/title':
                        session['title'] = _string(data.get('title')) or session['title']
                    elif typ in ('model/selection', 'subagent/descriptor'):
                        session['model'] = _string(data.get('model') or data.get('agentModel')) or session['model']
                    elif typ == 'turn/end' and isinstance(data.get('reason'), dict):
                        reason = _string(data['reason'].get('kind'))
                        session['status'] = ('last turn: ' + reason) if reason else ''
            if not header:
                state.append('session header or identifying metadata not found in scanned prefix')
            if any(item.startswith('source read error:') or item in
                   ('zstd could not fully decode the file', 'decompression timed out')
                   for item in state):
                unreadable += 1
            session['_notices'] = list(dict.fromkeys(state))
            if state:
                notices.extend(state)
            found.append(session)
        # Resolve only to actually published sessions. OMP duplicates recorded IDs.
        by_path = {s['path']: s for s in found}
        by_recorded = {}
        for s in found:
            if s.get('_recorded_id'):
                by_recorded.setdefault(s['_recorded_id'], []).append(s)
        for s in found:
            ref = s.get('_parent_ref', '')
            parent = None
            if ref:
                if os.path.isabs(ref):
                    parent = by_path.get(ref)
                else:
                    candidates = by_recorded.get(ref, [])
                    if len(candidates) == 1:
                        parent = candidates[0]
                if parent and parent is not s:
                    s['parent_id'] = parent['id']
                else:
                    s['_notices'].append('recorded parent is unavailable or ambiguous')
            elif kind == 'Claude':
                # Claude stores subagents under <session-id>/subagents/*.jsonl.
                p = Path(s['path'])
                if p.parent.name == 'subagents':
                    parent_path = p.parent.parent.parent / (p.parent.parent.name + '.jsonl')
                    parent = by_path.get(str(parent_path))
                    if parent:
                        s['parent_id'] = parent['id']
                    else:
                        s['_notices'].append('subagent parent session is unavailable')
        sessions.extend(found)
        status = ('error' if paths and unreadable == len(paths)
                  else 'ok' if found else 'error' if notices else 'empty')
        detail_text = _summarize_notices(notices) if notices else (f'{len(found)} session files found.' if found else 'No session files found.')
        sources.append({'name': source, 'status': status, 'detail': detail_text, 'root': str(root)})
    return sessions, sources


def _render(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    except (ValueError, TypeError, RecursionError):
        return '[unrenderable content]'


def detail(session: dict, offset=0, limit=80):
    # Imported lazily so the renderer can reuse the source security helpers.
    from . import file_history
    return file_history.detail(session, offset, limit)
