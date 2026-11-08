"""On-demand, paginated readers for the database-backed session formats.

Offsets count rendered blocks, including deterministic chunks of large blocks. Each
request reads a fresh SQLite snapshot or rollout; a changing history can shift an
offset between requests. Only explicit conversation fields are read.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import db_adapters as db

# A block is split rather than clipped; the offset addresses the resulting chunks.
CHUNK_CHARS = 128 * 1024
PAGE_CHARS = 1024 * 1024
MAX_PAGE_BLOCKS = 200


class _PageFull(Exception):
    pass


class _Detail:
    def __init__(self, offset, limit):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError('offset must be a non-negative integer')
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('limit must be a positive integer')
        self.offset = offset
        self.limit = min(limit, MAX_PAGE_BLOCKS)
        self.position = 0
        self.size = 0
        self.messages = []
        self.notes = []
        self.files = []
        self.attachment_roots = ()
        self.has_more = False

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    def add(self, role, value, kind='message', time=''):
        if value is None or value == '':
            return
        if not isinstance(value, str):
            try:
                value = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                self.note('A record has unsupported content.')
                return
        if len(value) > CHUNK_CHARS:
            self.note('Long content is displayed in consecutive blocks without truncation.')
        for start in range(0, len(value), CHUNK_CHARS):
            block = value[start:start + CHUNK_CHARS]
            if self.position >= self.offset:
                if len(self.messages) >= self.limit or (self.messages and self.size + len(block) > PAGE_CHARS):
                    self.has_more = True
                    raise _PageFull
                self.messages.append({'role': db._text(role), 'text': block, 'kind': kind, 'time': db._text(time)})
                self.size += len(block)
            self.position += 1

    def file(self, path, label):
        entry = {'path': str(path), 'label': label}
        if entry not in self.files:
            self.files.append(entry)

    def result(self):
        if self.has_more:
            self.note('The source is reread for each page; changes between requests can shift offsets.')
        return {'messages': self.messages, 'files': self.files, 'notice': ' '.join(self.notes),
                'has_more': self.has_more,
                'next_offset': self.offset + len(self.messages) if self.has_more else None}


def _json(raw, output):
    if raw is None:
        output.note('A missing record was omitted.')
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, UnicodeDecodeError):
        output.note('A malformed record was omitted.')
        return None
    if not isinstance(value, dict):
        output.note('An unsupported record was omitted.')
        return None
    return value


def _attachment(output, item, role, time):
    # A media reference is metadata, not permission to fetch a URL or outside path.
    fields = {key: item[key] for key in ('type', 'name', 'filename', 'mime', 'mimeType', 'mediaType', 'path')
              if key in item and isinstance(item[key], str) and not item[key].startswith('data:')}
    path = fields.get('path')
    if path and Path(path).is_absolute():
        for root in output.attachment_roots:
            safe = db._safe_path(root, path)
            if safe:
                output.file(safe, fields.get('name') or fields.get('filename') or 'Attachment')
                break
    output.note('Attachment metadata is shown; embedded images and external or unverified paths are not loaded.')
    output.add(role, fields or {'type': item.get('type', 'file')}, 'attachment', time)


def _content(output, role, content, time=''):
    if isinstance(content, str):
        output.add(role, content, time=time)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                output.add(role, item, time=time)
            elif isinstance(item, dict):
                typ = item.get('type')
                if typ in ('text', 'input_text', 'output_text'):
                    output.add(role, item.get('text'), time=time)
                elif typ in ('file', 'image', 'input_image', 'image_url', 'attachment'):
                    _attachment(output, item, role, time)
                else:
                    output.note('Some non-text message content has an unsupported format.')
            else:
                output.note('Some non-text message content has an unsupported format.')
    elif content is not None:
        output.note('Some message content has an unsupported format.')


def _tool(output, name, data, time=''):
    output.add('tool', {'name': db._text(name), 'data': data}, 'tool', time)


def _zcode_cli(con, key, output):
    output.note('Hidden or model-only content, reasoning, timeline and internal step records are not displayed.')
    messages = con.execute('''SELECT id, time_created,
        CASE WHEN json_valid(data) THEN json_extract(data, '$.role') END AS role,
        CASE WHEN json_valid(data) THEN json_extract(data, '$.semantics.transcriptVisibility') END AS visibility
        FROM message WHERE session_id=? ORDER BY sequence, time_created, id''', (key,))
    for message in messages:
        if message['visibility'] == 'hidden':
            continue
        role = message['role']
        if role not in ('user', 'assistant', 'tool'):
            output.note('Some ZCode message roles or records are unsupported.')
            continue
        parts = con.execute('''SELECT time_created, data FROM part
            WHERE session_id=? AND message_id=? ORDER BY sequence, time_created, id''', (key, message['id']))
        for part in parts:
            obj = _json(part['data'], output)
            if obj is None:
                continue
            metadata = obj.get('metadata')
            if isinstance(metadata, dict) and metadata.get('visibility') == 'model-only':
                continue
            typ = obj.get('type')
            time = db._timestamp(part['time_created'])
            if typ == 'text':
                _content(output, role, obj.get('text'), time)
            elif typ == 'tool':
                _tool(output, obj.get('tool'), obj.get('state'), time)
            elif typ in ('file', 'image', 'attachment'):
                _attachment(output, obj, role, time)
            elif typ not in ('step-start', 'step-finish', 'reasoning', 'timeline'):
                output.note('Some ZCode part types are unsupported.')


def _opencode(con, key, output):
    rows = con.execute('''SELECT id, type, time_created, data FROM session_message
        WHERE session_id=? ORDER BY seq, time_created, id''', (key,))
    for row in rows:
        role = row['type']
        obj = _json(row['data'], output)
        if obj is None:
            continue
        time = db._timestamp(row['time_created'])
        if role in ('idle', 'model-switched'):
            fields = {k: obj[k] for k in ('outcome', 'model', 'previous', 'error') if k in obj}
            output.add('event', {'type': role, **fields}, 'event', time)
            continue
        if role not in ('user', 'assistant', 'tool'):
            output.note('Some OpenCode event types are unsupported.')
            continue
        _content(output, role, obj.get('text'), time)
        _content(output, role, obj.get('content'), time)
        files = obj.get('files')
        if isinstance(files, list):
            for item in files:
                if isinstance(item, dict):
                    _attachment(output, item, role, time)
                elif isinstance(item, str):
                    _attachment(output, {'type': 'file', 'path': item}, role, time)
                else:
                    output.note('Some attachment metadata is unsupported.')
        elif files is not None:
            output.note('Some attachment metadata is unsupported.')
        parts = con.execute('''SELECT type, time_created, data FROM session_part
            WHERE session_id=? AND message_id=? ORDER BY time_created, ordinal''', (key, row['id']))
        for part in parts:
            part_obj = _json(part['data'], output)
            if part_obj is None:
                continue
            typ = part['type'] or part_obj.get('type')
            part_time = db._timestamp(part['time_created'])
            if typ in ('text', 'input_text', 'output_text'):
                _content(output, role, part_obj.get('text'), part_time)
            elif typ in ('tool', 'tool-call', 'tool_call', 'tool-result', 'tool_result'):
                _tool(output, part_obj.get('tool') or part_obj.get('name'), part_obj.get('state', part_obj), part_time)
            elif typ in ('file', 'image', 'attachment'):
                _attachment(output, part_obj, role, part_time)
            else:
                output.note('Some OpenCode part types are unsupported.')


def _hermes(con, key, output):
    output.note('System and unsupported message roles, reasoning and internal metadata are not displayed. Stored media may be represented only by a screenshot placeholder.')
    columns = {row['name'] for row in con.execute('PRAGMA table_info(messages)')}
    visible = " AND COALESCE(display_kind, '') != 'hidden'" if 'display_kind' in columns else ''
    rows = con.execute('''SELECT role, timestamp, tool_name, content, tool_calls
        FROM messages WHERE session_id=? AND role IN ('user', 'assistant', 'tool')''' + visible + ' ORDER BY id', (key,))
    for row in rows:
        role = row['role']
        time = db._timestamp(row['timestamp'])
        if row['content'] is not None:
            output.add(role, row['content'], 'tool' if role == 'tool' else 'message', time)
        if row['tool_calls']:
            try:
                calls = json.loads(row['tool_calls'])
            except (ValueError, TypeError):
                output.note('Some Hermes tool calls have an unsupported format.')
                continue
            _tool(output, row['tool_name'] or 'tool calls', calls, time)


def _codex(session, output):
    root = Path(session['_root'])
    safe = None
    for sub in ('sessions', 'archived_sessions'):
        allowed = root / sub
        if allowed.resolve() == allowed:
            safe = db._safe_path(allowed, session.get('path', ''))
            if safe:
                break
    if not safe:
        output.note('The indexed rollout is missing or outside the permitted Codex rollout directories.')
        return
    output.file(safe, 'Codex rollout')
    output.note('System/developer instructions, reasoning and internal events are not displayed.')
    fd = db._open_beneath(allowed, safe)
    with os.fdopen(fd, 'rb') as stream:
        for line in stream:
            if not line.endswith(b'\n'):
                output.note('An incomplete final rollout record was not read.')
                break
            record = _json(line, output)
            if record is None:
                continue
            payload = record.get('payload')
            if not isinstance(payload, dict):
                output.note('A rollout record has unsupported or missing payload.')
                continue
            typ = payload.get('type')
            timestamp = record.get('timestamp', '')
            if record.get('type') == 'event_msg':
                if typ in ('exec_command_begin', 'exec_command_end', 'mcp_tool_call_begin', 'mcp_tool_call_end', 'web_search_begin', 'web_search_end'):
                    _tool(output, typ, {k: payload[k] for k in ('call_id', 'command', 'cwd', 'invocation', 'query', 'output', 'aggregated_output', 'exit_code') if k in payload}, timestamp)
                elif typ in ('task_started', 'task_complete', 'turn_aborted'):
                    fields = {k: payload[k] for k in ('turn_id', 'duration_ms', 'reason') if k in payload}
                    output.add('event', {'type': typ, **fields}, 'event', timestamp)
                elif typ in ('warning', 'error'):
                    fields = {k: payload[k] for k in ('message', 'error') if k in payload}
                    output.add('event', {'type': typ, **fields}, 'event', timestamp)
                elif typ not in ('user_message', 'agent_message', 'agent_reasoning', 'token_count', 'context_compacted', 'thread_name_updated', 'thread_settings_applied', 'item_started', 'item_completed', 'plan_update'):
                    output.note('Some Codex event types are not displayed.')
                continue
            if record.get('type') != 'response_item':
                if record.get('type') not in ('session_meta', 'turn_context', 'compacted'):
                    output.note('Some Codex record types are unsupported.')
                continue
            if typ == 'message':
                role = payload.get('role')
                if role in ('user', 'assistant', 'tool'):
                    _content(output, role, payload.get('content'), timestamp)
                elif role not in ('system', 'developer'):
                    output.note('Some Codex message roles are unsupported.')
            elif typ == 'agent_message':
                _content(output, 'assistant', payload.get('content'), timestamp)
            elif typ in ('function_call', 'custom_tool_call', 'tool_call', 'web_search_call'):
                _tool(output, payload.get('name') or typ, {k: payload[k] for k in ('arguments', 'input', 'call_id', 'action') if k in payload}, timestamp)
            elif typ in ('function_call_output', 'custom_tool_call_output', 'tool_call_output'):
                _tool(output, typ, {k: payload[k] for k in ('output', 'call_id') if k in payload}, timestamp)
            elif typ not in ('reasoning', 'compaction', 'additional_tools'):
                output.note('Some Codex response item types are unsupported.')


def detail(session, offset=0, limit=80):
    output = _Detail(offset, limit)
    dataset = session.get('_dataset')
    root = Path(session.get('_root', ''))
    subroots = {'zcode_cli': ('cli/artifacts', 'cli/rollout'), 'codex': ('sessions', 'archived_sessions')}
    if root.is_absolute():
        output.attachment_roots = tuple(root / sub for sub in subroots.get(dataset, ()))
    try:
        if dataset == 'zcode_task':
            output.note('The ZCode task index contains metadata, not conversation messages. CLI sessions are listed separately; matching identifiers do not imply a parent-child relationship.')
        elif dataset == 'codex':
            _codex(session, output)
        else:
            dbnames = {'zcode_cli': 'cli/db/db.sqlite', 'opencode': 'opencode.db', 'hermes': 'state.db'}
            if dataset not in dbnames:
                output.note('Unsupported database source.')
            else:
                root = Path(session['_root'])
                if not root.is_absolute() or root.resolve() != root:
                    raise ValueError('source root is no longer safe')
                con, path = db._db(root, dbnames[dataset])
                try:
                    output.file(path, 'Source database')
                    if dataset == 'zcode_cli':
                        _zcode_cli(con, session['_key'], output)
                    elif dataset == 'opencode':
                        _opencode(con, session['_key'], output)
                    else:
                        _hermes(con, session['_key'], output)
                finally:
                    con.close()
    except _PageFull:
        pass
    except (db.sqlite3.Error, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        output.note('Conversation unavailable: ' + str(exc))
    return output.result()
