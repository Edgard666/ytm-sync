import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main
from swarm_monitor import SwarmMonitor


class ImportedPeersTests(unittest.TestCase):
    def test_manual_stop_retains_zero_and_independent_scrape_updates(self):
        h = 'a' * 40
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            registry = path / 'registry.json'
            monitor = SwarmMonitor(registry, now=lambda: 500)
            monitor.register_solo(h, 'Example', ['https://tracker.example/announce'])
            proc = Mock()
            proc.poll.return_value = None
            proc.telemetry.name = folder
            (path / 'upload.json').write_text(json.dumps({h: [100, 500, 0, 4, 400]}))
            with patch.object(main, 'SWARM_MONITOR', monitor), patch.object(main, 'RUNNERS', {'solo-rule': proc}), patch.object(main, 'ACTIVE_HASHES', {'solo-rule': {h}}):
                self.assertEqual(main.live_joal_upload()['peers'][h]['seeders'], 0)
                proc.poll.return_value = 0
                self.assertEqual(main.live_joal_upload()['peers'], {})
            monitor.flush_peers()
            monitor = SwarmMonitor(registry, now=lambda: 500)
            self.assertEqual(monitor.display_peers([h])[h]['seeders'], {'count': 0, 'source': 'JOAL', 'time': 400})
            monitor.refresh([], fetch=lambda *args: {'leechers': 7, 'seeders': 3})
            self.assertEqual(monitor.display_peers([h])[h]['leechers']['count'], 7)
            monitor.refresh([], min_age=0, fetch=lambda *args: None)
            self.assertEqual(monitor.display_peers([h])[h]['seeders']['count'], 3)

    def test_undated_zero_leecher_sample_survives_empty_idle_sample(self):
        h = 'c' * 40
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            monitor = SwarmMonitor(root / 'registry.json')
            monitor.register_solo(h, 'Imported', ['https://tracker.example/announce'])
            proc = Mock()
            proc.poll.return_value = None
            proc.telemetry.name = folder
            with patch.object(main, 'DATA', root), patch.object(main, 'SWARM_MONITOR', monitor), patch.object(main, 'RUNNERS', {'solo-test':proc}), patch.object(main, 'ACTIVE_HASHES', {'solo-test':{h}}):
                for values in ([0, 0, 63, 0, 0], [0, 0, -1, -1, 0]):
                    (root / 'upload.json').write_text(json.dumps({h:values}))
                    main.live_joal_upload()
                monitor.flush_peers()
            counts = SwarmMonitor(root / 'registry.json').display_peers([h])[h]
            self.assertEqual((counts['seeders']['count'], counts['leechers']['count']), (63, 0))
            self.assertEqual(counts['leechers']['source'], 'Lecture locale JOAL')

    def test_stopped_seed_only_import_keeps_independent_counter_dates(self):
        import test_v114_solo as fixture
        case = fixture.SoloTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        rule = dict(case.conf['rules'][0], trackerDomains=[], minUploadRate=0, maxUploadRate=0, minLeechers=0)
        case.conf.update(rules=[rule], dryRun=False, torrentRuleOverrides={case.hash:rule['id']})
        with patch.object(main, 'TORRENT_MODES', {case.hash:'stop'}), patch.object(main, 'runner_start') as launch, patch.object(case.monitor, 'now', return_value=5000) as clock:
            for when, counts in [(5000, {'seeders': 5, 'leechers': 0}), (6000, {'seeders': 9, 'leechers': None})]:
                clock.return_value = when
                case.monitor.refresh([rule], overrides=case.conf['torrentRuleOverrides'], fetch=lambda *_: counts)
                main.solo_evaluate()
            peers = case.monitor.display_peers([case.hash])[case.hash]
            self.assertEqual((peers['seeders']['count'], peers['seeders']['time']), (9, 6000))
            self.assertEqual((peers['leechers']['count'], peers['leechers']['time']), (0, 5000))
            self.assertFalse(main.STATUS['solo']['items'][0]['active'])
            self.assertIsNotNone(main.STATUS['solo']['items'][0]['peerAttempt'])
            launch.assert_not_called()

    def test_unmatched_secondary_tracker_is_observed_even_in_stop(self):
        from tracker_scrape import ScrapeUnavailable
        h = 'b' * 40
        urls = ['https://one.example/announce', 'https://two.example/announce']
        def fetch(url, infohash):
            if url == urls[0]:
                raise ScrapeUnavailable('Réponse HTTP 404 (scrape absent)')
            return {'seeders': 67, 'leechers': 11}
        with tempfile.TemporaryDirectory() as folder:
            monitor = SwarmMonitor(Path(folder) / 'registry.json')
            monitor.register_solo(h, 'Torrent', urls)
            rule = {'id': 'one', 'enabled': True, 'trackerDomains': ['one.example']}
            monitor.refresh([rule], fetch=fetch)
            observations = monitor.snapshot()['items'][0]['rules']
            self.assertIsNone(observations['one']['leechers'])
            self.assertEqual(observations[':import']['leechers'], 11)
            self.assertEqual(monitor.display_peers([h])[h]['seeders']['count'], 67)

    def test_new_import_checked_first_and_auto_uses_current_counts(self):
        import test_v114_solo as fixture
        case = fixture.SoloTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        monitor = case.monitor
        other = '0' * 40
        # An older, never-checked qBittorrent record is also due.
        monitor.entries[other] = {'mode': 'qbit', 'urls': ['https://tracker.example.org/announce']}
        requests = []
        def high(url, h):
            requests.append(h)
            return {'seeders': 11, 'leechers': 9}
        monitor.refresh(case.conf['rules'], max_checks=1, fetch=high, on_sample=main.solo_evaluate)
        self.assertEqual(requests, [case.hash])
        row = main.STATUS['solo']['items'][0]
        self.assertEqual((row['selected'], row['seeders'], row['leechers']), (True, 11, 9))
        monitor.refresh(case.conf['rules'], min_age=0, fetch=lambda *_: {'seeders': 11, 'leechers': 0})
        main.solo_evaluate()
        self.assertFalse(main.STATUS['solo']['items'][0]['selected'])

    def test_auto_retains_fresh_evidence_and_expires_it(self):
        import test_v114_solo as fixture
        case = fixture.SoloTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        monitor = case.monitor
        with patch.object(monitor, 'now', return_value=5000) as clock:
            monitor.refresh(case.conf['rules'], fetch=lambda *_: {'seeders': 67, 'leechers': 11})
            clock.return_value = 5100
            monitor.refresh(case.conf['rules'], min_age=0, fetch=lambda *_: None)
            main.solo_evaluate()
            row = main.STATUS['solo']['items'][0]
            self.assertEqual((row['selected'], row['leechers']), (True, 11))
            monitor.remember_peers(case.hash, {'leechers': 0}, 5100, 'JOAL')
            main.solo_evaluate()
            row = main.STATUS['solo']['items'][0]
            self.assertEqual((row['selected'], row['leechers']), (False, 0))
            monitor.remember_peers(case.hash, {'seeders': 67, 'leechers': 11}, 5101, 'JOAL')
            clock.return_value = 5102
            main.solo_evaluate()
            row = main.STATUS['solo']['items'][0]
            self.assertEqual((row['selected'], row['leechers'], row['source']), (True, 11, 'JOAL'))
            clock.return_value += 3600
            main.solo_evaluate()
            row = main.STATUS['solo']['items'][0]
            self.assertFalse(row['selected'])
            self.assertIn('trop ancien', row['reason'])

    def test_first_import_reply_starts_auto_at_exact_threshold(self):
        import test_v114_solo as fixture
        from tracker_scrape import ScrapeUnavailable
        case = fixture.SoloTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.conf['dryRun'] = False
        def start(rule, payloads):
            main.RUNNERS[rule['id']] = fixture.FakeProc()
            main.ACTIVE_HASHES[rule['id']] = set(payloads)
        case.monitor.register_solo(case.hash, 'test', case.monitor.entries[case.hash]['urls'], 999)
        unavailable = Mock(side_effect=ScrapeUnavailable('Tracker sans URL de scrape standard'))
        with patch('swarm_monitor.import_announce_stats', return_value={'seeders': 67, 'leechers': 4}) as probe, \
                patch.object(main, 'runner_start', side_effect=start) as launch:
            case.monitor.refresh(case.conf['rules'], fetch=unavailable, import_probe=lambda h: True,
                                 on_sample=main.solo_evaluate)
            probe.assert_called_once()
            launch.assert_called_once()
            row = main.STATUS['solo']['items'][0]
            self.assertEqual((row['seeders'], row['leechers'], row['active']), (67, 4, True))

    def test_import_http_stopped_response(self):
        import io
        from urllib.parse import urlsplit, parse_qs
        from core import encode_bencode
        from tracker_scrape import import_announce_stats
        opener = Mock()
        opener.open.return_value = io.BytesIO(encode_bencode({b'complete': 67, b'incomplete': 4}))
        with patch('tracker_scrape.public_ip_only'), patch('tracker_scrape.urllib.request.build_opener', return_value=opener):
            stats = import_announce_stats('https://test.invalid/announce?token=test', '3' * 40, 9000)
        query = parse_qs(urlsplit(opener.open.call_args.args[0]).query)
        self.assertEqual((query['event'], query['left'], query['uploaded'], query['downloaded'], query['numwant']),
                         (['stopped'], ['9000'], ['0'], ['0'], ['0']))
        self.assertEqual(stats, {'seeders': 67, 'leechers': 4})

    def test_udp_import_probe_asks_for_no_peers(self):
        import socket
        import struct
        from unittest.mock import MagicMock
        from tracker_scrape import fetch_udp_scrape
        transport = MagicMock()
        conn = transport.__enter__.return_value
        def reply(_):
            packet = conn.sendall.call_args.args[0]
            _, action, tx = struct.unpack('!QII', packet[:16])
            if action == 0:
                return struct.pack('!IIQ', 0, tx, 91)
            self.assertEqual(action, 1)
            self.assertEqual(struct.unpack('!QQQIIIiH', packet[56:98])[:4], (0, 99, 0, 3))
            self.assertEqual(struct.unpack('!i', packet[92:96])[0], 0)
            self.assertIn(b'/announce?token=test', packet[98:])
            return struct.pack('!IIIII', 1, tx, 120, 4, 67)
        conn.recv.side_effect = reply
        result = fetch_udp_scrape('udp://test.invalid:6969/announce?token=test', 'a'*40, stopped_left=99,
            resolver=lambda *a, **kw: [(socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('8.8.8.8',6969))],
            socket_factory=lambda *a: transport)
        self.assertEqual(result, {'seeders':67, 'leechers':4})
