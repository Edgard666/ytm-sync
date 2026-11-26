import io
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from core import (ConfigError, domain, domain_matches, allowed_tracker_urls,
                  decode_bencode, encode_bencode, filter_torrent, get_info_slice, validate)
import hashlib


POLICY = {
  "dryRun": True, "qbitTag": "joal", "refreshSeconds": 180, "maxCandidates": 250,
  "rules": [{"id": "example", "enabled": True, "trackerDomains": ["tracker.example.net"],
             "minLeechers": 3, "minUploadRate": 30, "maxUploadRate": 160,
             "simultaneousSeed": 10}]
}


class PolicyTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate(POLICY)["qbitTag"], "joal")

    def test_overlap_allowed_across_active_rules(self):
        p = json.loads(json.dumps(POLICY))
        p['rules'].append(dict(p['rules'][0], id='second', trackerDomains=['example.net']))
        self.assertEqual(len(validate(p)['rules']), 2)

    def test_client_name_no_traversal(self):
        p = json.loads(json.dumps(POLICY))
        p['rules'][0]['client'] = '../private.client'
        with self.assertRaises(ConfigError):
            validate(p)

    def test_bad_speed(self):
        p = json.loads(json.dumps(POLICY))
        p['rules'][0]['maxUploadRate'] = 2
        with self.assertRaises(ConfigError):
            validate(p)

    def test_domain_boundary(self):
        self.assertTrue(domain_matches('sub.tracker.example.net', 'tracker.example.net'))
        self.assertFalse(domain_matches('eviltracker.example.net', 'tracker.example.net'))
        self.assertFalse(domain_matches('tracker.example.net.evil.io', 'tracker.example.net'))
        self.assertEqual(domain('https://tracker.example.net:443/private/secret'), 'tracker.example.net')
        self.assertEqual(domain('udp://tracker.example.net:1337/announce'), 'tracker.example.net')

    def test_tracker_threshold(self):
        r = POLICY['rules'][0]
        ts = [
            {'url': 'https://tracker.example.net/announce/token', 'num_leeches': 2, 'status':2},
            {'url': 'https://sub.tracker.example.net/announce/token', 'num_leeches': 3, 'status':2},
            {'url': 'https://tracker.example.net/announce/bad', 'num_leeches': 500, 'status':4},
            {'url': 'https://attackerexample.net/announce', 'num_leeches': 500, 'status':2},
            {'url': 'udp://tracker.example.net/announce', 'num_leeches': 500, 'status':2},
            {'url': 'https://tracker.example.net/announce/unknown', 'num_leeches': -1, 'status':2}
        ]
        self.assertEqual(allowed_tracker_urls(ts, r), ['https://sub.tracker.example.net/announce/token', 'udp://tracker.example.net/announce'])

    def test_bencode_roundtrip_and_infohash(self):
        root = {b'announce': b'http://old.example.net/announce', b'info': {b'name': b'x', b'length': 1}}
        raw = encode_bencode(root)
        ih = hashlib.sha1(get_info_slice(raw)).hexdigest()
        updated = filter_torrent(raw, ['https://tracker.example.net/abc'], ih)
        self.assertEqual(hashlib.sha1(get_info_slice(updated)).hexdigest(), ih)
        decoded, end = decode_bencode(updated)
        self.assertEqual(end, len(updated))
        self.assertEqual(decoded[b'announce-list'], [[b'https://tracker.example.net/abc']])
        self.assertNotIn(b'old.example.net', updated)

    def test_bencode_noncanonical_info_preserved(self):
        info = b'd4:name1:x6:lengthi1ee'  # deliberately unsorted dictionary
        raw = b'd4:info' + info + b'8:announce12:http://a.org' + b'e'
        ih = hashlib.sha1(info).hexdigest()
        updated = filter_torrent(raw, ['https://tracker.example.net/a'], ih)
        self.assertEqual(get_info_slice(updated), info)

    def test_invalid_hash(self):
        raw = encode_bencode({b'info': {b'name': b'a'}})
        with self.assertRaises(ValueError):
            filter_torrent(raw, ['https://tracker.example.net/a'], '0'*40)


if __name__ == '__main__':
    unittest.main()
