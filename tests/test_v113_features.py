"""v1.13: alerting, historical tracker observations, secure persistent backup."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from notifications import NotificationCenter
from backup_bundle import build_backup, inspect_backup, schedule_restore, apply_pending_restore, PENDING_NAME
from swarm_monitor import SwarmMonitor
from web_auth import SessionManager
from webtest_helpers import login, auth_headers
import main

HASH = 'a' * 40
RULE = {'id': 'tracker-test', 'enabled': True, 'trackerDomains': ['tracker.example.org']}


class NotificationTests(unittest.TestCase):
    def test_alert_is_emitted_once_and_recovery_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'notifications.json'
            alerts = NotificationCenter(path)
            self.assertTrue(alerts.observe('qbit', True, 'Erreur qBittorrent', 'Rétabli'))
            self.assertFalse(alerts.observe('qbit', True, 'Erreur qBittorrent', 'Rétabli'))
            self.assertEqual(alerts.snapshot()['unread'], 1)
            self.assertTrue(alerts.observe('qbit', False, 'Erreur qBittorrent', 'Rétabli'))
            self.assertEqual(alerts.snapshot()['unread'], 2)
            self.assertEqual(alerts.acknowledge(), 2)
            self.assertEqual(NotificationCenter(path).snapshot()['unread'], 0)
            self.assertEqual(len(NotificationCenter(path).snapshot()['items']), 2)
            self.assertFalse(NotificationCenter(path).observe('qbit', False, 'Erreur', 'Rétabli'))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class HistoryTests(unittest.TestCase):
    def test_history_has_gap_and_never_exposes_tracker_url(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = SwarmMonitor(Path(folder) / 'tracker-registry.json', now=lambda: 1000000)
            secret = 'https://tracker.example.org/announce?passkey=SUPERSECRET'
            monitor.synchronize([{'hash': HASH,'name':'Test torrent'}], lambda h: [{'url': secret}], 'Joal')
            def fetch(url, infohash):
                return 8
            monitor.refresh([RULE], max_checks=3, min_age=0, fetch=fetch)
            history = monitor.history(HASH, 'tracker-test')
            self.assertEqual(history['points'][0]['leechers'], 8)
            self.assertEqual(history['trackerSeries']['tracker.example.org'][0]['leechers'], 8)
            self.assertNotIn('SUPERSECRET', json.dumps(history))
            self.assertNotIn('https://', json.dumps(history))
            restarted = SwarmMonitor(Path(folder) / 'tracker-registry.json', now=lambda: 1000000)
            self.assertEqual(restarted.history(HASH, 'tracker-test')['points'][0]['leechers'], 8)
            def failure(*args):
                raise ValueError('not supported')
            restarted.refresh([RULE], max_checks=3, min_age=0, fetch=failure)
            points = restarted.history(HASH, 'tracker-test')['points']
            self.assertEqual(len(points), 2)
            self.assertIsNone(points[1]['leechers'])
            series = restarted.history(HASH, 'tracker-test')['trackerSeries']['tracker.example.org']
            self.assertEqual([point['leechers'] for point in series], [8, None])


class BackupTests(unittest.TestCase):
    def make_folder(self, root):
        root = Path(root)
        (root / 'clients').mkdir(exist_ok=True)
        (root / 'clients' / 'qbit.client').write_bytes(b'{"client":"qbit"}')
        (root / 'policy.json').write_bytes(Path(__file__).resolve().parents[1].joinpath('config.example.json').read_bytes())
        (root / 'tracker-registry.json').write_text('{"secret":"KEY_PRIVATE"}')
        (root / 'runtime').mkdir(exist_ok=True)
        (root / 'runtime' / 'joal.log').write_text('not persistent')
        return root

    def test_backup_retains_persistent_files_without_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.make_folder(folder)
            blob = build_backup(root)
            entries = inspect_backup(blob)
            self.assertIn('clients/qbit.client', entries)
            self.assertIn('KEY_PRIVATE', entries['tracker-registry.json'].decode())
            self.assertNotIn('runtime/joal.log', entries)
            self.assertEqual(os.stat(root / 'policy.json').st_size, len(entries['policy.json']))

    def test_traversal_rejected_and_no_pending_zip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.make_folder(folder)
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as zf:
                zf.writestr('policy.json', (root/'policy.json').read_bytes())
                zf.writestr('../stolen.txt', b'X')
            with self.assertRaises(ValueError):
                schedule_restore(root, output.getvalue())
            self.assertFalse((root / PENDING_NAME).exists())

    def test_staged_restore_only_after_restart_safe_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.make_folder(folder)
            original = json.loads((root/'policy.json').read_text())
            archive = build_backup(root)
            changed = dict(original, qbitTag='Modified', dryRun=False)
            (root/'policy.json').write_text(json.dumps(changed))
            schedule_restore(root, archive)
            self.assertEqual(json.loads((root/'policy.json').read_text())['qbitTag'], 'Modified')
            self.assertTrue(apply_pending_restore(root))
            restored = json.loads((root/'policy.json').read_text())
            self.assertEqual(restored['qbitTag'], original['qbitTag'])
            self.assertTrue(restored['dryRun'])
            self.assertFalse(restored['handoff']['enabled'])
            self.assertFalse(restored['cleanup']['deleteData'])
            self.assertTrue(list((root/'backups').glob('pre-restore-*.zip')))
            self.assertFalse((root/PENDING_NAME).exists())
            self.assertFalse(apply_pending_restore(root))

    def test_symlinks_not_backed_up(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.make_folder(folder)
            (root/'clients'/'another.client').symlink_to('qbit.client')
            with self.assertRaises(ValueError):
                build_backup(root)


class NewApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        policy = Path(__file__).resolve().parents[1]/'config.example.json'
        (self.root/'policy.json').write_bytes(policy.read_bytes())
        self.patches=[patch.object(main,'DATA',self.root), patch.object(main,'CONFIG',self.root/'policy.json'),
                      patch.object(main,'NOTIFICATIONS',NotificationCenter(self.root/'notifications.json')),
                      patch.object(main,'SWARM_MONITOR',SwarmMonitor(self.root/'tracker-registry.json')),
                      patch.object(main,'HANDOFF_LEDGER',None), patch.object(main,'PASSWORD','local-test-password'),
                      patch.object(main,'AUTH',SessionManager(lambda:main.PASSWORD))]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        self.server=main.ThreadingHTTPServer(('127.0.0.1',0),main.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.thread.join,5);self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def request(self,path,method='GET',payload=None,headers=None):
        req=urllib.request.Request(self.base+path,data=payload,headers=headers or {},method=method)
        try:
            with urllib.request.urlopen(req,timeout=5) as res: return res.status,res.read()
        except urllib.error.HTTPError as error: return error.code,error.read()

    def test_backup_requires_auth_and_csrf(self):
        self.assertEqual(self.request('/api/backup/export')[0],401)
        cookie,csrf=login(self.base,'local-test-password')
        code,body=self.request('/api/backup/export',headers={'Cookie':cookie})
        self.assertEqual(code,200)
        self.assertIn('policy.json',inspect_backup(body))
        self.assertEqual(self.request('/api/backup/restore','POST',body,{'Cookie':cookie,'Content-Type':'application/zip'})[0],403)
        self.assertEqual(self.request('/api/backup/restore','POST',body,{**auth_headers(cookie,csrf),'Content-Type':'application/zip'})[0],202)
        self.assertTrue((self.root/PENDING_NAME).exists())

    def test_restore_refused_outside_dry_run(self):
        config = json.loads((self.root/'policy.json').read_text())
        config['dryRun'] = False
        (self.root/'policy.json').write_text(json.dumps(config))
        cookie, csrf = login(self.base, 'local-test-password')
        blob = build_backup(self.root)
        code, body = self.request('/api/backup/restore', 'POST', blob,
              {**auth_headers(cookie, csrf), 'Content-Type': 'application/zip'})
        self.assertEqual(code, 409)
        self.assertFalse((self.root/PENDING_NAME).exists())

    def test_notifications_auth_and_ack(self):
        self.assertEqual(self.request('/api/notifications')[0],401)
        main.NOTIFICATIONS.observe('tracker',True,'Problème réseau','Rétabli')
        cookie,csrf=login(self.base,'local-test-password')
        status,data=self.request('/api/notifications',headers={'Cookie':cookie})
        self.assertEqual(status,200)
        self.assertEqual(json.loads(data)['unread'],1)
        code,data=self.request('/api/notifications/read','POST',b'{"all":true}',{**auth_headers(cookie,csrf),'Content-Type':'application/json'})
        self.assertEqual(code,200)
        self.assertEqual(json.loads(data)['read'],1)

if __name__ == '__main__': unittest.main()
