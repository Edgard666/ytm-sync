import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import qbit_settings as settings


class ConnectionTests(unittest.TestCase):
    def test_defaults_override_redaction_and_reset(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            'QBIT_URL':'http://docker:8080', 'QBIT_USERNAME':'default', 'QBIT_PASSWORD':'secret'
        }):
            initial = settings.public(root)
            self.assertEqual(initial, dict(url='http://docker:8080', username='default', hasPassword=True))
            settings.save(root, dict(url=initial['url'], username='another'))
            self.assertEqual(settings.effective(root)['password'], 'secret')
            settings.save(root, dict(url='https://new', username='test'))
            self.assertFalse(settings.public(root)['hasPassword'])
            settings.save(root, dict(url='https://new', username='test', password='newsecret'))
            self.assertEqual(settings.effective(root)['password'], 'newsecret')
            self.assertEqual((Path(root)/'qbit-connection.json').stat().st_mode & 0o777, 0o600)
            settings.save(root, {'useDocker':True})
            self.assertEqual(initial, settings.public(root))

    def test_ui_only(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            settings.save(root, dict(url='http://nas:8080', username='user', password='test'))
            self.assertTrue(settings.public(root)['hasPassword'])
            with self.assertRaises(ValueError):
                settings.save(root, dict(url='https://user:pw@host', username='user'))
            self.assertEqual(settings.effective(root)['url'], 'http://nas:8080')
