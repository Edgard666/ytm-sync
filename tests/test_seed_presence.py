import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from core import encode_bencode
import seed_presence
from tracker_scrape import AnnounceRejected, zero_transfer_announce


class PresenceTests(unittest.TestCase):
    def test_session_starts_and_stops_with_stable_identity_and_zero_bytes(self):
        calls = []
        handlers = {}
        opener = Mock()
        def answer(target, timeout):
            params = parse_qs(urlsplit(target).query, encoding='latin-1')
            calls.append(params)
            if len(calls) == 1:
                handlers[seed_presence.signal.SIGTERM]()
            return io.BytesIO(encode_bencode({b'interval': 90}))
        opener.open.side_effect = answer
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / ('a' * 40 + '.torrent')).write_bytes(encode_bencode({
                b'announce': b'https://example.org/announce', b'info': {b'name': b'test'}}))
            with patch('tracker_scrape.public_ip_only'), patch('tracker_scrape.urllib.request.build_opener', return_value=opener), patch('seed_presence.signal.signal', side_effect=handlers.__setitem__):
                seed_presence.run(folder, folder / 'upload.json')
        self.assertEqual([v['event'] for v in calls], [['started'], ['stopped']])
        for params in calls:
            for key in ('left', 'uploaded', 'downloaded', 'numwant'):
                self.assertEqual(params[key], ['0'])
            for key in ('peer_id', 'key'):
                self.assertEqual(params[key], calls[0][key])

    def test_presence_fills_only_missing_counts(self):
        with patch.object(seed_presence, 'fetch_tracker_stats', return_value={'seeders': 67, 'leechers': 11}) as scrape:
            self.assertEqual(seed_presence.presence_counts('url', 'hash', {'leechers': 0}), [67, 0])
            self.assertEqual(seed_presence.presence_counts('url', 'hash', {}), [67, 11])
            self.assertEqual(seed_presence.presence_counts('url', 'hash', {'seeders': 0, 'leechers': 4}), [0, 4])
            self.assertEqual(scrape.call_count, 2)
            scrape.side_effect = ValueError()
            self.assertEqual(seed_presence.presence_counts('url', 'hash', {'seeders': 8}), [8, -1])

    def test_tracker_url_with_key_keeps_its_original_value(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(encode_bencode({b'interval': 600, b'complete': 24, b'incomplete': 0}))
        with patch('tracker_scrape.public_ip_only'), patch('tracker_scrape.urllib.request.build_opener', return_value=opener):
            counts = zero_transfer_announce('https://tracker.example/announce?key=example', 'b'*40, 0, (b'x'*20, 42, 'started'))
        params = parse_qs(urlsplit(opener.open.call_args.args[0]).query)
        self.assertEqual(params['key'], ['example'])
        self.assertEqual((counts['seeders'], counts['leechers']), (24, 0))
        self.assertEqual(params['uploaded'], ['0'])

    def test_second_tracker_supplies_counts_after_first_returns_404(self):
        urls = ['https://one.example/announce', 'https://two.example/announce']
        h = 'b' * 40
        with patch.object(seed_presence, 'fetch_tracker_stats', side_effect=[
                ValueError('Réponse HTTP 404 (scrape absent)'), {'seeders': 12, 'leechers': 0}]) as fetch:
            self.assertEqual(seed_presence.presence_counts(urls[0], h, {}, urls), [12, 0])
            self.assertEqual([call.args for call in fetch.call_args_list], [(u, h) for u in urls])

    def test_explicit_failure_in_http_success_is_reported_without_secrets(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(encode_bencode({b'failure reason': b'invalid passkey: PRIVATE'}))
        with patch('tracker_scrape.public_ip_only'), patch('tracker_scrape.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(AnnounceRejected) as rejected:
                zero_transfer_announce('https://tracker.example/announce/PRIVATE', 'a'*40, 0, (b'x'*20, 1, 'started'))
        self.assertEqual(str(rejected.exception), 'Authentification refusée')

    def test_http_403_retains_public_cause_only(self):
        from urllib.error import HTTPError
        opener = Mock()
        for body, expected in [
            (encode_bencode({b'failure reason': b'invalid passkey PRIVATE'}), 'Authentification refusée'),
            (b'<html>Forbidden PRIVATE</html>', 'motif non précisé'),
        ]:
            opener.open.side_effect = HTTPError('https://example.org/PRIVATE', 403, 'Forbidden', {}, io.BytesIO(body))
            with patch('tracker_scrape.public_ip_only'), patch('tracker_scrape.urllib.request.build_opener', return_value=opener):
                with self.assertRaises(AnnounceRejected) as rejected:
                    zero_transfer_announce('https://example.org/announce/PRIVATE', 'a'*40, 0, (b'x'*20, 1, 'started'))
            self.assertEqual(str(rejected.exception), 'HTTP 403 · ' + expected)

    def test_periodic_announce_honors_minimum_interval(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(encode_bencode({b'min interval': 150, b'interval': 60}))
        with patch('tracker_scrape.public_ip_only'), patch('tracker_scrape.urllib.request.build_opener', return_value=opener):
            result = zero_transfer_announce('https://tracker.example/announce', 'b'*40, 0, (b'x'*20, 1, ''))
        params = parse_qs(urlsplit(opener.open.call_args.args[0]).query)
        self.assertNotIn('event', params)
        self.assertEqual(result['interval'], 150)
        self.assertEqual(params['uploaded'], ['0'])
