"""v1.15: privacy-preserving per-tracker diagnostic reporting."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'app'))  # standalone runs
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from tracker_scrape import observed_scrapes, ScrapeUnavailable, safe_scrape_error, fetch_scrape
from swarm_monitor import SwarmMonitor

H = 'a' * 40
R = [{'id':'test','enabled': True, 'trackerDomains':['example.org']}]
GOOD = 'https://a.example.org/announce?passkey=top_secret'
BAD = 'https://b.example.org/announce?passkey=another_secret'

class TestTrackerDiagnostics(unittest.TestCase):
    def test_success_and_independent_refusal(self):
        def fetch(url, _hash):
            if url == GOOD: return 29
            raise ScrapeUnavailable('Réponse HTTP 403 (accès refusé)')
        value, done, total, counts, diagnostics = observed_scrapes(
            [{'url': GOOD},{'url': BAD}], R, H, fetch=fetch,
            details=True, diagnostics=True)
        self.assertEqual((value,done,total), (29,1,2))
        self.assertEqual(counts['a.example.org'],29)
        self.assertEqual(diagnostics['a.example.org']['state'],'ok')
        self.assertEqual(diagnostics['b.example.org']['reason'],'Réponse HTTP 403 (accès refusé)')
        self.assertNotIn('passkey',json.dumps(diagnostics))

    def test_error_whitelist_hides_secrets(self):
        self.assertEqual(safe_scrape_error(ScrapeUnavailable('password=very-secret')),
                         'Réponse scrape inexploitable')
        self.assertEqual(safe_scrape_error(ScrapeUnavailable('Résolution DNS du tracker impossible')),
                         'Résolution DNS du tracker impossible')

    def test_http_code_is_readable(self):
        class Rejected:
            def open(self, req, timeout):
                raise urllib.error.HTTPError(req.full_url, 429, 'TOO MANY REQUESTS secret=xxx', {}, None)
        with self.assertRaises(ScrapeUnavailable) as exc:
            fetch_scrape(GOOD, H, opener=Rejected())
        self.assertEqual(str(exc.exception),'Réponse HTTP 429 (limitation des requêtes)')
        self.assertNotIn('top_secret',str(exc.exception))

    def test_snapshot_keeps_reason_but_hides_tracker_urls(self):
        with tempfile.TemporaryDirectory() as td:
            mon=SwarmMonitor(Path(td)/'tracker-registry.json', now=lambda: 100000)
            mon.register_solo(H, 'Test torrent', [GOOD,BAD])
            def fake(url,h):
                if url==GOOD: return 7
                raise ScrapeUnavailable('Réponse HTTP 404 (scrape absent)')
            self.assertEqual(mon.refresh(R, min_age=0, fetch=fake),1)
            snap=mon.snapshot(3600)
            obs=snap['items'][0]['rules']['test']
            self.assertEqual(obs['status'],'partiel')
            self.assertEqual(obs['successCount'],1)
            self.assertIsNotNone(obs['attemptedAt'])
            self.assertEqual(len(obs['trackerDiagnostics']),2)
            self.assertIn('scrape absent', str(obs['trackerDiagnostics']))
            self.assertNotIn('passkey', json.dumps(snap))
            self.assertNotIn('secret', json.dumps(snap))
            persisted=SwarmMonitor(Path(td)/'tracker-registry.json',now=lambda: 100001).snapshot(3600)
            self.assertEqual(persisted['items'][0]['rules']['test']['successCount'],1)

    def test_unavailable_is_not_zero(self):
        with tempfile.TemporaryDirectory() as td:
            mon=SwarmMonitor(Path(td)/'tracker-registry.json', now=lambda: 100000)
            mon.register_solo(H, 'Test torrent', [GOOD,BAD])
            mon.refresh(R,min_age=0,fetch=lambda u,h: (_ for _ in ()).throw(ScrapeUnavailable('Tracker UDP : réponse d’erreur')))
            obs=mon.snapshot()['items'][0]['rules']['test']
            self.assertIsNone(obs['leechers'])
            self.assertEqual(obs['successCount'],0)
            self.assertTrue(all(x['leechers'] is None for x in obs['trackerDiagnostics']))

if __name__=='__main__': unittest.main()
