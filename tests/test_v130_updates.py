"""v1.30: image built and published by GitHub, JOAL and .client profiles kept up to date."""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'ci'))
import main  # noqa: E402
import client_identity  # noqa: E402
import resolve_inputs  # noqa: E402


class BundledProfiles(unittest.TestCase):
    def test_every_bundled_profile_is_valid_and_reproducible(self):
        profiles = sorted((ROOT / 'clients').glob('*.client'))
        self.assertGreaterEqual(len(profiles), 90)
        self.assertIn('qbittorrent-5.3.0.client', {p.name for p in profiles})
        for path in profiles:
            identity = client_identity.load(path)              # same identity as the JOAL sessions
            self.assertEqual(len(identity.peer_id.encode('latin-1')), 20, path.name)
            self.assertNotIn('{', identity.announce_query('ab' * 20, 0), path.name)

    def test_image_ships_and_syncs_them(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        self.assertIn('COPY clients/ /opt/joal/clients/', dockerfile)
        for arg in ('JOAL_IMAGE', 'JDK_IMAGE', 'JRE_IMAGE'):
            self.assertRegex(dockerfile, rf'ARG {arg}=\S+@sha256:[0-9a-f]{{64}}')   # local builds stay pinned
        self.assertIn('!clients/*.client', (ROOT / '.dockerignore').read_text())
        self.assertIn(' clients ', (ROOT / 'unraid' / 'update-joal.sh').read_text())


class EntrypointSync(unittest.TestCase):
    def test_missing_profiles_added_existing_ones_never_replaced(self):
        script = (ROOT / 'docker-entrypoint.sh').read_text()
        function = re.search(r'^sync_clients\(\) \{.*?^\}', script, re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            shipped, data = Path(tmp, 'shipped'), Path(tmp, 'data')
            shipped.mkdir()
            (data / 'clients').mkdir(parents=True)
            (shipped / 'qbittorrent-9.9.9.client').write_text('{"new": 1}')
            (shipped / 'mine.client').write_text('{"upstream": 1}')
            (data / 'clients' / 'mine.client').write_text('{"edited": 1}')
            program = function.replace('/opt/joal/clients', str(shipped)) + '\nsync_clients\nsync_clients\n'
            result = subprocess.run(['sh', '-c', program], env={**os.environ, 'DATA_DIR': str(data), 'PUID': '99',
                                                                 'PGID': '100'}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count('ajoutés'), 1)             # second start: nothing to add
            self.assertEqual((data / 'clients' / 'mine.client').read_text(), '{"edited": 1}')
            self.assertTrue((data / 'clients' / 'qbittorrent-9.9.9.client').exists())


class JoalVersionShown(unittest.TestCase):
    def test_footer_and_health(self):
        class Page:
            def __init__(self):
                self.wfile = io.BytesIO()
            def send_response(self, *_): pass
            def send_header(self, *_): pass
            def security_headers(self): pass
            def end_headers(self): pass
        page = Page()
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DATA', Path(tmp)), \
                patch.object(main, 'JOAL_VERSION', '2.1.38'):
            main.Handler.serve_static(page, 'index.html', 'text/html')
            html = page.wfile.getvalue().decode()
            self.assertIn('· JOAL 2.1.38 ·', html)
            self.assertNotIn('__JOAL_VERSION__', html)


class ResolveInputs(unittest.TestCase):
    def test_latest_plain_version_only(self):
        self.assertEqual(resolve_inputs.latest_version(['latest', '2.1.9', '2.1.37', '2.1.38-rc1', '2.0.99']), '2.1.37')
        self.assertEqual(resolve_inputs.latest_version(['2.1.37', '2.1.38', '2.2.0']), '2.2.0')
        with self.assertRaises(SystemExit):
            resolve_inputs.latest_version(['latest'])

    def test_digest_and_fingerprint(self):
        digest = 'sha256:' + 'c' * 64
        self.assertEqual(resolve_inputs.parse_digest(f'Name: x\nDigest:    {digest}\n\nManifests:\n'), digest)
        base = resolve_inputs.fingerprint('tree', 'joal@1', 'jdk@1', 'jre@1', 'profiles')
        self.assertEqual(base, resolve_inputs.fingerprint('tree', 'joal@1', 'jdk@1', 'jre@1', 'profiles'))
        self.assertNotEqual(base, resolve_inputs.fingerprint('tree', 'joal@2', 'jdk@1', 'jre@1', 'profiles'))
        self.assertNotEqual(base, resolve_inputs.fingerprint('tree', 'joal@1', 'jdk@1', 'jre@1', 'other'))

    def test_workflow_publishes_only_after_tests(self):
        workflow = (ROOT / '.github' / 'workflows' / 'image.yml').read_text()
        order = [workflow.index(step) for step in ('python3 -m unittest', 'docker build', 'ci/smoke_image.py', 'docker push')]
        self.assertEqual(order, sorted(order))
        self.assertIn("cron: '17 4 * * 1'", workflow)



class GitIgnore(unittest.TestCase):
    def test_data_and_secrets_never_committed(self):
        lines = (ROOT / '.gitignore').read_text().split()
        for entry in ('data/', '.env'):
            self.assertIn(entry, lines)


if __name__ == '__main__':
    unittest.main()
