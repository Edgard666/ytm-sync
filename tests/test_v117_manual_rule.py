"""v1.17: per-infohash manual rule assignment, zero leecher semantics, scrape."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))

from core import validate, ConfigError
from solo_torrents import matching_rules, confirmed_leechers
from swarm_monitor import SwarmMonitor
import main

H='a'*40
H2='b'*40
URL='https://one.example/announce?passkey=SECRET'

def rule(id_, threshold, hostname):
    return {'id':id_, 'enabled':True, 'trackerDomains':[hostname],
            'minLeechers':threshold, 'minUploadRate':10, 'maxUploadRate':120,
            'simultaneousSeed':10, 'targetRatio':0, 'client':'test.client'}

def config():
    return validate(dict(dryRun=True,qbitTag='Joal',refreshSeconds=180,
                         maxCandidates=10, rules=[rule('tracker-default',1,'one.example'),
                                                  rule('other',0,'two.example')]))

class Qbit:
    def torrents(self, tag):
        return [dict(hash=H, state='stoppedUP',progress=1.0,amount_left=0,name='Torrent A',ratio=0.1)]
    def trackers(self, hash_):
        return [dict(url=URL,status=2,num_leeches=0)]
    def export(self,*args):
        raise AssertionError('dryRun must not export')

class ManualRuleTests(unittest.TestCase):
    def setUp(self):
        for name,value in [('SWARM_MONITOR',None), ('HANDOFF_LEDGER',None), ('PENDING_DELETE',{})]:
            p=patch.object(main,name,value)
            p.start()
            self.addCleanup(p.stop)

    def test_zero_threshold_is_valid_but_not_negative(self):
        cfg=config()
        self.assertEqual(cfg['rules'][1]['minLeechers'],0)
        cfg['rules'][1]['minLeechersByHash']={H:0}
        self.assertEqual(validate(cfg)['rules'][1]['minLeechersByHash'][H],0)
        cfg['rules'][1]['minLeechers']=-1
        with self.assertRaises(ConfigError): validate(cfg)

    def test_manual_config_persists_and_invalid_rejected(self):
        cfg=config()
        cfg['torrentRuleOverrides']={H:'other'}
        self.assertEqual(validate(cfg)['torrentRuleOverrides'][H],'other')
        cfg['torrentRuleOverrides']={H:'bad rule'}
        with self.assertRaises(ConfigError): validate(cfg)
        cfg['torrentRuleOverrides']={'notahash':'other'}
        with self.assertRaises(ConfigError): validate(cfg)

    def test_default_selection_with_own_domain(self):
        sel,rows,_,_=main.find_candidates(Qbit(),config())
        self.assertEqual(sum(len(v) for v in sel.values()),0)
        self.assertEqual(main.compact_dashboard_rows(rows)[0]['rule'],'tracker-default')
        self.assertIsNone(main.compact_dashboard_rows(rows)[0].get('ruleOverride'))

    def test_explicit_other_rule_applies_only_to_infohash(self):
        cfg=config()
        cfg['torrentRuleOverrides']={H:'other'}
        sel,rows,_,_=main.find_candidates(Qbit(),cfg)
        self.assertIn(H,sel['other'])  # known zero leechers matches minimum zero
        self.assertNotIn(H,sel['tracker-default'])
        item=main.compact_dashboard_rows(rows)[0]
        self.assertEqual(item['rule'],'other')
        self.assertEqual(item['threshold'],0)
        self.assertEqual(item['ruleAssignment'],'manuel')
        self.assertEqual(cfg['rules'][1]['trackerDomains'],['two.example'])
        self.assertNotIn('SECRET',repr(rows))

    def test_rule_disabled_keeps_manual_choice_visible_no_auto_fallback(self):
        cfg=config()
        cfg['rules'][1]['enabled']=False
        cfg['torrentRuleOverrides']={H:'other'}
        _,rows,_,_=main.find_candidates(Qbit(),cfg)
        item=main.compact_dashboard_rows(rows)[0]
        self.assertEqual(item['rule'],'other')
        self.assertFalse(item['eligible'])
        self.assertIn('désactivée',item['reason'])

    def test_solo_manual_rule_overrides_domain(self):
        cfg=config()
        info={'hash':H,'urls':[URL]}
        self.assertEqual(matching_rules(info,cfg['rules'])[0][0]['id'],'tracker-default')
        self.assertEqual(matching_rules(info,cfg['rules'],'other')[0][0]['id'],'other')
        self.assertEqual(matching_rules(info,cfg['rules'],'other')[0][1],[URL])
        self.assertEqual(matching_rules(info,cfg['rules'],'gone'),[])

    def test_scrape_under_manual_rule_works_even_when_host_not_in_rule(self):
        cfg=config()
        with tempfile.TemporaryDirectory() as td:
            tick=[1000.0]
            monitor=SwarmMonitor(Path(td)/'registry.json',now=lambda:tick[0])
            monitor.register_solo(H,'Test',[URL])
            seen=[]
            def fetch(url,h):
                seen.append((url,h))
                return 0
            self.assertEqual(monitor.refresh(cfg['rules'],max_checks=20,min_age=900,
                                              overrides={H:'other'},fetch=fetch),1)
            self.assertEqual(seen,[(URL,H)])
            snap=monitor.snapshot(max_age_seconds=60)
            self.assertEqual(snap['items'][0]['rules']['other']['leechers'],0)
            self.assertNotIn('tracker-default',snap['items'][0]['rules'])
            self.assertNotIn('SECRET',repr(snap))

    def test_zero_is_no_requirement_even_when_unknown(self):
        obs={'leechers':0,'sampledAt':'2026-11-18T10:00:00+00:00',
             'successCount':1,'status':'récent'}
        self.assertTrue(confirmed_leechers(obs,0))
        self.assertTrue(confirmed_leechers({},0))
        self.assertTrue(confirmed_leechers(dict(obs,leechers=None),0))
        self.assertFalse(confirmed_leechers({},1))

if __name__=='__main__': unittest.main()
