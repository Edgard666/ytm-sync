import base64
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
from core import encode_bencode
from webtest_helpers import login, auth_headers
import main

POLICY = {
    'dryRun': True, 'qbitTag': 'joal', 'refreshSeconds': 180, 'maxCandidates': 250,
    'rules': [
        {'id': 'example', 'enabled': True, 'trackerDomains': ['example.net'],
         'minLeechers': 3, 'minUploadRate': 30, 'maxUploadRate': 160,
         'simultaneousSeed': 1, 'client': 'qbittorrent.client'}
    ],
}


class Stub:
    def torrents(self, tag):
        return [
            {'name': 'Prêt', 'hash': 'a' * 40, 'progress': 1.0, 'state': 'stoppedUP', 'ratio': 0.3},
            {'name': 'À la limite', 'hash': 'b' * 40, 'progress': 1.0, 'state': 'stoppedUP', 'ratio': 0.3},
            {'name': 'Téléchargement', 'hash': 'c' * 40, 'progress': 0.5, 'state': 'downloading'},
            {'name': 'Pas assez de leechers', 'hash': 'd' * 40, 'progress': 1.0, 'state': 'stoppedUP', 'ratio': 0.3},
            {'name': 'Pas le bon tracker', 'hash': 'e' * 40, 'progress': 1.0, 'state': 'stoppedUP', 'ratio': 0.3},
        ]

    def trackers(self, h):
        host = 'https://tracker.example.net/announce/SECRET_TOKEN'
        if h.startswith('e'):
            host = 'https://other.example.org/announce/SECRET_TOKEN'
        count = 2 if h.startswith('d') else 5
        return [{'url': host, 'status': 2, 'num_leeches': count}]

    def export(self, h):
        raise AssertionError('No export in dry-run')


class DashboardTests(unittest.TestCase):
    def test_status_distinguishes_selected_eligible_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            conf = Path(folder) / 'policy.json'
            conf.write_text(json.dumps(POLICY))
            with patch.object(main, 'CONFIG', conf), patch.object(main, 'DATA', Path(folder)), \
                 patch.object(main, 'RUNNERS', {}), patch.object(main, 'LAST_START', {}):
                main.run_once(Stub())
                state = json.loads(json.dumps(main.STATUS))
        self.assertEqual(state['totals']['tagged'], 5)
        self.assertEqual(state['totals']['selected'], 1)
        self.assertEqual(state['totals']['eligible'], 2)
        rows = {r['name']: r for r in state['torrents']}
        self.assertTrue(rows['Prêt']['selected'])
        self.assertFalse(rows['À la limite']['selected'])
        self.assertEqual(rows['À la limite']['reason'], 'Limite de la règle example atteinte')
        self.assertEqual(rows['Téléchargement']['reason'], 'Torrent incomplet')
        self.assertEqual(rows['Pas assez de leechers']['reason'], 'Leechers insuffisants : 2 / minimum 3')
        self.assertEqual(rows['Pas le bon tracker']['reason'], 'Tracker non ciblé : domaine différent de la règle')
        self.assertNotIn('SECRET_TOKEN', json.dumps(state))
        self.assertEqual(rows['Prêt']['trackers'], ['tracker.example.net'])

    def test_static_assets_config_auth_and_csrf(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'policy.json').write_text(json.dumps(POLICY))
            (path / 'clients').mkdir()
            (path / 'clients' / 'qbittorrent.client').write_text('dummy')
            with patch.object(main, 'CONFIG', path / 'policy.json'), patch.object(main, 'DATA', path), \
                 patch.object(main, 'PASSWORD', 'test-secret-password'):
                server = main.ThreadingHTTPServer(('127.0.0.1', 0), main.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = 'http://127.0.0.1:' + str(server.server_port)
                cookie, csrf = login(base, 'test-secret-password')

                def req(endpoint, *, auth_enabled=True, method='GET', payload=None, origin=None):
                    headers = {}
                    if auth_enabled:
                        headers.update(auth_headers(cookie, csrf))
                    if origin:
                        headers['Origin'] = origin
                    if payload is not None:
                        headers['Content-Type'] = 'application/json'
                    request = urllib.request.Request(base + endpoint, data=payload, headers=headers, method=method)
                    return urllib.request.urlopen(request, timeout=5)

                try:
                    with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                        req('/api/config', auth_enabled=False)
                    self.assertEqual(unauthorized.exception.code, 401)
                    with req('/') as resp:
                        self.assertIn(b'/assets/app.js', resp.read())
                    with req('/assets/app.js') as resp:
                        self.assertIn(b'renderTorrents', resp.read())
                    with req('/assets/torrent_table.js') as resp:
                        self.assertIn(b'function sortRows', resp.read())
                    with req('/assets/app.css') as resp:
                        self.assertIn(b'.sidebar', resp.read())
                    with req('/api/clients') as resp:
                        self.assertEqual(json.load(resp)['clients'], ['qbittorrent.client'])
                    with self.assertRaises(urllib.error.HTTPError) as forbidden:
                        req('/api/config', method='POST', payload=json.dumps(POLICY).encode(), origin='https://attacker.example.net')
                    self.assertEqual(forbidden.exception.code, 403)
                    new_conf = json.loads(json.dumps(POLICY))
                    new_conf['rules'][0]['minLeechers'] = 8
                    with req('/api/config', method='POST', payload=json.dumps(new_conf).encode(), origin=base) as resp:
                        self.assertTrue(json.load(resp)['success'])
                    self.assertEqual(json.loads((path / 'policy.json').read_text())['rules'][0]['minLeechers'], 8)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

if __name__ == '__main__':
    unittest.main()
