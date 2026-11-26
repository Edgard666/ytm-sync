"""v1.29.3: JOAL heap sized on the session's .torrent files; out-of-memory makes the JVM exit."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import main  # noqa: E402

MIB = 1048576


class JoalHeap(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('JOAL_XMX', None)
        os.environ.pop('JOAL_JAVA_OPTS', None)

    def test_small_session_keeps_the_light_default(self):
        self.assertEqual(main.joal_heap_mb(40_000), 128)

    def test_huge_torrent_gets_room(self):
        # « 00 - BD Français » : 279 787 pieces (5.6 MiB) + 6 641 files, with a second big torrent.
        # Measured minimums on JOAL 2.1.37: 29 MiB of metadata -> 256m, 57 MiB -> 448m.
        self.assertGreaterEqual(main.joal_heap_mb(int(28.8 * MIB)), 256 * 1.3)
        self.assertGreaterEqual(main.joal_heap_mb(int(57.4 * MIB)), 448 * 1.3)
        heap = main.joal_heap_mb(int(9 * MIB))
        self.assertEqual(heap % 32, 0)

    def test_ceiling_and_minimum(self):
        self.assertEqual(main.joal_heap_mb(500 * MIB), 1536)
        os.environ['JOAL_XMX'] = '256m'
        self.assertEqual(main.joal_heap_mb(0), 256)
        os.environ['JOAL_XMX'] = '1g'
        self.assertEqual(main.joal_heap_mb(0), 1024)
        os.environ['JOAL_XMX'] = 'n/a'
        self.assertEqual(main.joal_heap_mb(0), 128)

    def test_jvm_exits_on_out_of_memory(self):
        options = main._joal_java_opts(int(6 * MIB))
        self.assertIn('-XX:+ExitOnOutOfMemoryError', options)
        self.assertTrue(options[0].startswith('-Xmx') and options[0].endswith('m'))

    def test_full_override_still_wins(self):
        os.environ['JOAL_JAVA_OPTS'] = '-Xmx2g -XX:+UseG1GC'
        self.assertEqual(main._joal_java_opts(int(6 * MIB)), ['-Xmx2g', '-XX:+UseG1GC'])

    def test_runner_start_sizes_the_heap_on_its_torrents(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / 'clients').mkdir()
            (data / 'clients' / 'demo.client').write_text('{}')
            launched = []

            class Proc:
                stdout = None
                def poll(self):
                    return None
            rule = {'id': 'big', 'minUploadRate': 30, 'maxUploadRate': 6250, 'simultaneousSeed': 2, 'client': 'demo.client'}
            payloads = {'a' * 40: b'x' * (6 * MIB), 'b' * 40: b'y' * (3 * MIB)}
            with patch.object(main, 'DATA', data), patch.object(main, 'RUNNERS', {}), patch.object(main, 'ACTIVE_HASHES', {}), \
                 patch.object(main, 'LAST_START', {}), patch.object(main, 'TORRENT_MODES', {}), \
                 patch.object(main, 'simulations_paused', lambda: False), patch.object(main, 'safe_audit', lambda *a, **k: None), \
                 patch.object(main.subprocess, 'Popen', lambda command, **kw: launched.append(command) or Proc()):
                main.runner_start(rule, payloads)
            heap = next(arg for arg in launched[0] if arg.startswith('-Xmx'))
            self.assertEqual(heap, '-Xmx%dm' % main.joal_heap_mb(9 * MIB))
            self.assertIn('-XX:+ExitOnOutOfMemoryError', launched[0])


if __name__ == '__main__':
    unittest.main()
