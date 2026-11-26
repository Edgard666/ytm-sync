"""Read-only UDP scraper, qBittorrent informational fallback and boundary tests."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main
from tracker_scrape import (fetch_udp_scrape, checked_scrapes, fetch_tracker_scrape,
                            ScrapeUnavailable)
from swarm_monitor import SwarmMonitor

HASH = 'f' * 40
UDP = 'udp://tracker.example.org:1337/announce'
HTTP = 'https://tracker.example.org/announce?token=VERY_PRIVATE'
RULE = {'id': 'tracker', 'enabled': True, 'trackerDomains': ['example.org'],
        'minLeechers': 4, 'minUploadRate': 10, 'maxUploadRate': 100,
        'simultaneousSeed': 3, 'targetRatio': 0}


def public_resolver(*args, **kwargs):
    import socket
    return [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, '', ('1.1.1.1', 1337))]


class FakeSocket:
    def __init__(self, *, count=6, corrupt=None):
        self.count = count
        self.corrupt = corrupt
        self.sent = []
        self.destination = None

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def settimeout(self, timeout): self.timeout = timeout
    def connect(self, address): self.destination = address
    def sendall(self, payload): self.sent.append(payload)

    def recv(self, maximum):
        if len(self.sent) == 1:
            _protocol, action, tx = struct.unpack('!QII', self.sent[0])
            assert action == 0
            if self.corrupt == 'connect-transaction': tx ^= 3
            return struct.pack('!IIQ', 0, tx, 0x0102030405060708)
        connection_id, action, tx, infohash = struct.unpack('!QII20s', self.sent[1])
        assert connection_id == 0x0102030405060708 and action == 2
        assert infohash == bytes.fromhex(HASH)
        if self.corrupt == 'scrape-transaction': tx ^= 1
        if self.corrupt == 'truncated': return struct.pack('!II', 2, tx)
        return struct.pack('!IIIII', 2, tx, 3, 1, self.count)


class UDPStatsTests(unittest.TestCase):
    def test_valid_udp_scrape_and_no_announce(self):
        sock = FakeSocket(count=0)
        self.assertEqual(fetch_udp_scrape(UDP, HASH, resolver=public_resolver,
                                         socket_factory=lambda *args: sock), 0)
        self.assertEqual(sock.destination, ('1.1.1.1', 1337))
        self.assertEqual(len(sock.sent), 2)
        self.assertEqual(struct.unpack('!I', sock.sent[0][8:12])[0], 0)
        self.assertEqual(struct.unpack('!I', sock.sent[1][8:12])[0], 2)

    def test_invalid_transactions_and_missing_data_never_return_zero(self):
        for corrupt in ('connect-transaction', 'scrape-transaction', 'truncated'):
            with self.subTest(corrupt=corrupt):
                with self.assertRaises(ScrapeUnavailable):
                    fetch_udp_scrape(UDP, HASH, resolver=public_resolver,
                                     socket_factory=lambda *args: FakeSocket(corrupt=corrupt))

    def test_no_requests_to_private_addresses_and_invalid_hash(self):
        import socket
        def private(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, '', ('127.0.0.1', 1337))]
        with self.assertRaises(ScrapeUnavailable):
            fetch_udp_scrape(UDP, HASH, resolver=private, socket_factory=lambda *args: self.fail('socket opened'))
        with self.assertRaises(ScrapeUnavailable):
            fetch_udp_scrape(UDP, 'bad', resolver=public_resolver)

    def test_protocol_dispatch_and_mixed_tracker_rules(self):
        with patch('tracker_scrape.fetch_udp_scrape', return_value=6) as udp, \
             patch('tracker_scrape.fetch_scrape', return_value=4) as http:
            total, quantity = checked_scrapes([{'url': UDP}, {'url': HTTP}], [RULE], HASH)
            self.assertEqual((total, quantity), (6, 2))
            udp.assert_called_once_with(UDP, HASH)
            http.assert_called_once_with(HTTP, HASH)

    def test_autonomous_udp_monitor_records_timestamp_without_leaking_url(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = SwarmMonitor(Path(directory) / 'registry.json', now=lambda: 2500)
            monitor.synchronize([{'hash': HASH, 'name': 'Example Torrent'}], lambda _h: [{'url': UDP}], 'Joal')
            self.assertEqual(monitor.refresh([RULE], fetch=lambda url, h: 7), 1)
            result = monitor.snapshot()
            item = result['items'][0]
            self.assertEqual(item['rules']['tracker']['leechers'], 7)
            self.assertIn('2500', str(monitor.entries[HASH]['observations']['tracker']['sampledAt']))
            self.assertNotIn('VERY_PRIVATE', str(result))

    def test_qbit_historic_count_is_display_only(self):
        config = {'qbitTag':'Joal','rules':[RULE],'maxCandidates':10,
                  'dryRun':True,'swarm':{'enabled':False},'handoff':{'enabled':False}}
        class Qbit:
            def torrents(self, tag):
                self_tag = tag
                assert self_tag == 'Joal'
                return [{'hash':HASH, 'name':'Example', 'state':'stoppedUP',
                         'progress':1.0,'amount_left':0,'ratio':0.1,
                         'num_incomplete':9,'tags':'Joal'}]
            def trackers(self, hash_):
                return [{'url':HTTP, 'status':1, 'num_leeches':-1}]
        with patch.object(main, 'SWARM_MONITOR', None):
            selection, rows, total, completed = main.find_candidates(Qbit(), config)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['displayLeechers'], 9)
        self.assertEqual(rows[0]['leechers'], -1)
        self.assertFalse(rows[0]['eligible'])
        self.assertEqual(selection['tracker'], {})
        self.assertIn('non datée', rows[0]['displaySource'])


    def test_partial_result_is_valid_for_responding_tracker_only(self):
        from tracker_scrape import ScrapeUnavailable
        other = 'udp://backup.example.org:1337/announce'
        with tempfile.TemporaryDirectory() as directory:
            monitor = SwarmMonitor(Path(directory) / 'registry.json', now=lambda: 15000)
            monitor.synchronize([{'hash': HASH, 'name': 'Example'}],
                                lambda _h: [{'url': HTTP}, {'url': other}], 'Joal')
            def intermittent(url, hash_):
                if url == HTTP:
                    raise ScrapeUnavailable('not supported')
                return 8
            self.assertEqual(monitor.refresh([RULE], fetch=intermittent), 1)
            observation = monitor.snapshot()['items'][0]['rules']['tracker']
            self.assertEqual(observation['status'], 'partiel')
            self.assertEqual(observation['leechers'], 8)
            self.assertEqual(observation['successCount'], 1)
            self.assertEqual(observation['trackerCount'], 2)
            class Qbit:
                def torrents(self, tag):
                    return [{'hash':HASH, 'name':'Example', 'state':'stoppedUP',
                             'progress':1, 'amount_left':0, 'ratio':0.0}]
                def trackers(self, hash_):
                    return [{'url': HTTP, 'status':1, 'num_leeches':-1},
                            {'url': other, 'status':1, 'num_leeches':-1}]
            conf = {'qbitTag':'Joal','rules':[RULE],'maxCandidates':10,'dryRun':True,
                    'swarm':{'enabled':True,'maxAgeMinutes':45},'handoff':{'enabled':False}}
            with patch.object(main, 'SWARM_MONITOR', monitor):
                _, rows, _, _ = main.find_candidates(Qbit(), conf)
            self.assertEqual(rows[0]['displayLeechers'], 8)
            self.assertEqual(rows[0]['leechers'], 8)
            self.assertTrue(rows[0]['eligible'])
            self.assertIn('partiel frais', rows[0]['displaySource'])
            self.assertIn('1/2 trackers', rows[0]['leecherSource'])

    def test_registry_refreshes_trackers_after_one_hour(self):
        clock = [2000.0]
        with tempfile.TemporaryDirectory() as directory:
            monitor = SwarmMonitor(Path(directory) / 'registry.json', now=lambda: clock[0])
            tor = [{'hash': HASH, 'name': 'Example'}]
            monitor.synchronize(tor, lambda _h: [{'url': UDP}], 'Joal')
            self.assertEqual(monitor.entries[HASH]['urls'], [UDP])
            clock[0] += 100
            monitor.synchronize(tor, lambda _h: [{'url': HTTP}], 'Joal')
            self.assertEqual(monitor.entries[HASH]['urls'], [UDP])
            clock[0] += 3601
            monitor.synchronize(tor, lambda _h: [{'url': HTTP}], 'Joal')
            self.assertEqual(monitor.entries[HASH]['urls'], [HTTP])

if __name__ == '__main__':
    unittest.main()
