"""v1.10 safety/security regression tests (no live tracker or qBittorrent)."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main
from cleanup import Ledger
from handoff import HandoffLedger
from core import validate

HASH = 'c' * 40
RULE = {'id':'alpha', 'enabled':True, 'trackerDomains':['alpha.test'],
        'minLeechers':4, 'minUploadRate':30, 'maxUploadRate':160,
        'simultaneousSeed':10, 'client':'normal.client'}


def config():
    return validate({'dryRun':False, 'qbitTag':'joal', 'refreshSeconds':180,
                     'guardSeconds':5, 'maxCandidates':10, 'rules':[copy.deepcopy(RULE)],
                     'handoff':{'enabled':True,'cooldownMinutes':60,'confirmations':2}})

class Client:
    def torrents(self, tag):
        return [{'hash':HASH, 'name':'Test', 'progress':1.0, 'amount_left':0,
                 'state':'uploading', 'ratio':0.2}]

class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.ledger = HandoffLedger(self.path / 'handoff-state.json', clock=lambda: 1000.)
        self.ledger.recovery_pending = False
        self.ledger.mark_stopped_by_controller(HASH, 'alpha')

    def test_restarted_qbit_is_not_marked_returned_if_joal_stop_fails(self):
        class Process:
            def poll(self): return None
        with patch.object(main, 'HANDOFF_LEDGER', self.ledger), \
             patch.object(main, 'RUNNERS', {'alpha':Process()}), \
             patch.object(main, 'ACTIVE_HASHES', {'alpha':{HASH}}), \
             patch.object(main, 'runner_stop', side_effect=RuntimeError('cannot terminate')):
            with self.assertRaisesRegex(RuntimeError, 'cannot terminate'):
                main._reconcile_handoff(Client(), config())
            self.assertTrue(self.ledger.entry(HASH)['owned'])
            self.assertEqual(self.ledger.cooldown_remaining(HASH), 0)

    def test_successful_stop_happens_before_releasing_ownership(self):
        events = []
        class Process:
            def poll(self): return None
        def stop(rid):
            events.append(('stop', self.ledger.entry(HASH)['owned']))
        with patch.object(main, 'HANDOFF_LEDGER', self.ledger), \
             patch.object(main, 'RUNNERS', {'alpha':Process()}), \
             patch.object(main, 'ACTIVE_HASHES', {'alpha':{HASH}}), \
             patch.object(main, 'runner_stop', side_effect=stop):
            main._reconcile_handoff(Client(), config())
        self.assertEqual(events, [('stop', True)])
        self.assertFalse(self.ledger.entry(HASH)['owned'])
        self.assertEqual(self.ledger.cooldown_remaining(HASH), 3600)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions test')
    def test_config_write_owner_only(self):
        conf_path = self.path / 'policy.json'
        with patch.object(main, 'CONFIG', conf_path):
            main.write_config(config())
        self.assertEqual(conf_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(conf_path.read_text())['handoff']['cooldownMinutes'], 60)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions test')
    def test_cleanup_history_owner_only(self):
        path = self.path / 'cleanup-observations.json'
        ledger = Ledger(path)
        ledger.observe(HASH, True, 100, 3600, source='scrape')
        ledger.save()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupted_persisted_state_fails_closed(self):
        path = self.path / 'damaged-handoff.json'
        path.write_text('{incomplete', encoding='utf-8')
        ledger = HandoffLedger(path, clock=lambda: 1000.)
        self.assertTrue(ledger.corrupt)
        with patch.object(main, 'HANDOFF_LEDGER', ledger):
            with self.assertRaisesRegex(RuntimeError, 'illisible'):
                main._reconcile_handoff(Client(), config())

    def test_fast_guard_stops_joal_before_releasing_ownership(self):
        class OneCycle:
            calls = 0
            def is_set(self): return self.calls > 0
            def wait(self, seconds):
                self.calls += 1
                return True
        class Process:
            def poll(self): return None
        proc = Process()
        running = {'alpha': proc}
        assignments = {'alpha': {HASH}}
        stopped = []
        def stop(rule):
            stopped.append((rule, self.ledger.entry(HASH).get('owned')))
            running.pop(rule, None)
            assignments.pop(rule, None)
        with patch.object(main, 'STOP', OneCycle()), \
             patch.object(main, 'QBitClient', lambda *_: Client()), \
             patch.object(main, 'read_config', config), \
             patch.object(main, 'HANDOFF_LEDGER', self.ledger), \
             patch.object(main, 'SWARM_MONITOR', None), \
             patch.object(main, 'CLEANUP_LEDGER', None), \
             patch.object(main, 'RUNNERS', running), \
             patch.object(main, 'ACTIVE_HASHES', assignments), \
             patch.object(main, 'runner_stop', side_effect=stop), \
             patch.object(main, 'update_inventory_snapshot', return_value=False):
            main.guard_loop()
        self.assertEqual(stopped, [('alpha', True)])
        self.assertFalse(self.ledger.entry(HASH).get('owned'))
        self.assertEqual(self.ledger.cooldown_remaining(HASH), 3600)

    def test_manual_stop_does_not_create_automatic_ownership(self):
        self.ledger.entries.clear()
        with patch.object(main, 'HANDOFF_LEDGER', self.ledger), \
             patch.object(main, 'SWARM_MONITOR', None):
            class ManuallyStopped(Client):
                def torrents(self, tag):
                    items = super().torrents(tag)
                    items[0]['state'] = 'stoppedUP'
                    return items
            main._reconcile_handoff(ManuallyStopped(), config())
        self.assertEqual(self.ledger.owned_hashes(), set())

if __name__ == '__main__':
    unittest.main()
