import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'app'))  # standalone runs
import io
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

import interface_design as design
import main
from backup_bundle import build_backup, inspect_backup
from notifications import NotificationCenter
from swarm_monitor import SwarmMonitor
from web_auth import SessionManager
from webtest_helpers import auth_headers, login

STATIC = Path(__file__).resolve().parents[1] / 'app' / 'static'


class DesignStoreTests(unittest.TestCase):
    def test_default_save_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(design.current(root), 'classique')
            design.save(root, 'baie')
            self.assertEqual(design.current(root), 'baie')
            self.assertEqual((Path(root) / 'interface.json').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                design.save(root, '../app.css')
            self.assertEqual(design.current(root), 'baie')
            for raw in ('{', '[]', '{"design": "inconnu"}'):
                (Path(root) / 'interface.json').write_text(raw)
                self.assertEqual(design.current(root), 'classique', raw)

    def test_render_points_both_pages_at_the_chosen_stylesheet(self):
        for page, name, default in (('app', 'index.html', 'app.css'), ('login', 'login.html', 'login.css')):
            raw = (STATIC / name).read_bytes()
            self.assertIn(f'href="/assets/{default}"'.encode(), raw)
            classic = design.render(raw, page, 'classique')
            self.assertIn(f'href="/assets/{default}"'.encode(), classic)
            self.assertIn(b'data-design="classique"', classic)
            baie = design.render(raw, page, 'baie')
            self.assertIn(f'href="/assets/{design.DESIGNS["baie"][page]}"'.encode(), baie)
            self.assertNotIn(f'href="/assets/{default}"'.encode(), baie)
            self.assertIn(b'data-design="baie"', baie)
            self.assertIn(b'<meta name="color-scheme" content="light">', baie)

    def test_every_design_asset_exists_and_fonts_are_local(self):
        for spec in design.DESIGNS.values():
            for key in ('app', 'login'):
                css = (STATIC / spec[key]).read_text(encoding='utf-8')
                for url in re.findall(r'url\(([^)]+)\)', css):
                    self.assertTrue(url.startswith('/assets/fonts/'), url)   # CSP: self only
                    self.assertTrue((STATIC / url[len('/assets/'):]).is_file(), url)

    def test_baie_stylesheet_covers_every_class_of_the_classic_one(self):
        def classes(name):
            return set(re.findall(r'\.([a-zA-Z][\w-]*)', (STATIC / name).read_text(encoding='utf-8')))
        missing = classes('app.css') - classes('design-baie.css')
        self.assertEqual(missing, set(), 'Classes sans style dans le design Baie')


class DesignHttpTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        policy = Path(__file__).resolve().parents[1] / 'config.example.json'
        (self.root / 'policy.json').write_bytes(policy.read_bytes())
        for p in (patch.object(main, 'DATA', self.root), patch.object(main, 'CONFIG', self.root / 'policy.json'),
                  patch.object(main, 'NOTIFICATIONS', NotificationCenter(self.root / 'notifications.json')),
                  patch.object(main, 'SWARM_MONITOR', SwarmMonitor(self.root / 'tracker-registry.json')),
                  patch.object(main, 'HANDOFF_LEDGER', None), patch.object(main, 'PASSWORD', 'local-test-password'),
                  patch.object(main, 'AUTH', SessionManager(lambda: main.PASSWORD))):
            p.start(); self.addCleanup(p.stop)
        self.server = main.ThreadingHTTPServer(('127.0.0.1', 0), main.Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start()
        self.addCleanup(thread.join, 5); self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def request(self, path, method='GET', payload=None, headers=None):
        req = urllib.request.Request(self.base + path, data=payload, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, res.read(), res.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def test_switch_requires_session_and_csrf_and_changes_served_pages(self):
        self.assertEqual(self.request('/api/interface-design')[0], 401)
        body = json.dumps({'design': 'baie'}).encode()
        self.assertEqual(self.request('/api/interface-design', 'POST', body, {'Content-Type': 'application/json'})[0], 401)
        cookie, csrf = login(self.base, 'local-test-password')
        self.assertEqual(self.request('/api/interface-design', 'POST', body,
                                      {'Cookie': cookie, 'Content-Type': 'application/json'})[0], 403)
        headers = {**auth_headers(cookie, csrf), 'Content-Type': 'application/json'}
        for bad in (b'{"design": "rose"}', b'{"design": "baie", "x": 1}', b'["baie"]', b'{'):
            self.assertEqual(self.request('/api/interface-design', 'POST', bad, headers)[0], 400, bad)
        code, data, _ = self.request('/api/interface-design', 'POST', body, headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)['stylesheet'], '/assets/design-baie.css')
        page = self.request('/', headers={'Cookie': cookie})[1]
        self.assertIn(b'href="/assets/design-baie.css"', page)
        self.assertIn(b'href="/assets/design-baie-login.css"', self.request('/login')[1])

    def test_public_assets_of_the_login_page(self):
        code, data, headers = self.request('/assets/design-baie-login.css')
        self.assertEqual((code, headers.get_content_type()), (200, 'text/css'))
        code, data, headers = self.request('/assets/fonts/barlow-latin-400-normal.woff2')
        self.assertEqual((code, headers.get_content_type()), (200, 'font/woff2'))
        self.assertTrue(data.startswith(b'wOF2'))
        # Exact-match routes only: a traversal attempt is never served.
        code, data, _ = self.request('/assets/fonts/../policy.json')
        self.assertIn(code, (401, 404)); self.assertNotIn(b'dryRun', data)
        self.assertEqual(self.request('/assets/design-baie.css')[0], 401)   # app stylesheet stays private

    def test_backup_carries_the_design_and_rejects_bad_values(self):
        design.save(self.root, 'baie')
        files = inspect_backup(build_backup(self.root))
        self.assertEqual(json.loads(files['interface.json']), {'design': 'baie'})
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('policy.json', (self.root / 'policy.json').read_bytes())
            archive.writestr('interface.json', '{"design": "../x"}')
        with self.assertRaises(ValueError):
            inspect_backup(buffer.getvalue())


if __name__ == '__main__':
    unittest.main()
