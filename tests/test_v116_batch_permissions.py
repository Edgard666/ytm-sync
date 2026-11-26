"""v1.16 batch upload and Unraid ownership/permission regressions."""
import json
import os
import stat
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from test_import_v15 import WebImportTests, torrent_bytes
from core import encode_bencode, get_info_slice
from torrent_import import parse_upload_batch, ImportErrorValue
from solo_torrents import SoloLibrary
import main


def payload(name):
    return encode_bencode({b'announce': b'https://tracker.example.net/announce/PRIVATE_PASSKEY',
        b'info': {b'name': name.encode(),b'length': 100,b'piece length': 16384,b'pieces':b'1'*20}})


def multi(items):
    boundary = '----BatchTestBoundary'
    body = b''
    for name, blob in items:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="torrent"; filename="{name}"\r\n'
                 'Content-Type: application/x-bittorrent\r\n\r\n').encode() + blob + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    return f'multipart/form-data; boundary={boundary}', body


class BatchMetadataTests(unittest.TestCase):
    def test_parses_multiple_and_preserves_individual_invalid_extension(self):
        ct, body = multi([('one.torrent', payload('one')),('bad.txt',b'x'),('two.torrent',payload('two'))])
        entries = parse_upload_batch(ct, body)
        self.assertEqual(len(entries), 3)
        self.assertEqual([x['error'] is None for x in entries], [True,False,True])
    def test_rejects_more_than_thirty_files(self):
        ct, body = multi([(f'{i}.torrent',b'a') for i in range(31)])
        with self.assertRaisesRegex(ImportErrorValue,'30 fichiers'):
            parse_upload_batch(ct,body)
    def test_rejects_unexpected_fields(self):
        ct, body=multi([('a.torrent',b'a')]); body=body.replace(b'name="torrent"',b'name="savePath"')
        with self.assertRaisesRegex(ImportErrorValue,'Champ inattendu'):
            parse_upload_batch(ct,body)


class BatchApiTests(WebImportTests):
    """Reuses session-auth setup; test methods inherited from WebImportTests too."""
    def test_import_three_and_duplicate_and_corrupt_without_blocking(self):
        ct, body=multi([('one.torrent',payload('one')),('repeat.torrent',payload('one')),
                        ('corrupt.torrent',b'not bencode'),('two.torrent',payload('two'))])
        status,info=self.request('/api/import/inspect',ct=ct,body=body)
        self.assertEqual(status,200)
        self.assertEqual([r['status'] for r in info['results']],['ok','ok','error','ok'])
        self.assertNotIn('PRIVATE_PASSKEY',json.dumps(info))
        status,res=self.request('/api/import/add',ct=ct,body=body)
        self.assertEqual(status,200)
        self.assertEqual(res['added'],2)
        self.assertEqual(res['duplicates'],1)
        self.assertEqual(res['failed'],1)
        self.assertEqual([r['status'] for r in res['results']],['added','duplicate','error','added'])
        folder=Path(self.folder.name)/'torrents'
        self.assertEqual(len(list(folder.glob('*.torrent'))),2)
        self.assertNotIn('PRIVATE_PASSKEY',json.dumps(res))
    def test_committed_edits_visible_before_background_evaluation(self):
        ct, body = multi([('fast.torrent', payload('fast'))])
        with patch.object(main, 'STATUS', {'solo': {'count': 0, 'active': 0, 'items': []}}):
            status, added = self.request('/api/import/add', ct=ct, body=body)
            self.assertEqual(status, 200)
            _, state = self.request('/api/state')
            self.assertEqual(state['solo']['items'], [added['item']])
            self.assertEqual(state['solo']['count'], 1)
            status, _ = self.request('/api/solo/delete', ct='application/json',
                                     body=json.dumps({'infoHash': added['hash']}).encode())
            self.assertEqual(status, 200)
            _, state = self.request('/api/state')
            self.assertEqual(state['solo']['items'], [])
            self.assertEqual(state['solo']['count'], 0)

    def test_unwritable_library_returns_diagnostic_without_hiding_other_successes(self):
        lib=main.SOLO_LIBRARY
        original=lib.add
        def flaky(blob,rules,filename=None):
            if b'not writable' in blob: raise PermissionError('injected denied')
            return original(blob,rules,filename=filename)
        with patch.object(lib,'add',side_effect=flaky):
            ct,body=multi([('one.torrent',payload('one')),
                           ('blocked.torrent',payload('not writable'))])
            code,response=self.request('/api/import/add',ct=ct,body=body)
        self.assertEqual(code,200)
        self.assertEqual(response['added'],1)
        self.assertEqual(response['failed'],1)
        self.assertIn('droits',response['results'][1]['message'])


class PermissionTests(unittest.TestCase):
    def test_new_torrent_world_writable_and_directory_world_accessible(self):
        with tempfile.TemporaryDirectory() as td:
            lib=SoloLibrary(Path(td)/'torrents')
            with patch.dict(os.environ, {'TORRENT_FILE_MODE':'0666', 'TORRENT_DIR_MODE':'0777',
                                         'TORRENT_UID':str(os.getuid()), 'TORRENT_GID':str(os.getgid())}):
                info=lib.add(payload('one'),[])
            file=lib.root/(info['hash']+'.torrent')
            self.assertEqual(stat.S_IMODE(file.stat().st_mode),0o666)
            self.assertEqual(stat.S_IMODE(lib.root.stat().st_mode),0o777)
    def test_chmod_or_chown_denied_does_not_reject_saved_file(self):
        with tempfile.TemporaryDirectory() as td:
            lib=SoloLibrary(Path(td)/'torrents')
            with patch('solo_torrents.os.chmod',side_effect=PermissionError('denied')), \
                 patch('solo_torrents.os.chown',side_effect=PermissionError('denied')):
                with self.assertLogs('joal-smart',level='WARNING'):
                    info=lib.add(payload('two'),[])
            self.assertTrue((lib.root/(info['hash']+'.torrent')).is_file())
    def test_normalize_existing_private_file(self):
        with tempfile.TemporaryDirectory() as td:
            lib=SoloLibrary(Path(td)/'torrents')
            info=lib.add(payload('one'),[])
            file=lib.root/(info['hash']+'.torrent')
            file.chmod(0o600)
            with patch.dict(os.environ,{'TORRENT_FILE_MODE':'0666','TORRENT_DIR_MODE':'0777',
                                        'TORRENT_UID':str(os.getuid()),'TORRENT_GID':str(os.getgid())}):
                lib.normalize_permissions()
            self.assertEqual(stat.S_IMODE(file.stat().st_mode),0o666)

if __name__=='__main__':unittest.main()
