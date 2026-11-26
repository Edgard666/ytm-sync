import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_v19_ranking as ranking
import test_v114_solo as solo_fixture
import main
from core import validate


class SimulationModesTests(unittest.TestCase):
    def test_batch_stop_force_and_auto(self):
        client = ranking.Client([0, 9])
        config = validate(ranking.configuration())
        first, second = [t['hash'] for t in client.torrents('joal')]
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DATA', Path(tmp)), patch.object(main, 'TORRENT_MODES', {}):
            main.set_torrent_modes({first, second}, 'stop')
            main.set_torrent_modes({first}, 'forced')
            selected, _, _, _ = main.find_candidates(client, main.simulation_config(config))
            self.assertEqual(selected[main.FORCED_RULE], {first: b''})
            self.assertFalse(selected['beta'])
            main.set_torrent_modes({first, second}, 'auto')
            selected, _, _, _ = main.find_candidates(client, main.simulation_config(config))
            self.assertEqual(set(selected['beta']), {second})
            self.assertEqual(json.loads((Path(tmp) / 'simulation-modes.json').read_text()), {})

    def test_forced_import_preserves_zero_limit_with_no_client(self):
        fixture = solo_fixture.SoloTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        idle = dict(fixture.rule, trackerDomains=[], id='idle', minUploadRate=0, maxUploadRate=0, client=None)
        fixture.conf.update(rules=[idle], torrentRuleOverrides={fixture.hash:'idle'})
        with patch.object(main, 'TORRENT_MODES', {fixture.hash:'forced'}):
            effective = main.simulation_config(fixture.conf)
            rid = effective['torrentRuleOverrides'][fixture.hash]
            policy = next(r for r in effective['rules'] if r['id'] == rid)
            self.assertEqual((policy['minUploadRate'], policy['maxUploadRate']), (0, 0))
            main.solo_evaluate()
            self.assertTrue(main.STATUS['solo']['items'][0]['selected'])

    def test_solo_override_without_enabled_rule(self):
        fixture = solo_fixture.SoloTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.conf['rules'] = [dict(fixture.conf['rules'][0], enabled=False)]
        with patch.object(main, 'TORRENT_MODES', {fixture.hash: 'forced'}):
            main.solo_evaluate()
            self.assertTrue(main.STATUS['solo']['items'][0]['selected'])
            main.set_torrent_modes({fixture.hash}, 'stop')
            main.solo_evaluate()
            self.assertFalse(main.STATUS['solo']['items'][0]['selected'])
