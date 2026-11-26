"""Seed/leech stats are read-only, never fabricated or used to authorize handoffs."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'app'))  # standalone runs
import json
import struct
import tempfile
import unittest
from pathlib import Path
from tracker_scrape import fetch_scrape, fetch_udp_scrape, observed_scrapes, ScrapeUnavailable
from swarm_monitor import SwarmMonitor
from test_v114_leechers import FakeSocket, public_resolver, HASH, UDP

GOOD='https://track.example.org/announce?passkey=PRIVATE'
RULE=[{'id':'r','enabled':True,'trackerDomains':['example.org']}]

def bint(n):return b'i'+str(n).encode()+b'e'
class FakeOpener:
    def __init__(self, seeders, leechers):
        self.seeders=seeders;self.leechers=leechers
    def open(self,req,timeout=10):
        body=(b'd5:filesd20:'+bytes.fromhex(HASH)+b'd8:complete'+bint(self.seeders)+
              b'10:incomplete'+bint(self.leechers)+b'eee')
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return body
        return Response()

class CounterTests(unittest.TestCase):
    def test_http_scrape_both_counts_single_request(self):
        for seeders, leechers in ((15,30),(0,0)):
            stats=fetch_scrape(GOOD,HASH,opener=FakeOpener(seeders,leechers),stats=True)
            self.assertEqual(stats, {'seeders':seeders,'leechers':leechers})
            self.assertEqual(fetch_scrape(GOOD,HASH,opener=FakeOpener(seeders,leechers)),leechers)
    def test_udp_scrape_both_counts_and_no_announce(self):
        sock=FakeSocket(count=9)
        stats=fetch_udp_scrape(UDP,HASH,resolver=public_resolver,socket_factory=lambda *a:sock,stats=True)
        self.assertEqual(stats,{'seeders':3,'leechers':9})
        self.assertEqual([struct.unpack('!QII',sock.sent[0])[1],struct.unpack('!QII20s',sock.sent[1])[1]],[0,2])
    def test_partial_tracker_does_not_hide_valid_individual_stats(self):
        tracker1={'url':'https://a.example.org/announce?passkey=HIDDEN'}
        tracker2={'url':'https://b.example.org/announce?passkey=ALSO_HIDDEN'}
        def fetch(url,h):
            if 'a.example.org' in url:return {'seeders':15,'leechers':30}
            raise ScrapeUnavailable('Réponse HTTP 403 (accès refusé)')
        count,done,total,per_tracker,errors,seeders,per_tracker_seed=observed_scrapes(
            [tracker1,tracker2],RULE,HASH,fetch=fetch,details=True,diagnostics=True,stats_details=True)
        self.assertEqual((count,done,total,seeders),(30,1,2,15))
        self.assertIsNone(per_tracker_seed['b.example.org'])
        self.assertNotIn('HIDDEN',json.dumps([per_tracker_seed,errors]))
    def test_monitored_stats_persist_and_stale_means_unknown(self):
        tick=[100000]
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'registry.json'
            sw=SwarmMonitor(path,now=lambda:tick[0])
            sw.register_solo(HASH,'Torrent test',[GOOD])
            sw.refresh(RULE,min_age=0,fetch=lambda *a:{'seeders':15,'leechers':30})
            obs=sw.snapshot()['items'][0]['rules']['r']
            self.assertEqual((obs['seeders'],obs['leechers']),(15,30))
            self.assertEqual(obs['trackerDiagnostics'][0]['seeders'],15)
            self.assertNotIn('PRIVATE',json.dumps(sw.snapshot()))
            again=SwarmMonitor(path,now=lambda:tick[0])
            self.assertEqual(again.snapshot()['items'][0]['rules']['r']['seeders'],15)
            tick[0]+=4000
            self.assertIsNone(sw.snapshot(3600)['items'][0]['rules']['r']['seeders'])
    def test_no_scrape_does_not_mean_zero(self):
        with tempfile.TemporaryDirectory() as td:
            sw=SwarmMonitor(Path(td)/'reg.json',now=lambda:10000)
            sw.register_solo(HASH,'T',[GOOD])
            sw.refresh(RULE,min_age=0,fetch=lambda *a:(_ for _ in ()).throw(ScrapeUnavailable('Réponse HTTP 404 (scrape absent)')))
            ob=sw.snapshot()['items'][0]['rules']['r']
            self.assertIsNone(ob['seeders'])
            self.assertIsNone(ob['leechers'])

if __name__=='__main__':unittest.main()
