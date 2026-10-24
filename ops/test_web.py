from pathlib import Path
import gzip
import tempfile
import unittest
from unittest.mock import patch

from ops.web import entry_assets, publish, site_mount, validate_bundle


class WebPublicationTests(unittest.TestCase):
    public = 'https://example.invalid/sub2api'

    def bundle(self, root):
        for path, text in {
            'index.html': '<script src="/sub2api/assets/app.js"></script>',
            'assets/app.js': 'console.log("current")',
            'chat-ui/index.html': '<script src="./assets/chat.js"></script>',
            'chat-ui/assets/chat.js': 'console.log("chat")',
        }.items():
            target = root/path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def test_rejects_root_asset_on_a_subpath_before_publication(self):
        with self.assertRaisesRegex(ValueError, 'mount'):
            entry_assets('<script src="/assets/app.js"></script>', self.public+'/index.html', self.public)

    def test_resolves_relative_chat_assets_within_the_chat_directory(self):
        self.assertEqual(entry_assets('<script src="./assets/chat.js"></script>',
                                     self.public+'/chat-ui/index.html', self.public), ['chat-ui/assets/chat.js'])

    def test_missing_asset_never_replaces_a_working_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'build'; target = root/'www'
            self.bundle(source); self.bundle(target)
            (source/'assets/app.js').unlink()
            previous = (target/'index.html').read_bytes()
            with self.assertRaisesRegex(ValueError, 'missing'):
                publish(source, target, self.public)
            self.assertEqual((target/'index.html').read_bytes(), previous)

    def test_publishes_both_entries_and_keeps_chunks_used_by_open_tabs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'build'; target = root/'www'
            self.bundle(source); target.mkdir()
            (target/'old-hash.js').write_text('open tab')
            result = publish(source, target, self.public)
            self.assertEqual(result['entry_assets_checked'], 2)
            self.assertEqual((target/'old-hash.js').read_text(), 'open tab')
            self.assertEqual(validate_bundle(target, self.public), ['assets/app.js', 'chat-ui/assets/chat.js'])
            self.assertEqual(gzip.decompress((target/'assets/app.js.gz').read_bytes()), (target/'assets/app.js').read_bytes())

    def test_entry_pages_are_replaced_after_all_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'build'; target = root/'www'
            self.bundle(source)
            import os
            original = os.replace
            replaced = []
            def replace(pending, destination):
                replaced.append(Path(destination).relative_to(target).as_posix())
                return original(pending, destination)
            with patch('ops.web.os.replace', side_effect=replace):
                publish(source, target, self.public)
            first_html = next(i for i, path in enumerate(replaced) if path.endswith('.html'))
            self.assertTrue(all(path.endswith('.html') for path in replaced[first_html:]))

    def test_root_deployments_remain_supported(self):
        self.assertEqual(site_mount('https://example.invalid'), '/')
        self.assertEqual(entry_assets('<script src="/assets/app.js"></script>',
                                     'https://example.invalid/index.html', 'https://example.invalid'), ['assets/app.js'])

    def test_rejects_cross_origin_and_encoded_traversal(self):
        for reference in ('https://other.invalid/app.js', '/sub2api/%2e%2e/app.js'):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                entry_assets('<script src="'+reference+'"></script>', self.public+'/index.html', self.public)


if __name__ == '__main__':
    unittest.main()
