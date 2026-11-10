"""Fixed, stdin-driven worker executed over SSH on the user's Hermes host.

It reads only the Hermes state database and the sessions artifact directory.
The temporary SQLite/WAL snapshot is private and removed on normal exit; an
abrupt process or machine failure can leave private snapshot files on the VPS.
"""
import base64
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

ROOT = None  # Set from the explicit request before any operation.
SOURCE = 'Hermes VPS'
MAX_SNAPSHOT = 2 * 1024 * 1024 * 1024
CHUNK = 48 * 1024
FILE_CHUNK = 128 * 1024
MAX_IMAGE = 4 * 1024 * 1024
DIR_PAGE = 250
NOFOLLOW = os.O_NOFOLLOW
DIRECTORY = os.O_DIRECTORY
NONBLOCK = os.O_NONBLOCK
SECRET = re.compile(r'(secret|credential|password|passwd|token|key|private|auth|config|setting|\.env|cookie|^id_)', re.I)
IMAGES = {'.png': ('image/png', lambda b: b.startswith(b'\x89PNG\r\n\x1a\n')),
          '.jpg': ('image/jpeg', lambda b: b.startswith(b'\xff\xd8\xff')),
          '.jpeg': ('image/jpeg', lambda b: b.startswith(b'\xff\xd8\xff')),
          '.gif': ('image/gif', lambda b: b.startswith((b'GIF87a', b'GIF89a'))),
          '.webp': ('image/webp', lambda b: b.startswith(b'RIFF') and b[8:12] == b'WEBP')}


def configure_root(value):
    """Accept only an explicit absolute root, without normalizing traversal."""
    global ROOT
    if (not isinstance(value, str) or not value.startswith('/') or value == '/'
            or any(part in ('', '.', '..') for part in value.split('/')[1:])
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
            or len(value.encode('utf-8')) > 4096):
        raise ValueError('Недопустимый корень Hermes.')
    ROOT = Path(value)


def signature(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)


def stat_at(fd, name):
    try:
        return signature(os.stat(name, dir_fd=fd, follow_symlinks=False))
    except FileNotFoundError:
        return None


