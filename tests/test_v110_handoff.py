"""v1.10 qBittorrent-first fail-closed handoff and persistent cooldown tests."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from core import validate, ConfigError
from handoff import HandoffLedger, fresh_observation, complete_running
import main

H = '1' * 40
RULE = {'id': 'alpha', 'enabled': True, 'trackerDomains': ['alpha.test'],
        'minLeechers': 4, 'minUploadRate': 30, 'maxUploadRate': 160,
        'simultaneousSeed': 10, 'client': 'normal.client'}


def policy():
    return validate({'dryRun': False, 'qbitTag': 'joal', 'refreshSeconds': 180,
                     'guardSeconds': 5, 'maxCandidates': 10,
                     'handoff': {'enabled': True, 'cooldownMinutes': 60, 'confirmations': 2},
                     'rules': [copy.deepcopy(RULE)]})


class FakeClient:
    def __init__(self, state='uploading'):
        self.row = {'hash': H, 'name': 'Torrent.AV1', 'progress': 1.0,
                    'amount_left': 0, 'state': state, 'ratio': 0.4}
        self.started = 0
        self.stopped = 0

    def torrents(self, tag):
        return [dict(self.row)]

    def trackers(self, h):
        return [{'url': 'https://tracker.alpha.test/announce', 'num_leeches': 10, 'status': 2}]

    def stop_torrent(self, h):
        self.stopped += 1
        self.row['state'] = 'stoppedUP'

    def start_torrent(self, h):
        self.started += 1
        self.row['state'] = 'uploading'


class Monitor:
    def __init__(self, count=5, stamp='2026-11-17T08:00:00+00:00', fresh=True):
        self.count, self.stamp, self.fresh = count, stamp, fresh

    def snapshot(self, age):
        return {'items': [{'infoHash': H, 'rules': {'alpha': {
            'leechers': self.count if self.fresh else None,
            'sampledAt': self.stamp if self.fresh else None,
            'status': 'récent' if self.fresh else 'périmé'}}}]}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [1794910000.0]
        self.ledger = HandoffLedger(Path(self.temp.name) / 'handoff-state.json', clock=lambda: self.now[0])
        self.ledger.recovery_pending = False
        self.cfg = policy()
        self.monitor = Monitor()
        patches = [patch.object(main, 'HANDOFF_LEDGER', self.ledger),
                   patch.object(main, 'SWARM_MONITOR', self.monitor),
                   patch.object(main, 'RUNNERS', {}),
                   patch.object(main, 'ACTIVE_HASHES', {}),
                   patch.object(main, 'LAST_START', {})]
        for handle in patches:
            handle.start()
            self.addCleanup(handle.stop)

    def test_default_disabled_and_minimum_60_minutes(self):
        cfg = policy(); cfg.pop('handoff')
        self.assertFalse(validate(cfg)['handoff']['enabled'])
        cfg['handoff']['enabled'] = True
        cfg['handoff']['cooldownMinutes'] = 59
        with self.assertRaises(ConfigError): validate(cfg)

    def test_only_distinct_scrape_samples_count(self):
        obs = (6, '2026-11-17T08:00:00+00:00')
        self.assertEqual(self.ledger.observe(H, 'alpha', obs, 4), 1)
        self.assertEqual(self.ledger.observe(H, 'alpha', obs, 4), 1)
        self.assertEqual(self.ledger.observe(H, 'alpha', (6, '2026-11-17T08:15:00+00:00'), 4), 2)
        self.assertEqual(self.ledger.observe(H, 'alpha', (2, '2026-11-17T08:30:00+00:00'), 4), 1)

    def test_fresh_observation_never_turns_unknown_into_zero(self):
        self.assertIsNone(fresh_observation({H: {'alpha': {'leechers': None, 'status': 'indisponible'}}}, H, 'alpha'))
        self.assertIsNone(fresh_observation({H: {'alpha': {'leechers': 0, 'status': 'périmé'}}}, H, 'alpha'))
        self.assertEqual(fresh_observation({H: {'alpha': {'leechers': 0, 'status': 'récent', 'sampledAt': '2026-11-17T08:00:00+00:00'}}}, H, 'alpha')[0], 0)

    def test_does_not_stop_qbit_on_first_measurement(self):
        client = FakeClient()
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 0)
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 0)
        self.assertEqual(self.ledger.entry(H).get('confirmations'), 1)

    def test_stops_only_after_second_fresh_measurement(self):
        client = FakeClient()
        main._reconcile_handoff(client, self.cfg)
        self.monitor.stamp = '2026-11-17T08:15:00+00:00'
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 1)
        self.assertTrue(self.ledger.entry(H)['owned'])

    def test_stale_scrape_never_auto_stops(self):
        self.monitor.fresh = False
        client = FakeClient()
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 0)

    def test_manual_stop_is_never_auto_resumed(self):
        client = FakeClient('stoppedUP')
        self.monitor.fresh = False
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.started, 0)

    def test_return_cooldown_persists_and_blocks_immediate_restop(self):
        client = FakeClient()
        self.ledger.mark_stopped_by_controller(H, 'alpha')
        client.row['state'] = 'stoppedUP'
        self.monitor.fresh = False
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.started, 1)
        self.assertEqual(self.ledger.cooldown_remaining(H), 3600)
        another = HandoffLedger(self.ledger.path, clock=lambda: self.now[0])
        self.assertEqual(another.cooldown_remaining(H), 3600)
        self.monitor.fresh = True
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 0)
        self.now[0] += 3601
        self.assertEqual(self.ledger.cooldown_remaining(H), 0)

    def test_recovery_always_returns_to_qbit(self):
        client = FakeClient('stoppedUP')
        self.ledger.mark_stopped_by_controller(H, 'alpha')
        self.ledger.recovery_pending = True
        main._reconcile_handoff(client, self.cfg)
        self.assertFalse(self.ledger.entry(H)['owned'])
        self.assertEqual(client.started, 1)

    def test_manually_resumed_qbit_blocks_joal_and_cooldown(self):
        client = FakeClient('uploading')
        self.ledger.mark_stopped_by_controller(H, 'alpha')
        main._reconcile_handoff(client, self.cfg)
        self.assertFalse(self.ledger.entry(H)['owned'])
        self.assertEqual(client.started, 0)
        self.assertGreater(self.ledger.cooldown_remaining(H), 3500)

    def test_no_handoff_if_tracker_domain_does_not_match(self):
        client = FakeClient()
        client.trackers = lambda _: [{'url': 'https://not-alpha.test/announce', 'status': 2, 'num_leeches': 99}]
        self.ledger.observe(H, 'alpha', (7, '2026-11-17T08:00:00+00:00'), 4)
        self.monitor.stamp = '2026-11-17T08:15:00+00:00'
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 0)

    def test_failure_after_stop_request_restores_qbit(self):
        class LostReply(FakeClient):
            def stop_torrent(self, h):
                super().stop_torrent(h)
                raise TimeoutError('HTTP reply lost after qBittorrent stopped')
        client = LostReply()
        self.ledger.observe(H, 'alpha', (7, '2026-11-17T08:00:00+00:00'), 4)
        self.monitor.stamp = '2026-11-17T08:15:00+00:00'
        with self.assertRaises(TimeoutError):
            main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.started, 1)
        self.assertFalse(self.ledger.entry(H).get('owned'))
        self.assertEqual(self.ledger.cooldown_remaining(H), 3600)

    def test_two_low_samples_restore_real_seeding(self):
        client = FakeClient('stoppedUP')
        self.ledger.mark_stopped_by_controller(H, 'alpha')
        self.monitor.count = 3
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.started, 0)
        self.monitor.stamp = '2026-11-17T08:15:00+00:00'
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.started, 1)
        self.assertGreaterEqual(self.ledger.cooldown_remaining(H), 3599)

    def test_dry_run_does_not_stop_torrent(self):
        client = FakeClient()
        self.cfg['dryRun'] = True
        self.ledger.observe(H, 'alpha', (5, '2026-11-17T08:00:00+00:00'), 4)
        self.monitor.stamp = '2026-11-17T08:15:00+00:00'
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 0)

if __name__ == '__main__': unittest.main()
