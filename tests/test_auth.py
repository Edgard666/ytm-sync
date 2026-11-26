import sys
import unittest
import urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from qbit import QBitClient


class FakeClient(QBitClient):
    def __init__(self, auth_required):
        super().__init__('http://localhost:8080', 'admin', 'pwd')
        self.auth_required = auth_required
        self.calls = []
        self.session = False

    def request(self, path, params=None, method='GET'):
        self.calls.append((path, method))
        if path == 'app/version':
            if self.auth_required:
                raise urllib.error.HTTPError(self.url, 403, 'Forbidden', {}, None)
            return b'v5.3.0'
        if path == 'auth/login':
            self.session = True
            return b'Ok.'
        if self.auth_required and not self.session:
            raise urllib.error.HTTPError(self.url, 403, 'Forbidden', {}, None)
        return b'[]'


class AuthenticationTests(unittest.TestCase):
    def test_bypass_does_not_call_login(self):
        q = FakeClient(auth_required=False)
        self.assertEqual(q.torrents('joal'), [])
        self.assertNotIn(('auth/login', 'POST'), q.calls)
        self.assertEqual(q.calls[0], ('app/version', 'GET'))

    def test_protected_endpoint_triggers_login(self):
        q = FakeClient(auth_required=True)
        self.assertEqual(q.torrents('joal'), [])
        self.assertIn(('auth/login', 'POST'), q.calls)

    def test_expired_session_reauthenticates(self):
        q = FakeClient(auth_required=True)
        self.assertEqual(q.torrents('joal'), [])
        q.session = False
        self.assertEqual(q.torrents('joal'), [])
        self.assertEqual(q.calls.count(('auth/login', 'POST')), 2)


if __name__ == '__main__':
    unittest.main()
