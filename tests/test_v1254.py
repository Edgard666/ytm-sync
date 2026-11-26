import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'app'))  # standalone runs
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import import_watch
import main


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.h = 'a' * 40

    def test_smb_copy_resumes_after_partial_read(self):
        import_watch.ensure(self.root)
        path = self.root / 'film.torrent'
        seen = {}
        calls = []

        def importer(data, name):
            calls.append(data)
            return ('added', 'ok') if data == b'complete' else ('error', 'incomplet')

        def scan():
            return import_watch.scan(self.root, importer, min_age=0, observations=seen)

        path.write_bytes(b'comp')
        self.assertEqual(scan(), [])
        self.assertEqual(scan(), [])
        self.assertTrue(path.exists())
        path.write_bytes(b'complete')
        self.assertEqual(scan(), [])
        self.assertEqual(scan()[0]['status'], 'added')
        self.assertEqual(calls, [b'comp', b'complete'])
        self.assertEqual(seen, {})

    def test_stable_invalid_file_quarantined_after_retry(self):
        import_watch.ensure(self.root)
        (self.root / 'bad.torrent').write_bytes(b'bad')
        seen = {}
        def scan():
            return import_watch.scan(self.root, lambda *_: ('error', 'invalide'), min_age=0, observations=seen)
        self.assertEqual(scan(), [])
        self.assertEqual(scan(), [])
        self.assertEqual(scan()[0]['status'], 'error')
        self.assertTrue((self.root / 'erreurs/bad.torrent').exists())

    def test_two_tracker_results_survive_a_later_refusal(self):
        rows = {'one.example': [1, 100, ''], 'two.example': [0, 101, 'status 403']}
        (self.root / 'announce.json').write_text(json.dumps({self.h: rows}))
        from types import SimpleNamespace
        data = main.read_announce_outcomes(SimpleNamespace(telemetry=SimpleNamespace(name=self.root)))
        row = {'infoHash': self.h, 'reason': 'Session active'}
        main.announce_issue_rows({'torrents': [row], 'joalUpload': {'announcements': data}})
        self.assertEqual(data[self.h]['two.example'], {'time': 101, 'result': 'Réponse HTTP 403'})
        self.assertEqual(data[self.h]['one.example']['result'], 'Acceptée')
        self.assertEqual(row['reason'], 'Session active')

    def test_forced_uses_real_rates_unless_explicitly_assigned_to_zero_rule(self):
        other = 'b' * 40
        config = {'rules': [dict(id='normal', enabled=True, minUploadRate=1, maxUploadRate=5, client='x'),
                            dict(id='idle', enabled=True, minUploadRate=0, maxUploadRate=0)], 'maxCandidates': 5,
                  'torrentRuleOverrides': {other: 'idle'}}
        status = {'solo': {'items': [{'infoHash': self.h, 'rule': 'idle'}]}, 'torrents': []}
        with patch.multiple(main, DATA=self.root, STATUS=status, TORRENT_MODES={}):
            main.set_torrent_modes({self.h, other}, 'forced')
            stored = json.loads((self.root / 'simulation-modes.json').read_text())
            self.assertEqual(stored, {self.h: 'forced', other: 'forced'})
            # Matched by tracker only: Forced gives it a real upload rate.
            self.assertEqual(main.simulation_config(config)['torrentRuleOverrides'],
                             {other: main.FORCED_IDLE, self.h: main.FORCED_RULE})
            rules = {r['id']: r for r in main.simulation_config(config)['rules']}
            self.assertEqual(rules[main.FORCED_RULE]['maxUploadRate'], 5)

    def test_log_failures_never_leave_an_unmanaged_child(self):
        rule = {'id': 'idle', 'minUploadRate': 0, 'maxUploadRate': 0}
        real_popen = subprocess.Popen
        children = []
        def spawn(command, **options):
            child = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **options)
            children.append(child)
            self.addCleanup(lambda: (child.kill(), child.wait()) if child.poll() is None else None)
            return child
        with patch.multiple(main, DATA=self.root, RUNNERS={}, ACTIVE_HASHES={}, DIAGNOSTICS=None), \
                patch.object(main, 'presence_profile', return_value=None), \
                patch.object(subprocess, 'Popen', side_effect=spawn):
            with patch.object(main.log_rotation, 'RotatingLog', side_effect=OSError('disque plein')), \
                    self.assertRaises(OSError):
                main.runner_start(rule, {self.h: b'x'})
            self.assertEqual(children, [])
            with patch.object(main.log_rotation, 'start_pump', side_effect=RuntimeError('journal')), \
                    self.assertRaises(RuntimeError):
                main.runner_start(rule, {self.h: b'x'})
            self.assertIsNotNone(children[0].poll())
            self.assertEqual(main.RUNNERS, {})


class ForcedDiagnosticsTests(unittest.TestCase):
    def test_forced_runner_sessions_are_recorded(self):
        import diagnostics
        with tempfile.TemporaryDirectory() as tmp:
            store = diagnostics.DiagnosticsStore(Path(tmp) / 'diagnostics.json')
            store.start_rule('solo-:forced', ['c' * 40])
            store.stop_rule('solo-:forced')
            sessions = store.snapshot()['sessions']
            self.assertEqual([(s['rule'], s['status']) for s in sessions], [('solo-:forced', 'stopped')])


class UploadHintTests(unittest.TestCase):
    def test_hint_explains_joal_zero_upload(self):
        h = 'd' * 40
        cases = [
            ({'seeders': 0, 'leechers': 16}, False, 'aucun autre seeder'),
            ({'seeders': 3, 'leechers': 0}, False, 'aucun leecher'),
            ({'seeders': 3, 'leechers': 16}, False, None),   # bandwidth not dispatched yet
            ({'seeders': 0, 'leechers': 16}, True, None),    # deliberate 0/0 rule
            ({}, False, None),                               # no announce yet
        ]
        for peers, zero, expected in cases:
            row = {'infoHash': h}
            snap = {'solo': {'items': [row]}, 'joalUpload': {'byInfoHash': {h: 0}, 'peers': {h: peers},
                                                          'zeroRate': [h] if zero else []}}
            main.upload_hint_rows(snap)
            hint = row.get('uploadHint')
            if expected is None:
                self.assertIsNone(hint, (peers, zero))
            else:
                self.assertIn(expected, hint)
        row = {'infoHash': h}
        main.upload_hint_rows({'torrents': [row], 'joalUpload': {'byInfoHash': {h: 2048},
                                                                 'peers': {h: {'seeders': 0, 'leechers': 1}}}})
        self.assertNotIn('uploadHint', row)
