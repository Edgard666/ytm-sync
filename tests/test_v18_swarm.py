import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from swarm_monitor import SwarmMonitor
from core import validate, ConfigError

HASH = 'c' * 40
OTHER = 'd' * 40
URL = 'https://tracker.example.org/announce?passkey=REALLY_PRIVATE'
RULES = [{'id': 'tracker_a', 'enabled': True, 'trackerDomains': ['example.org']}]

class StandaloneScrapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'registry.json'
        self.tick = 1000.
        self.monitor = SwarmMonitor(self.path, lambda: self.tick)
        self.torrents = [{'hash': HASH, 'name': 'Test Torrent', 'tags': 'joal', 'state': 'stoppedUP'}]

    def admit(self):
        self.assertTrue(self.monitor.synchronize(self.torrents, lambda h: [{'url': URL}], 'joal'))

    def test_private_urls_are_not_exposed(self):
        self.admit()
        self.assertEqual(self.monitor.refresh(RULES, fetch=lambda url, infohash: 2), 1)
        snap = self.monitor.snapshot()
        self.assertEqual(snap['count'], 1)
        self.assertEqual(snap['items'][0]['rules']['tracker_a']['leechers'], 2)
        self.assertNotIn('REALLY_PRIVATE', json.dumps(snap))
        self.assertEqual(snap['items'][0]['trackers'], ['tracker.example.org'])

    def test_stateless_refresh_after_qbit_disconnect_or_restart(self):
        self.admit()
        copy = SwarmMonitor(self.path, lambda: self.tick)
        self.assertEqual(copy.refresh(RULES, fetch=lambda _url, _infohash: 0), 1)
        self.assertEqual(copy.snapshot()['items'][0]['rules']['tracker_a']['leechers'], 0)
        self.assertEqual(len(copy.entries), 1)

    def test_expired_data_not_misread_as_zero(self):
        self.admit()
        self.monitor.refresh(RULES, fetch=lambda _url, _infohash: 0)
        self.tick += 5000
        snap = self.monitor.snapshot(max_age_seconds=3600)
        obs = snap['items'][0]['rules']['tracker_a']
        self.assertIsNone(obs['leechers'])
        self.assertEqual(obs['status'], 'périmé')

    def test_failed_scrape_not_zero(self):
        self.admit()
        def broken(*args):
            raise OSError('no connection')
        self.monitor.refresh(RULES, fetch=broken)
        obs = self.monitor.snapshot()['items'][0]['rules']['tracker_a']
        self.assertIsNone(obs['leechers'])
        self.assertEqual(obs['status'], 'indisponible')

    def test_removed_tagged_torrent_removes_private_metadata(self):
        self.admit()
        self.assertTrue(self.monitor.synchronize([], lambda _hash: [], 'joal'))
        self.assertEqual(self.monitor.snapshot()['count'], 0)
        self.assertNotIn('REALLY_PRIVATE', self.path.read_text())

    def test_file_has_private_permissions(self):
        self.admit()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_not_fetching_domains_outside_allowlist(self):
        self.admit()
        other = [{'id': 'other', 'enabled': True, 'trackerDomains': ['bad.example.net']}]
        called = []
        self.assertEqual(self.monitor.refresh(other, fetch=lambda u,h: called.append(u)), 0)
        self.assertEqual(called, [])

    def test_rate_limited_and_no_duplicate_scrape(self):
        self.admit()
        self.assertEqual(self.monitor.refresh(RULES, min_age=900, fetch=lambda u,h: 3), 1)
        self.assertEqual(self.monitor.refresh(RULES, min_age=900, fetch=lambda u,h: 3), 0)
        self.tick += 901
        self.assertEqual(self.monitor.refresh(RULES, min_age=900, fetch=lambda u,h: 3), 1)

    def test_config_migration_defaults(self):
        template = json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text())
        template.pop('swarm')
        validated = validate(template)
        self.assertTrue(validated['swarm']['enabled'])
        self.assertEqual(validated['swarm']['intervalMinutes'], 15)
        validated['swarm']['maxAgeMinutes'] = 5
        with self.assertRaises(ConfigError):
            validate(validated)

if __name__ == '__main__': unittest.main()
