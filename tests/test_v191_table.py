"""v1.9.1: qBittorrent addition timestamps & table assets."""
import sys
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import main
from core import validate


class _Client:
    def torrents(self, tag):
        return [
            {'hash': 'a' * 40, 'name': 'Avant', 'state': 'stoppedUP', 'progress': 1,
             'amount_left': 0, 'ratio': 2, 'added_on': 1710000000},
            {'hash': 'b' * 40, 'name': 'Sans date', 'state': 'stoppedUP', 'progress': 1,
             'amount_left': 0, 'ratio': 0, 'added_on': -1},
        ]

    def trackers(self, h):
        return [{'url': 'https://tracker.alpha.test/announce', 'status': 2, 'num_leeches': 7}]


class LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.ids = [], set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.add(attrs['id'])
        if tag == 'script':
            self.scripts.append(attrs.get('src'))


class TableTests(unittest.TestCase):
    def test_added_timestamp_validated(self):
        self.assertEqual(main.torrent_added_timestamp({'added_on': 1710000000}), 1710000000)
        for value in (None, -1, 0, True, '1710000000', float('inf')):
            self.assertIsNone(main.torrent_added_timestamp({'added_on': value}))

    def test_evaluated_rows_have_dates_when_qbit_supplies_them(self):
        cfg = json.loads((ROOT / 'config.example.json').read_text())
        cfg['rules'] = [{'id': 'alpha', 'enabled': True, 'trackerDomains': ['alpha.test'],
                         'minLeechers': 3, 'minUploadRate': 30, 'maxUploadRate': 100,
                         'simultaneousSeed': 10, 'client': 'test-client.client'}]
        cfg = validate(cfg)
        with patch.object(main, 'SWARM_MONITOR', None), patch.object(main, 'PENDING_DELETE', {}):
            _sel, preview, _tagged, _completed = main.find_candidates(_Client(), cfg)
        self.assertEqual({r['name']: r['addedOn'] for r in preview},
                         {'Avant': 1710000000, 'Sans date': None})

    def test_html_controls_and_static_assets(self):
        collector = LinkCollector()
        html = (ROOT / 'app/static/index.html').read_text()
        collector.feed(html)
        self.assertIn('/assets/torrent_table.js', collector.scripts)
        self.assertLess(collector.scripts.index('/assets/torrent_table.js'),
                        collector.scripts.index('/assets/app.js'))
        for id_ in ('torrent-page-size', 'torrent-prev', 'torrent-next',
                    'torrent-page-label', 'torrent-items'):
            self.assertIn(id_, collector.ids)
        for key in ('name', 'trackers', 'rule', 'leechers', 'ratio', 'addedOn', 'status', 'reason'):
            self.assertIn(f'data-sort-key="{key}"', html)
        for value in (10, 20, 50, 100, 500):
            self.assertIn(f'value="{value}"', html)
        main_source = (ROOT / 'app/main.py').read_text()
        self.assertIn('"/assets/torrent_table.js": ("torrent_table.js"', main_source)


if __name__ == '__main__':
    unittest.main()
