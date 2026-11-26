"""Standalone web imports: no qBittorrent upload, persistent metadata, safe pausing."""
import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from core import encode_bencode, get_info_slice
from solo_torrents import SoloLibrary, applicable_rule, confirmed_leechers, ImportErrorValue
from swarm_monitor import SwarmMonitor
from backup_bundle import build_backup, inspect_backup
import main

RULE = {'id':'alpha','enabled':True,'trackerDomains':['example.org'],
        'minLeechers':4,'minLeechersByHash':{},'minUploadRate':20,'maxUploadRate':100,
        'simultaneousSeed':5,'targetRatio':0,'client':'demo.client'}

def payload():
    return encode_bencode({b'announce':b'udp://tracker.example.org:6969/announce/PRIVATE_TOKEN',
        b'info':{b'name':b'example',b'length':999,b'pieces':b'X'*20,b'piece length':16384}})


def configuration(dry_run=True):
    return {'dryRun':dry_run,'qbitTag':'Joal','refreshSeconds':180,'maxCandidates':10,
            'rules':[RULE],'completedTag':'JoalCompleted','guardSeconds':5,
            'handoff':{'enabled':False,'confirmations':2,'cooldownMinutes':60,'fastTrackMargin':5},
            'swarm':{'enabled':True,'intervalMinutes':15,'maxAgeMinutes':45}}


class FakeProc:
    def __init__(self): self.dead = False
    def poll(self): return 0 if self.dead else None


class SoloTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lib = SoloLibrary(self.root/'torrents')
        self.monitor = SwarmMonitor(self.root/'tracker-registry.json')
        self.rule = dict(RULE)
        self.conf = configuration()
        self.info = self.lib.add(payload(), [self.rule])
        self.hash = self.info['hash']
        self.monitor.register_solo(self.hash, self.info['name'],
                                   ['udp://tracker.example.org:6969/announce/PRIVATE_TOKEN'])
        self.patches = [patch.object(main,'SOLO_LIBRARY',self.lib),
                        patch.object(main,'SWARM_MONITOR',self.monitor),
                        patch.object(main,'RUNNERS',{}),
                        patch.object(main,'ACTIVE_HASHES',{}),
                        patch.object(main,'LAST_START',{}),
                        patch.object(main,'STATUS',{'solo':{},'torrents':[]}),
                        patch.object(main,'read_config',lambda: self.conf),
                        patch.object(main,'DATA',self.root),
                        patch.object(main,'QBIT_URL','')]
        for p in self.patches: p.start()
    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()
    def obs(self, count):
        t = time.time()
        self.monitor.entries[self.hash]['observations'] = {'alpha':{'sampledAt':t,'attemptedAt':t,
            'leechers':count,'status':'ok','trackerCount':1,'successCount':1,'source':'scrape UDP'}}
        self.monitor.entries[self.hash]['history'] = {'alpha':[{'time':t,'leechers':count,'status':'ok'}]}
    def test_library_stores_exact_torrent_private_and_does_not_need_downloads(self):
        target = self.root/'torrents'/(self.hash+'.torrent')
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(),payload())
        self.assertEqual(target.stat().st_mode & 0o777,0o660)  # v1.23 : passkeys non lisibles par tous
        self.assertEqual(len(self.lib.list([self.rule])),1)
        self.assertEqual(self.lib.list([])[0]['hash'],self.hash)
        with self.assertRaises(ImportErrorValue):
            self.lib.add(payload(), [self.rule])
    def test_scrape_library_not_purged_by_qbittorrent_sync(self):
        self.monitor.synchronize([],lambda _: [],'Joal')
        self.assertIn(self.hash,self.monitor.entries)
        self.assertEqual(self.monitor.entries[self.hash]['mode'],'solo')
        self.assertNotIn('PRIVATE_TOKEN',json.dumps(self.monitor.snapshot()))
    def test_dry_run_eligible_does_not_start_process(self):
        self.obs(11)
        with patch.object(main,'runner_start',side_effect=AssertionError('must never run')):
            main.solo_evaluate()
        self.assertEqual(main.STATUS['solo']['count'],1)
        self.assertEqual(main.STATUS['solo']['active'],0)
        self.assertIn('dryRun',main.STATUS['solo']['items'][0]['reason'])
    def test_starts_at_threshold_plus_five_then_stops_below_threshold(self):
        self.conf['dryRun'] = False
        self.obs(9)
        def fake_start(rule, selected):
            main.RUNNERS[rule['id']] = FakeProc()
            main.ACTIVE_HASHES[rule['id']] = set(selected)
            main.LAST_START[rule['id']] = time.monotonic()
        def fake_stop(rid):
            main.RUNNERS[rid].dead = True
            main.RUNNERS.pop(rid)
            main.ACTIVE_HASHES.pop(rid,None)
        with patch.object(main,'runner_start',side_effect=fake_start), patch.object(main,'runner_stop',side_effect=fake_stop):
            main.solo_evaluate()
            self.assertEqual(main.STATUS['solo']['active'],1)
            self.assertIn('solo-alpha',main.RUNNERS)
            self.obs(3)
            main.solo_evaluate()
            self.assertEqual(main.STATUS['solo']['active'],0)
            self.assertNotIn('solo-alpha',main.RUNNERS)
    def test_qbit_api_failure_does_not_stop_solo_runner(self):
        main.RUNNERS['solo-alpha'] = FakeProc()
        main.ACTIVE_HASHES['solo-alpha'] = {self.hash}
        main.RUNNERS['alpha'] = FakeProc()
        main.ACTIVE_HASHES['alpha'] = {'f'*40}
        stopped = []
        def fake_stop(rid):
            stopped.append(rid)
            main.RUNNERS.pop(rid,None)
            main.ACTIVE_HASHES.pop(rid,None)
        with patch.object(main,'runner_stop',side_effect=fake_stop):
            self.assertTrue(main.stop_qbit_runners())
        self.assertEqual(stopped,['alpha'])
        self.assertIn('solo-alpha', main.RUNNERS)

    def test_qbittorrent_duplicate_blocks_standalone_selection(self):
        self.conf['dryRun']=False
        self.obs(30)
        client = Mock()
        client.torrents_all.return_value = [{'hash': 'f'*64, 'infohash_v1': self.hash.upper(), 'tags':'', 'state':'stoppedUP'}]
        with patch.object(main, 'QBIT_URL', 'http://qbit'), patch.object(main,'runner_start') as start:
            main.solo_evaluate(client)
            start.assert_not_called()
            client.torrents_all.assert_called_once_with()
        item=main.STATUS['solo']['items'][0]
        self.assertIn('Conflit', item['reason'])
        self.assertFalse(item['selected'])

    def test_new_duplicate_or_unreachable_inventory_stops_running_import(self):
        self.conf['dryRun'] = False
        self.obs(10)
        for answer in ([{'hash': self.hash}], OSError('offline'), {}):
            with self.subTest(answer=answer):
                client = Mock()
                if isinstance(answer, Exception):
                    client.torrents_all.side_effect = answer
                else:
                    client.torrents_all.return_value = answer
                main.RUNNERS['solo-alpha'] = FakeProc()
                main.ACTIVE_HASHES['solo-alpha'] = {self.hash}
                def stop(rid):
                    main.RUNNERS.pop(rid)
                    main.ACTIVE_HASHES.pop(rid)
                with patch.object(main, 'QBIT_URL', 'http://qbit'), patch.object(main, 'runner_stop', side_effect=stop) as stopped:
                    main.solo_evaluate(client)
                    stopped.assert_called_once_with('solo-alpha')
                self.assertFalse(main.STATUS['solo']['items'][0]['active'])

    def test_unknown_does_not_trigger_a_session(self):
        self.conf['dryRun']=False
        with patch.object(main,'runner_start',side_effect=AssertionError('unexpected start')):
            main.solo_evaluate()
        self.assertEqual(main.STATUS['solo']['active'],0)
        self.assertIn('inconnus',main.STATUS['solo']['items'][0]['reason'])
    def test_backup_contains_torrent_and_restores_original_bytes(self):
        (self.root/'policy.json').write_text(json.dumps(configuration()))
        zip_blob=build_backup(self.root)
        files=inspect_backup(zip_blob)
        self.assertEqual(files['torrents/'+self.hash+'.torrent'],payload())
    def test_rule_disabled_remains_in_library_but_cannot_activate(self):
        self.conf['rules']=[dict(RULE,enabled=False)]
        self.obs(20)
        main.solo_evaluate()
        self.assertEqual(main.STATUS['solo']['count'],1)
        self.assertIn('Aucune règle active',main.STATUS['solo']['items'][0]['reason'])
    def test_delete_removes_only_metadata(self):
        self.lib.delete(self.hash)
        self.monitor.forget_solo(self.hash)
        self.assertFalse((self.root/'torrents'/(self.hash+'.torrent')).exists())
        self.assertNotIn(self.hash,self.monitor.entries)

if __name__=='__main__': unittest.main()
