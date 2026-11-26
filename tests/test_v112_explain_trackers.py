"""v1.11.2: all tracker domains exposed safely, per-rule decision checklist."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import main
from core import validate


class ManyTrackersClient:
    def torrents(self, _tag):
        return [{'hash': 'a' * 40, 'name': 'Test multi-tracker', 'state': 'stoppedUP',
                 'progress': 1.0, 'amount_left': 0, 'ratio': 0.1}]

    def trackers(self, _hash):
        return ([{'url': f'https://other{i}.test/announce/SECRET-{i}',
                  'status': 1, 'num_leeches': -1} for i in range(11)] +
                [{'url': 'https://bt4.t-ru.org/announce/SECRET-PASSKEY',
                  'status': 2, 'num_leeches': 6},
                 {'url': 'udp://other-udp.test:6969/announce/SECRET-UDP',
                  'status': 2, 'num_leeches': 1}])


class TooltipTest(unittest.TestCase):
    def config(self):
        cfg = json.loads((ROOT / 'config.example.json').read_text())
        cfg['qbitTag'] = 'Joal'
        cfg['rules'] = [
            {'id': 'unrelated', 'enabled': True, 'trackerDomains': ['absent.test'],
             'minLeechers': 2, 'minUploadRate': 10, 'maxUploadRate': 15,
             'simultaneousSeed': 10, 'client': 'test.client'},
            {'id': 'matched', 'enabled': True, 'trackerDomains': ['t-ru.org'],
             'minLeechers': 4, 'minUploadRate': 10, 'maxUploadRate': 15,
             'simultaneousSeed': 10, 'client': 'test.client'},
        ]
        return validate(cfg)

    def test_many_trackers_and_passkeys_never_exposed(self):
        details = main.safe_tracker_details(ManyTrackersClient().trackers('abc'))
        self.assertEqual(len(details), 13)
        self.assertTrue(any(d['domain'] == 'bt4.t-ru.org' for d in details))
        self.assertTrue(any(d['protocol'] == 'udp' for d in details))
        self.assertNotIn('SECRET', json.dumps(details))
        self.assertNotIn('/announce', json.dumps(details))

    def test_matching_rule_wins_even_if_other_rule_is_first(self):
        with patch.object(main, 'SWARM_MONITOR', None), patch.object(main, 'PENDING_DELETE', {}):
            selection, rows, tagged, _ = main.find_candidates(ManyTrackersClient(), self.config())
        self.assertEqual(tagged, 1)
        self.assertEqual(len(rows), 2)
        chosen = main.compact_dashboard_rows(rows)[0]
        self.assertEqual(chosen['rule'], 'matched')
        self.assertEqual(chosen['leechers'], 6)
        self.assertTrue(chosen['selected'])
        self.assertEqual(len(chosen['trackerDetails']), 13)
        self.assertIn('Seuil de leechers atteint', [c['label'] for c in chosen['checks']])
        self.assertEqual(next(c['result'] for c in chosen['checks']
                              if c['label'] == 'Seuil de leechers atteint'), True)
        self.assertNotIn('SECRET', json.dumps(rows))

    def test_unknown_leechers_never_represented_as_zero(self):
        cfg = self.config()
        class Unknown(ManyTrackersClient):
            def trackers(self, h):
                return [{'url':'https://bt4.t-ru.org/announce/SECRET',
                         'status': 1, 'num_leeches': -1}]
        with patch.object(main, 'SWARM_MONITOR', None), patch.object(main, 'PENDING_DELETE', {}):
            _, rows, _, _ = main.find_candidates(Unknown(), cfg)
        row = main.compact_dashboard_rows(rows)[0]
        self.assertEqual(row['rule'], 'matched')
        self.assertEqual(row['leechers'], -1)
        self.assertFalse(next(c['result'] for c in row['checks']
                              if c['label'] == 'Contrôle des leechers'))
        self.assertIsNone(next(c['result'] for c in row['checks']
                               if c['label'] == 'Seuil de leechers atteint'))


if __name__ == '__main__':
    unittest.main()
