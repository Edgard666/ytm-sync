"""Regression tests for the v1.23.0 robustness and security fixes."""
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main  # noqa: E402
import qbit_settings  # noqa: E402
from core import ConfigError, rules_missing_client, validate  # noqa: E402
from qbit import QBitClient  # noqa: E402
from web_auth import SessionManager  # noqa: E402
from diagnostics import DiagnosticsStore  # noqa: E402
from webtest_helpers import login, auth_headers  # noqa: E402

BASE = json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text())


def policy():
    return json.loads(json.dumps(BASE))


class ValidationTests(unittest.TestCase):
    def test_non_object_sections_raise_config_error(self):
        for section in ('swarm', 'cleanup', 'handoff'):
            p = policy()
            p['handoff'] = {'enabled': True}
            p[section] = []
            with self.assertRaises(ConfigError):
                validate(p)

    def test_rules_missing_client(self):
        p = policy()
        p['rules'][0].update(enabled=True, client=None)
        self.assertEqual(rules_missing_client(validate(p)), ['tracker-exemple'])
        p['rules'][0].update(minUploadRate=0, maxUploadRate=0)  # presence only: no client needed
        self.assertEqual(rules_missing_client(validate(p)), [])

    def test_existing_policy_without_client_still_loads(self):
        p = policy()
        p['rules'][0].update(enabled=True, client=None)
        validate(p)  # must not raise: upgrade compatibility