def open_root():
    """Open every component from / without following symlinks."""
    if ROOT is None or not ROOT.is_absolute() or any(part in ('.', '..') for part in ROOT.parts[1:]):
        raise ValueError('Недопустимый корень Hermes.')
    fd = os.open('/', os.O_RDONLY | DIRECTORY | NOFOLLOW)
    try:
        for part in ROOT.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def snapshot():
    """Copy a stable database and WAL without opening SQLite on the source."""
    root = open_root()
    temp = None
    try:
        names = ('state.db', 'state.db-wal', 'state.db-journal')
        before = [stat_at(root, name) for name in names]
        if before[0] is None:
            raise RuntimeError('База Hermes не найдена.')
        if before[2] is not None:
            raise RuntimeError('Обнаружен rollback-журнал; безопасный снимок невозможен.')
        if sum(s[2] for s in before if s) > MAX_SNAPSHOT:
            raise RuntimeError('База и WAL превышают предел снимка 2 ГиБ.')
        temp = tempfile.TemporaryDirectory(prefix='hermes-dashboard-')
        os.chmod(temp.name, 0o700)
        for name, expected in zip(names[:2], before[:2]):
            if expected is None:
                continue
            fd = os.open(name, os.O_RDONLY | NOFOLLOW | NONBLOCK, dir_fd=root)
            try:
                current = os.fstat(fd)
                if not stat.S_ISREG(current.st_mode) or signature(current) != expected:
                    raise RuntimeError('База изменилась при создании снимка.')
                target = os.open(os.path.join(temp.name, name), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(target, 'wb') as out, os.fdopen(os.dup(fd), 'rb') as src:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                if signature(os.fstat(fd)) != expected:
                    raise RuntimeError('База изменилась при создании снимка.')
            finally:
                os.close(fd)
        if [stat_at(root, name) for name in names] != before:
            raise RuntimeError('База изменилась при создании снимка; повторите запрос.')
        conn = sqlite3.connect('file:' + os.path.join(temp.name, 'state.db') + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        return conn, temp
    except BaseException:
        if temp is not None:
            temp.cleanup()
        raise
    finally:
        os.close(root)


def scan():
    conn, temp = snapshot()
    try:
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(messages)')}
        visible = " AND COALESCE(display_kind, '') != 'hidden'" if 'display_kind' in columns else ''
        rows = conn.execute('''WITH counts AS (
            SELECT session_id, COUNT(*) AS message_count FROM messages
            WHERE role IN ('user','assistant','tool')''' + visible + '''
            GROUP BY session_id)
            SELECT s.id, s.display_name, s.title, s.cwd, s.git_repo_root,
            s.ended_at, s.started_at, s.last_activity_at, s.parent_session_id,
            s.end_reason, s.model, COALESCE(counts.message_count, 0) AS message_count
            FROM sessions AS s LEFT JOIN counts ON counts.session_id = s.id
            ORDER BY COALESCE(s.last_activity_at, s.ended_at, s.started_at) DESC''')
        return {'rows': [dict(row) for row in rows]}
    finally:
        conn.close()
        temp.cleanup()


def pieces(text):
    for pos in range(0, len(text), CHUNK):
        yield text[pos:pos + CHUNK]


def detail(req):
    key = req.get('key')
    offset = req.get('offset', 0)
    limit = req.get('limit', 80)
    if not isinstance(key, str) or not key or len(key) > 2048 or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 80:
        raise ValueError('Некорректные параметры истории.')
    conn, temp = snapshot()
    try:
        if conn.execute('SELECT 1 FROM sessions WHERE id=?', (key,)).fetchone() is None:
            raise ValueError('Сессия на VPS не найдена.')
        notes = ['Системные и неподдерживаемые роли не отображаются; вложения из базы не извлекаются автоматически.']
        output = []
        index = 0
        more = False
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(messages)')}
        visible = " AND COALESCE(display_kind, '') != 'hidden'" if 'display_kind' in columns else ''
        rows = conn.execute('''SELECT role, timestamp, content, tool_calls, tool_name
            FROM messages WHERE session_id=? AND role IN ('user','assistant','tool')''' + visible + ' ORDER BY id', (key,))
        for row in rows:
            role = row['role']
            timestamp = '' if row['timestamp'] is None else str(row['timestamp'])
            values = []
            content = row['content']
            if content is not None and not isinstance(content, str):
                notes.append('Некоторое содержимое сообщения имеет неподдерживаемый формат.')
            elif content:
                values.append((role, 'tool' if role == 'tool' else 'message', content))
            calls = row['tool_calls']
            if calls is not None and not isinstance(calls, str):
                notes.append('Некоторые вызовы инструментов имеют неподдерживаемый формат.')
            elif calls:
                try:
                    json.loads(calls)
                except (ValueError, TypeError):
                    notes.append('Некоторые вызовы инструментов содержат некорректный JSON и показаны как текст.')
                name = row['tool_name'] or 'tool calls'
                values.append(('tool', 'tool', str(name) + '\n' + calls))
            for block_role, kind, value in values:
                for part in pieces(value):
                    if index >= offset:
                        if len(output) == limit:
                            more = True
                            break
                        output.append({'role': block_role, 'kind': kind, 'text': part, 'time': timestamp})
                    index += 1
                if more:
                    break
            if more:
                break
        return {'messages': output, 'files': [], 'notice': ' '.join(dict.fromkeys(notes)),
                'has_more': more, 'next_offset': offset + len(output) if more else None}
    finally:
        conn.close()
        temp.cleanup()


def components(path):
    if not isinstance(path, str) or len(path) > 4096 or '\x00' in path or path.startswith('/'):
        raise ValueError('Недопустимый путь.')
    if path in ('', '.'):
        return []
    parts = path.split('/')
    if any(p in ('', '.', '..') or len(p.encode()) > 255 for p in parts):
        raise ValueError('Недопустимый путь.')
    return parts


def restricted(parts, is_dir=False):
    if any(p.startswith('.') or SECRET.search(p) for p in parts):
        return True
    if is_dir or not parts:
        return False
    name = parts[-1]
    return Path(name).suffix.lower() not in {'.txt', '.md', '.jsonl', *IMAGES}


def open_path(parts, directory):
    root_fd = open_root()
    try:
        fd = os.open('sessions', os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        for i, part in enumerate(parts):
            is_dir = i < len(parts) - 1 or directory
            if restricted(parts[:i + 1], is_dir=is_dir):
                raise PermissionError('Доступ к этому пути ограничен.')
            nxt = os.open(part, os.O_RDONLY | NOFOLLOW | (DIRECTORY if is_dir else 0) | NONBLOCK, dir_fd=fd)
            os.close(fd)
            fd = nxt
        mode = os.fstat(fd).st_mode
        if directory and not stat.S_ISDIR(mode) or not directory and not stat.S_ISREG(mode):
            raise ValueError('Неподдерживаемый тип файла.')
        return fd
    except BaseException:
        os.close(fd)
        raise


def files(req):
    parts = components(req.get('path', ''))
    offset = req.get('offset', 0)
    if type(offset) is not int or offset < 0:
        raise ValueError('Некорректное смещение.')
    fd = open_path(parts, True)
    try:
        entries = []
        with os.scandir(fd) as it:
            for entry in it:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                    continue
                is_dir = stat.S_ISDIR(info.st_mode)
                subparts = parts + [entry.name]
                entries.append({'name': entry.name, 'path': '/'.join(subparts),
                                'kind': 'directory' if is_dir else 'file', 'size': info.st_size,
                                'modified': info.st_mtime,
                                'restricted': stat.S_ISLNK(info.st_mode) or restricted(subparts, is_dir)})
        entries.sort(key=lambda x: (x['kind'] != 'directory', x['name'].casefold(), x['name']))
        more = offset + DIR_PAGE < len(entries)
        return {'source': SOURCE, 'root': str(ROOT / 'sessions'), 'path': '/'.join(parts),
                'entries': entries[offset:offset + DIR_PAGE],
                'notice': 'Ссылки, конфигурация и потенциально секретные файлы недоступны для чтения.',
                'next_offset': offset + DIR_PAGE if more else None}
    finally:
        os.close(fd)


def file_content(req):
    parts = components(req.get('path', ''))
    offset = req.get('offset', 0)
    if not parts or type(offset) is not int or offset < 0:
        raise ValueError('Некорректный путь или смещение.')
    fd = open_path(parts, False)
    try:
        info = os.fstat(fd)
        name = parts[-1]
        result = {'name': name, 'path': '/'.join(parts)}
        image = IMAGES.get(Path(name).suffix.lower())
        if image:
            if offset or info.st_size > MAX_IMAGE:
                return dict(result, kind='unsupported', notice='Изображение слишком велико или смещение некорректно.')
            with os.fdopen(os.dup(fd), 'rb') as f:
                raw = f.read(MAX_IMAGE + 1)
            if len(raw) > MAX_IMAGE or not image[1](raw):
                return dict(result, kind='unsupported', notice='Неподдерживаемое или некорректное изображение.')
            return dict(result, kind='image', data_url='data:' + image[0] + ';base64,' + base64.b64encode(raw).decode(), next_offset=None)
        if offset > info.st_size:
            raise ValueError('Смещение за пределами файла.')
        os.lseek(fd, offset, os.SEEK_SET)
        raw = os.read(fd, FILE_CHUNK + 4)
        page = raw[:FILE_CHUNK]
        try:
            text = page.decode('utf-8')
        except UnicodeDecodeError as exc:
            if exc.reason == 'unexpected end of data' and exc.end == len(page):
                page = page[:exc.start]
                text = page.decode('utf-8')
            else:
                return dict(result, kind='unsupported', notice='Файл не является корректным UTF-8 текстом.')
        if '\x00' in text:
            return dict(result, kind='unsupported', notice='Двоичный файл не поддерживается.')
        next_offset = offset + len(page) if offset + len(page) < info.st_size else None
        return dict(result, kind='text', text=text, next_offset=next_offset)
    finally:
        os.close(fd)


def main():
    try:
        req = json.load(sys.stdin)
        if not isinstance(req, dict):
            raise ValueError('Некорректный запрос.')
        configure_root(req.get('hermes_root'))
        op = req.get('operation')
        if op == 'scan':
            result = scan()
        elif op == 'detail':
            result = detail(req)
        elif op == 'files':
            result = files(req)
        elif op == 'file':
            result = file_content(req)
        else:
            raise ValueError('Неизвестная операция.')
        json.dump({'ok': True, 'result': result}, sys.stdout, ensure_ascii=False)
    except (OSError, sqlite3.Error, ValueError, RuntimeError, TypeError) as exc:
        if isinstance(exc, (OSError, sqlite3.Error)):
            message = 'Ошибка доступа к данным или несовместимая база Hermes на VPS.'
        else:
            message = str(exc)
        json.dump({'ok': False, 'error': message}, sys.stdout, ensure_ascii=False)


if __name__ == '__main__':
    main()
