"""Regression: zero disables leecher gate; never rewrites unknown to zero."""
import unittest
from unittest.mock import patch
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from test_v114_solo import SoloTests
from solo_torrents import confirmed_leechers
from core import validate
import main

H='f'*40
URL='https://tracker.example.org/announce'

class Qbit:
    def torrents(self,tag):
        return [{'hash':H,'name':'Zero test','progress':1,'amount_left':0,'state':'stoppedUP','ratio':0.0}]
    def trackers(self,h):
        return [{'url':URL,'status':1,'num_leeches':-1}]
    def export(self,h):
        raise AssertionError('dryRun must not export')

class TestZero(unittest.TestCase):
    def rule(self,min_leechers):
        return dict(id='alpha',enabled=True,trackerDomains=['example.org'],minLeechers=min_leechers,
                    minUploadRate=1,maxUploadRate=50,simultaneousSeed=5,targetRatio=0,client='demo.client')
    def conf(self,threshold=0):
        return validate(dict(dryRun=True,qbitTag='Joal',refreshSeconds=180,maxCandidates=10,
                             rules=[self.rule(threshold)]))
    def test_qbit_zero_suppresses_leechers_only_if_stopped(self):
        with patch.object(main,'SWARM_MONITOR',None),patch.object(main,'PENDING_DELETE',{}):
            selection, preview, _, _ = main.find_candidates(Qbit(),self.conf(0))
        row=main.compact_dashboard_rows(preview)[0]
        self.assertTrue(row['selected'])
        self.assertEqual(row['leechers'],-1) # unknown remains unknown
        self.assertTrue(next(c['result'] for c in row['checks'] if c['label']=='Contrôle des leechers'))
        self.assertIn(H,selection['alpha'])
        with patch.object(main,'SWARM_MONITOR',None),patch.object(main,'PENDING_DELETE',{}):
            selection, preview, _, _ = main.find_candidates(Qbit(),self.conf(4))
        self.assertNotIn(H,selection['alpha'])
        self.assertFalse(main.compact_dashboard_rows(preview)[0]['selected'])

    def test_solo_no_scrape_eligible_and_still_true_unknown(self):
        solo=SoloTests('test_dry_run_eligible_does_not_start_process')
        solo.setUp()
        try:
            solo.conf['rules']=[dict(solo.rule,minLeechers=0)]
            solo.monitor.entries[solo.hash]['observations']={}
            solo.monitor.entries[solo.hash]['history']={}
            with patch.object(main,'runner_start',side_effect=AssertionError('dryRun must not launch')):
                main.solo_evaluate()
            row=main.STATUS['solo']['items'][0]
            self.assertTrue(row['selected'])
            self.assertIsNone(row['leechers'])
            self.assertEqual(row['threshold'],0)
            self.assertIn('dryRun',row['reason'])
        finally:solo.tearDown()

    def test_solo_zero_no_scrape_starts_when_explicitly_enabled(self):
        from test_v114_solo import FakeProc
        solo=SoloTests('test_unknown_does_not_trigger_a_session')
        solo.setUp()
        try:
            solo.conf['rules']=[dict(solo.rule,minLeechers=0)]
            solo.conf['dryRun']=False
            solo.monitor.entries[solo.hash]['observations']={}
            started=[]
            def fake_start(rule, selected):
                started.append(rule['id'])
                main.RUNNERS[rule['id']] = FakeProc()
                main.ACTIVE_HASHES[rule['id']] = set(selected)
            with patch.object(main,'runner_start',side_effect=fake_start):
                main.solo_evaluate()
            row=main.STATUS['solo']['items'][0]
            self.assertEqual(started,['solo-alpha'])
            self.assertTrue(row['active'])
            self.assertIsNone(row['leechers'])
        finally:solo.tearDown()

    def test_solo_positive_threshold_unknown_remains_unselected(self):
        solo=SoloTests('test_unknown_does_not_trigger_a_session')
        solo.setUp()
        try:
            solo.conf['rules']=[dict(solo.rule,minLeechers=4)]
            solo.monitor.entries[solo.hash]['observations']={}
            with patch.object(main,'runner_start',side_effect=AssertionError('unexpected')):
                main.solo_evaluate()
            row=main.STATUS['solo']['items'][0]
            self.assertFalse(row['selected'])
            self.assertIsNone(row['leechers'])
        finally: solo.tearDown()

    def test_no_rule_cannot_run_even_with_zero(self):
        solo=SoloTests('test_unknown_does_not_trigger_a_session')
        solo.setUp()
        try:
            solo.conf['rules']=[dict(solo.rule,minLeechers=0,enabled=False)]
            with patch.object(main,'runner_start',side_effect=AssertionError('unexpected')):
                main.solo_evaluate()
            self.assertFalse(main.STATUS['solo']['items'][0]['selected'])
        finally:solo.tearDown()

    def test_zero_semantics_for_function(self):
        self.assertTrue(confirmed_leechers({},0))
        self.assertFalse(confirmed_leechers({},1))

if __name__=='__main__':unittest.main()
