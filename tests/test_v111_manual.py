"""v1.11 manual switch API + qBittorrent-first safety regression tests."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from handoff import HandoffLedger
from webtest_helpers import login, auth_headers
import main
from test_v110_handoff import FakeClient, Monitor, policy, H


class ManualSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = [200000.0]
        self.ledger = HandoffLedger(Path(self.temp.name) / 'handoff-state.json', clock=lambda:self.clock[0])
        self.ledger.recovery_pending = False
        self.cfg = policy()
        self.monitor = Monitor()
        self.client = FakeClient('uploading')
        for attr, value in [('HANDOFF_LEDGER',self.ledger),('SWARM_MONITOR',self.monitor),
                            ('RUNNERS',{}),('ACTIVE_HASHES',{}),('LAST_START',{})]:
            ctx=patch.object(main,attr,value); ctx.start(); self.addCleanup(ctx.stop)

    def test_request_persists_across_restart(self):
        self.ledger.queue_manual(H, 'joal')
        fresh=HandoffLedger(self.ledger.path, clock=lambda:self.clock[0])
        self.assertEqual(fresh.pending_manual()[H]['target'], 'joal')
        self.assertEqual(fresh.path.stat().st_mode & 0o777, 0o600)

    def test_qbit_request_starts_manual_stop_and_cooldown(self):
        self.client.row['state']='stoppedUP'
        self.ledger.queue_manual(H,'qbit')
        main._reconcile_handoff(self.client,self.cfg)
        self.assertEqual(self.client.started,1)
        self.assertEqual(self.ledger.cooldown_remaining(H), 3600)
        self.assertEqual(self.ledger.manual_request(H)['status'],'completed')

    def test_qbit_request_preempts_pending_joal(self):
        self.client.row['state']='stoppedUP'
        self.ledger.queue_manual(H,'joal')
        self.ledger.queue_manual(H,'qbit')
        main._reconcile_handoff(self.client,self.cfg)
        self.assertEqual(self.ledger.manual_request(H)['target'],'qbit')
        self.assertEqual(self.client.started,1)
        self.assertEqual(self.client.stopped,0)

    def test_qbit_request_stops_joal_before_start(self):
        self.client.row['state']='stoppedUP'
        self.ledger.mark_stopped_by_controller(H,'alpha')
        self.ledger.queue_manual(H,'qbit')
        calls=[]
        def fake_stop(rule):
            calls.append('stop')
            main.RUNNERS.pop(rule,None)
            main.ACTIVE_HASHES.pop(rule,None)
        orig=self.client.start_torrent
        def start(h):
            calls.append('start'); orig(h)
        self.client.start_torrent=start
        with patch.object(main, 'RUNNERS', {'alpha':object()}), \
             patch.object(main, 'ACTIVE_HASHES', {'alpha':{H}}), \
             patch.object(main, 'runner_stop', side_effect=fake_stop):
            main._reconcile_handoff(self.client,self.cfg)
        self.assertEqual(calls[:2],['stop','start'])

    def test_failed_joal_stop_never_starts_qbit(self):
        self.client.row['state']='stoppedUP'
        self.ledger.queue_manual(H,'qbit')
        with patch.object(main,'RUNNERS',{'alpha':object()}), \
             patch.object(main,'ACTIVE_HASHES',{'alpha':{H}}), \
             patch.object(main,'runner_stop', side_effect=RuntimeError('cannot stop JOAL')):
            with self.assertRaisesRegex(RuntimeError,'cannot stop JOAL'):
                main._reconcile_handoff(self.client,self.cfg)
        self.assertEqual(self.client.started,0)
        self.assertEqual(self.ledger.manual_request(H)['status'],'pending')

    def test_manual_joal_priority_never_bypasses_two_samples(self):
        self.ledger.queue_manual(H,'joal')
        main._reconcile_handoff(self.client,self.cfg)
        self.assertEqual(self.client.stopped,0)
        self.monitor.stamp='2026-11-17T08:15:00+00:00'
        main._reconcile_handoff(self.client,self.cfg)
        self.assertEqual(self.client.stopped,1)
        self.assertEqual(self.ledger.manual_request(H)['status'],'pending')  # JOAL JVM not launched yet

    def test_manual_joal_never_bypasses_cooldown(self):
        self.ledger.mark_returned(H,60,'user preference')
        self.ledger.queue_manual(H,'joal')
        self.ledger.observe(H,'alpha',(8,'2026-11-17T08:00:00+00:00'),4)
        self.monitor.stamp='2026-11-17T08:15:00+00:00'
        main._reconcile_handoff(self.client,self.cfg)
        self.assertEqual(self.client.stopped,0)

    def test_joal_request_completed_only_after_running_process(self):
        self.ledger.queue_manual(H,'joal')
        class FakeProcess:
            def poll(self):return None
        with patch.object(main,'RUNNERS',{'alpha':FakeProcess()}), \
             patch.object(main,'ACTIVE_HASHES',{'alpha':{H}}):
            main._finish_running_joal_requests(self.cfg)
        self.assertEqual(self.ledger.manual_request(H)['status'],'completed')

    def test_joal_pending_invalidated_if_torrent_untagged(self):
        self.ledger.queue_manual(H,'joal')
        self.client.torrents=lambda _: []
        main._reconcile_handoff(self.client,self.cfg)
        self.assertNotIn(H,self.ledger.pending_manual())

    def test_prioritizes_requested_torrent_but_respects_capacity(self):
        other='2'*40
        class TwoTorrents(FakeClient):
            def torrents(self, tag):
                own=dict(self.row)
                second=dict(self.row, hash=other, name='Higher demand', state='uploading')
                return [own,second]
            def stop_torrent(self,h):
                self.stopped+=1
                self.last_stopped=h
                if h==H:self.row['state']='stoppedUP'
            def trackers(self,h):
                return super().trackers(h)
        client=TwoTorrents()
        self.cfg['maxCandidates']=1
        self.cfg['rules'][0]['simultaneousSeed']=1
        class TwoMonitor:
            stamp='2026-11-17T08:00:00+00:00'
            def snapshot(self,age):
                return {'items':[{'infoHash':h,'rules':{'alpha':{
                    'leechers':count,'sampledAt':self.stamp,'status':'récent'}}}
                    for h,count in [(H,6),(other,20)]]}
        monitor=TwoMonitor()
        self.ledger.queue_manual(H,'joal','running')
        with patch.object(main,'SWARM_MONITOR',monitor):
            main._reconcile_handoff(client,self.cfg)
            self.assertEqual(client.stopped,0)
            monitor.stamp='2026-11-17T08:15:00+00:00'
            main._reconcile_handoff(client,self.cfg)
        self.assertEqual(client.stopped,1)
        self.assertEqual(client.last_stopped,H)

    def test_pending_joal_cancels_on_user_qbit_resume(self):
        self.client.row['state']='uploading'
        self.ledger.queue_manual(H,'joal','stopped')
        main._reconcile_handoff(self.client,self.cfg)
        self.assertEqual(self.client.stopped,0)
        self.assertEqual(self.ledger.manual_request(H)['status'],'rejected')
        self.assertEqual(self.ledger.cooldown_remaining(H),3600)

    def test_pending_request_expires_after_24_hours(self):
        self.ledger.queue_manual(H,'joal','running')
        self.clock[0]+=86401
        self.assertNotIn(H,self.ledger.pending_manual())
        self.assertEqual(self.ledger.manual_request(H)['status'],'rejected')

    def test_http_requires_auth_and_refuses_dryrun(self):
        self.cfg['dryRun']=True
        cfg_path=Path(self.temp.name)/'policy.json'
        cfg_path.write_text(json.dumps(self.cfg))
        with patch.object(main,'CONFIG',cfg_path), patch.object(main,'HANDOFF_LEDGER',self.ledger), \
             patch.object(main,'PASSWORD','secret-test-password'):
            server=main.ThreadingHTTPServer(('127.0.0.1',0),main.Handler)
            worker=threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base=f'http://127.0.0.1:{server.server_port}'
            cookie, csrf = login(base, 'secret-test-password')
            url=base+'/api/torrents/handoff'
            data=json.dumps({'infoHash':H,'target':'joal'}).encode()
            def send(auth=True, content_type='application/json', extra=None):
                headers={'Content-Type':content_type}
                if auth:headers.update(auth_headers(cookie, csrf))
                headers.update(extra or {})
                req=urllib.request.Request(url,data=data,headers=headers,method='POST')
                return urllib.request.urlopen(req,timeout=3)
            with self.assertRaises(urllib.error.HTTPError) as err:send(auth=False)
            self.assertEqual(err.exception.code,401)
            with self.assertRaises(urllib.error.HTTPError) as err:send(content_type='text/plain')
            self.assertEqual(err.exception.code,415)
            with self.assertRaises(urllib.error.HTTPError) as err:send(extra={'Sec-Fetch-Site':'cross-site'})
            self.assertEqual(err.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError) as err:send()
            self.assertEqual(err.exception.code,409)
            self.assertFalse(self.ledger.pending_manual())

    def test_http_queue_does_not_directly_switch(self):
        cfg_path=Path(self.temp.name)/'policy.json'
        cfg_path.write_text(json.dumps(self.cfg))
        with patch.object(main,'CONFIG',cfg_path), patch.object(main,'HANDOFF_LEDGER',self.ledger), \
             patch.object(main,'PASSWORD','secret-test-password'), \
             patch.object(main,'QBitClient',lambda *_:self.client):
            server=main.ThreadingHTTPServer(('127.0.0.1',0),main.Handler)
            worker=threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base=f'http://127.0.0.1:{server.server_port}'
            cookie, csrf = login(base, 'secret-test-password')
            headers={'Content-Type':'application/json', **auth_headers(cookie, csrf)}
            req=urllib.request.Request(base+'/api/torrents/handoff',
                   data=json.dumps({'infoHash':H,'target':'joal'}).encode(),headers=headers,method='POST')
            with urllib.request.urlopen(req,timeout=4) as response:
                self.assertEqual(response.status,202)
            self.assertEqual(self.client.stopped,0)
            self.assertEqual(self.ledger.pending_manual()[H]['target'],'joal')
            with self.assertRaises(urllib.error.HTTPError) as err:
                req=urllib.request.Request(base+'/api/torrents/handoff',
                  data=json.dumps({'infoHash':'f'*40,'target':'qbit'}).encode(),headers=headers,method='POST')
                urllib.request.urlopen(req,timeout=4)
            self.assertEqual(err.exception.code,404)


if __name__=='__main__': unittest.main()
