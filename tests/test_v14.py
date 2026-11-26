"""Regression tests added for v1.4 safety & completion behavior."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import main
from core import is_complete_stopped, recorded_ratio, validate, ConfigError


def policy(dry=True, ratio=0, rules=1):
    return {
        'dryRun': dry, 'qbitTag': 'joal', 'refreshSeconds': 180, 'maxCandidates': 250,
        'guardSeconds': 5, 'completedTag': 'JoalCompleted',
        'rules': [dict(id=f'alpha-{i}', enabled=True, trackerDomains=[f'tracker{i}.example.net'],
                       minLeechers=2, minUploadRate=30, maxUploadRate=100,
                       simultaneousSeed=5, client='valid.client', targetRatio=ratio)
                  for i in range(rules)]
    }


def torrent(state='stoppedUP', ratio=0.5, progress=1.0):
    return {'hash': 'a' * 40, 'name': 'My torrent', 'progress': progress,
            'amount_left': 0 if progress == 1 else 50, 'state': state, 'ratio': ratio}


class StubQBit:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [torrent()]
        self.events = []
        self.export_calls = 0

    def torrents(self, tag):
        assert tag == 'joal'
        return self.rows

    def trackers(self, hash_):
        return [dict(url=f'https://tracker{i}.example.net/announce/PRIVATE',
                     status=2, num_leeches=5) for i in range(2)]

    def export(self, hash_):
        self.export_calls += 1
        return b'fake'

    def add_tag(self, hash_, tag):
        self.events.append(('add', hash_, tag))

    def remove_tag(self, hash_, tag):
        self.events.append(('remove', hash_, tag))


class OnePassEvent:
    def __init__(self):
        self.done = False

    def is_set(self):
        return self.done

    def wait(self, duration):
        self.done = True


class V14Tests(unittest.TestCase):
    def test_only_stopped_complete_torrents_are_allowed(self):
        self.assertTrue(is_complete_stopped(torrent()))
        self.assertTrue(is_complete_stopped(torrent(state='pausedUP')))
        for state in ('uploading', 'stalledUP', 'queuedUP', 'forcedUP',
                      'stoppedDL', 'checkingUP', 'unknown', 'error'):
            with self.subTest(state=state):
                self.assertFalse(is_complete_stopped(torrent(state=state)))
        self.assertFalse(is_complete_stopped(torrent(progress=0.5)))
        self.assertFalse(is_complete_stopped(dict(torrent(), amount_left=1)))
        self.assertFalse(is_complete_stopped(dict(torrent(), progress='nan')))

    def test_ratio_rejects_unknown_or_nonfinite(self):
        for value in (-1, None, '1.2', True, float('nan'), float('inf')):
            with self.subTest(value=value):
                self.assertIsNone(recorded_ratio({'ratio': value}))
        self.assertEqual(recorded_ratio({'ratio': 1.20}), 1.20)

    def test_migrates_v13_and_validates_new_fields(self):
        p = policy()
        del p['guardSeconds']
        del p['completedTag']
        del p['rules'][0]['targetRatio']
        validate(p)
        self.assertEqual(p['guardSeconds'], 5)
        self.assertEqual(p['completedTag'], 'JoalCompleted')
        self.assertEqual(p['rules'][0]['targetRatio'], 0)
        p['rules'][0]['targetRatio'] = float('nan')
        with self.assertRaises(ConfigError):
            validate(p)

    def test_dry_run_requires_stopped_and_never_exports_or_tags(self):
        with tempfile.TemporaryDirectory() as folder:
            conf = Path(folder) / 'policy.json'
            conf.write_text(json.dumps(policy(dry=True)))
            stub = StubQBit([torrent(state='uploading')])
            with patch.object(main, 'CONFIG', conf), patch.object(main, 'DATA', Path(folder)), \
                    patch.object(main, 'RUNNERS', {}), patch.object(main, 'ACTIVE_HASHES', {}), \
                    patch.object(main, 'LAST_START', {}):
                selection, rows, tagged, completions = main.find_candidates(stub, main.read_config())
            self.assertEqual(tagged, 1)
            self.assertEqual(selection['alpha-0'], {})
            self.assertEqual(completions, set())
            self.assertIn('arrêt obligatoire', rows[0]['reason'])
            self.assertEqual(stub.export_calls, 0)

    def test_only_one_rule_can_select_a_torrent(self):
        with tempfile.TemporaryDirectory() as folder:
            conf = Path(folder) / 'policy.json'
            conf.write_text(json.dumps(policy(dry=True, rules=2)))
            stub = StubQBit()
            with patch.object(main, 'CONFIG', conf):
                selection, rows, _, _ = main.find_candidates(stub, main.read_config())
            self.assertEqual(sum(len(x) for x in selection.values()), 1)
            self.assertEqual(sum(bool(row['selected']) for row in rows), 1)
            self.assertIn('Autre règle applicable', rows[1]['reason'])

    def test_ratio_target_tags_only_when_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            conf = path / 'policy.json'
            conf.write_text(json.dumps(policy(dry=False, ratio=1.2)))
            stub = StubQBit([torrent(ratio=1.25)])
            with patch.object(main, 'CONFIG', conf), patch.object(main, 'DATA', path), \
                    patch.object(main, 'RUNNERS', {}), patch.object(main, 'ACTIVE_HASHES', {}), \
                    patch.object(main, 'LAST_START', {}):
                main.run_once(stub)
            self.assertEqual(stub.events, [('add', 'a'*40, 'JoalCompleted'), ('remove', 'a'*40, 'joal')])
            self.assertEqual(stub.export_calls, 0)
            stub = StubQBit([torrent(state='uploading', ratio=1.25)])
            with patch.object(main, 'CONFIG', conf):
                _, _, _, completed = main.find_candidates(stub, main.read_config())
            self.assertEqual(completed, set())
            self.assertEqual(stub.events, [])

    def test_select_by_category_and_keep_it_after_completion(self):
        conf = validate(dict(policy(dry=False, ratio=1.2), qbitFilterMode='category', qbitCategory='Films HD'))
        stub = StubQBit()
        rows = [dict(torrent(ratio=1.25), category='Films HD', tags='other'),
                dict(torrent(), category='Films HD/4K', tags='joal'),
                dict(torrent(), category='Films HD', tags='JoalCompleted')]
        stub.torrents = Mock(return_value=rows)
        self.assertEqual(main.watched_torrents(stub, conf), rows[:1])
        stub.torrents.assert_called_once_with('Films HD', field='category')
        with tempfile.TemporaryDirectory() as folder, patch.object(main, 'read_config', return_value=conf), \
                patch.object(main, 'DATA', Path(folder)), patch.object(main, 'RUNNERS', {}):
            main.run_once(stub)
        self.assertEqual(stub.events, [('add', 'a'*40, 'JoalCompleted')])

    def test_multiple_categories_use_exact_names_and_preserve_commas(self):
        conf = validate(dict(policy(), qbitFilterMode='category', qbitCategory=' Films HD \n\nSéries, VO\nFilms HD'))
        rows = [dict(torrent(), category='Films HD'), dict(torrent(), category='Séries, VO', hash='b'*40),
                dict(torrent(), category='Films HD/4K', hash='c'*40)]
        stub = StubQBit()
        stub.torrents = Mock(return_value=rows)
        self.assertEqual(main.watched_torrents(stub, conf), rows[:2])
        self.assertEqual(stub.torrents.call_count, 2)
        self.assertTrue(main.matches_watch(rows[1], conf))
        self.assertFalse(main.matches_watch(rows[2], conf))

    def test_either_watches_union_once_and_excludes_completed(self):
        conf = validate(dict(policy(), qbitFilterMode='either', qbitCategory='Films\nSéries'))
        examples = [('Films',''), ('Autre','abc, joal'), ('Séries','joal'), ('Autre','joal2'),
                    ('Films/4K',''), ('Films','joal, JoalCompleted')]
        rows = [dict(torrent(), hash=f'{i:040x}', category=cat, tags=tags)
                for i, (cat, tags) in enumerate(examples)]
        client = Mock()
        client.torrents_all.return_value = rows
        self.assertEqual(main.watched_torrents(client, conf), rows[:3])
        client.torrents_all.assert_called_once_with()

    def test_watch_filter_migration_and_validation(self):
        self.assertEqual(validate(policy())['qbitFilterMode'], 'tag')
        with self.assertRaises(ConfigError):
            validate(dict(policy(), qbitFilterMode='category', qbitCategory=''))
        with self.assertRaises(ConfigError):
            validate(dict(policy(), qbitFilterMode='unknown'))
        client = main.QBitClient('http://localhost:8080', '', '')
        with patch.object(client, 'call', return_value=b'[]') as call:
            client.torrents('Films HD', field='category')
            call.assert_called_once_with('torrents/info', {'category':'Films HD'})

    def test_ratio_dryrun_never_changes_tags(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            conf = path / 'policy.json'
            conf.write_text(json.dumps(policy(dry=True, ratio=1.2)))
            stub = StubQBit([torrent(ratio=1.25)])
            with patch.object(main, 'CONFIG', conf), patch.object(main, 'DATA', path), \
                    patch.object(main, 'RUNNERS', {}), patch.object(main, 'ACTIVE_HASHES', {}), \
                    patch.object(main, 'LAST_START', {}):
                main.run_once(stub)
            self.assertFalse(stub.events)
            self.assertFalse(stub.export_calls)

    def test_guard_stops_when_qbit_torrent_resumes(self):
        with tempfile.TemporaryDirectory() as folder:
            conf = Path(folder) / 'policy.json'
            conf.write_text(json.dumps(policy(dry=False)))
            proc = Mock()
            proc.poll.side_effect = lambda: 0 if proc.terminate.called else None
            proc.wait.return_value = 0
            mock_client = StubQBit([torrent(state='uploading')])
            snapshot = {'rules': [{'id':'alpha-0', 'state':'en cours'}],
                        'torrents':[{'rule':'alpha-0', 'selected':True, 'reason':'Retenu'}],
                        'totals': {'selected': 1}, 'qbitConnected': True, 'error': None}
            with patch.object(main, 'CONFIG', conf), patch.object(main, 'RUNNERS', {'alpha-0':proc}), \
                    patch.object(main, 'ACTIVE_HASHES', {'alpha-0': {'a'*40}}), \
                    patch.object(main, 'STATUS', snapshot), patch.object(main, 'STOP', OnePassEvent()), \
                    patch.object(main, 'QBitClient', return_value=mock_client), \
                    patch.object(main, 'POLL_NOW'):
                main.guard_loop()
                self.assertEqual(main.RUNNERS, {})
                self.assertEqual(main.ACTIVE_HASHES, {})
                self.assertEqual(snapshot['totals']['selected'], 0)
                self.assertIn('suspendue', snapshot['rules'][0]['state'])
                self.assertFalse(snapshot['torrents'][0]['selected'])
                proc.terminate.assert_called_once()

    def test_guard_fails_closed_when_qbit_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            conf = Path(folder) / 'policy.json'
            conf.write_text(json.dumps(policy(dry=False)))
            proc = Mock()
            proc.poll.side_effect = lambda: 0 if proc.terminate.called else None
            proc.wait.return_value = 0
            qbit = Mock()
            qbit.torrents.side_effect = RuntimeError('API hors service')
            snapshot = {'rules': [{'id':'alpha-0', 'state':'en cours'}],
                        'torrents':[{'rule':'alpha-0', 'selected':True, 'reason':'Retenu'}],
                        'totals': {'selected': 1}, 'qbitConnected': True, 'error': None}
            with patch.object(main, 'CONFIG', conf), patch.object(main, 'RUNNERS', {'alpha-0':proc}), \
                    patch.object(main, 'ACTIVE_HASHES', {'alpha-0': {'a'*40}}), \
                    patch.object(main, 'STATUS', snapshot), patch.object(main, 'STOP', OnePassEvent()), \
                    patch.object(main, 'QBitClient', return_value=qbit), \
                    patch.object(main, 'POLL_NOW'):
                with patch.object(main.LOG, 'exception'):
                    main.guard_loop()
                self.assertEqual(main.RUNNERS, {})
                self.assertFalse(snapshot['qbitConnected'])
                self.assertEqual(snapshot['totals']['selected'], 0)
                proc.terminate.assert_called_once()

    def test_failed_java_stop_keeps_process_handle(self):
        proc = Mock()
        proc.poll.return_value = None
        proc.terminate.side_effect = PermissionError('blocked')
        with patch.object(main, 'RUNNERS', {'alpha-0':proc}), \
                patch.object(main, 'ACTIVE_HASHES', {'alpha-0': {'a'*40}}):
            with self.assertRaises(PermissionError):
                main.runner_stop('alpha-0')
            self.assertIn('alpha-0', main.RUNNERS)
            self.assertIn('alpha-0', main.ACTIVE_HASHES)


if __name__ == '__main__':
    unittest.main()
