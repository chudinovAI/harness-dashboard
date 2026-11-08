"""On-demand, read-only navigation within discovered harness roots."""

import base64
import os
import re
import stat
from pathlib import Path

import file_adapters

PAGE_SIZE = 200
TEXT_PAGE = 64 * 1024
IMAGE_LIMIT = 8 * 1024 * 1024
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.gif': 'image/gif', '.webp': 'image/webp'}


def _parts(relative):
    if not isinstance(relative, str) or relative.startswith('/') or '\x00' in relative:
        raise ValueError('Недопустимый путь')
    if not relative:
        return []
    parts = relative.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('Недопустимый путь')
    return parts


def _restricted(parts):
    for part in parts:
        name = part.lower()
        if (name.startswith('.') or name in {'.env', '.envrc', '.ssh', 'keychain'}
                or re.search(r'(^|[._-])(auth|config(?:uration)?|settings?|credentials?|tokens?|secrets?|passwords?|passwd|private|keys?|certs?|certificates?|api[_-]?key|apikey|cookies?|oauth)([._-]|$)', name)
                or name.startswith(('.env.', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'id_xmss'))
                or name.endswith(('.json', '.json5', '.jsonc', '.yaml', '.yml', '.toml', '.ini', '.conf', '.plist', '.pem', '.key', '.p12', '.pfx', '.keystore'))):
            return True
    return False


def _preview_allowed(source, parts):
    if not parts or _restricted(parts):
        return False
    suffix = Path(parts[-1]).suffix.lower()
    if suffix not in {'.jsonl', '.md', '.txt', '.log', *IMAGE_TYPES}:
        return False
    if source in ('OMP', 'DSH', 'Claude') or source.startswith('Claude · '):
        return True
    allowed = {
        'Codex': (('sessions',), ('archived_sessions',)),
        'ZCode': (('cli', 'artifacts'), ('cli', 'rollout'), ('cli', 'agents')),
        'OpenCode': (('sessions',), ('storage',), ('artifacts',)),
        'Hermes': (('sessions',),),
    }
    return any(tuple(parts[:len(prefix)]) == prefix for prefix in allowed.get(source, ()))


def _root(root, identity):
    fd = file_adapters._open_root(Path(root))
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino) != tuple(identity):
        os.close(fd)
        raise ValueError('Хранилище изменилось. Обновите источники.')
    return fd


def open_directory(root, identity, relative=''):
    parts = _parts(relative)
    if _restricted(parts):
        raise ValueError('Этот каталог может содержать секреты и недоступен для просмотра')
    fd = _root(root, identity)
    try:
        for part in parts:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file(root, identity, relative, source=''):
    parts = _parts(relative)
    if not _preview_allowed(source, parts):
        raise ValueError('Просмотр разрешён только для файлов сессий и артефактов')
    parent = open_directory(root, identity, '/'.join(parts[:-1]))
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError('Открывать можно только обычные файлы')
        return fd
    finally:
        os.close(parent)


def list_files(root, identity, relative='', offset=0, source=''):
    if offset < 0:
        raise ValueError('Недопустимая позиция')
    fd = open_directory(root, identity, relative)
    try:
        entries = []
        with os.scandir(fd) as iterator:
            for item in iterator:
                child = '/'.join(_parts(relative) + [item.name])
                try:
                    info = item.stat(follow_symlinks=False)
                    kind = ('directory' if stat.S_ISDIR(info.st_mode) else
                            'file' if stat.S_ISREG(info.st_mode) else
                            'symlink' if stat.S_ISLNK(info.st_mode) else 'other')
                    entries.append({'name': item.name, 'path': child, 'kind': kind,
                                    'size': info.st_size if kind == 'file' else None,
                                    'modified': info.st_mtime,
                                    'restricted': (_restricted(_parts(child)) or kind in ('symlink', 'other')
                                                   or (kind == 'file' and not _preview_allowed(source, _parts(child))))})
                except OSError:
                    entries.append({'name': item.name, 'path': child, 'kind': 'other',
                                    'size': None, 'modified': None, 'restricted': True})
        entries.sort(key=lambda item: (item['kind'] != 'directory', item['name'].casefold(), item['name']))
        end = min(offset + PAGE_SIZE, len(entries))
        return {'root': str(root), 'path': relative, 'entries': entries[offset:end],
                'next_offset': end if end < len(entries) else None, 'notice': ''}
    finally:
        os.close(fd)


def read_file(root, identity, relative, offset=0, source=''):
    if offset < 0:
        raise ValueError('Недопустимая позиция')
    fd = open_file(root, identity, relative, source)
    try:
        info = os.fstat(fd)
        name = Path(relative).name
        ext = Path(name).suffix.lower()
        if ext in IMAGE_TYPES:
            if info.st_size > IMAGE_LIMIT:
                return {'name': name, 'path': relative, 'kind': 'unsupported',
                        'notice': 'Изображение больше 8 МиБ; откройте его в Finder.'}
            with os.fdopen(os.dup(fd), 'rb') as stream:
                blob = stream.read(IMAGE_LIMIT + 1)
            signatures = {'.png': blob.startswith(b'\x89PNG\r\n\x1a\n'),
                          '.jpg': blob.startswith(b'\xff\xd8\xff'), '.jpeg': blob.startswith(b'\xff\xd8\xff'),
                          '.gif': blob.startswith((b'GIF87a', b'GIF89a')),
                          '.webp': blob.startswith(b'RIFF') and blob[8:12] == b'WEBP'}
            if not signatures[ext]:
                return {'name': name, 'path': relative, 'kind': 'unsupported', 'notice': 'Формат изображения не подтверждён.'}
            return {'name': name, 'path': relative, 'kind': 'image',
                    'data_url': 'data:' + IMAGE_TYPES[ext] + ';base64,' + base64.b64encode(blob).decode('ascii'),
                    'next_offset': None, 'notice': ''}
        if offset > info.st_size:
            raise ValueError('Файл изменился. Откройте его снова.')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            stream.seek(offset)
            data = stream.read(TEXT_PAGE + 4)
        if b'\x00' in data:
            return {'name': name, 'path': relative, 'kind': 'unsupported', 'notice': 'Бинарный файл нельзя просмотреть как текст.'}
        page = data[:TEXT_PAGE]
        while page:
            try:
                text = page.decode('utf-8')
                break
            except UnicodeDecodeError as exc:
                if exc.end == len(page) and exc.reason == 'unexpected end of data':
                    page = page[:exc.start]
                else:
                    return {'name': name, 'path': relative, 'kind': 'unsupported', 'notice': 'Файл не является текстом UTF-8.'}
        else:
            text = ''
        next_offset = offset + len(page) if offset + len(page) < info.st_size else None
        return {'name': name, 'path': relative, 'kind': 'text', 'text': text,
                'next_offset': next_offset, 'notice': ''}
    finally:
        os.close(fd)
