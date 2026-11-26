"""v1.29: torrents that do not come from qBittorrent show their seeders/leechers and need ONE
recent result to be activated, whatever rule or source produced it. Retired designs fall back."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_v114_solo as solo_fixture  # noqa: E402
import main  # noqa: E402
import interface_design  # noqa: E402
from backup_bundle import build_backup, inspect_backup  # noqa: E402

IDLE = {'id': 'seed-sans-upload', 'enabled': True, 'trackerDomains': [], 'client': None, 'minLeechers': 0,
        'minLeechersByHash': {}, 'minUploadRate': 0, 'maxUploadRate': 0, 'simultaneousSeed': 10, 'targetRatio': 0}


class StandaloneCounts(unittest.TestCase):
    def setUp(self):
        self.case = solo_fixture.SoloTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.case.conf.update(dryRun=False)
        self.started = []
        for p in (patch.object(main, 'runner_start', lambda rule, payloads: self.started.append(rule['id'])),
                  patch.object(main, 'simulations_paused', lambda: False)):
            p.start()
            self.addCleanup(p.stop)
        self.now = time.time()

    def evaluate(self, modes=None):
        with patch.object(main, 'TORRENT_MODES', modes or {}):
            main.solo_evaluate()
        return main.STATUS['solo']['items'][0]

    def test_forced_seed_without_upload_shows_the_scrape_of_its_rule(self):
        case = self.case
        case.conf.update(rules=[dict(case.rule), IDLE], torrentRuleOverrides={case.hash: 'seed-sans-upload'})
        # Same call as swarm_loop: the scrape is stored under the assigned rule, not the Forced one.
        case.monitor.refresh(case.conf['rules'], overrides=case.conf['torrentRuleOverrides'],
                             fetch=lambda *_: {'seeders': 7, 'leechers': 3})
        row = self.evaluate({case.hash: 'forced'})
        self.assertEqual((row['rule'], row['leechers'], row['seeders']), (main.FORCED_IDLE, 3, 7))
        self.assertEqual(self.started, ['solo-' + main.FORCED_IDLE])

    def test_one_joal_announce_read_locally_is_enough(self):
        self.case.monitor.entries[self.case.hash]['lastPeers'] = {
            'leechers': {'count': 9, 'time': self.now - 60, 'source': 'Lecture locale JOAL'},
            'seeders': {'count': 12, 'time': self.now - 60, 'source': 'Lecture locale JOAL'}}
        row = self.evaluate()
        self.assertEqual((row['leechers'], row['seeders']), (9, 12))
        self.assertEqual(row['reason'], 'Seuil de leechers confirmé')
        self.assertEqual(self.started, ['solo-alpha'])

    def test_one_scrape_under_another_rule_is_enough(self):
        self.case.monitor.entries[self.case.hash]['observations'] = {'seed-sans-upload': {
            'sampledAt': self.now - 60, 'attemptedAt': self.now - 60, 'leechers': 6, 'seeders': 20,
            'status': 'ok', 'trackerCount': 1, 'successCount': 1, 'source': 'scrape UDP'}}
        row = self.evaluate()
        self.assertEqual((row['leechers'], row['seeders']), (6, 20))
        self.assertEqual(self.started, ['solo-alpha'])

    def test_newest_result_wins(self):
        self.case.obs(2)                                   # rule scrape: 2 leechers (below 4)...
        self.case.monitor.entries[self.case.hash]['observations']['alpha']['sampledAt'] = self.now - 600
        self.case.monitor.entries[self.case.hash]['lastPeers'] = {
            'leechers': {'count': 8, 'time': self.now - 30, 'source': 'JOAL'}}    # ...a newer announce: 8
        row = self.evaluate()
        self.assertEqual(row['leechers'], 8)
        self.assertEqual(self.started, ['solo-alpha'])

    def test_threshold_and_freshness_still_apply(self):
        self.case.monitor.entries[self.case.hash]['lastPeers'] = {
            'leechers': {'count': 2, 'time': self.now - 60, 'source': 'JOAL'}}
        row = self.evaluate()
        self.assertEqual(row['reason'], 'Leechers insuffisants : 2 / minimum 4')
        self.case.monitor.entries[self.case.hash]['lastPeers'] = {
            'leechers': {'count': 9, 'time': self.now - 3 * 3600, 'source': 'JOAL'}}
        row = self.evaluate()
        self.assertIsNone(row['leechers'])
        self.assertEqual(self.started, [])

    def test_rule_limited_to_some_trackers_keeps_its_own_readings(self):
        case = self.case
        url = 'udp://tracker.example.org:6969/announce/PRIVATE_TOKEN'
        other = 'https://other.example.net/announce'
        info = dict(next(i for i in case.lib.list([case.rule])), urls=[url, other])
        case.monitor.entries[case.hash]['lastPeers'] = {
            'leechers': {'count': 9, 'time': self.now - 60, 'source': 'JOAL'}}
        with patch.object(case.lib, 'list', lambda rules: [info]):
            row = self.evaluate()
        self.assertIsNone(row['leechers'])                # alpha covers example.org only
        self.assertEqual(self.started, [])

    def test_mode_change_scrapes_uncovered_torrent_at_once(self):
        case = self.case
        case.conf['rules'] = [dict(case.rule, trackerDomains=['other.net'])]   # no rule covers its tracker
        calls = []
        fetch = lambda *_: calls.append(1) or {'seeders': 1, 'leechers': 1}
        case.monitor.refresh(case.conf['rules'], fetch=fetch)                  # ':import' scrape
        case.monitor.refresh(case.conf['rules'], fetch=fetch)                  # too recent: skipped
        self.assertEqual(len(calls), 1)
        case.monitor.entries[case.hash]['observations'][':import']['attemptedAt'] -= 120
        case.monitor.request_refresh({case.hash})                             # Forced / startup
        case.monitor.refresh(case.conf['rules'], fetch=fetch)
        self.assertEqual(len(calls), 2)


class RetiredDesigns(unittest.TestCase):
    def test_trame_and_regie_fall_back_to_classique(self):
        self.assertEqual(set(interface_design.DESIGNS), {'classique', 'baie'})
        with tempfile.TemporaryDirectory() as tmp:
            for old in ('trame', 'regie-sombre'):
                (Path(tmp) / 'interface.json').write_text(json.dumps({'design': old}))
                self.assertEqual(interface_design.current(tmp), 'classique')

    def test_backup_from_v128_with_regie_still_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / 'policy.json').write_bytes((Path(__file__).resolve().parents[1] / 'config.example.json').read_bytes())
            (data / 'interface.json').write_text(json.dumps({'design': 'regie'}))
            files = inspect_backup(build_backup(data))
            self.assertIn('interface.json', files)


class TrackerWithoutScrape(unittest.TestCase):
    """v1.29.1: a tracker that refuses scraping is read by a stopped announce at every due pass."""
    def setUp(self):
        self.case = solo_fixture.SoloTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.clock = [time.time()]
        self.case.monitor.now = lambda: self.clock[0]
        self.announces = []
        p = patch('swarm_monitor.import_announce_stats',
                  lambda url, h, left: self.announces.append(h) or {'seeders': 12, 'leechers': 9})
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def refused(url, h):
        from tracker_scrape import ScrapeUnavailable
        raise ScrapeUnavailable('Réponse HTTP 403 (accès refusé)')

    def test_counts_stay_fresh_beyond_the_validity_window(self):
        for _ in range(5):                                 # one hour of 16-minute passes
            self.case.monitor.refresh(self.case.conf['rules'], min_age=900, fetch=self.refused,
                                      import_probe=lambda h: True)
            self.clock[0] += 16 * 60
        self.assertEqual(len(self.announces), 5)
        observation = self.case.monitor.entries[self.case.hash]['observations']['alpha']
        self.assertEqual(observation['leechers'], 9)
        self.assertGreater(observation['sampledAt'], self.clock[0] - 45 * 60)

    def test_any_failed_or_incomplete_scrape_falls_back_to_the_announce(self):
        from tracker_scrape import ScrapeUnavailable
        def raising(message):
            def fetch(url, h):
                raise ScrapeUnavailable(message)
            return fetch
        fetches = [raising('Délai de réponse du tracker dépassé'), raising('Réponse HTTP 503 (service indisponible)'),
                   raising('Statut HTTP du scrape incorrect'), lambda url, h: {'seeders': 4, 'leechers': None}]
        for fetch in fetches:
            self.clock[0] += 16 * 60
            before = len(self.announces)
            self.case.monitor.refresh(self.case.conf['rules'], min_age=900, fetch=fetch, import_probe=lambda h: True)
            self.assertEqual(len(self.announces), before + 1, fetch)
            self.assertEqual(self.case.monitor.entries[self.case.hash]['observations']['alpha']['leechers'], 9)

    def test_working_scrape_sends_no_announce(self):
        self.case.monitor.refresh(self.case.conf['rules'], min_age=900, import_probe=lambda h: True,
                                  fetch=lambda url, h: {'seeders': 4, 'leechers': 6})
        self.assertEqual(self.announces, [])
        self.assertEqual(self.case.monitor.entries[self.case.hash]['observations']['alpha']['leechers'], 6)

    def test_refused_announce_keeps_the_scrape_seeders(self):
        with patch('swarm_monitor.import_announce_stats', side_effect=OSError('refusé')):
            self.case.monitor.refresh(self.case.conf['rules'], min_age=900, import_probe=lambda h: True,
                                      fetch=lambda url, h: {'seeders': 4, 'leechers': None})
        self.assertEqual(self.case.monitor.entries[self.case.hash]['observations']['alpha']['seeders'], 4)

    def test_no_announce_while_joal_runs_the_torrent(self):
        proc = solo_fixture.FakeProc()
        with patch.object(main, 'RUNNERS', {'solo-alpha': proc}), \
             patch.object(main, 'ACTIVE_HASHES', {'solo-alpha': {self.case.hash}}), patch.object(main, 'QBIT_URL', ''):
            self.assertFalse(main.import_probe_allowed(self.case.hash))
            proc.dead = True
            self.assertTrue(main.import_probe_allowed(self.case.hash))


if __name__ == '__main__':
    unittest.main()
