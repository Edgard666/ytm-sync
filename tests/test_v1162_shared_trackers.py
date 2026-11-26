"""v1.16.2: overlapping tracker domains use one, least-restrictive rule."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main
from core import validate, rule_preference
from solo_torrents import applicable_rule, matching_rules

H = 'a' * 40
H2 = 'b' * 40

def rule(id_, minimum, **overrides):
    output = dict(id=id_, enabled=True, trackerDomains=['shared.example.org'],
                  minLeechers=minimum, minUploadRate=1, maxUploadRate=100,
                  simultaneousSeed=10, targetRatio=0, client='example.client')
    output.update(overrides)
    return output

def config(rules, maximum=10):
    return validate(dict(dryRun=True, qbitTag='Joal', maxCandidates=maximum,
                         refreshSeconds=180, rules=rules))

class QBit:
    def __init__(self, count=15, hashes=(H,)):
        self.count, self.hashes = count, hashes
    def torrents(self, tag):
        assert tag == 'Joal'
        return [dict(hash=h, name=h[:2], state='stoppedUP', progress=1.0,
                     amount_left=0, ratio=0.1) for h in self.hashes]
    def trackers(self, hash_):
        return [dict(url='https://shared.example.org/announce?passkey=PRIVATE',
                     num_leeches=self.count, status=2)]
    def export(self, hash_):
        raise AssertionError('dryRun must not export metadata')

class SharedTrackerTests(unittest.TestCase):
    def setUp(self):
        self.patchers = [patch.object(main,'SWARM_MONITOR',None),
                         patch.object(main,'HANDOFF_LEDGER',None),
                         patch.object(main,'PENDING_DELETE',{})]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_domains_shared_across_rules_are_valid(self):
        cfg = config([rule('strict',8), rule('lenient',4)])
        self.assertEqual(len(cfg['rules']), 2)
        cfg['rules'][0]['trackerDomains'] = ['deep.shared.example.org']
        self.assertEqual(len(validate(cfg)['rules']), 2)

    def test_lenient_selected_even_if_strict_is_first(self):
        cfg=config([rule('strict',8), rule('lenient',4)])
        selection, rows, tagged, _ = main.find_candidates(QBit(15), cfg)
        self.assertEqual(tagged,1)
        self.assertEqual(set(selection['lenient']),{H})
        self.assertEqual(selection['strict'],{})
        self.assertEqual(sum(len(x) for x in selection.values()),1)
        self.assertEqual(main.compact_dashboard_rows(rows)[0]['rule'],'lenient')
        self.assertNotIn('PRIVATE',json.dumps(rows))

    def test_effective_hash_override_takes_precedence(self):
        cfg=config([rule('default',4),rule('override',12,minLeechersByHash={H:2})])
        selection, _, _, _ = main.find_candidates(QBit(15),cfg)
        self.assertIn(H,selection['override'])

    def test_rule_fallback_when_lenient_quota_is_full(self):
        cfg=config([rule('lenient',4,simultaneousSeed=1),
                    rule('other',8,simultaneousSeed=2)])
        sel,rows,_,_=main.find_candidates(QBit(15,(H,H2)),cfg)
        self.assertEqual(sum(len(x) for x in sel.values()),2)
        self.assertEqual(len(sel['lenient']),1)
        self.assertEqual(len(sel['other']),1)
        self.assertEqual(len(main.compact_dashboard_rows(rows)),2)

    def test_global_cap_is_one_torrent_not_one_rule(self):
        cfg=config([rule('lenient',4),rule('other',8)], maximum=1)
        sel,rows,_,_=main.find_candidates(QBit(15,(H,H2)),cfg)
        self.assertEqual(sum(len(x) for x in sel.values()),1)
        self.assertEqual(sum(r['selected'] for r in rows),1)

    def test_applies_one_solo_rule_independent_of_config_order(self):
        info=dict(hash=H,urls=['https://shared.example.org/announce?passkey=PRIVATE'])
        rules=[rule('strict',8),rule('lenient',4)]
        self.assertEqual(applicable_rule(info,rules)[0]['id'],'lenient')
        self.assertEqual(applicable_rule(info,list(reversed(rules)))[0]['id'],'lenient')
        self.assertEqual(len(matching_rules(info,rules)),2)

    def test_tiebreak_for_equal_threshold_uses_no_ratio_limit(self):
        r1=rule('ratio-bound',4,targetRatio=1.2)
        r2=rule('unlimited',4,targetRatio=0)
        self.assertLess(rule_preference(r2,H),rule_preference(r1,H))

if __name__=='__main__': unittest.main()
