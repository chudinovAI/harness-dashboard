"""Read the user's Hermes VPS over its existing SSH alias, without modifying it.

Only the fixed adjacent worker is executed remotely; user-controlled values go
through JSON stdin, never a remote shell command. SQLite snapshots remain on the
VPS and are normally deleted when the worker exits. A crash can leave a private
snapshot behind on the VPS.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
from dataclasses import dataclass

SOURCE = 'Hermes VPS'
TIMEOUT = 45


@dataclass(frozen=True)
class RemoteConfig:
    ssh_alias: str
    hermes_root: str


def load_config(path):
    """Load only an explicitly selected local config; never guess a host or path."""
    try:
        with Path(path).open('r', encoding='utf-8') as file:
            config = json.load(file)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError('Не удалось прочитать конфигурацию Hermes VPS.') from exc
    if not isinstance(config, dict) or set(config) != {'ssh_alias', 'hermes_root'}:
        raise ValueError('Конфигурация должна содержать только ssh_alias и hermes_root.')
    alias, root = config['ssh_alias'], config['hermes_root']
    if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}', alias):
        raise ValueError('Недопустимый SSH alias в конфигурации.')
    if (not isinstance(root, str) or not root.startswith('/') or root == '/'
            or any(part in ('', '.', '..') for part in root.split('/')[1:])
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in root)
            or len(root.encode('utf-8')) > 4096):
        raise ValueError('Корень Hermes должен быть абсолютным нормализованным путём.')
    return RemoteConfig(alias, root)


def _configured(config):
    if config is None:
        raise RemoteError('Hermes VPS не настроен: укажите --hermes-config.')
    return config


class RemoteError(RuntimeError):
    """The remote source is unreachable or cannot safely answer a request."""


def _id(key):
    encoded = base64.urlsafe_b64encode(json.dumps((key,), ensure_ascii=False, separators=(',', ':')).encode()).decode().rstrip('=')
    return SOURCE + ':' + encoded


def _request(operation, config=None, **params):
    config = _configured(config)
    worker = Path(__file__).with_name('remote_worker.py')
    try:
        code = worker.read_text(encoding='utf-8')
        # This command contains only our fixed, locally installed worker. Request
        # data is passed on stdin, not interpolated into the remote shell.
        command = 'python3 -c ' + shlex.quote(code)
        proc = subprocess.run([
            'ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=8', '-o', 'ConnectionAttempts=1',
            '-o', 'NumberOfPasswordPrompts=0', '-o', 'ClearAllForwardings=yes',
            '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'ControlMaster=no',
            '-o', 'ControlPath=none', config.ssh_alias, command,
        ], input=json.dumps({**params, 'operation': operation, 'hermes_root': config.hermes_root}, ensure_ascii=False),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace', timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RemoteError('Время ожидания SSH к Hermes VPS истекло.') from exc
    except OSError as exc:
        raise RemoteError('Не удалось запустить SSH или прочитать локальный адаптер Hermes VPS.') from exc
    if proc.returncode:
        err = proc.stderr.lower()
        if 'host key verification failed' in err or 'remote host identification has changed' in err:
            message = 'Проверка SSH-ключа Hermes VPS не пройдена.'
        elif 'permission denied' in err:
            message = 'SSH-доступ к Hermes VPS отклонён.'
        elif 'timed out' in err:
            message = 'Соединение с Hermes VPS превысило время ожидания.'
        else:
            message = 'Hermes VPS недоступен по SSH или удалённый Python завершился с ошибкой.'
        raise RemoteError(message)
    try:
        result = json.loads(proc.stdout)
        if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
            raise ValueError('invalid response')
        if not result['ok']:
            error = result.get('error')
            raise RemoteError(error if isinstance(error, str) else 'Ошибка чтения Hermes VPS.')
        if not isinstance(result.get('result'), dict):
            raise ValueError('invalid result')
        return result['result']
    except (json.JSONDecodeError, ValueError) as exc:
        raise RemoteError('Hermes VPS вернул некорректный ответ.') from exc


def scan(home=None, config=None):
    """Return session metadata and a single source status; home is ignored."""
    root = str(PurePosixPath(config.hermes_root) / 'sessions') if config else ''
    try:
        result = _request('scan', config=config)
        rows = result.get('rows')
        if not isinstance(rows, list):
            raise RemoteError('Hermes VPS вернул некорректные метаданные сессий.')
        sessions = []
        for row in rows:
            key = row.get('id')
            if not isinstance(key, str) or not key:
                continue
            parent = row.get('parent_session_id')
            sessions.append({
                'id': _id(key), 'source': SOURCE,
                'title': row.get('title') or row.get('display_name') or 'Hermes session',
                'project': row.get('cwd') or row.get('git_repo_root') or '',
                'path': str(PurePosixPath(config.hermes_root) / 'state.db'), 'updated': row.get('last_activity_at') or row.get('ended_at') or row.get('started_at') or '',
                'parent_id': _id(parent) if isinstance(parent, str) and parent else '',
                'status': row.get('end_reason') or '', 'model': row.get('model') or '',
                'message_count': row.get('message_count', 0),
                'remote': True, '_adapter': 'remote', '_dataset': 'remote_hermes',
                '_key': key, '_root': root,
            })
        return sessions, {'name': SOURCE, 'status': 'ok' if sessions else 'empty',
                          'detail': '%d сессий; SSH: %s' % (len(sessions), config.ssh_alias),
                          'root': root, 'remote': True}
    except RemoteError as exc:
        return [], {'name': SOURCE, 'status': 'error', 'detail': str(exc), 'root': root, 'remote': True}


def detail(session, offset=0, limit=80, config=None):
    if session.get('_adapter') != 'remote' or session.get('source') != SOURCE:
        raise RemoteError('Некорректная удалённая сессия.')
    return _request('detail', config=config, key=session.get('_key'), offset=offset, limit=limit)


def remote_files(path='', offset=0, config=None):
    return _request('files', config=config, path=path, offset=offset)


def remote_file(path='', offset=0, config=None):
    return _request('file', config=config, path=path, offset=offset)
