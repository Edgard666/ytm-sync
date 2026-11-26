import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'app'))  # standalone runs
import unittest
from io import BytesIO
from unittest.mock import Mock
from main import APP_VERSION, Handler, STATIC


class VersionDisplayTests(unittest.TestCase):
    def test_server_version_tracks_shared_value(self):
        self.assertEqual(Handler.server_version, f"JOALController/{APP_VERSION}")

    def test_both_pages_have_dynamic_version_marker(self):
        for name in ('login.html', 'index.html'):
            raw = (STATIC / name).read_bytes()
            self.assertIn(b'v__APP_VERSION__', raw)
            self.assertNotIn(b'v1.13', raw)
            rendered = raw.replace(b'__APP_VERSION__', APP_VERSION.encode('ascii'))
            self.assertIn(f'v{APP_VERSION}'.encode(), rendered)
            self.assertNotIn(b'__APP_VERSION__', rendered)

    def test_real_http_html_renderer_replaces_version(self):
        for name in ('login.html', 'index.html'):
            handler = object.__new__(Handler)
            handler.wfile = BytesIO()
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.security_headers = Mock()
            handler.end_headers = Mock()
            handler.serve_static(name, 'text/html; charset=utf-8')
            rendered = handler.wfile.getvalue()
            self.assertIn(f'v{APP_VERSION}'.encode(), rendered)
            self.assertNotIn(b'__APP_VERSION__', rendered)
            self.assertNotIn(b'v1.13', rendered)


class FaviconTests(unittest.TestCase):
    def test_icon_is_public_and_linked_from_both_pages(self):
        for path in ('/favicon.ico', '/assets/favicon.svg'):
            handler = object.__new__(Handler)
            handler.path = path
            handler.session = Mock(return_value=None)   # not logged in
            handler.serve_static = Mock()
            handler.unauthorized = Mock()
            Handler.do_GET(handler)
            handler.serve_static.assert_called_once_with('favicon.svg', 'image/svg+xml')
            handler.unauthorized.assert_not_called()
        self.assertTrue((STATIC / 'favicon.svg').read_bytes().startswith(b'<svg'))
        for name in ('login.html', 'index.html'):
            self.assertIn(b'href="/assets/favicon.svg"', (STATIC / name).read_bytes())
