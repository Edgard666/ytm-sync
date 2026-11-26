"""The progress bar only reports real qBittorrent upload counters."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main


class RealUploadProgressTests(unittest.TestCase):
    def test_known_uploaded_and_target(self):
        result = main.real_upload_progress({'uploaded': 512, 'size': 1024}, 1.0)
        self.assertEqual(result['uploadedBytes'], 512)
        self.assertEqual(result['targetBytes'], 1024)
        self.assertEqual(result['percent'], 50)
        self.assertIn('réel', result['source'])

    def test_ratio_above_one_and_clamp(self):
        result = main.real_upload_progress({'total_uploaded': 3600, 'total_size': 1000}, 2)
        self.assertEqual(result['targetBytes'], 2000)
        self.assertEqual(result['percent'], 100)

    def test_unknown_does_not_become_zero(self):
        for uploaded in (None, -1, '123', True):
            result = main.real_upload_progress({'uploaded': uploaded, 'size': 1000}, 1.2)
            self.assertIsNone(result['uploadedBytes'])
            self.assertIsNone(result['percent'])
        result = main.real_upload_progress({'size': 1000}, 1.2)
        self.assertIsNone(result['percent'])

    def test_no_target_does_not_invent_progress(self):
        result = main.real_upload_progress({'uploaded': 200, 'size': 1000}, 0)
        self.assertIsNone(result['targetBytes'])
        self.assertIsNone(result['percent'])
        self.assertEqual(result['uploadedBytes'], 200)

    def test_one_runner_only_marks_assigned_hashes(self):
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(main, 'RUNNERS', {'alpha': process}), \
             patch.object(main, 'ACTIVE_HASHES', {'alpha': {'a' * 40}}), \
             patch.object(main, 'LAST_START', {'alpha': 10.0}), \
             patch.object(main.time, 'monotonic', return_value=120.0):
            session = main.live_joal_sessions()
        self.assertEqual(session['a' * 40]['uptimeSeconds'], 110)
        self.assertIsNone(session['a' * 40]['simulatedBytes'])
        self.assertNotIn('b' * 40, session)


if __name__ == '__main__':
    unittest.main()
