"""Configuration remains usable on bind-mounted Unraid shares with restricted chmod."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'app'))  # standalone runs
import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class PermissionHandling(unittest.TestCase):
    def test_chmod_works_normally(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'policy.json'
            path.write_text('{}')
            with patch.object(main, 'CONFIG', path):
                main.ensure_config_permissions()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unraid_chmod_refused_but_read_write_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'policy.json'
            path.write_text('{}')
            with patch.object(main, 'CONFIG', path), patch.object(main.os, 'chmod', side_effect=PermissionError(errno.EPERM, 'Operation not permitted')), patch.object(main.os, 'access', return_value=True):
                with self.assertLogs('joal-smart', level='WARNING'):
                    main.ensure_config_permissions()

    def test_unreadable_config_still_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'policy.json'
            path.write_text('{}')
            with patch.object(main, 'CONFIG', path), patch.object(main.os, 'chmod', side_effect=PermissionError(errno.EPERM, 'Operation not permitted')), patch.object(main.os, 'access', return_value=False):
                with self.assertRaisesRegex(PermissionError, 'Permissions sur /data/policy.json insuffisantes'):
                    main.ensure_config_permissions()


if __name__ == '__main__':
    unittest.main()
