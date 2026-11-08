import hashlib
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import db_adapters
import file_adapters
import server


class SourceSafetyTests(unittest.TestCase):
    def test_replaced_root_ancestor_cannot_redirect_session_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / 'store' / 'sessions'
            root.mkdir(parents=True)
            source = root / 'item.jsonl'
            source.write_text('original', encoding='utf-8')
            stat = root.stat()
            identity = (stat.st_dev, stat.st_ino)
            outside = base / 'outside'
            (outside / 'sessions').mkdir(parents=True)
            (outside / 'sessions' / 'item.jsonl').write_text('private', encoding='utf-8')
            (base / 'store').rename(base / 'original')
            (base / 'store').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                file_adapters._open_source(source, root, identity)

    def test_db_snapshot_reads_wal_without_touching_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = root / 'state.sqlite'
            original = sqlite3.connect(path)
            try:
                original.execute('PRAGMA journal_mode=WAL')
                original.execute('PRAGMA wal_autocheckpoint=0')
                original.execute('CREATE TABLE records (value TEXT)')
                original.execute("INSERT INTO records VALUES ('in wal')")
                original.commit()
                source_names = [path.name, path.name + '-wal', path.name + '-shm']
                def fingerprints():
                    return {name: (hashlib.sha256((root / name).read_bytes()).hexdigest(), (root / name).stat().st_mtime_ns)
                            for name in source_names if (root / name).exists()}
                before = fingerprints()
                snapshot, _ = db_adapters._db(root, Path(path.name))
                try:
                    self.assertEqual(snapshot.execute('SELECT value FROM records').fetchone()[0], 'in wal')
                    with self.assertRaises(sqlite3.OperationalError):
                        snapshot.execute("INSERT INTO records VALUES ('must fail')")
                finally:
                    snapshot.close()
                self.assertEqual(fingerprints(), before)
            finally:
                original.close()

    def test_database_path_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            outside = root / 'other'
            outside.mkdir()
            (outside / 'data').write_text('private', encoding='utf-8')
            (root / 'link').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                db_adapters._open_beneath(root, root / 'link' / 'data')

    def test_fifo_is_rejected_without_waiting_for_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fifo = root / 'rollout'
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                db_adapters._open_beneath(root, fifo)

    def test_finder_reference_tracks_open_file_after_path_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = root / 'session.jsonl'
            path.write_text('original', encoding='utf-8')
            stat = root.stat()
            with file_adapters._open_source(path, root, (stat.st_dev, stat.st_ino)) as source:
                original_reference = server.file_reference(source)
                with path.open('rb') as another_descriptor:
                    self.assertEqual(server.file_reference(another_descriptor), original_reference)
                path.rename(root / 'moved.jsonl')
                path.write_text('replacement', encoding='utf-8')
                self.assertEqual(server.file_reference(source), original_reference)
                with (root / 'moved.jsonl').open('rb') as moved:
                    self.assertEqual(server.file_reference(moved), original_reference)
                with path.open('rb') as replacement:
                    self.assertNotEqual(server.file_reference(replacement), original_reference)


class ApiBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = server.Dashboard(0)
        cls.thread = threading.Thread(target=cls.dashboard.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.dashboard.shutdown()
        cls.dashboard.server_close()
        cls.thread.join()

    def request(self, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.dashboard.server_port, timeout=5)
        try:
            conn.request('GET', path, headers=headers or {})
            response = conn.getresponse()
            body = response.read()
            return response.status, body
        finally:
            conn.close()

    def test_metadata_requires_token_and_rejects_cross_origin(self):
        self.assertEqual(self.request('/api/sessions')[0], 403)
        headers = {'X-Harness-Token': self.dashboard.token, 'Origin': 'https://example.org'}
        self.assertEqual(self.request('/api/sessions', headers)[0], 403)
        headers = {'X-Harness-Token': self.dashboard.token, 'Host': 'attacker.example'}
        self.assertEqual(self.request('/api/sessions', headers)[0], 403)
        with patch.object(file_adapters, 'scan', return_value=([], [])), patch.object(db_adapters, 'scan', return_value=([], [])), patch.object(server.remote_adapter, 'scan', return_value=([], [])):
            self.assertEqual(self.request('/api/sessions', {'X-Harness-Token': self.dashboard.token})[0], 200)

    def test_bootstrap_secret_is_only_in_page_not_cross_origin_script(self):
        status, page = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(self.dashboard.token.encode(), page)
        self.assertLess(page.index(self.dashboard.token.encode()), page.index(b'/bootstrap.js'))
        status, script = self.request('/bootstrap.js')
        self.assertEqual(status, 200)
        self.assertNotIn(self.dashboard.token.encode(), script)
        self.assertEqual(self.request('/bootstrap.js', {'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_reveal_rejects_replaced_source_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / 'store' / 'sessions'
            root.mkdir(parents=True)
            source = root / 'item.jsonl'
            source.write_text('original', encoding='utf-8')
            item = {'id': 'OMP:test', 'source': 'OMP', 'path': str(source)}
            status = {'name': 'OMP', 'root': str(root), 'status': 'ok', 'detail': ''}
            with patch.object(file_adapters, 'scan', return_value=([item], [status])), patch.object(db_adapters, 'scan', return_value=([], [])), patch.object(server.remote_adapter, 'scan', return_value=([], [])):
                self.dashboard.scan()
            outside = base / 'outside'
            (outside / 'sessions').mkdir(parents=True)
            (outside / 'sessions' / 'item.jsonl').write_text('private', encoding='utf-8')
            (base / 'store').rename(base / 'original')
            (base / 'store').symlink_to(outside, target_is_directory=True)
            conn = http.client.HTTPConnection('127.0.0.1', self.dashboard.server_port, timeout=5)
            try:
                with patch.object(server.subprocess, 'run') as run:
                    conn.request('POST', '/api/reveal', body=json.dumps({'id': 'OMP:test'}), headers={
                        'Origin': self.dashboard.origin, 'X-Harness-Token': self.dashboard.token,
                        'Content-Type': 'application/json',
                    })
                    response = conn.getresponse()
                    response.read()
                    self.assertEqual(response.status, 400)
                    run.assert_not_called()
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
