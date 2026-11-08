import base64
import json
import tempfile
import unittest
from pathlib import Path

import file_adapters


class HistoryPagingTests(unittest.TestCase):
    def test_complete_history_large_message_and_inline_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            directory = home / '.omp/agent/sessions/project'
            directory.mkdir(parents=True)
            path = directory / 'session.jsonl'
            strings = ['первый', 'второй', 'третий', 'x' * 80000 + 'конец']
            png = base64.b64encode(bytes.fromhex('89504e470d0a1a0a') + b'payload').decode()
            image_path = directory / 'image.png'
            image_path.write_bytes(base64.b64decode(png))
            rows = [{'type': 'session', 'id': 'example', 'cwd': str(home)}]
            rows += [{'type': 'message', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]}} for text in strings]
            rows.append({'type': 'message', 'message': {'role': 'user', 'content': [{'type': 'image', 'mimeType': 'image/png', 'data': png, 'path': str(image_path)}]}})
            path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
            sessions, _ = file_adapters.scan(home)
            session = next(item for item in sessions if item['source'] == 'OMP')
            offset = 0
            seen = []
            file_paths = []
            for _ in range(20):
                page = file_adapters.detail(session, offset=offset, limit=2)
                seen.extend(page['messages'])
                file_paths.extend(item['path'] for item in page['files'])
                if not page['has_more']:
                    break
                self.assertGreater(page['next_offset'], offset)
                offset = page['next_offset']
            else:
                self.fail('Pagination did not finish')
            self.assertEqual(''.join(m['text'] for m in seen if m['kind'] == 'message'), ''.join(strings))
            images = [m for m in seen if m.get('image')]
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0]['image'], 'data:image/png;base64,' + png)
            self.assertIn(str(image_path), file_paths)
            self.assertNotIn(png, ''.join(m['text'] for m in seen))


if __name__ == '__main__':
    unittest.main()
