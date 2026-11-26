"""v1.9: monitoring and ranking are independent, thresholds remain per rule/torrent."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from core import validate, ConfigError, allowed_tracker_urls
import main


def configuration(limit=10, thresholds=(4, 7), quotas=(10, 10)):
    return {'dryRun': True, 'qbitTag': 'joal', 'refreshSeconds': 180,
            'guardSeconds': 5, 'maxCandidates': limit,
            'rules': [
                {'id': 'alpha', 'enabled': True, 'trackerDomains': ['alpha.test'],
                 'minLeechers': thresholds[0], 'minUploadRate': 30, 'maxUploadRate': 160,
                 'simultaneousSeed': quotas[0], 'client': 'normal.client'},
                {'id': 'beta', 'enabled': True, 'trackerDomains': ['beta.test'],
                 'minLeechers': thresholds[1], 'minUploadRate': 30, 'maxUploadRate': 160,
                 'simultaneousSeed': quotas[1], 'client': 'normal.client'},
            ]}


class Client:
    def __init__(self, counts):
        self.counts = counts
        self.export_calls = []

    def torrents(self, tag):
        return [{'hash': f'{i + 1:040x}', 'name': f'Film {i:03d}',
                 'state': 'stoppedUP', 'progress': 1.0, 'amount_left': 0, 'ratio': 0.1}
                for i in range(len(self.counts))]

    def trackers(self, hash_):
        i = int(hash_, 16) - 1
        tracker = 'alpha.test' if i % 2 == 0 else 'beta.test'
        return [{'url': f'https://tracker.{tracker}/announce/PRIVATE', 'status': 2,
                 'num_leeches': self.counts[i]}]

    def export(self, hash_):
        self.export_calls.append(hash_)
        raise AssertionError('dryRun must not export torrent files')


class V19RankingTests(unittest.TestCase):
    def test_monitor_all_57_select_global_top_10_respect_thresholds(self):
        config = validate(configuration(limit=10))
        data = [i for i in range(57)]
        with patch.object(main, 'PENDING_DELETE', {}):
            selected, preview, tagged, completed = main.find_candidates(Client(data), config)
        self.assertEqual(tagged, 57)
        self.assertEqual(len(preview), 57 * 2)  # each torrent is evaluated against both rules
        self.assertEqual(sum(map(len, selected.values())), 10)
        ranked = [r for r in preview if r['selected']]
        self.assertEqual(sorted(r['leechers'] for r in ranked), list(range(47, 57)))
        self.assertEqual(len({r['infoHash'] for r in ranked}), 10)
        self.assertFalse(completed)
        self.assertTrue(any('minimum 7' in r['reason'] for r in preview))
        self.assertTrue(any('Limite globale de 10' in r['reason'] for r in preview))
        self.assertEqual({r['threshold'] for r in preview if r['rule'] == 'alpha'}, {4})
        self.assertEqual({r['threshold'] for r in preview if r['rule'] == 'beta'}, {7})

    def test_threshold_can_change_within_same_rule_by_infohash(self):
        cfg = configuration(limit=10, thresholds=(4, 7))
        overridden_hash = f'{1:040x}'
        cfg['rules'][0]['minLeechersByHash'] = {overridden_hash: 50}
        data = [49, 6, 5]
        with patch.object(main, 'PENDING_DELETE', {}):
            selected, preview, _, _ = main.find_candidates(Client(data), validate(cfg))
        overridden = [r for r in preview if r['rule'] == 'alpha' and r['infoHash'] == overridden_hash][0]
        self.assertEqual(overridden['threshold'], 50)
        self.assertFalse(overridden['selected'])
        self.assertEqual(overridden['reason'], 'Leechers insuffisants : 49 / minimum 50')
        cfg['rules'][0]['minLeechersByHash'][overridden_hash] = 40
        with patch.object(main, 'PENDING_DELETE', {}):
            selected, preview, _, _ = main.find_candidates(Client(data), validate(cfg))
        overridden = [r for r in preview if r['rule'] == 'alpha' and r['infoHash'] == overridden_hash][0]
        self.assertTrue(overridden['selected'])
        self.assertEqual(overridden['threshold'], 40)

    def test_max_candidates_not_ever_block_cleanup_inspection(self):
        cfg = validate(configuration(limit=1))
        cfg['cleanup']['enabled'] = True
        with patch.object(main, 'CLEANUP_LEDGER', None), patch.object(main, 'STATE_LOCK'):
            # No code path may reject the 57-torrent inventory merely due to maxCandidates.
            # A later attempt to use the absent ledger is a different concern.
            try:
                main.cleanup_sweep(Client([0] * 57), cfg)
            except Exception as error:
                self.assertNotIn('Trop de torrents', str(error))

    def test_per_rule_capacity_still_respected(self):
        cfg = validate(configuration(limit=10, quotas=(2, 2)))
        data = [10 + i for i in range(57)]
        with patch.object(main, 'PENDING_DELETE', {}):
            selected, preview, _, _ = main.find_candidates(Client(data), cfg)
        self.assertLessEqual(len(selected['alpha']), 2)
        self.assertLessEqual(len(selected['beta']), 2)
        self.assertEqual(sum(map(len, selected.values())), 4)
        self.assertTrue(any('Limite de la règle' in row['reason'] for row in preview))

    def test_invalid_override_rejected(self):
        cfg = configuration()
        cfg['rules'][0]['minLeechersByHash'] = {'notahash': 3}
        with self.assertRaises(ConfigError):
            validate(cfg)
        cfg['rules'][0]['minLeechersByHash'] = {f'{1:040x}': -1}
        with self.assertRaises(ConfigError):
            validate(cfg)


if __name__ == '__main__':
    unittest.main()

class ScanFairnessTests(unittest.TestCase):
    def test_all_57_eventually_observed_with_batch_of_20(self):
        import tempfile
        from swarm_monitor import SwarmMonitor
        now = [1000.0]
        with tempfile.TemporaryDirectory() as folder:
            m = SwarmMonitor(Path(folder) / 'registry.json', lambda: now[0])
            data = [{'hash': f'{i + 1:040x}', 'name': 'Torrent', 'tags': 'joal'} for i in range(57)]
            url = lambda h: [{'url': 'https://tracker.alpha.test/announce?passkey=SECRET'}]
            m.synchronize(data, url, 'joal')
            rules = [{'id': 'alpha', 'enabled': True, 'trackerDomains': ['alpha.test']}]
            counts = []
            for _ in range(3):
                counts.append(m.refresh(rules, max_checks=20, min_age=900, fetch=lambda _url, _h: 4))
                now[0] += 900
            self.assertEqual(counts, [20, 20, 20])  # 57 distinct + 3 rechecks after min_age
            snap = m.snapshot(max_age_seconds=3600)
            self.assertEqual(sum(1 for t in snap['items'] if t['rules']['alpha']['status'] == 'récent'), 57)

class ScrapePriorityTests(unittest.TestCase):
    def test_fresh_scrape_overrides_cached_qbit_count(self):
        cfg = validate(configuration(limit=10))
        hash_ = f'{1:040x}'
        class Monitor:
            def snapshot(self, max_age):
                return {'items': [{'infoHash': hash_, 'rules': {'alpha': {
                    'status': 'récent', 'leechers': 0}}}]}
        with patch.object(main, 'SWARM_MONITOR', Monitor()), patch.object(main, 'PENDING_DELETE', {}):
            sel, preview, _, _ = main.find_candidates(Client([40]), cfg)
        row = next(r for r in preview if r['rule'] == 'alpha')
        self.assertEqual(row['leechers'], 0)
        self.assertEqual(row['leecherSource'], 'scrape autonome récent')
        self.assertFalse(row['selected'])
        self.assertIn('minimum 4', row['reason'])

    def test_fresh_scrape_enables_display_when_qbit_tracker_status_unknown(self):
        cfg = validate(configuration(limit=10))
        hash_ = f'{1:040x}'
        class BrokenClient(Client):
            def trackers(self, hash_):
                return [{'url': 'https://tracker.alpha.test/announce/SECRET',
                         'status': 0, 'num_leeches': -1}]
        class Monitor:
            def snapshot(self, max_age):
                return {'items': [{'infoHash': hash_, 'rules': {'alpha': {
                    'status': 'récent', 'leechers': 15}}}]}
        with patch.object(main, 'SWARM_MONITOR', Monitor()), patch.object(main, 'PENDING_DELETE', {}):
            sel, preview, _, _ = main.find_candidates(BrokenClient([0]), cfg)
        row = next(r for r in preview if r['rule'] == 'alpha')
        self.assertEqual(row['leechers'], 15)
        self.assertTrue(row['selected'])
        self.assertEqual(row['leecherSource'], 'scrape autonome récent')

    def test_expired_scrape_does_not_override_qbit(self):
        cfg = validate(configuration(limit=10))
        hash_ = f'{1:040x}'
        class Monitor:
            def snapshot(self, max_age):
                return {'items': [{'infoHash': hash_, 'rules': {'alpha': {
                    'status': 'périmé', 'leechers': 50}}}]}
        with patch.object(main, 'SWARM_MONITOR', Monitor()), patch.object(main, 'PENDING_DELETE', {}):
            sel, preview, _, _ = main.find_candidates(Client([2]), cfg)
        row = next(r for r in preview if r['rule'] == 'alpha')
        self.assertEqual(row['leechers'], 2)
        self.assertFalse(row['selected'])
