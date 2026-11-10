"""Paged, read-only rendering of JSONL harness histories.

A page replays the stream from the beginning because its public cursor is a
rendered-block ordinal. Memory is bounded by the requested page and one JSONL
record, not by the whole history. Large text blocks are split into page-sized
pieces; malformed records and unsupported media are reported, not executed.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
from collections import Counter

from . import file_adapters

_CHUNK = 65536
_TEXT_CHUNK = 32768
_IDLE_SECONDS = 15
_IMAGE_LIMIT = 8 * 1024 * 1024
_PAGE_IMAGE_LIMIT = 8 * 1024 * 1024

def _records(session, notices):
    """Stream complete JSONL records; the open descriptor pins the source file."""
    path = Path(session.get('path', ''))
    root = Path(session.get('_root', ''))
    proc = None
    selector = None
    malformed = 0
    try:
        with file_adapters._open_source(path, root, session.get('_root_identity'),
                                       session.get('_file_identity')) as source:
            size = os.fstat(source.fileno()).st_size
            if size < session.get('_scan_size', 0):
                notices.append('Session was truncated since scan; rescan required')
                return
            if session.get('_compressed', session.get('_kind', session.get('source')) == 'DSH'):
                executable = shutil.which('zstd')
                if not executable:
                    notices.append('zstd executable is unavailable')
                    return
                proc = subprocess.Popen([executable, '-dc'], stdin=source, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, close_fds=True)
                stream = proc.stdout
                selector = selectors.DefaultSelector()
                selector.register(stream, selectors.EVENT_READ)
                remaining = None
            else:
                stream = source
                # A stable end position avoids reading an actively appended partial record.
                remaining = size
            pending = bytearray()
            search = 0
            while remaining is None or remaining > 0:
                if selector and not selector.select(_IDLE_SECONDS):
                    notices.append('Decompression stalled; history is incomplete')
                    return
                count = _CHUNK if remaining is None else min(_CHUNK, remaining)
                chunk = os.read(stream.fileno(), count)
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                pending.extend(chunk)
                start = 0
                while True:
                    end = pending.find(b'\n', search)
                    if end < 0:
                        break
                    line = bytes(pending[start:end])
                    start = end + 1
                    search = start
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                        if not isinstance(obj, dict):
                            raise ValueError('not an object')
                    except (ValueError, UnicodeError, RecursionError):
                        malformed += 1
                        continue
                    yield obj
                if start:
                    del pending[:start]
                search = len(pending)
            if pending and pending.strip():
                try:
                    obj = json.loads(pending)
                    if not isinstance(obj, dict):
                        raise ValueError('not an object')
                except (ValueError, UnicodeError, RecursionError):
                    notices.append('Incomplete or malformed final JSONL record omitted; it may still be writing')
                else:
                    notices.append('Final JSONL record has no newline and may still be changing')
                    yield obj
            if proc:
                try:
                    if proc.wait(timeout=_IDLE_SECONDS):
                        notices.append('zstd could not fully decode the file')
                except subprocess.TimeoutExpired:
                    notices.append('Decompression stalled; history is incomplete')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        notices.append('Source read error: ' + str(exc))
    finally:
        if malformed:
            notices.append(f'Malformed JSONL records omitted ({malformed})')
        if selector:
            selector.close()
        if proc:
            if proc.poll() is None:
                proc.kill()
            if proc.stdout:
                proc.stdout.close()
            proc.wait()


def _safe_media(value):
    """Keep media metadata without turning embedded binary into browser content."""
    if isinstance(value, str) and value[:5].lower() == 'data:':
        return '[embedded data omitted]'
    if isinstance(value, list):
        return [_safe_media(item) for item in value]
    if not isinstance(value, dict):
        return value
    media = value.get('type') in ('base64', 'image', 'image_url', 'input_image',
                                  'document', 'audio', 'input_audio', 'video', 'file')
    return {key: ('[embedded data omitted]' if media and key in ('data', 'base64', 'bytes')
                  else _safe_media(item)) for key, item in value.items()}


def _raster(data, mime):
    """Validate encoded length, base64 and MIME magic without fetching resources."""
    signatures = {
        'image/png': lambda b: b.startswith(b'\x89PNG\r\n\x1a\n'),
        'image/jpeg': lambda b: b.startswith(b'\xff\xd8\xff'),
        'image/gif': lambda b: b.startswith((b'GIF87a', b'GIF89a')),
        'image/webp': lambda b: len(b) >= 12 and b[:4] == b'RIFF' and b[8:12] == b'WEBP',
    }
    if not isinstance(mime, str) or mime not in signatures:
        return None, 0, 'unsupported image format'
    if not isinstance(data, str):
        return None, 0, 'missing embedded image data'
    if len(data) > ((_IMAGE_LIMIT + 2) // 3) * 4:
        return None, 0, 'image exceeds 8 MiB'
    try:
        decoded = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        return None, 0, 'invalid base64 image'
    if len(decoded) > _IMAGE_LIMIT:
        return None, 0, 'image exceeds 8 MiB'
    if not signatures[mime](decoded):
        return None, 0, 'image signature does not match its declared format'
    return 'data:' + mime + ';base64,' + data, len(decoded), ''


def detail(session, offset=0, limit=80):
    metadata_limits = {'reading stopped at byte limit; more content may exist',
                       'metadata scan stopped at record limit'}
    notices = ['Metadata scan: ' + notice for notice in session.get('_notices', [])
               if notice not in metadata_limits]
    files = [{'path': session.get('path', ''), 'label': 'Session source'}]
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return {'messages': [], 'files': files, 'notice': 'Invalid history offset.',
                'has_more': False, 'next_offset': None}
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return {'messages': [], 'files': files, 'notice': 'Invalid history page size.',
                'has_more': False, 'next_offset': None}
    limit = min(limit, 200)
    kind = session.get('_kind', session.get('source', ''))
    if kind not in ('OMP', 'DSH', 'Claude'):
        return {'messages': [], 'files': [], 'notice': 'Unsupported file source.',
                'has_more': False, 'next_offset': None}
    root = Path(session.get('_root', ''))
    path = Path(session.get('path', ''))
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError:
        return {'messages': [], 'files': [], 'notice': 'Source path is outside its root.',
                'has_more': False, 'next_offset': None}
    unknown = Counter()
    messages = []
    position = 0
    has_more = False
    media_noted = False
    image_bytes = 0

    def note_unknown(name):
        name = name[:120]
        if name not in unknown and len(unknown) >= 50:
            name = 'other unknown types'
        unknown[name] += 1

    def pieces(role, value, message_kind, timestamp):
        if not isinstance(value, str):
            value = file_adapters._render(_safe_media(value))
        if value:
            for start in range(0, len(value), _TEXT_CHUNK):
                yield {'role': role, 'text': value[start:start + _TEXT_CHUNK],
                       'kind': message_kind, 'time': file_adapters._time(timestamp)}

    def attachment(block, role, timestamp):
        nonlocal media_noted
        typ = str(block.get('type') or 'attachment')[:100]
        source = block.get('source') if isinstance(block.get('source'), dict) else {}
        mime = block.get('media_type') or source.get('media_type') or block.get('mimeType')
        mime_label = mime[:100] if isinstance(mime, str) else ''
        description = '[' + typ + (' · ' + mime_label if mime_label else '') + ': unsupported embedded media]'
        def record_file():
            candidate = block.get('path') or block.get('file_path') or source.get('path')
            if not isinstance(candidate, str) or not candidate:
                return ''
            candidate_path = Path(candidate)
            if not candidate_path.is_absolute():
                candidate_path = root / candidate_path
            try:
                candidate_path = candidate_path.absolute()
                candidate_path.relative_to(root.absolute())
                with file_adapters._open_source(candidate_path, root, session.get('_root_identity')):
                    pass
                if offset <= position < offset + limit and all(item['path'] != str(candidate_path) for item in files):
                    files.append({'path': str(candidate_path), 'label': typ})
                return ' [source file available]'
            except (OSError, ValueError):
                return ' [media path unavailable or outside source root]'
        if typ == 'image':
            encoded = source.get('data') if source.get('type') == 'base64' else block.get('data')
            # Cursor replay does not decode payloads from earlier or look-ahead blocks.
            if not offset <= position < offset + limit:
                yield {'role': role, 'text': '[image]', 'kind': 'attachment',
                       'time': file_adapters._time(timestamp)}
                return
            if source.get('type') == 'url' or (not encoded and (source.get('url') or block.get('url'))):
                url, size, error = None, 0, 'external image URLs are not fetched'
            else:
                url, size, error = _raster(encoded, mime)
            if url:
                record_file()
                yield {'role': role, 'text': '[image · ' + mime + ']', 'kind': 'attachment',
                       'time': file_adapters._time(timestamp), 'image': url, '_image_bytes': size}
                return
            description = '[image: ' + error + ']'
        description += record_file()
        if not media_noted:
            notices.append('Unsupported media represented as an attachment; embedded content is not displayed')
            media_noted = True
        yield from pieces(role, description, 'attachment', timestamp)

    def content(role, value, timestamp):
        if isinstance(value, str):
            yield from pieces(role, value, 'tool_result' if role == 'tool' else 'message', timestamp)
        elif isinstance(value, list):
            for block in value:
                yield from content(role, block, timestamp)
        elif isinstance(value, dict):
            typ = value.get('type') if isinstance(value.get('type'), str) else '<invalid>'
            if typ in ('text', 'input_text', 'output_text'):
                yield from pieces(role, value.get('text', ''), 'tool_result' if role == 'tool' else 'message', timestamp)
            elif typ in ('thinking', 'reasoning', 'reasoning_text'):
                yield from pieces(role, value.get('thinking', value.get('text', value.get('reasoning', ''))), 'reasoning', timestamp)
            elif typ in ('toolCall', 'tool_use', 'tool-call', 'function_call'):
                name = value.get('name') or 'tool'
                args = value.get('arguments', value.get('input', value.get('args', {})))
                yield from pieces('tool', str(name) + '\n' + file_adapters._render(_safe_media(args)), 'tool_call', timestamp)
            elif typ in ('tool_result', 'tool-result', 'function_call_output'):
                result = value.get('content', value.get('output', value.get('result', '')))
                yield from content('tool', result, timestamp)
            elif typ in ('image', 'image_url', 'input_image', 'document', 'audio', 'input_audio', 'video', 'file', 'attachment'):
                yield from attachment(value, role, timestamp)
            else:
                note_unknown('content block ' + str(typ or '<missing>'))
                # Unknown structured content remains visible, with embedded media redacted.
                yield from pieces(role, _safe_media(value), 'message', timestamp)
        elif value is not None:
            note_unknown('non-text content')
            yield from pieces(role, value, 'message', timestamp)

    known_omp = {'session', 'title', 'title_change', 'model_change', 'model_usage',
                 'thinking_level_change', 'service_tier_change', 'credential_pin', 'custom', 'compaction'}
    known_dsh = {'session', 'session/title', 'session/title-llm-request', 'session/end-seed',
                 'turn/start', 'turn/end', 'step/start', 'step/end', 'job/registered',
                 'job/settled', 'sandbox/mode', 'approval/policy', 'permission/preset',
                 'agent/inbox/spliced', 'agent-preset/selected', 'request/header',
                 'request/context', 'subagent/descriptor', 'model/selection'}
    known_claude = {'summary', 'custom-title', 'file-history-snapshot', 'queue-operation',
                    'progress', 'system', 'last-prompt', 'pr-link'}

    def blocks(obj):
        typ = obj.get('type') if isinstance(obj.get('type'), str) else '<invalid>'
        timestamp = obj.get('timestamp', obj.get('time', ''))
        if kind == 'OMP':
            if typ == 'message':
                msg = obj.get('message')
                if not isinstance(msg, dict):
                    note_unknown('invalid message')
                    return
                role = file_adapters._string(msg.get('role')) or 'unknown'
                timestamp = obj.get('timestamp', msg.get('timestamp', ''))
                if role == 'pythonExecution':
                    yield from pieces('tool', file_adapters._string(msg.get('code')) + '\n' + file_adapters._string(msg.get('output')), 'tool_result', timestamp)
                else:
                    yield from content('tool' if role in ('toolResult', 'tool') else role, msg.get('content'), timestamp)
            elif typ == 'custom_message':
                yield from content('custom', obj.get('content'), timestamp)
            elif typ == 'compaction':
                yield from pieces('system', obj.get('summary', ''), 'message', timestamp)
            elif typ not in known_omp:
                note_unknown('record ' + str(typ or '<missing>'))
        elif kind == 'DSH':
            data = obj.get('data') if isinstance(obj.get('data'), dict) else {}
            if typ in ('user/message', 'assistant/message', 'tool/result', 'system/message'):
                msg = data.get('message', data)
                if isinstance(msg, dict):
                    role = {'user/message': 'user', 'assistant/message': 'assistant',
                            'tool/result': 'tool', 'system/message': 'system'}[typ]
                    yield from content(role, msg.get('content'), timestamp)
                else:
                    note_unknown('invalid ' + typ)
            elif typ == 'tool/call':
                yield from pieces('tool', str(data.get('name') or 'tool') + '\n' + file_adapters._render(_safe_media(data.get('arguments', ''))), 'tool_call', timestamp)
            elif typ not in known_dsh:
                note_unknown('record ' + str(typ or '<missing>'))
        elif typ in ('user', 'assistant'):
            msg = obj.get('message')
            if isinstance(msg, dict):
                yield from content(file_adapters._string(msg.get('role')) or typ, msg.get('content'), timestamp)
            else:
                note_unknown('invalid ' + typ + ' message')
        elif typ == 'progress':
            data = obj.get('data') if isinstance(obj.get('data'), dict) else {}
            msg = data.get('message')
            if isinstance(msg, dict):
                msg = msg.get('message', msg)
                if isinstance(msg, dict):
                    yield from content(file_adapters._string(msg.get('role')) or 'assistant',
                                       msg.get('content'), timestamp)
        elif typ == 'system' and isinstance(obj.get('content'), (str, list)):
            yield from content('system', obj['content'], timestamp)
        elif typ == 'summary' and isinstance(obj.get('summary'), str):
            yield from pieces('system', obj['summary'], 'message', timestamp)
        elif typ == 'attachment':
            value = obj.get('attachment', obj)
            if isinstance(value, dict):
                yield from attachment(value, 'system', timestamp)
            else:
                note_unknown('invalid attachment')
        elif typ not in known_claude:
            note_unknown('record ' + str(typ or '<missing>'))

    records = _records(session, notices)
    try:
        for obj in records:
            for message in blocks(obj):
                if position >= offset + limit:
                    has_more = True
                    break
                if position >= offset:
                    size = message.pop('_image_bytes', 0)
                    if image_bytes + size > _PAGE_IMAGE_LIMIT:
                        has_more = True
                        break
                    image_bytes += size
                    messages.append(message)
                position += 1
            if has_more:
                break
    finally:
        records.close()
    if unknown:
        notices.append('Unknown or invalid content: ' + ', '.join(f'{name} ({count})' for name, count in unknown.items()))
    if not has_more and position < offset:
        notices.append('History offset exceeds available blocks; source may have changed')
    return {'messages': messages, 'files': files, 'notice': file_adapters._summarize_notices(notices),
            'has_more': has_more, 'next_offset': offset + len(messages) if has_more else None}
