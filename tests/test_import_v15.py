"""v1.5 regression tests for .torrent upload, WebUI and qBittorrent bridge.
No outbound tracker calls or JOAL sessions are made.
"""
import base64
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from core import encode_bencode, get_info_slice
from torrent_import import inspect_torrent, parse_upload, ImportErrorValue
from qbit import QBitClient
from solo_torrents import SoloLibrary
from webtest_helpers import login, auth_headers
import main

RULE = {'id':'sample','enabled':True,'trackerDomains':['tracker.example.net'],
        'minLeechers':3,'minUploadRate':10,'maxUploadRate':40,
        'simultaneousSeed':2,'targetRatio':1.2,'client':'qbittorrent.client'}
CONF = {'dryRun':True,'qbitTag':'joal','refreshSeconds':180,
        'maxCandidates':250,'rules':[RULE], 'completedTag':'JoalCompleted', 'guardSeconds':5}

def torrent_bytes():
    root = {b'announce':b'https://tracker.example.net/announce/PRIVATE_PASSKEY',
            b'info':{b'name':b'test-data',b'length':123,b'piece length':16384,b'pieces':b'0'*20}}
    return encode_bencode(root)

def multipart(content, fields=None):
    boundary = '----JoalTestBoundary'
    body = b''
    for name,value in (fields or {}).items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                 + value.encode() + b'\r\n')
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="torrent"; filename="test.torrent"\r\n'
             'Content-Type: application/x-bittorrent\r\n\r\n').encode() + content + b'\r\n'
    body += (f'--{boundary}--\r\n').encode()
    return f'multipart/form-data; boundary={boundary}', body


class MetadataTests(unittest.TestCase):
    def test_parse_and_hash_and_no_passkey(self):
        torrent = torrent_bytes()
        ct, body = multipart(torrent, {'savePath':'/data/seeding'})
        filename, actual, folder = parse_upload(ct, body)
        self.assertEqual(filename,'test.torrent')
        self.assertEqual(actual,torrent)
        self.assertEqual(folder,'/data/seeding')
        result = inspect_torrent(actual,[RULE])
        self.assertEqual(result['hash'],hashlib.sha1(get_info_slice(torrent)).hexdigest())
        self.assertEqual(result['matchedDomains'],['tracker.example.net'])
        self.assertNotIn('PRIVATE_PASSKEY',json.dumps(result))
    def test_reject_disabled_or_mismatched_domains(self):
        with self.assertRaises(ImportErrorValue):
            inspect_torrent(torrent_bytes(), [{**RULE,'enabled':False}])
        with self.assertRaises(ImportErrorValue):
            inspect_torrent(torrent_bytes(), [{**RULE,'trackerDomains':['notexample.net']}])
    def test_reject_bad_payload_and_filename(self):
        with self.assertRaises(ImportErrorValue):
            inspect_torrent(b'random garbage', [RULE])
        ct, body = multipart(torrent_bytes())
        with self.assertRaises(ImportErrorValue):
            parse_upload('text/plain',body)
        with self.assertRaises(ImportErrorValue):
            parse_upload(ct,body[:20])
    def test_bencode_with_announce_list(self):
        root = {b'info':{b'name':b'x',b'length':1},
                b'announce':b'udp://tracker.example.net:80/announce',
                b'announce-list':[[b'https://tracker.example.net/announce/key'],
                                  [b'https://other.example.org/announce/foo']]}
        result = inspect_torrent(encode_bencode(root),[RULE])
        self.assertEqual(result['domains'],['other.example.org','tracker.example.net'])
        self.assertEqual(result['matchedDomains'],['tracker.example.net'])


class StubQbit:
    inventory = []
    imported = []
    def __init__(self,*args): pass
    def torrents_all(self): return list(self.inventory)
    def add_torrent_file(self,payload,filename,tag,savepath):
        self.imported.append((payload,filename,tag,savepath))


class WebImportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        path = Path(self.folder.name)
        (path/'policy.json').write_text(json.dumps(CONF),encoding='utf-8')
        self.patches = [patch.object(main,'CONFIG',path/'policy.json'),
                        patch.object(main,'DATA',path),
                        patch.object(main,'SOLO_LIBRARY',SoloLibrary(path/'torrents')),
                        patch.object(main,'PASSWORD','a-safe-test-password'),
                        patch.object(main,'QBitClient',StubQbit),
                        patch.object(main,'QBIT_URL','http://127.0.0.1:8080')]
        for item in self.patches: item.start()
        StubQbit.inventory = []
        StubQbit.imported = []
        self.server = main.ThreadingHTTPServer(('127.0.0.1',0),main.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.cookie, self.csrf = login(self.base, 'a-safe-test-password')
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for item in reversed(self.patches): item.stop()
        self.folder.cleanup()
    def request(self,route,*,ct=None,body=None,auth=True,origin=None):
        headers = auth_headers(self.cookie, self.csrf) if auth else {}
        if ct: headers['Content-Type']=ct
        if origin: headers['Origin']=origin
        req = urllib.request.Request(self.base+route, data=body,headers=headers, method='POST' if body is not None else 'GET')
        try:
            with urllib.request.urlopen(req,timeout=5) as response:
                return response.status,json.load(response)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code,(json.loads(raw) if raw else {})
    def test_import_success_preview_and_tracker_privacy(self):
        ct,body=multipart(torrent_bytes())
        status,data=self.request('/api/import/inspect',ct=ct,body=body)
        self.assertEqual(status,200)
        self.assertEqual(data['torrent']['matchedDomains'],['tracker.example.net'])
        self.assertNotIn('PRIVATE_PASSKEY',json.dumps(data))
        self.assertEqual(StubQbit.imported,[])
        status,data=self.request('/api/import/add',ct=ct,body=body)
        self.assertEqual(status,200)
        self.assertTrue(data['success'])
        self.assertEqual(StubQbit.imported, [])
        self.assertEqual(data['mode'], 'solo')
        self.assertEqual(data['storedFileName'], 'test.torrent')
        saved = Path(self.folder.name)/'torrents'/'test.torrent'
        self.assertEqual(saved.read_bytes(), torrent_bytes())
    def test_import_large_piece_table(self):
        pieces = 891290
        payload = encode_bencode({b'announce': b'https://tracker.example.net/announce',
            b'info': {b'name': b'large', b'length': pieces * 16384, b'piece length': 16384,
                      b'pieces': hashlib.sha1(b'piece').digest() * pieces}})
        self.assertGreater(len(payload), 17 * 1024 * 1024)
        ct, body = multipart(payload)
        for action in ('inspect', 'add'):
            status, result = self.request('/api/import/' + action, ct=ct, body=body)
            self.assertEqual(status, 200, result)
        saved = Path(self.folder.name) / 'torrents' / 'test.torrent'
        self.assertEqual(saved.read_bytes(), payload)
        self.assertEqual(main.SOLO_LIBRARY.list([RULE])[0]['name'], 'large')

    def test_duplicate_is_rejected_without_upload(self):
        hash_ = hashlib.sha1(get_info_slice(torrent_bytes())).hexdigest()
        ct,body=multipart(torrent_bytes())
        self.assertEqual(self.request('/api/import/add',ct=ct,body=body)[0],200)
        status,data=self.request('/api/import/add',ct=ct,body=body)
        self.assertEqual(status,409)
        self.assertIn('déjà',data['error'])
        self.assertEqual(StubQbit.imported,[])
    def test_rejected_unauthorized_and_csrf(self):
        ct,body=multipart(torrent_bytes())
        self.assertEqual(self.request('/api/import/add',ct=ct,body=body,auth=False)[0],401)
        self.assertEqual(self.request('/api/import/add',ct=ct,body=body,origin='https://attacker.example.org')[0],403)
        self.assertEqual(StubQbit.imported,[])
    def test_does_not_import_unknown_trackers(self):
        (Path(self.folder.name)/'policy.json').write_text(json.dumps({**CONF,'rules':[{**RULE,'enabled':False}]}))
        ct,body=multipart(torrent_bytes())
        status,_=self.request('/api/import/add',ct=ct,body=body)
        self.assertEqual(status,200)  # Stored for later rule activation
        self.assertEqual(StubQbit.imported,[])


class QbitUploadTests(unittest.TestCase):
    def test_multipart_to_qbittorrent_has_paused_and_tag(self):
        captured = []
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b''
        class Opener:
            def open(self, request,timeout):
                captured.append(request)
                return Response()
        q = QBitClient('http://qbit.local:8080','','')
        q.logged = True
        q.opener = Opener()
        q.add_torrent_file(torrent_bytes(),'private-name.torrent','joal','/download')
        self.assertEqual(len(captured),1)
        request = captured[0]
        self.assertEqual(request.get_method(),'POST')
        self.assertIn('torrents/add',request.full_url)
        self.assertIn(b'name="paused"\r\n\r\ntrue',request.data)
        self.assertIn(b'name="tags"\r\n\r\njoal',request.data)
        self.assertIn(b'name="savepath"\r\n\r\n/download',request.data)
        self.assertNotIn(b'private-name.torrent',request.data)

if __name__ == '__main__': unittest.main()
