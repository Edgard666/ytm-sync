"""Regression tests for v1.6 tagged inventory versus policy eligibility."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main

HASH = 'f' * 40

def conf(rules=1):
    return {
        'dryRun': True, 'qbitTag': 'joal', 'refreshSeconds': 180,
        'guardSeconds': 5, 'maxCandidates': 250,
        'rules': [
            {'id': f'rule{i}', 'enabled': True, 'trackerDomains': [f'tracker{i}.org'],
             'minLeechers': 2, 'minUploadRate': 30, 'maxUploadRate': 160,
             'simultaneousSeed': 10, 'client': 'normal.client'}
            for i in range(rules)
        ],
    }


def torrent(state='uploading', progress=1.0, hash_=HASH):
    return {'name': 'Film terminé', 'hash': hash_, 'state': state,
            'progress': progress, 'amount_left': 0 if progress == 1.0 else 3,
            'ratio': 0.3}

class Client:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.queries = []

    def torrents(self, tag):
        self.queries.append(('torrents', tag))
        return self.items

    def trackers(self, hash_):
        self.queries.append(('trackers', hash_))
        return [{'url': f'https://tracker{i}.org/announce/SECRET', 'status': 2,
                 'num_leeches': 4} for i in range(2)]

    def export(self, hash_):
        raise AssertionError('dry run must not export private torrent')

class OneShot:
    def is_set(self):
        return False
    def wait(self, seconds):
        pass

class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.path.joinpath('policy.json').write_text(json.dumps(conf()))
        self.patches = [patch.object(main, 'CONFIG', self.path / 'policy.json'),
                        patch.object(main, 'DATA', self.path),
                        patch.object(main, 'RUNNERS', {}),
                        patch.object(main, 'ACTIVE_HASHES', {}),
                        patch.object(main, 'LAST_START', {}),
                        patch.object(main, 'STATUS', {
                            'time': None, 'error': None, 'qbitConnected': False,
                            'rules': [], 'torrents': [], 'totals': {}})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_active_stopped_active_remain_tagged_with_fast_snapshot(self):
        qbit = Client([torrent()])
        main.run_once(qbit)
        self.assertEqual(main.STATUS['totals']['tagged'], 1)
        self.assertEqual(main.STATUS['totals']['selected'], 0)
        self.assertEqual(main.STATUS['totals']['blocked'], 1)
        self.assertEqual(main.STATUS['torrents'][0]['qbitState'], 'uploading')

        qbit.items = [torrent('stoppedUP')]
        self.assertTrue(main.update_inventory_snapshot(qbit.torrents('joal')))
        self.assertEqual(main.STATUS['totals']['tagged'], 1)
        self.assertEqual(main.STATUS['totals']['selected'], 0)
        self.assertIn('en attente', main.STATUS['torrents'][0]['reason'])
        main.run_once(qbit)
        self.assertEqual(main.STATUS['totals']['tagged'], 1)
        self.assertEqual(main.STATUS['totals']['eligible'], 1)
        self.assertEqual(main.STATUS['totals']['selected'], 1)

        qbit.items = [torrent('uploading')]
        self.assertTrue(main.update_inventory_snapshot(qbit.torrents('joal')))
        self.assertEqual(main.STATUS['totals']['tagged'], 1)
        self.assertEqual(main.STATUS['totals']['selected'], 0)
        self.assertEqual(main.STATUS['totals']['eligible'], 0)
        self.assertEqual(main.STATUS['totals']['blocked'], 1)
        self.assertEqual(len(main.STATUS['torrents']), 1)
        self.assertIn('actif', main.STATUS['torrents'][0]['reason'])

    def test_all_tagged_remain_visible_when_no_rules(self):
        (self.path / 'policy.json').write_text(json.dumps(conf(0)))
        qbit = Client([torrent('uploading')])
        main.run_once(qbit)
        self.assertEqual(main.STATUS['totals']['tagged'], 1)
        self.assertEqual(main.STATUS['totals']['blocked'], 1)
        self.assertEqual(main.STATUS['torrents'][0]['rule'], '')
        self.assertEqual(main.STATUS['torrents'][0]['reason'], 'Aucune règle configurée — ouvrir Règles & trackers')
        self.assertEqual(main.STATUS['torrents'][0]['trackers'], ['tracker0.org', 'tracker1.org'])
        self.assertTrue(any(x[0] == 'trackers' for x in qbit.queries))

    def test_two_matching_rules_one_torrent_one_line(self):
        (self.path / 'policy.json').write_text(json.dumps(conf(2)))
        main.run_once(Client([torrent('stoppedUP')]))
        self.assertEqual(main.STATUS['totals']['tagged'], 1)
        self.assertEqual(main.STATUS['totals']['selected'], 1)
        self.assertEqual(main.STATUS['totals']['eligible'], 1)
        self.assertEqual(main.STATUS['totals']['evaluated'], 1)
        self.assertEqual(len(main.STATUS['torrents']), 1)
        self.assertEqual(main.STATUS['torrents'][0]['rule'], 'rule0')
        self.assertNotIn('SECRET', json.dumps(main.STATUS))

    def test_tag_add_and_remove_recomputes_inventory_count_without_tracker_evaluation(self):
        main.run_once(Client([]))
        self.assertEqual(main.STATUS['totals']['tagged'], 0)
        self.assertTrue(main.update_inventory_snapshot([torrent('uploading')]))
        self.assertEqual(main.STATUS['totals']['tagged'], 1)
        self.assertEqual(len(main.STATUS['torrents']), 1)
        self.assertFalse(main.update_inventory_snapshot([torrent('uploading')]))
        self.assertTrue(main.update_inventory_snapshot([]))
        self.assertEqual(main.STATUS['totals']['tagged'], 0)
        self.assertEqual(main.STATUS['torrents'], [])

    def test_guard_refreshes_inventory_even_when_dry_run_and_no_process(self):
        qbit = Client([torrent('uploading')])
        with patch.object(main, 'QBitClient', return_value=qbit), \
             patch.object(main, 'STOP') as stop, patch.object(main, 'POLL_NOW') as poll:
            stop.is_set.side_effect = [False, True]
            main.guard_loop()
            poll.set.assert_called()
        self.assertEqual(main.STATUS['totals']['tagged'], 1)
        self.assertEqual(main.STATUS['totals']['selected'], 0)
        self.assertTrue(any(x[0] == 'torrents' for x in qbit.queries))

    def test_api_failure_marks_counts_unknown_not_zero(self):
        main.run_once(Client([torrent()]))
        self.assertEqual(main.STATUS['totals']['tagged'], 1)
        client = Mock()
        client.torrents.side_effect = ConnectionError('unavailable')
        with patch.object(main, 'QBitClient', return_value=client), \
             patch.object(main, 'STOP') as stop, patch.object(main, 'POLL_NOW') as poll, \
             patch.object(main.LOG, 'exception'):
            stop.is_set.side_effect = [False, True]
            main.guard_loop()
        self.assertFalse(main.STATUS['qbitConnected'])
        self.assertIsNone(main.STATUS['totals']['tagged'])
        self.assertEqual(main.STATUS['totals']['selected'], 0)
        self.assertIn('indisponible', main.STATUS['torrents'][0]['reason'])

    def test_inventory_recovery_resets_only_fast_polling_error(self):
        main.STATUS['error'] = 'Surveillance rapide indisponible : timeout'
        main.update_inventory_snapshot([torrent()])
        self.assertIsNone(main.STATUS['error'])
        main.STATUS['error'] = 'Échec trackers'
        main.update_inventory_snapshot([torrent()])
        self.assertEqual(main.STATUS['error'], 'Échec trackers')

if __name__ == '__main__':
    unittest.main()
