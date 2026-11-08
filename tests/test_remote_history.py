import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import remote_worker


class RemoteHistoryTests(unittest.TestCase):
    def test_hidden_messages_not_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'state.db'
            con = sqlite3.connect(db)
            con.executescript('''
                CREATE TABLE sessions (id TEXT PRIMARY KEY);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    timestamp REAL, content TEXT, tool_calls TEXT, tool_name TEXT, display_kind TEXT);
                INSERT INTO sessions VALUES ('one');
                INSERT INTO messages VALUES (1, 'one', 'user', 1, 'visible', NULL, NULL, 'message');
                INSERT INTO messages VALUES (2, 'one', 'assistant', 2, 'hidden', NULL, NULL, 'hidden');
            ''')
            con.close()

            def snapshot():
                reader = sqlite3.connect(db)
                reader.row_factory = sqlite3.Row
                class NoCleanup:
                    def cleanup(self):
                        pass
                return reader, NoCleanup()

            with patch.object(remote_worker, 'snapshot', snapshot):
                result = remote_worker.detail({'key': 'one', 'offset': 0, 'limit': 80})
            self.assertEqual([message['text'] for message in result['messages']], ['visible'])

    def test_remote_artifacts_exclude_secret_names(self):
        for name in ('id_ecdsa.txt', 'ssh-key.txt', 'settings.txt', '.env_backup'):
            with self.subTest(name=name):
                self.assertTrue(remote_worker.restricted([name]))
        self.assertFalse(remote_worker.restricted(['report.md']))
        self.assertTrue(remote_worker.restricted(['sessions.json']))


if __name__ == '__main__':
    unittest.main()
