import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main


class PersistentVolumeTests(unittest.TestCase):
    def test_restart_final_sample_and_independent_torrents(self):
        a, b = 'a' * 40, 'b' * 40
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DATA', Path(tmp)), \
                patch.object(main, 'RUNNERS', {}), patch.object(main, 'ACTIVE_HASHES', {}):
            file = Path(tmp) / 'upload.json'
            proc = SimpleNamespace(telemetry=SimpleNamespace(name=tmp), volume_session='first', poll=lambda: None)
            main.RUNNERS['alpha'] = proc
            main.ACTIVE_HASHES['alpha'] = {a, b}
            file.write_text(json.dumps({a: [1, 10**10], b: [1, 100]}))
            for _ in range(2):
                self.assertEqual(main.live_joal_upload()['totalUploadedBytes'], {a: 10**10, b: 100})
            file.write_text(json.dumps({a: [1, 10**10 + 50]}))
            proc.poll = lambda: 0
            main.runner_stop('alpha')
            stopped = main.live_joal_upload()
            self.assertEqual(stopped['uploadedBytes'], {})
            self.assertEqual(stopped['totalUploadedBytes'], {a: 10**10 + 50, b: 100})
            main.RUNNERS['beta'] = SimpleNamespace(telemetry=proc.telemetry, volume_session='new', poll=lambda: None)
            main.ACTIVE_HASHES['beta'] = {a}
            file.write_text(json.dumps({a: [1, 200]}))
            after = main.live_joal_upload()
            self.assertEqual(after['uploadedBytes'], {a: 200})
            self.assertEqual(after['totalUploadedBytes'], {a: 10**10 + 250, b: 100})