class QbitSettingsTests(unittest.TestCase):
    def test_corrupt_connection_file_falls_back_to_docker(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.dict('os.environ', {'QBIT_URL': 'http://q:8080'}):
            (Path(root) / 'qbit-connection.json').write_text('{broken')
            self.assertEqual(qbit_settings.effective(root)['url'], 'http://q:8080')
            (Path(root) / 'qbit-connection.json').write_text('["list"]')
            self.assertEqual(qbit_settings.effective(root)['url'], 'http://q:8080')


class QbitLegacyApiTests(unittest.TestCase):
    def test_stop_falls_back_to_pause_on_qbittorrent_4(self):
        client = QBitClient('http://q:8080', '', '')
        calls = []

        def call(path, params=None, method='GET'):
            calls.append(path)
            if path in ('torrents/stop', 'torrents/start'):
                raise urllib.error.HTTPError(path, 404, 'Not Found', {}, io.BytesIO())
            return b'Ok.'
        client.call = call
        client.stop_torrent('a' * 40)
        client.start_torrent('a' * 40)
        self.assertEqual(calls, ['torrents/stop', 'torrents/pause', 'torrents/resume'])

    def test_other_http_errors_are_not_masked(self):
        client = QBitClient('http://q:8080', '', '')
        client.call = Mock(side_effect=urllib.error.HTTPError('x', 500, 'err', {}, io.BytesIO()))
        with self.assertRaises(urllib.error.HTTPError):
            client.stop_torrent('a' * 40)


class ThreadRobustnessTests(unittest.TestCase):
    def test_supervisor_restarts_crashed_loop_and_runs_safety_action(self):
        stop = threading.Event()
        runs, crashes = [], []

        def loop():
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError('boom')
            stop.set()
        with patch.object(main, 'STOP', stop), patch.object(main.LOG, 'exception'):
            thread = main.supervised(loop, 't', on_crash=lambda: crashes.append(1))
            with patch.object(stop, 'wait', side_effect=lambda _t: stop.is_set()):
                thread.run()
        self.assertEqual(len(runs), 2)
        self.assertEqual(crashes, [1])

    def test_live_upload_ignores_malformed_telemetry(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'upload.json').write_text('[1, 2, 3]')
            proc = Mock()
            proc.poll.return_value = None
            proc.telemetry.name = folder
            with patch.object(main, 'RUNNERS', {'r': proc}), \
                    patch.object(main, 'ACTIVE_HASHES', {'r': {'a' * 40}}), \
                    patch.object(main, 'DATA', Path(folder)):
                result = main.live_joal_upload()
            self.assertIsNone(result['bytesPerSecond'])
            (Path(folder) / 'upload.json').write_text(json.dumps({'a' * 40: 'bad', 'b' * 40: [5, 1]}))
            with patch.object(main, 'RUNNERS', {'r': proc}), \
                    patch.object(main, 'ACTIVE_HASHES', {'r': {'a' * 40}}), \
                    patch.object(main, 'DATA', Path(folder)), \
                    patch.object(main, 'SWARM_MONITOR', None):
                result = main.live_joal_upload()
            self.assertEqual(result['bytesPerSecond'], 5)
            self.assertIsNone(result['byInfoHash']['a' * 40])

    def test_wait_torrent_state_retries_until_qbittorrent_updates(self):
        states = iter(['stoppedUP', 'stoppedUP', 'uploading'])
        with patch.object(main, 'watched_torrents',
                          side_effect=lambda *_: [{'hash': 'a' * 40, 'state': next(states)}]), \
                patch.object(main, 'STOP', threading.Event()) as stop:
            with patch.object(stop, 'wait', return_value=False):
                torrent = main.wait_torrent_state(None, {}, 'a' * 40,
                                                  lambda t: t['state'] == 'uploading')
        self.assertEqual(torrent['state'], 'uploading')


class LiveConnectionTests(unittest.TestCase):
    def test_saved_connection_applies_without_restart(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(main, 'DATA', Path(root)), \
                patch.object(main, 'QBIT_THREADS_STARTED', True), \
                patch.object(main, 'QBIT_URL', ''), patch.object(main, 'QBIT_USER', ''), \
                patch.object(main, 'QBIT_PASS', ''), patch.object(main, 'QBIT_GENERATION', 0), \
                patch.dict('os.environ', {'QBIT_URL': '', 'QBIT_USERNAME': '', 'QBIT_PASSWORD': ''}):
            qbit_settings.save(root, {'url': 'http://first:8080', 'username': 'u', 'password': 'p'})
            main.apply_qbit_connection()
            first = main.qbit_client()
            self.assertIs(main.qbit_client(first), first)  # cached while unchanged
            qbit_settings.save(root, {'url': 'http://second:8080', 'username': 'u', 'password': 'p'})
            main.apply_qbit_connection()
            second = main.qbit_client(first)
            self.assertIsNot(second, first)
            self.assertEqual(second.url, 'http://second:8080')


class RestoreTests(unittest.TestCase):
    def test_invalid_pending_restore_is_set_aside_not_fatal(self):
        with tempfile.TemporaryDirectory() as root, patch.object(main, 'DATA', Path(root)), \
                patch.object(main.LOG, 'error'):
            (Path(root) / main.PENDING_NAME).write_bytes(b'not a zip')
            reason = main.apply_restore_safely()
            self.assertTrue(reason)
            self.assertFalse((Path(root) / main.PENDING_NAME).exists())
            self.assertEqual(len(list(Path(root).glob('.restore-rejected-*.zip'))), 1)

    def test_no_pending_restore(self):
        with tempfile.TemporaryDirectory() as root, patch.object(main, 'DATA', Path(root)):
            self.assertIsNone(main.apply_restore_safely())


class HttpTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / 'policy.json').write_text(json.dumps(policy()))
        for ctx in (patch.object(main, 'PASSWORD', 'test-password-123'),
                    patch.object(main, 'AUTH', SessionManager(lambda: main.PASSWORD)),
                    patch.object(main, 'DIAGNOSTICS', DiagnosticsStore(self.root / 'diagnostics.json')),
                    patch.object(main, 'DATA', self.root),
                    patch.object(main, 'CONFIG', self.root / 'policy.json')):
            ctx.start()
            self.addCleanup(ctx.stop)
        self.server = main.ThreadingHTTPServer(('127.0.0.1', 0), main.Handler)
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def post(self, path, body, headers):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), method='POST',
                                     headers={**headers, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_healthz_is_public_and_minimal(self):
        with urllib.request.urlopen(self.base + '/healthz', timeout=5) as res:
            self.assertEqual(json.load(res), {'status': 'ok', 'version': main.APP_VERSION})

    def test_malformed_config_gets_json_400(self):
        headers = auth_headers(*login(self.base, 'test-password-123'))
        p = policy()
        p['handoff'] = {'enabled': True}
        p['swarm'] = []
        status, body = self.post('/api/config', p, headers)
        self.assertEqual(status, 400)
        self.assertIn('error', body)

    def test_enabled_rule_without_client_rejected_at_save(self):
        headers = auth_headers(*login(self.base, 'test-password-123'))
        p = policy()
        p['rules'][0].update(enabled=True, client=None)
        status, body = self.post('/api/config', p, headers)
        self.assertEqual(status, 400)
        self.assertIn('profil client', body['error'])

    def test_login_rate_limit_uses_forwarded_ip_only_from_trusted_proxy(self):
        seen = []
        real_create = main.AUTH.create

        def create(user, password, remote):
            seen.append(remote)
            return real_create(user, password, remote)
        for trusted in (frozenset(), frozenset({'127.0.0.1'})):
            with patch.object(main, 'TRUSTED_PROXIES', trusted), \
                    patch.object(main.AUTH, 'create', side_effect=create):
                self.post('/api/auth/login', {'username': 'admin', 'password': 'bad'},
                          {'X-Forwarded-For': '1.2.3.4, 203.0.113.9'})
        self.assertEqual(seen, ['127.0.0.1', '203.0.113.9'])


if __name__ == '__main__':
    unittest.main()


class PresenceEngineTests(unittest.TestCase):
    """Zero-rate rules must use JOAL's client emulation when a profile exists."""

    def start(self, rule, rules, clients):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'clients').mkdir()
            for name in clients:
                (root / 'clients' / name).write_text('{}')
            p = policy()
            p['rules'] = rules
            (root / 'policy.json').write_text(json.dumps(p))
            popen = Mock(return_value=Mock())
            with patch.object(main, 'DATA', root), patch.object(main, 'CONFIG', root / 'policy.json'), \
                    patch.object(main, 'RUNNERS', {}), patch.object(main, 'ACTIVE_HASHES', {}), \
                    patch.object(main, 'LAST_START', {}), patch.object(main, 'TORRENT_MODES', {}), \
                    patch.object(main, 'DIAGNOSTICS', None), patch.object(main.subprocess, 'Popen', popen):
                main.runner_start(rule, {'a' * 40: b'd4:infod4:name1:xee'})
                conf_path = root / 'runtime' / rule['id'] / 'config.json'
                conf = json.loads(conf_path.read_text()) if conf_path.exists() else None
            return popen.call_args[0][0], conf

    def rule(self, **extra):
        return dict({'id': 'idle', 'enabled': True, 'trackerDomains': ['t.example'], 'minLeechers': 0,
                     'minUploadRate': 0, 'maxUploadRate': 0, 'simultaneousSeed': 5, 'client': None}, **extra)

    def test_zero_rate_uses_joal_with_other_rule_profile(self):
        other = dict(self.rule(id='upload', minUploadRate=10, maxUploadRate=20, client='qbittorrent-5.0.0.client'))
        command, conf = self.start(self.rule(), [self.rule(), other], ['qbittorrent-5.0.0.client'])
        self.assertEqual(command[0], 'java')
        self.assertEqual((conf['minUploadRate'], conf['maxUploadRate']), (0, 0))
        self.assertEqual(conf['client'], 'qbittorrent-5.0.0.client')

    def test_zero_rate_prefers_its_own_profile(self):
        rule = self.rule(client='deluge-2.1.1.client')
        command, conf = self.start(rule, [rule], ['deluge-2.1.1.client', 'qbittorrent-5.0.0.client'])
        self.assertEqual(conf['client'], 'deluge-2.1.1.client')

    def test_zero_rate_without_any_profile_falls_back_to_script(self):
        with patch.object(main.LOG, 'warning') as warning:
            command, conf = self.start(self.rule(), [self.rule()], [])
        self.assertTrue(command[1].endswith('seed_presence.py'))
        self.assertIsNone(conf)
        warning.assert_called()
