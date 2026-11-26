import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class AutoImportFrontendTests(unittest.TestCase):
    def test_single_file_auto_inspected_and_no_confirmation(self):
        html = (ROOT / 'app/static/index.html').read_text()
        js = (ROOT / 'app/static/app.js').read_text()
        self.assertIn('id="import-file"', html)
        self.assertNotIn('id="import-inspect"', html)
        self.assertIn("$('import-file').addEventListener('change', onImportFileChanged);", js)
        self.assertIn("const response = await api('/api/import/inspect'", js)
        self.assertIn("const response = await api('/api/import/add'", js)
        self.assertNotIn("confirm('Enregistrer ce fichier dans la bibliothèque", js)
        self.assertIn('multiple', html.split('id="import-file"')[1].split('>')[0])

if __name__ == '__main__': unittest.main()
