import os
import tempfile
import unittest
from pathlib import Path

import source_files


class FileBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        info = self.root.stat()
        self.identity = (info.st_dev, info.st_ino)

    def test_directory_navigation_displays_files_without_following_symlinks(self):
        (self.root / 'sessions').mkdir()
        (self.root / 'sessions' / 'thread.jsonl').write_text('session', encoding='utf-8')
        (self.root / 'auth.json').write_text('secret', encoding='utf-8')
        (self.root / 'config.toml').write_text('secret', encoding='utf-8')
        (self.root / '.envrc').write_text('secret', encoding='utf-8')
        (self.root / 'outside').symlink_to('/etc', target_is_directory=True)
        entries = {row['name']: row for row in source_files.list_files(self.root, self.identity, source='OMP')['entries']}
        self.assertEqual(entries['sessions']['kind'], 'directory')
        self.assertTrue(entries['auth.json']['restricted'])
        self.assertTrue(entries['config.toml']['restricted'])
        self.assertTrue(entries['.envrc']['restricted'])
        self.assertEqual(entries['outside']['kind'], 'symlink')
        with self.assertRaises((OSError, ValueError)):
            source_files.list_files(self.root, self.identity, 'outside')
        with self.assertRaises(ValueError):
            source_files.read_file(self.root, self.identity, 'auth.json', source='OMP')
        with self.assertRaises(ValueError):
            source_files.read_file(self.root, self.identity, 'config.toml', source='OMP')
        with self.assertRaises(ValueError):
            source_files.list_files(self.root, self.identity, '../etc')

    def test_text_pages_preserve_utf8_across_byte_boundary(self):
        text = 'a' * (source_files.TEXT_PAGE - 1) + '🙂' + 'окончание'
        (self.root / 'history.md').write_text(text, encoding='utf-8')
        first = source_files.read_file(self.root, self.identity, 'history.md', source='OMP')
        self.assertEqual(first['kind'], 'text')
        self.assertIsNotNone(first['next_offset'])
        second = source_files.read_file(self.root, self.identity, 'history.md', first['next_offset'], source='OMP')
        self.assertEqual(first['text'] + second['text'], text)
        self.assertIsNone(second['next_offset'])

    def test_non_image_cannot_be_rendered_as_image(self):
        (self.root / 'spoof.png').write_bytes(b'<svg onload="alert(1)"></svg>')
        result = source_files.read_file(self.root, self.identity, 'spoof.png', source='OMP')
        self.assertEqual(result['kind'], 'unsupported')
        self.assertNotIn('data_url', result)

    def test_wide_root_allows_only_known_session_and_artifact_subtrees(self):
        (self.root / 'sessions').mkdir()
        (self.root / 'sessions' / 'thread.jsonl').write_text('session', encoding='utf-8')
        (self.root / 'sessions' / 'private-key.txt').write_text('private', encoding='utf-8')
        (self.root / 'sessions' / 'id_ecdsa.txt').write_text('private', encoding='utf-8')
        (self.root / 'outside.txt').write_text('private', encoding='utf-8')
        (self.root / '.env_backup').write_text('private', encoding='utf-8')
        (self.root / '.netrc').write_text('private', encoding='utf-8')
        entries = {row['name']: row for row in source_files.list_files(self.root, self.identity, source='Codex')['entries']}
        self.assertTrue(entries['outside.txt']['restricted'])
        self.assertTrue(entries['.env_backup']['restricted'])
        self.assertTrue(entries['.netrc']['restricted'])
        for name in ('outside.txt', '.env_backup', '.netrc'):
            with self.assertRaises(ValueError):
                source_files.read_file(self.root, self.identity, name, source='Codex')
        result = source_files.read_file(self.root, self.identity, 'sessions/thread.jsonl', source='Codex')
        self.assertEqual(result['text'], 'session')
        for name in ('sessions/private-key.txt', 'sessions/id_ecdsa.txt'):
            with self.assertRaises(ValueError):
                source_files.read_file(self.root, self.identity, name, source='Codex')

    def test_replaced_root_is_rejected(self):
        info = self.root.stat()
        with self.assertRaises(ValueError):
            source_files.list_files(self.root, (info.st_dev, info.st_ino + 1))


if __name__ == '__main__':
    unittest.main()
