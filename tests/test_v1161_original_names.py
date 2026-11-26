"""Regression tests for preserving uploaded .torrent filenames without losing infohash identity."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'app'))  # standalone runs
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from solo_torrents import SoloLibrary, ImportErrorValue
from torrent_import import parse_upload_batch, validate_torrent_filename
from backup_bundle import build_backup, inspect_backup, permitted, schedule_restore, apply_pending_restore
from test_v116_batch_permissions import payload, multi
from test_v114_solo import configuration


class OriginalFilenameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lib = SoloLibrary(self.root / 'torrents')

    def test_preserves_original_filename_with_spaces_accents_and_unicode(self):
        name = 'Mon film – édition française 東京.TORRENT'
        data = payload('one')
        info = self.lib.add(data, [], filename=name)
        self.assertEqual(info['fileName'], name)
        self.assertEqual((self.lib.root / name).read_bytes(), data)
        self.assertFalse((self.lib.root / (info['hash'] + '.torrent')).exists())
        items = self.lib.list([])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['fileName'], name)
        self.assertEqual(items[0]['hash'], info['hash'])
        self.lib.delete(info['hash'])
        self.assertEqual(self.lib.list([]), [])
        self.assertFalse((self.lib.root / name).exists())

    def test_duplicate_hash_rejected_even_with_different_filenames(self):
        data = payload('one')
        self.lib.add(data, [], filename='Original.torrent')
        with self.assertRaisesRegex(ImportErrorValue, 'déjà dans la bibliothèque'):
            self.lib.add(data, [], filename='AutreNom.torrent')
        self.assertEqual(len(self.lib.list([])), 1)

    def test_same_filename_different_infohash_gets_non_overwriting_suffix(self):
        original_name = 'Même nom.torrent'
        first = self.lib.add(payload('first'), [], filename=original_name)
        second = self.lib.add(payload('second'), [], filename=original_name)
        self.assertEqual(first['fileName'], original_name)
        self.assertNotEqual(first['fileName'], second['fileName'])
        self.assertIn(second['hash'][:10], second['fileName'])
        self.assertEqual(len(self.lib.list([])), 2)
        self.lib.delete(first['hash'])
        self.assertEqual([t['hash'] for t in self.lib.list([])], [second['hash']])

    def test_existing_hash_named_file_kept_and_detects_duplicate(self):
        existing = self.lib.add(payload('one'), [])
        old = self.lib.root / (existing['hash'] + '.torrent')
        self.assertTrue(old.is_file())
        self.assertEqual(self.lib.list([])[0]['fileName'], old.name)
        with self.assertRaisesRegex(ImportErrorValue, 'déjà'):
            self.lib.add(payload('one'), [], filename='Original restored.torrent')
        self.assertTrue(old.is_file())

    def test_backup_preserves_original_name_and_legacy_file(self):
        first = self.lib.add(payload('first'), [], filename='Film Français.torrent')
        second = self.lib.add(payload('second'), [])
        (self.root / 'policy.json').write_text(json.dumps(configuration()))
        zip_data = build_backup(self.root)
        contents = inspect_backup(zip_data)
        self.assertEqual(contents['torrents/Film Français.torrent'], payload('first'))
        self.assertEqual(contents['torrents/' + second['hash'] + '.torrent'], payload('second'))

    def test_roundtrip_restore_keeps_original_filename(self):
        name = 'Un titre avec espaces – 東京.torrent'
        self.lib.add(payload('one'), [], filename=name)
        (self.root / 'policy.json').write_text(json.dumps(configuration()))
        archive = build_backup(self.root)
        destination = self.root / 'restored'
        destination.mkdir()
        self.assertEqual(schedule_restore(destination, archive), 2)
        self.assertTrue(apply_pending_restore(destination))
        self.assertEqual((destination / 'torrents' / name).read_bytes(), payload('one'))
        self.assertEqual(SoloLibrary(destination / 'torrents').list([])[0]['fileName'], name)

    def test_backup_rejects_duplicate_hash_under_multiple_filenames(self):
        (self.root / 'policy.json').write_text(json.dumps(configuration()))
        data = build_backup(self.root)
        with zipfile.ZipFile(io.BytesIO(data), 'r') as zf:
            files = {name: zf.read(name) for name in zf.namelist()}
        files['torrents/Film A.torrent'] = payload('same')
        files['torrents/Film B.torrent'] = payload('same')
        result = io.BytesIO()
        with zipfile.ZipFile(result, 'w') as zf:
            for name, contents in files.items():
                zf.writestr(name, contents)
        with self.assertRaisesRegex(ValueError, 'dupliqué'):
            inspect_backup(result.getvalue())

    def test_rejects_unsafe_names_and_zip_traversal(self):
        for name in ('../evil.torrent', 'folder\\evil.torrent', '.torrent', 'bad\x00.torrent',
                     'bad\u202e.torrent', 'too-long-' + 'a' * 240 + '.torrent'):
            with self.subTest(name=name), self.assertRaises(ImportErrorValue):
                validate_torrent_filename(name)
            self.assertFalse(permitted('torrents/' + name))

    def test_batch_import_returns_each_original_filename(self):
        ct, body = multi([('My Original.torrent', payload('one')), ('Other.torrent', payload('two'))])
        rows = parse_upload_batch(ct, body)
        self.assertEqual([r['filename'] for r in rows], ['My Original.torrent', 'Other.torrent'])
        self.assertTrue(all(row['error'] is None for row in rows))


if __name__ == '__main__':
    unittest.main()
