import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import db_adapters
import file_adapters
import remote_adapter
import server


class RemoteConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.path = self.base / 'config.json'

    def config(self, alias='example-host', root='/srv/example/.hermes'):
        self.path.write_text(json.dumps({'ssh_alias': alias, 'hermes_root': root}), encoding='utf-8')
        return remote_adapter.load_config(self.path)

    def test_no_config_is_unavailable_and_does_not_launch_ssh(self):
        with patch.object(remote_adapter.subprocess, 'run') as run:
            sessions, status = remote_adapter.scan()
            self.assertEqual(sessions, [])
            self.assertEqual(status['status'], 'error')
            self.assertEqual(status['root'], '')
            self.assertIn('не настроен', status['detail'])
            for call in (remote_adapter.remote_files, remote_adapter.remote_file):
                with self.assertRaises(remote_adapter.RemoteError):
                    call()
            with self.assertRaises(remote_adapter.RemoteError):
                remote_adapter.detail({'source': remote_adapter.SOURCE, '_adapter': 'remote', '_key': 'example'})
            run.assert_not_called()

    def test_config_rejects_invalid_values(self):
        for alias in ('-oProxyCommand=x', 'name;touch x', '', 'a b', 'a\nname'):
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                self.config(alias=alias)
        for root in ('/', 'relative', '~/x', '/tmp/../private', '/tmp/./x', '/tmp//x', '/tmp/x/', '/tmp/\x00x', '/tmp/\ud800'):
            with self.subTest(root=repr(root)), self.assertRaises(ValueError):
                self.config(root=root)
        for data in ({}, {'ssh_alias': 'host', 'hermes_root': '/tmp/x', 'other': 1}, []):
            self.path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(ValueError):
                remote_adapter.load_config(self.path)
        self.path.write_text('{', encoding='utf-8')
        with self.assertRaises(ValueError):
            remote_adapter.load_config(self.path)
        with self.assertRaises(ValueError):
            remote_adapter.load_config(self.base / 'absent')

    def test_ssh_arguments_are_fixed_and_root_only_in_json_stdin(self):
        root = '/srv/a;touch injected/.hermes'
        config = self.config(root=root)
        fake = subprocess.CompletedProcess([], 0, stdout=json.dumps({'ok': True, 'result': {'rows': [{'id': 'one'}]}}), stderr='')
        with patch.object(remote_adapter.subprocess, 'run', return_value=fake) as run:
            sessions, status = remote_adapter.scan(config=config)
        args, kwargs = run.call_args
        argv = args[0]
        self.assertEqual(argv[-2], 'example-host')
        self.assertNotIn(root, argv[-1])
        self.assertEqual(json.loads(kwargs['input']), {'operation': 'scan', 'hermes_root': root})
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('ForwardAgent=no', argv)
        self.assertEqual(status['root'], root + '/sessions')
        self.assertEqual(sessions[0]['path'], root + '/state.db')
        self.assertEqual(sessions[0]['_root'], root + '/sessions')

    def test_worker_reads_only_explicit_synthetic_root_and_blocks_symlinks(self):
        root = self.base / 'hermes'
        (root / 'sessions').mkdir(parents=True)
        (root / 'sessions' / 'sample.txt').write_text('synthetic', encoding='utf-8')
        (root / 'sessions' / 'settings.txt').write_text('not for preview', encoding='utf-8')
        (root / 'sessions' / 'link.txt').symlink_to(root / 'sessions' / 'sample.txt')
        con = sqlite3.connect(root / 'state.db')
        con.executescript('''
            CREATE TABLE sessions (id TEXT PRIMARY KEY, display_name TEXT, title TEXT, cwd TEXT,
                git_repo_root TEXT, ended_at REAL, started_at REAL, last_activity_at REAL,
                parent_session_id TEXT, end_reason TEXT, model TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                timestamp REAL, content TEXT, tool_calls TEXT, tool_name TEXT, display_kind TEXT);
            INSERT INTO sessions (id) VALUES ('example');
            INSERT INTO messages VALUES (1, 'example', 'user', 1, 'synthetic message', NULL, NULL, 'message');
        ''')
        con.close()
        worker = Path(remote_adapter.__file__).with_name('remote_worker.py')
        def request(operation, **params):
            result = subprocess.run([sys.executable, '-B', str(worker)], input=json.dumps(
                {'operation': operation, 'hermes_root': str(root), **params}), text=True,
                capture_output=True, check=True, timeout=10)
            return json.loads(result.stdout)
        self.assertEqual(request('scan')['result']['rows'][0]['id'], 'example')
        self.assertEqual(request('detail', key='example')['result']['messages'][0]['text'], 'synthetic message')
        files = request('files')['result']
        self.assertEqual(files['root'], str(root / 'sessions'))
        self.assertTrue(next(x for x in files['entries'] if x['name'] == 'link.txt')['restricted'])
        self.assertEqual(request('file', path='sample.txt')['result']['text'], 'synthetic')
        for name in ('settings.txt', 'link.txt', '../state.db'):
            self.assertFalse(request('file', path=name)['ok'])
        for value in ('/tmp/../etc', '/', '', None):
            self.assertFalse(request('scan', hermes_root=value)['ok'])
        root_link = self.base / 'root-link'
        root_link.symlink_to(root, target_is_directory=True)
        self.assertFalse(request('scan', hermes_root=str(root_link))['ok'])

    def test_http_boundary_reports_unconfigured_without_scanning_real_sources(self):
        dashboard = server.Dashboard(0)
        thread = threading.Thread(target=dashboard.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(dashboard.server_close)
        self.addCleanup(dashboard.shutdown)
        with patch.object(file_adapters, 'scan', return_value=([], [])), \
             patch.object(db_adapters, 'scan', return_value=([], [])), \
             patch.object(remote_adapter.subprocess, 'run') as run:
            conn = http.client.HTTPConnection('127.0.0.1', dashboard.server_port, timeout=5)
            try:
                conn.request('GET', '/api/sessions', headers={'X-Harness-Token': dashboard.token})
                response = conn.getresponse()
                result = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(result['sources'][0]['status'], 'error')
                self.assertIn('не настроен', result['sources'][0]['detail'])
                conn.request('GET', '/api/files?source=Hermes%20VPS', headers={'X-Harness-Token': dashboard.token})
                self.assertEqual(conn.getresponse().status, 400)
                run.assert_not_called()
            finally:
                conn.close()

    def test_http_routes_forward_explicit_config_without_real_ssh(self):
        config = self.config()
        dashboard = server.Dashboard(0, remote_config=config)
        thread = threading.Thread(target=dashboard.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(dashboard.server_close)
        self.addCleanup(dashboard.shutdown)
        operations = []
        def fake_ssh(argv, **kwargs):
            request = json.loads(kwargs['input'])
            self.assertEqual(argv[-2], config.ssh_alias)
            self.assertEqual(request['hermes_root'], config.hermes_root)
            operation = request['operation']
            operations.append(operation)
            payloads = {'scan': {'rows': [{'id': 'example'}]},
                        'detail': {'messages': [], 'files': [], 'has_more': False},
                        'files': {'entries': []},
                        'file': {'kind': 'text', 'text': 'synthetic'}}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({'ok': True, 'result': payloads[operation]}), stderr='')
        with patch.object(file_adapters, 'scan', return_value=([], [])), \
             patch.object(db_adapters, 'scan', return_value=([], [])), \
             patch.object(remote_adapter.subprocess, 'run', side_effect=fake_ssh):
            conn = http.client.HTTPConnection('127.0.0.1', dashboard.server_port, timeout=5)
            def get(path):
                conn.request('GET', path, headers={'X-Harness-Token': dashboard.token})
                response = conn.getresponse()
                body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                return body
            try:
                item = get('/api/sessions')['sessions'][0]
                get('/api/session?id=' + quote(item['id']))
                get('/api/files?source=Hermes%20VPS')
                get('/api/file?source=Hermes%20VPS&path=sample.txt')
            finally:
                conn.close()
        self.assertEqual(operations, ['scan', 'detail', 'files', 'file'])

    def test_actual_entry_point_starts_without_config_or_source_scan(self):
        env = dict(os.environ, HOME=str(self.base), PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.Popen([sys.executable, '-B', str(Path(server.__file__)), '--port', '0'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            line = process.stdout.readline()
            self.assertTrue(line.startswith('Панель: http://127.0.0.1:'), line)
            port = int(line.rsplit(':', 1)[1].strip().rstrip('/'))
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                conn.request('GET', '/')
                self.assertEqual(conn.getresponse().status, 200)
            finally:
                conn.close()
        finally:
            process.terminate()
            process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
