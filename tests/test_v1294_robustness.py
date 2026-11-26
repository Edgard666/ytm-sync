"""v1.29.4: stalled JOAL sessions are stopped with their reason; the controller's stopped
announce uses the JOAL client identity (peer_id, key, query, headers, gzip)."""
import gzip
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import main  # noqa: E402
import client_identity  # noqa: E402
import tracker_scrape  # noqa: E402
from core import encode_bencode  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


class FakeJoal:
    """Live JVM stand-in: alive until terminated, with a telemetry folder."""
    def __init__(self, folder, code=None):
        self.code, self.engine = code, 'joal'
        self.telemetry = type('T', (), {'name': str(folder)})()

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class StalledSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.telemetry = self.data / 'telemetry'
        self.telemetry.mkdir()
        (self.data / 'runtime' / 'solo-big').mkdir(parents=True)
        self.patches = [patch.object(main, 'DATA', self.data), patch.object(main, 'RUNNERS', {}),
                        patch.object(main, 'ACTIVE_HASHES', {}), patch.object(main, 'LAST_START', {}),
                        patch.object(main, 'RUNNER_FAILURES', {}), patch.object(main, 'SWARM_MONITOR', None),
                        patch.object(main, 'save_final_volumes', lambda *a: None),
                        patch.object(main, 'safe_audit', lambda *a, **k: None),
                        patch.object(main, 'notify_incident', lambda *a: self.alerts.append(a))]
        self.alerts = []
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def start(self, seconds_ago, code=None):
        proc = FakeJoal(self.telemetry, code)
        main.RUNNERS['solo-big'] = proc
        main.ACTIVE_HASHES['solo-big'] = {'a' * 40}
        main.LAST_START['solo-big'] = time.monotonic() - seconds_ago
        return proc

    def log(self, text):
        (self.data / 'runtime' / 'solo-big' / 'joal.log').write_text(text)

    def test_session_without_measurements_is_stopped_with_its_reason(self):
        self.log('INFO starting\nCaused by: java.lang.OutOfMemoryError: Java heap space\n'
                 '1. https://tracker.example/announce/SECRETPASSKEY\n')
        proc = self.start(150)
        main.check_runner_health()
        self.assertNotIn('solo-big', main.RUNNERS)
        self.assertEqual(proc.code, -15)
        text = main.runner_failure_text('solo-big')
        self.assertEqual(text, 'JOAL arrêté : mémoire insuffisante (OutOfMemoryError)')
        self.assertNotIn('SECRETPASSKEY', json.dumps([text, self.alerts]))
        self.assertTrue(self.alerts and self.alerts[0][1] is True)

    def test_young_or_measuring_sessions_are_left_alone(self):
        self.start(30)
        main.check_runner_health()                           # still starting
        self.assertIn('solo-big', main.RUNNERS)
        self.start(600)
        (self.telemetry / 'upload.json').write_text('{}')    # fresh measurement
        main.check_runner_health()
        self.assertIn('solo-big', main.RUNNERS)

    def test_recovery_clears_the_reason(self):
        main.RUNNER_FAILURES['solo-big'] = {'time': time.time(), 'reason': 'x'}
        self.start(600)
        (self.telemetry / 'upload.json').write_text('{}')
        main.check_runner_health()
        self.assertIsNone(main.runner_failure_text('solo-big'))
        self.assertEqual(self.alerts[-1][1], False)

    def test_exit_by_itself_is_recorded_once(self):
        self.log('java.lang.OutOfMemoryError')
        self.start(10, code=3)                               # -XX:+ExitOnOutOfMemoryError
        main.check_runner_health()
        main.check_runner_health()
        self.assertEqual(main.runner_failure_text('solo-big'), 'JOAL arrêté : mémoire insuffisante (OutOfMemoryError)')
        self.assertEqual(len(self.alerts), 1)

    def test_unknown_log_gives_a_neutral_reason(self):
        self.start(150)
        main.check_runner_health()
        self.assertEqual(main.runner_failure_text('solo-big'), 'JOAL arrêté : aucune mesure reçue de JOAL')


class ClientIdentityTests(unittest.TestCase):
    def test_every_joal_profile_shape(self):
        for name in ('qbittorrent-5.1.4.client', 'transmission-3.00.client'):
            identity = client_identity.load(FIXTURES / name)
            self.assertEqual(len(identity.peer_id.encode('latin-1')), 20)
            query = identity.announce_query('ab' * 20, 123)
            self.assertNotIn('{', query)
            self.assertIn('event=stopped', query)
        self.assertTrue(client_identity.load(FIXTURES / 'qbittorrent-5.1.4.client').peer_id.startswith('-qB5140-'))

    def test_key_is_kept_per_torrent(self):
        identity = client_identity.ClientIdentity(json.loads((FIXTURES / 'qbittorrent-5.1.4.client').read_text()))
        self.assertEqual(identity.key('a' * 40), identity.key('a' * 40))
        self.assertRegex(identity.key('a' * 40), r'^[0-9A-F]{1,8}$')

    def test_unsupported_profile_is_refused(self):
        with self.assertRaises(client_identity.Unsupported):
            client_identity.ClientIdentity({'query': 'x={secret}'})

    def test_http_announce_looks_like_the_profile(self):
        seen = {}

        class Tracker(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen['path'], seen['headers'] = self.path, dict(self.headers)
                body = gzip.compress(encode_bencode({b'interval': 1800, b'complete': 12, b'incomplete': 9}))
                self.send_response(200)
                self.send_header('Content-Encoding', 'gzip')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Tracker)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        identity = client_identity.load(FIXTURES / 'qbittorrent-5.1.4.client')
        infohash = '4bfdca82ff1f9a1dd1326720efc840e86892c2ef'
        with patch.object(tracker_scrape, 'public_ip_only', lambda *a: None):
            result = tracker_scrape.import_announce_stats(
                f'http://127.0.0.1:{server.server_port}/announce/PASSKEY', infohash, 548587113, identity=identity)
        self.assertEqual((result['seeders'], result['leechers']), (12, 9))
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(seen['path']).query, encoding='latin-1'))
        self.assertEqual(query['info_hash'].encode('latin-1'), bytes.fromhex(infohash))
        self.assertEqual(query['peer_id'], identity.peer_id)
        self.assertEqual((query['event'], query['numwant'], query['left']), ('stopped', '0', '548587113'))
        self.assertEqual(seen['headers']['User-Agent'], 'qBittorrent/5.1.4')
        self.assertTrue(seen['path'].startswith('/announce/PASSKEY?info_hash='))

    def test_probe_uses_the_profile_of_the_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / 'clients').mkdir()
            (data / 'clients' / 'qbittorrent-5.1.4.client').write_bytes((FIXTURES / 'qbittorrent-5.1.4.client').read_bytes())
            config = {'rules': [{'enabled': True, 'client': 'qbittorrent-5.1.4.client'}]}
            with patch.object(main, 'DATA', data), patch.object(main, 'read_config', lambda: config):
                identity = main.probe_identity('a' * 40)
            self.assertTrue(identity.peer_id.startswith('-qB5140-'))
            with patch.object(main, 'DATA', data), patch.object(main, 'read_config', lambda: {'rules': []}):
                self.assertIsNone(main.probe_identity('a' * 40))


if __name__ == '__main__':
    unittest.main()
