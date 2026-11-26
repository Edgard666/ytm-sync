"""v1.7 cleanup: no deletion on stale, missing, incomplete or ambiguous evidence."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main
from cleanup import Ledger, time_until
from core import ConfigError, validate
from tracker_scrape import scrape_url, fetch_scrape, checked_scrapes, ScrapeUnavailable

H = 'a' * 40
URL = 'https://tracker.example.org/announce?passkey=SECRET'

def config(dry=True, cleanup_on=True, delete=False):
    return {
        'dryRun': dry, 'qbitTag': 'joal', 'refreshSeconds': 180,
        'maxCandidates': 250, 'guardSeconds': 5, 'completedTag': 'JoalCompleted',
        'cleanup': {'enabled': cleanup_on, 'deleteData': delete,
                    'afterDays': 3, 'scrapeEveryMinutes': 60},
        'rules': [dict(id='alpha', enabled=True, trackerDomains=['example.org'],
                       minLeechers=3, minUploadRate=30, maxUploadRate=100,
                       simultaneousSeed=5, client='qbittorrent.client', targetRatio=0.0)]}


def torrent(state='stoppedUP', tags='joal'):
    return {'hash': H, 'name': 'Some torrent', 'state': state,
            'tags': tags, 'progress': 1.0, 'amount_left': 0, 'ratio': 0.2}


class Client:
    def __init__(self, torrents=None):
        self.items = list(torrents if torrents is not None else [torrent()])
        self.deletions = []
        self.calls = 0

    def torrents(self, tag):
        self.calls += 1
        return self.items

    def trackers(self, hash_):
        return [{'url': URL, 'status': 2, 'num_leeches': 0}]

    def delete_torrent_with_data(self, infohash):
        self.deletions.append(infohash)


class ScrapeTests(unittest.TestCase):
    def test_standard_url_preserves_tracker_passkey_and_replaces_announce(self):
        url = scrape_url(URL, H)
        self.assertTrue(url.startswith('https://tracker.example.org/scrape?passkey=SECRET&info_hash='))
        self.assertRaises(ScrapeUnavailable, scrape_url, 'udp://tracker.example.org:80/announce', H)
        self.assertRaises(ScrapeUnavailable, scrape_url, 'https://tracker.example.org/other', H)
        self.assertRaises(ScrapeUnavailable, scrape_url, URL, 'a' * 64)

    def test_parses_standard_bencoded_response_and_rejects_missing_hash(self):
        # d5:filesd20:<hash>d10:incompletei0eee
        payload = b'd5:filesd20:' + bytes.fromhex(H) + b'd10:incompletei0eeee'
        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, n): return payload
        class FakeOpener:
            def open(self, req, timeout): return FakeResponse()
        self.assertEqual(fetch_scrape(URL, H, opener=FakeOpener()), 0)
        with self.assertRaises(ScrapeUnavailable):
            fetch_scrape(URL, 'b' * 40, opener=FakeOpener())

    def test_all_authorized_trackers_must_respond(self):
        trackers = [{'url': URL}, {'url': 'https://another.example.org/announce'}]
        rules = config()['rules']
        count, length = checked_scrapes(trackers, rules, H, fetch=lambda url, hash_: 0)
        self.assertEqual((count, length), (0, 2))
        count, length = checked_scrapes(trackers, rules, H, fetch=lambda url, hash_: 2 if 'another' in url else 0)
        self.assertEqual((count, length), (2, 2))
        with self.assertRaises(ScrapeUnavailable):
            checked_scrapes(trackers, rules, H, fetch=lambda u, h: (_ for _ in ()).throw(ScrapeUnavailable('no')))


class LedgerTests(unittest.TestCase):
    def test_persist_continuity_reset_on_gap_and_scope_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cleanup-observations.json'
            ledger = Ledger(path)
            entry = ledger.observe(H, True, 1000, 120, 'scope1')
            self.assertEqual(entry['zeroSince'], 1000)
            entry = ledger.observe(H, True, 1070, 120, 'scope1')
            self.assertEqual(entry['observations'], 2)
            self.assertEqual(entry['zeroSince'], 1000)
            self.assertEqual(time_until(entry, 3, 1070), 3*86400 - 70)
            ledger.save()
            ledger = Ledger(path)
            self.assertEqual(ledger.entries[H]['observations'], 2)
            self.assertEqual(ledger.observe(H, True, 1080, 120, 'scope2')['zeroSince'], 1080)
            self.assertEqual(ledger.observe(H, True, 2000, 120, 'scope2')['zeroSince'], 2000)
            ledger.observe(H, False, 2001, 120, 'scope2')
            self.assertNotIn(H, ledger.entries)

    def test_cleanup_config_opt_in_validation(self):
        c = validate(config())
        self.assertFalse(c['cleanup']['deleteData'])
        c = config(cleanup_on=False, delete=True)
        with self.assertRaises(ConfigError): validate(c)
        c = config(); c['cleanup']['scrapeEveryMinutes'] = 5
        with self.assertRaises(ConfigError): validate(c)
        c = config(); c['cleanup']['afterDays'] = 1
        with self.assertRaises(ConfigError): validate(c)
        legacy = config(); legacy.pop('cleanup')
        self.assertEqual(validate(legacy)['cleanup']['afterDays'], 3)


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / 'hist.json')
        self.patches = [patch.object(main, 'CLEANUP_LEDGER', self.ledger),
                        patch.object(main, 'PENDING_DELETE', {}),
                        patch.object(main, 'RUNNERS', {}),
                        patch.object(main, 'ACTIVE_HASHES', {}),
                        patch.object(main, 'STATUS', {
                            'torrents': [], 'cleanup': {'enabled': False, 'deleteData': False,
                                                        'items': [], 'deleted': 0}})]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)

    def sample(self, client, conf, timestamp, count=0):
        with patch.object(main, 'checked_scrapes', return_value=(count, 1)):
            main.cleanup_sweep(client, conf, now=timestamp)

    def test_writes_ledger_and_never_deletes_in_dry_run(self):
        client = Client()
        c = config(dry=True, delete=True)
        initial = 500000
        for t in [initial, initial+3600, initial+7200, initial+3*86400+1]:
            self.sample(client, c, t)
        # Gaps > 2h suspend and restart 3 day countdown.
        self.assertEqual(client.deletions, [])
        self.assertGreater(main.STATUS['cleanup']['items'][0]['secondsRemaining'], 0)
        self.assertIn('scrape', main.STATUS['cleanup']['items'][0]['status'])

    def test_after_72_hours_deletes_only_with_two_scrapes_and_opt_in(self):
        client = Client()
        c = config(dry=False, delete=True)
        # Four samples at constant 1h interval, spanning 72h.
        with patch.object(main, 'checked_scrapes', return_value=(0, 1)) as scrape, \
             patch.object(main, 'read_config', return_value=c):
            for i in range(73):
                main.cleanup_sweep(client, c, now=100000 + 3600*i)
        self.assertEqual(client.deletions, [H])
        self.assertEqual(main.STATUS['cleanup']['deleted'], 1)
        self.assertGreaterEqual(scrape.call_count, 74)  # Last-minute double check
        self.assertIn('Suppression demandée', main.STATUS['cleanup']['items'][0]['status'])

    def test_reset_when_peer_arrives_or_tracker_inaccessible(self):
        client = Client()
        c = config()
        self.sample(client, c, 1000)
        self.assertIn(H, self.ledger.entries)
        self.sample(client, c, 4600, count=1)
        self.assertNotIn(H, self.ledger.entries)
        self.sample(client, c, 8200)
        with patch.object(main, 'checked_scrapes', side_effect=ScrapeUnavailable('scrape indisponible')):
            main.cleanup_sweep(client, c, now=11800)
        self.assertNotIn(H, self.ledger.entries)
        self.assertIn('indisponible', main.STATUS['cleanup']['items'][0]['status'])

    def test_no_deletion_when_qbit_activates_before_final_confirmation(self):
        client = Client()
        c = config(dry=False, delete=True)
        with patch.object(main, 'read_config', return_value=c):
            for i in range(72):
                self.sample(client, c, 100000+i*3600)
            with patch.object(main, 'checked_scrapes', return_value=(0, 1)):
                client.items = [torrent('uploading')]
                main.cleanup_sweep(client, c, now=100000+72*3600)
        self.assertEqual(client.deletions, [])
        self.assertNotIn(H, self.ledger.entries)

    def test_tag_missing_or_torrent_incomplete_skips(self):
        client = Client([torrent(tags='other')])
        c = config(dry=False, delete=True)
        self.sample(client, c, 1000)
        self.assertNotIn(H, self.ledger.entries)
        self.assertIn('tag', main.STATUS['cleanup']['items'][0]['status'])


if __name__ == '__main__':
    unittest.main()
