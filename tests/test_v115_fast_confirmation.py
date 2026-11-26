"""v1.11.5: confirmed tracker sample threshold+5 shortcut and safeguards."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from core import validate, ConfigError
from handoff import entry_confirmations_required, fresh_observation

import test_v110_handoff as previous
H = previous.H
RULE = previous.RULE
FakeClient = previous.FakeClient
import main


class ConfigAndThresholdTests(unittest.TestCase):
    def test_margin_one_measure_vs_multiple(self):
        self.assertEqual(entry_confirmations_required(9, 4), 1)
        self.assertEqual(entry_confirmations_required(8, 4), 2)
        self.assertEqual(entry_confirmations_required(4, 4, 4), 4)
        self.assertEqual(entry_confirmations_required(12, 8, 3, 5), 3)
        self.assertEqual(entry_confirmations_required(13, 8, 3, 5), 1)
        self.assertEqual(entry_confirmations_required(-1, 4), 2)
        self.assertEqual(entry_confirmations_required(100, 4, margin=0), 2)

    def test_config_compat_and_validations(self):
        original = {'dryRun': True, 'qbitTag': 'Joal', 'refreshSeconds': 180,
                    'maxCandidates': 10, 'rules': [copy.deepcopy(RULE)]}
        conf = validate(original)
        self.assertEqual(conf['handoff']['fastTrackMargin'], 5)
        for wrong in (-1, 0, 1001, True, '5', 5.0):
            with self.subTest(value=wrong):
                bad = copy.deepcopy(conf)
                bad['handoff']['fastTrackMargin'] = wrong
                with self.assertRaises(ConfigError):
                    validate(bad)

    def test_partial_requires_actual_success(self):
        raw = {H: {'alpha': {'leechers': 30, 'status': 'partiel',
                             'sampledAt': '2026-11-17T08:00:00+00:00',
                             'successCount': 1, 'trackerCount': 5}}}
        self.assertEqual(fresh_observation(raw, H, 'alpha')[0], 30)
        raw[H]['alpha']['successCount'] = 0
        self.assertIsNone(fresh_observation(raw, H, 'alpha'))
        raw[H]['alpha']['status'] = 'périmé'
        raw[H]['alpha']['successCount'] = 1
        self.assertIsNone(fresh_observation(raw, H, 'alpha'))


class ShortcutIntegrationTests(unittest.TestCase):
    """Reuse the tested qBittorrent-first harness without duplicating tests."""
    def setUp(self):
        previous.HandoffTests.setUp(self)

    def test_single_fresh_sample_above_margin_stops(self):
        client = FakeClient()
        self.monitor.count = 9
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 1)
        self.assertTrue(self.ledger.entry(H).get('owned'))

    def test_below_margin_requires_distinct_second_sample(self):
        client = FakeClient()
        self.monitor.count = 8
        main._reconcile_handoff(client, self.cfg)
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 0)
        self.monitor.stamp = '2026-11-17T08:15:00+00:00'
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 1)

    def test_per_hash_override_changes_fast_boundary(self):
        self.cfg['rules'][0]['minLeechersByHash'] = {H: 10}
        client = FakeClient()
        self.monitor.count = 14
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 0)
        self.monitor.count = 15
        self.monitor.stamp = '2026-11-17T08:15:00+00:00'
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 1)

    def test_partial_valid_sample_one_tracker_sufficient(self):
        original = self.monitor.snapshot
        def partial(age):
            snap = original(age)
            snap['items'][0]['rules']['alpha'].update(status='partiel',
                                        successCount=1, trackerCount=5)
            return snap
        self.monitor.snapshot = partial
        self.monitor.count = 29
        client = FakeClient()
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 1)

    def test_invalid_partial_without_success_no_stop(self):
        original = self.monitor.snapshot
        def invalid(age):
            snap = original(age)
            snap['items'][0]['rules']['alpha'].update(status='partiel',
                                        successCount=0, trackerCount=5)
            return snap
        self.monitor.snapshot = invalid
        self.monitor.count = 29
        client = FakeClient()
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 0)

    def test_cooldown_wins_against_high_count(self):
        self.monitor.count = 29
        self.ledger.mark_returned(H, 60, 'qBittorrent prioritaire')
        client = FakeClient()
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 0)

    def test_dry_run_wins_against_high_count(self):
        self.cfg['dryRun'] = True
        self.monitor.count = 29
        client = FakeClient()
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 0)

    def test_manually_stopped_never_autostart_with_high_count(self):
        self.monitor.count = 29
        client = FakeClient('stoppedUP')
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.started, 0)

    def test_low_count_still_requires_multiple_samples_to_return(self):
        self.monitor.count = 29
        client = FakeClient()
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.stopped, 1)
        self.monitor.count = 3
        self.monitor.stamp = '2026-11-17T08:15:00+00:00'
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.started, 0)
        self.monitor.stamp = '2026-11-17T08:30:00+00:00'
        main._reconcile_handoff(client, self.cfg)
        self.assertEqual(client.started, 1)

if __name__ == '__main__':
    unittest.main()
