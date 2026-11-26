"""v1.24.0: log rotation, watched folder, announce reasons, health, schedules, S/L chart."""
import datetime as dt
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main  # noqa: E402
import import_watch  # noqa: E402
import log_rotation  # noqa: E402
from core import ConfigError, rule_active_now, schedule_label, validate  # noqa: E402
from swarm_monitor import SwarmMonitor  # noqa: E402
from tracker_scrape import announce_category  # noqa: E402
import test_v114_solo as solo  # noqa: E402
FakeProc = solo.FakeProc

BASE = json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text())


class LogRotationTests(unittest.TestCase):
    def test_rotates_by_size_and_keeps_n_backups(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'joal.log'
            log = log_rotation.RotatingLog(path, max_bytes=100, backups=2)
            for i in range(10):
                log.write(b'x' * 40 + b'\n')
            log.close()
            names = sorted(p.name for p in Path(root).iterdir())
            self.assertEqual(names, ['joal.log', 'joal.log.1', 'joal.log.2'])
            self.assertTrue(all(p.stat().st_size <= 100 for p in Path(root).iterdir()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_oversized_old_log_is_rotated_at_start(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'joal.log'
            path.write_bytes(b'y' * 500)
            log_rotation.RotatingLog(path, max_bytes=100, backups=3).close()
            self.assertEqual(path.stat().st_size, 0)
            self.assertEqual((Path(root) / 'joal.log.1').stat().st_size, 500)

    def test_pump_copies_child_output_until_eof(self):
        with tempfile.TemporaryDirectory() as root:
            seen = []
            log = log_rotation.RotatingLog(Path(root) / 'joal.log')
            log_rotation.pump(io.BytesIO(b'line 1\nline 2\n'), log, on_line=seen.append)
            self.assertEqual((Path(root) / 'joal.log').read_bytes(), b'line 1\nline 2\n')
            self.assertEqual(seen, [b'line 1\n', b'line 2\n'])


class ImportWatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name) / 'import'
        import_watch.ensure(self.folder)

    def drop(self, name, content=b'd4:infod4:name1:xee', age=60):
        path = self.folder / name
        path.write_bytes(content)
        stamp = time.time() - age
        import os
        os.utime(path, (stamp, stamp))
        return path

    def test_files_are_sorted_into_subfolders(self):
        self.drop('ok.torrent')
        self.drop('dup.torrent')
        self.drop('bad.torrent')
        outcomes = {'ok.torrent': ('added', 'Importé'), 'dup.torrent': ('duplicate', 'déjà là'),
                    'bad.torrent': ('error', 'Fichier .torrent invalide')}
        results = import_watch.scan(self.folder, lambda payload, name: outcomes[name])
        self.assertEqual({r['filename']: r['status'] for r in results},
                         {name: status for name, (status, _) in outcomes.items()})
        self.assertTrue((self.folder / 'traités' / 'ok.torrent').exists())
        self.assertTrue((self.folder / 'doublons' / 'dup.torrent').exists())
        self.assertTrue((self.folder / 'erreurs' / 'bad.torrent').exists())
        self.assertIn('invalide', (self.folder / 'erreurs' / 'bad.torrent.erreur.txt').read_text())

    def test_recent_or_foreign_files_are_left_alone(self):
        self.drop('copie-en-cours.torrent', age=0)
        self.drop('notes.txt')
        calls = []
        self.assertEqual(import_watch.scan(self.folder, lambda *a: calls.append(a) or ('added', '')), [])
        self.assertEqual(calls, [])
        self.assertTrue((self.folder / 'copie-en-cours.torrent').exists())

    def test_importer_crash_does_not_loop_on_the_same_file(self):
        self.drop('boom.torrent')
        results = import_watch.scan(self.folder, lambda *a: 1 / 0)
        self.assertEqual(results[0]['status'], 'error')
        self.assertFalse((self.folder / 'boom.torrent').exists())

    def test_name_collision_keeps_both_files(self):
        (self.folder / 'traités' / 'a.torrent').write_bytes(b'old')
        self.drop('a.torrent')
        import_watch.scan(self.folder, lambda *a: ('added', ''))
        self.assertEqual(len(list((self.folder / 'traités').glob('a*.torrent'))), 2)


class SoloFixture(unittest.TestCase):
    """Reuse the v1.14 library fixture without re-running its tests."""
    setUp = solo.SoloTests.setUp
    tearDown = solo.SoloTests.tearDown
    obs = solo.SoloTests.obs


class WatchedImportIntegrationTests(SoloFixture):
    """The watched folder goes through the same library path as the WebUI."""
    def test_watched_import_adds_and_detects_duplicates(self):
        payload = solo.payload
        with patch.object(main, 'DIAGNOSTICS', None):
            self.lib.delete(self.hash)
            self.assertEqual(main.watched_import(payload(), 'nouveau.torrent')[0], 'added')
            self.assertEqual(main.watched_import(payload(), 'encore.torrent')[0], 'duplicate')
            self.assertEqual(main.watched_import(b'garbage', 'x.torrent')[0], 'error')


class AnnounceReasonTests(unittest.TestCase):
    def test_categories_never_expose_urls(self):
        label = announce_category(False, '<tracker>: Client qBittorrent 4.1 is banned')
        self.assertEqual(label, 'Client refusé par le tracker')
        self.assertEqual(announce_category(False, 'Tracker response is an error: status 403'), 'Réponse HTTP 403')
        self.assertEqual(announce_category(True, None), 'Acceptée')

    def test_refused_announce_replaces_row_reason(self):
        snapshot = {'torrents': [], 'solo': {'items': [{'infoHash': 'a' * 40, 'reason': 'Session JOAL active'}]},
                    'joalUpload': {'announcements': {'a' * 40: {'Moteur JOAL': {'result': 'Client refusé par le tracker', 'time': 5}}}}}
        main.announce_issue_rows(snapshot)
        row = snapshot['solo']['items'][0]
        self.assertEqual(row['reason'], 'Annonce refusée : Client refusé par le tracker')
        self.assertEqual(row['lastAnnounce']['time'], 5)

    def test_reads_joal_announce_outcomes_from_telemetry(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'announce.json').write_text(json.dumps(
                {'a' * 40: [0, 123, '<tracker>: unregistered torrent'], 'bad': [1, 1, ''], 'b' * 40: 'x'}))
            proc = type('P', (), {'telemetry': type('T', (), {'name': folder})()})()
            self.assertEqual(main.read_announce_outcomes(proc),
                             {'a' * 40: {'Moteur JOAL': {'result': 'Torrent inconnu du tracker', 'time': 123}}})


class ScheduleTests(unittest.TestCase):
    def rule(self, schedule):
        p = json.loads(json.dumps(BASE))
        p['rules'][0]['schedule'] = schedule
        return p

    def test_validation(self):
        validate(self.rule({'days': [5, 1], 'start': '22:00', 'end': '07:00'}))
        for bad in ({'days': [], 'start': '22:00', 'end': '07:00'}, {'days': [0], 'start': '22:00', 'end': '07:00'},
                    {'days': [1], 'start': '9:00', 'end': '17:00'}, {'days': [1], 'start': '09:00', 'end': '09:00'},
                    {'days': [1], 'start': '09:00'}, 'nuit'):
            with self.assertRaises(ConfigError, msg=bad):
                validate(self.rule(bad))

    def test_overnight_window_belongs_to_its_start_day(self):
        rule = {'schedule': {'days': [5], 'start': '22:00', 'end': '07:00'}}   # vendredi soir
        friday = dt.datetime(2026, 11, 20)
        self.assertTrue(rule_active_now(rule, friday.replace(hour=23)))
        self.assertTrue(rule_active_now(rule, friday + dt.timedelta(days=1, hours=6, minutes=59)))
        self.assertFalse(rule_active_now(rule, friday + dt.timedelta(days=1, hours=7)))
        self.assertFalse(rule_active_now(rule, friday.replace(hour=6)))      # jeudi soir non coché
        self.assertTrue(rule_active_now({}, friday))
        self.assertEqual(schedule_label(rule), 'ven 22:00–07:00')

    def test_forced_mode_ignores_schedules(self):
        conf = validate(self.rule({'days': [1], 'start': '01:00', 'end': '02:00'}))
        conf['rules'][0].update(enabled=True, client='demo.client')
        with patch.object(main, 'TORRENT_MODES', {'c' * 40: 'forced'}):
            forced = [r for r in main.simulation_config(conf)['rules'] if r['id'] == main.FORCED_RULE]
        self.assertIsNone(forced[0]['schedule'])


class SoloScheduleTests(SoloFixture):
    def test_imported_torrent_waits_outside_its_window(self):
        self.conf['dryRun'] = False
        self.obs(9)
        self.conf['rules'][0] = dict(self.conf['rules'][0], schedule={'days': [1], 'start': '01:00', 'end': '02:00'})
        with patch.object(main, 'rule_active_now', lambda rule: not rule.get('schedule')), \
                patch.object(main, 'runner_start', side_effect=AssertionError('hors plage')):
            main.solo_evaluate()
        row = main.STATUS['solo']['items'][0]
        self.assertIn('Hors plage horaire', row['reason'])
        self.assertFalse(row['active'])

    def test_running_session_stops_when_window_closes(self):
        self.conf['dryRun'] = False
        self.obs(9)
        def fake_start(rule, selected):
            main.RUNNERS[rule['id']] = FakeProc()
            main.ACTIVE_HASHES[rule['id']] = set(selected)
        def fake_stop(rid):
            main.RUNNERS.pop(rid)
            main.ACTIVE_HASHES.pop(rid, None)
        with patch.object(main, 'runner_start', side_effect=fake_start), \
                patch.object(main, 'runner_stop', side_effect=fake_stop):
            main.solo_evaluate()
            self.assertIn('solo-alpha', main.RUNNERS)
            with patch.object(main, 'rule_active_now', lambda rule: False):
                main.solo_evaluate()
            self.assertNotIn('solo-alpha', main.RUNNERS)


class PeerChartTests(unittest.TestCase):
    def test_series_merges_scrapes_and_announces_without_inventing_zeros(self):
        with tempfile.TemporaryDirectory() as root:
            monitor = SwarmMonitor(Path(root) / 'registry.json')
            h = 'd' * 40
            monitor.register_solo(h, 'demo', ['http://t.example/announce'])
            monitor.entries[h]['history'] = {'r': [
                {'time': 1000, 'leechers': 3, 'seeders': 7, 'status': 'ok'},
                {'time': 2000, 'leechers': None, 'seeders': None, 'status': 'scrape indisponible'}]}
            monitor.remember_peers(h, {'seeders': 5, 'leechers': 2}, 3000, 'JOAL')
            monitor.remember_peers(h, {'seeders': 5, 'leechers': 2}, 3060, 'JOAL')    # unchanged: skipped
            monitor.remember_peers(h, {'seeders': 6, 'leechers': 2}, 3120, 'JOAL')
            points = monitor.peer_series(h)['points']
            self.assertEqual([(p['source'], p['seeders'], p['leechers']) for p in points],
                             [('Scrape', 7, 3), ('JOAL', 5, 2), ('JOAL', 6, 2)])
            with self.assertRaises(KeyError):
                monitor.peer_series('e' * 40)


class HealthTests(unittest.TestCase):
    def test_threads_report_status_and_restarts(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(main, 'HEARTBEATS', {'solo': {'last': time.time(), 'restarts': 2, 'lastError': 'KeyError'},
                                                   'swarm': {'last': time.time() - 99999, 'restarts': 0, 'lastError': None}}), \
                patch.object(main, 'QBIT_URL', ''), patch.object(main, 'DATA', Path(root)), \
                patch.object(main, 'read_config', side_effect=OSError):
            threads = {t['name']: t for t in main.thread_health()}
            storage = main.storage_health()
        self.assertEqual(threads['solo']['status'], 'ok')
        self.assertEqual(threads['solo']['restarts'], 2)
        self.assertEqual(threads['swarm']['status'], 'en retard')
        self.assertTrue(threads['guard']['status'].startswith('inactif'))
        self.assertEqual(threads['import-watch']['status'], 'non démarré')
        self.assertIsNotNone(storage['disk'])


if __name__ == '__main__':
    unittest.main()


class ForcedRefreshTests(unittest.TestCase):
    """v1.25.1: switching a torrent to Auto/Forced scrapes it immediately."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.monitor = SwarmMonitor(Path(temp.name) / 'registry.json')
        self.clock = [10_000.0]
        self.monitor.now = lambda: self.clock[0]
        self.h = 'f' * 40
        self.monitor.register_solo(self.h, 'demo', ['http://t.example/announce'])
        self.monitor.pending_imports.clear()
        self.rules = [{'id': 'r', 'enabled': True, 'trackerDomains': ['t.example']}]
        self.calls = []

    def fetch(self, url, infohash):
        self.calls.append(infohash)
        return {'leechers': 4, 'seeders': 9}

    def refresh(self):
        return self.monitor.refresh(self.rules, min_age=900, fetch=self.fetch)

    def test_recently_checked_torrent_is_rescraped_on_request(self):
        self.refresh()
        self.clock[0] += 120                       # 2 min later: normally not due (15 min)
        self.refresh()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.monitor.request_refresh([self.h, 'a' * 40]), {self.h})
        self.assertTrue(self.monitor.refresh_pending(self.h))
        self.refresh()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.monitor.refresh_pending(self.h))

    def test_repeated_requests_are_rate_limited(self):
        self.refresh()
        self.clock[0] += 10                        # 10 s later: inside the 60 s guard
        self.monitor.request_refresh([self.h])
        self.refresh()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.monitor.refresh_pending(self.h))   # request dropped, not kept forever

    def test_mode_change_to_active_requests_a_scrape(self):
        events = {'swarm': threading.Event()}
        with tempfile.TemporaryDirectory() as root, \
                patch.object(main, 'DATA', Path(root)), patch.object(main, 'TORRENT_MODES', {}), \
                patch.object(main, 'ACTIVE_HASHES', {}), patch.object(main, 'SWARM_MONITOR', self.monitor), \
                patch.object(main, 'SWARM_NOW', events['swarm']):
            main.set_torrent_modes({self.h}, 'stop')
            self.assertFalse(events['swarm'].is_set())
            main.set_torrent_modes({self.h}, 'auto')
            self.assertTrue(events['swarm'].is_set())
            self.assertTrue(self.monitor.refresh_pending(self.h))


class StartupRefreshTests(ForcedRefreshTests):
    """v1.25.2: at startup, only torrents without a usable sample are rescraped."""
    def test_stale_hashes(self):
        self.assertEqual(self.monitor.stale_hashes(2700), {self.h})       # never sampled
        self.refresh()
        self.assertEqual(self.monitor.stale_hashes(2700), set())          # fresh sample
        self.clock[0] += 3000
        self.assertEqual(self.monitor.stale_hashes(2700), {self.h})       # older than 45 min

    test_recently_checked_torrent_is_rescraped_on_request = None
    test_repeated_requests_are_rate_limited = None
    test_mode_change_to_active_requests_a_scrape = None
