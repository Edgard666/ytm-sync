"""v1.30.2: remove the selected JOAL-only torrents in one request, and forget their settings."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_import_v15 as web  # noqa: E402
import test_v116_batch_permissions as batch  # noqa: E402
import main  # noqa: E402


class BatchRemove(unittest.TestCase):
    setUp, tearDown, request = web.WebImportTests.setUp, web.WebImportTests.tearDown, web.WebImportTests.request

    def add(self, *names):
        ct, body = batch.multi([(f'{n}.torrent', batch.payload(n)) for n in names])
        status, result = self.request('/api/import/add', ct=ct, body=body)
        self.assertEqual(status, 200)
        if 'results' not in result:                       # one file: single-import answer
            return [result['hash']]
        return [row['hash'] for row in result['results']]

    def remove(self, hashes):
        return self.request('/api/solo/delete', ct='application/json', body=json.dumps({'infoHashes': hashes}).encode())

    def test_selection_removed_in_one_request(self):
        with patch.object(main, 'STATUS', {'solo': {'count': 0, 'active': 0, 'items': []}}):
            one, two, three = self.add('one', 'two', 'three')
            unknown = 'f' * 40
            status, result = self.remove([one, three, unknown, one])
            self.assertEqual(status, 200)
            self.assertEqual(result['removed'], [one, three])
            self.assertEqual(result['missing'], [unknown])
            _, state = self.request('/api/state')
            self.assertEqual([row['infoHash'] for row in state['solo']['items']], [two])
            self.assertEqual(state['solo']['count'], 1)
        left = list((Path(self.folder.name) / 'torrents').glob('*.torrent'))
        self.assertEqual(len(left), 1)

    def test_single_removal_unchanged(self):
        with patch.object(main, 'STATUS', {'solo': {'count': 0, 'active': 0, 'items': []}}):
            (one,) = self.add('solo')
            status, _ = self.request('/api/solo/delete', ct='application/json',
                                     body=json.dumps({'infoHash': one}).encode())
            self.assertEqual(status, 200)
            status, _ = self.request('/api/solo/delete', ct='application/json',
                                     body=json.dumps({'infoHash': one}).encode())
            self.assertEqual(status, 404)

    def test_invalid_requests_refused(self):
        for body in ({'infoHashes': []}, {'infoHashes': ['x']}, {'infoHashes': ['a' * 40] * 501},
                     {'infoHashes': 'a' * 40}, {'infoHash': 'a' * 40, 'infoHashes': ['a' * 40]}):
            status, _ = self.request('/api/solo/delete', ct='application/json', body=json.dumps(body).encode())
            self.assertEqual(status, 400, body)

    def settings_for(self, h, other):
        conf = json.loads(main.CONFIG.read_text())
        conf['torrentRuleOverrides'] = {h: conf['rules'][0]['id'], other: conf['rules'][0]['id']}
        conf['rules'][0]['minLeechersByHash'] = {h: 3, other: 5}
        main.CONFIG.write_text(json.dumps(conf))
        main.set_torrent_modes({h, other}, 'forced')

    def test_settings_of_removed_torrents_are_forgotten(self):
        with patch.object(main, 'STATUS', {'solo': {'count': 0, 'active': 0, 'items': []}}), \
                patch.object(main, 'TORRENT_MODES', {}), patch.object(main, 'SWARM_MONITOR', None):
            gone, kept = self.add('gone', 'kept')
            self.settings_for(gone, kept)
            self.assertEqual(self.remove([gone])[0], 200)
            conf = json.loads(main.CONFIG.read_text())
            self.assertEqual(set(conf['torrentRuleOverrides']), {kept})
            self.assertEqual(set(conf['rules'][0]['minLeechersByHash']), {kept})
            self.assertEqual(set(main.TORRENT_MODES), {kept})
            saved = json.loads((Path(self.folder.name) / 'simulation-modes.json').read_text())
            self.assertEqual(set(saved), {kept})

    def test_settings_kept_when_the_torrent_is_also_in_qbittorrent(self):
        with patch.object(main, 'STATUS', {'solo': {'count': 0, 'active': 0, 'items': []}}), \
                patch.object(main, 'TORRENT_MODES', {}), patch.object(main, 'SWARM_MONITOR', None):
            both, other = self.add('both', 'other')
            self.settings_for(both, other)
            web.StubQbit.inventory = [{'hash': both, 'name': 'both'}]
            self.assertEqual(self.remove([both])[0], 200)
            self.assertIn(both, json.loads(main.CONFIG.read_text())['torrentRuleOverrides'])
            self.assertEqual(main.TORRENT_MODES.get(both), 'forced')

    def test_settings_kept_when_qbittorrent_cannot_be_checked(self):
        with patch.object(main, 'STATUS', {'solo': {'count': 0, 'active': 0, 'items': []}}), \
                patch.object(main, 'TORRENT_MODES', {}), patch.object(main, 'SWARM_MONITOR', None):
            gone, other = self.add('gone', 'other')
            self.settings_for(gone, other)
            web.StubQbit.inventory = 'indisponible'
            self.assertEqual(self.remove([gone])[0], 200)
            self.assertIn(gone, json.loads(main.CONFIG.read_text())['torrentRuleOverrides'])


if __name__ == '__main__':
    unittest.main()
