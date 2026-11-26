"""Session-login/CSRF and operational log regression checks."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main
from web_auth import SessionManager
from diagnostics import DiagnosticsStore
from webtest_helpers import login, auth_headers


class WebSessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        folder = Path(self.folder.name)
        self.patches = [patch.object(main, 'PASSWORD', 'test-session-password'),
                        patch.object(main, 'AUTH', SessionManager(lambda: main.PASSWORD)),
                        patch.object(main, 'DIAGNOSTICS', DiagnosticsStore(folder / 'diagnostics.json')),
                        patch.object(main, 'DATA', folder)]
        for ctx in self.patches:
            ctx.start()
            self.addCleanup(ctx.stop)
        self.server = main.ThreadingHTTPServer(('127.0.0.1', 0), main.Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.worker.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def request(self, path, method='GET', data=None, headers=None):
        payload = json.dumps(data).encode() if data is not None else None
        headers = dict(headers or {})
        if data is not None:
            headers.setdefault('Content-Type', 'application/json')
        req = urllib.request.Request(self.base + path, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, res.headers, res.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_login_page_is_served_without_basic_popup(self):
        code, headers, body = self.request('/api/config')
        self.assertEqual(code, 401)
        self.assertNotIn('WWW-Authenticate', headers)
        code, _, body = self.request('/login')
        self.assertEqual(code, 200)
        self.assertIn(b'login-form', body)
        self.assertIn(b'login-password', body)
        self.assertNotIn(b'ADMIN_PASSWORD', body)
        code, _, body = self.request('/assets/login.css')
        self.assertEqual(code, 200)
        self.assertIn(b'.login-card', body)
        code, _, body = self.request('/assets/app.js')
        self.assertEqual(code, 401)

    def test_private_qbit_connection_roundtrip(self):
        route = '/api/qbit-connection'
        self.assertEqual(self.request(route)[0], 401)
        cookie, csrf = login(self.base, 'test-session-password')
        body = dict(url='http://nas:8080', username='test', password='private-example')
        self.assertEqual(self.request(route, 'POST', body, {'Cookie':cookie})[0], 403)
        code, _, response = self.request(route, 'POST', body, auth_headers(cookie, csrf))
        self.assertEqual(code, 200)
        public = dict(url=body['url'], username='test', hasPassword=True)
        self.assertEqual(json.loads(response), public)
        self.assertEqual(json.loads(self.request(route, headers={'Cookie':cookie})[2]), public)

    def test_session_cookie_csrf_logout(self):
        code, _, body = self.request('/api/auth/login', 'POST', {'username':'admin','password':'wrong'})
        self.assertEqual(code, 401)
        self.assertNotIn(b'test-session-password', body)
        cookie, csrf = login(self.base, 'test-session-password')
        code, _, body = self.request('/api/auth/session', headers={'Cookie': cookie})
        self.assertTrue(json.loads(body)['authenticated'])
        code, _, _ = self.request('/api/health', headers={'Cookie': cookie})
        self.assertEqual(code, 200)
        code, _, _ = self.request('/api/auth/logout', 'POST', headers={'Cookie': cookie})
        self.assertEqual(code, 403)
        code, headers, body = self.request('/api/auth/logout', 'POST', headers=auth_headers(cookie, csrf))
        self.assertEqual(code, 200)
        self.assertIn('Max-Age=0', headers.get('Set-Cookie', ''))
        code, _, _ = self.request('/api/health', headers={'Cookie': cookie})
        self.assertEqual(code, 401)

    def test_bad_origin_cannot_login_or_post(self):
        code, _, _ = self.request('/api/auth/login', 'POST', {'username':'admin','password':'test-session-password'},
                                  headers={'Origin':'https://attacker.invalid'})
        self.assertEqual(code, 403)
        cookie, csrf = login(self.base, 'test-session-password')
        code, _, _ = self.request('/api/auth/logout', 'POST', headers={**auth_headers(cookie, csrf), 'Sec-Fetch-Site':'cross-site'})
        self.assertEqual(code, 403)
        code, _, _ = self.request('/api/health', headers={'Cookie': cookie})
        self.assertEqual(code, 200)

    def test_bad_password_rate_limited(self):
        for _ in range(5):
            code, _, _ = self.request('/api/auth/login', 'POST', {'username':'admin','password':'incorrect'})
            self.assertEqual(code, 401)
        code, _, _ = self.request('/api/auth/login', 'POST', {'username':'admin','password':'test-session-password'})
        self.assertEqual(code, 429)

    def test_no_plaintext_secrets_in_diagnostics(self):
        cookie, _ = login(self.base, 'test-session-password')
        code, _, body = self.request('/api/diagnostics', headers={'Cookie': cookie})
        self.assertEqual(code, 200)
        self.assertNotIn(b'test-session-password', body)
        self.assertIn(b'joalTransferCounterAvailable', body)


class DiagnosticHistoryTests(unittest.TestCase):
    def test_session_end_and_restart_persist(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / 'diagnostics.json'
            journal = DiagnosticsStore(file)
            h = 'b' * 40
            journal.start_rule('rule1', [h])
            self.assertEqual(journal.snapshot(infohash=h)['sessions'][0]['status'], 'running')
            journal.stop_rule('rule1')
            self.assertEqual(journal.snapshot(infohash=h)['sessions'][0]['status'], 'stopped')
            reopened = DiagnosticsStore(file)
            result = reopened.snapshot(infohash=h)
            self.assertEqual(len(result['events']), 2)
            self.assertEqual(result['sessions'][0]['status'], 'stopped')
            self.assertIsNone(result['sessions'][0]['simulatedBytes'])
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_state_transition_without_duplicate_polling(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = DiagnosticsStore(Path(temp) / 'diagnostics.json')
            rows = [{'infoHash':'a'*40, 'rule':'sample', 'selected': False, 'eligible': False}]
            journal.observe(rows)
            self.assertEqual(journal.snapshot()['events'], [])
            journal.observe(rows)
            self.assertEqual(journal.snapshot()['events'], [])
            rows[0]['eligible'] = True
            journal.observe(rows)
            self.assertEqual(len(journal.snapshot()['events']), 1)
            self.assertNotIn('announce', json.dumps(journal.snapshot()))

    def test_invalid_hash_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = DiagnosticsStore(Path(temp) / 'diagnostics.json')
            with self.assertRaises(ValueError):
                journal.snapshot(infohash='invalid')


if __name__ == '__main__':
    unittest.main()
