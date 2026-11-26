import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main
from core import encode_bencode


POLICY = {
 "dryRun": True, "qbitTag": "joal", "refreshSeconds": 180, "maxCandidates": 250,
 "rules": [{"id": "example", "enabled": True, "trackerDomains": ["example.net"],
             "minLeechers": 2, "minUploadRate": 30, "maxUploadRate": 160,
             "simultaneousSeed": 10, "client": "qbittorrent-5.3.0.client"}]
}


class MockQBit:
    def torrents(self, tag):
        assert tag == "joal"
        return [{"hash": "somehash", "name": "Sample", "progress": 1.0, "state": "stoppedUP", "ratio": 0.6}]

    def trackers(self, hash_):
        return [{"url": "https://tracker.example.net/announce/secret", "status": 2, "num_leeches": 3}]

    def export(self, hash_):
        raise AssertionError("Export must not be called while dryRun is true")


class IntegrationTests(unittest.TestCase):
    def test_monitor_mode_creates_dashboard_state_without_joal(self):
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder) / 'policy.json'
            config_path.write_text(json.dumps(POLICY))
            with patch.object(main, 'CONFIG', config_path), patch.object(main, 'DATA', Path(folder)), \
                 patch.object(main, 'RUNNERS', {}), patch.object(main, 'LAST_START', {}):
                main.run_once(MockQBit())
                with main.STATE_LOCK:
                    data = dict(main.STATUS)
                self.assertIsNone(data['error'])
                self.assertEqual(data['rules'][0]['eligible'], 1)
                self.assertEqual(data['rules'][0]['state'], 'simulation locale (aucune annonce)')


if __name__ == '__main__':
    unittest.main()
