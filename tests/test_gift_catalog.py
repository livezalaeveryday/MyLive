from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from lca.models import ChatEvent

spec = importlib.util.spec_from_file_location('import_gifts', ROOT / 'scripts/import_gifts.py')
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


class GiftCatalogTests(unittest.TestCase):
    def test_catalog_assets_and_valid_user_ids(self):
        catalog = json.loads((ROOT / 'server/static/gifts/catalog.json').read_text(encoding='utf-8'))
        self.assertEqual(773, len(catalog['entries']))
        ids = []
        for item in catalog['entries']:
            for value in item['verifiedIds']:
                self.assertRegex(str(value), r'^[1-9]\d{0,19}$')
                ids.append(str(value))
            self.assertTrue((ROOT / 'server/static/gifts' / item['asset']).is_file())
        self.assertEqual(len(ids), len(set(ids)))

    def test_import_does_not_infer_ids_from_sequences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root/'sample.zip', 'w') as archive:
                archive.writestr('gifts/005_White_Dove.png', b'\x89PNG\r\n\x1a\n')
            result = importer.import_catalog(root/'sample.zip', root/'output')
            self.assertEqual([], result['entries'][0]['verifiedIds'])
            self.assertEqual(['White Dove'], result['entries'][0]['names'])

    def test_gift_id_roundtrip_and_invalid_id(self):
        event = ChatEvent.create(platform='tiktok',channel_id='creator',author='A',comment='gift',event_type='gift',gift_id=9001,gift_name='Rose',gift_count=3)
        self.assertEqual('9001', ChatEvent.from_mapping(event.to_dict()).gift_id)
        for unsafe in ('../001', '001', -1, True, 'javascript:alert(1)'):
            self.assertIsNone(ChatEvent.create(platform='tiktok',channel_id='creator',author='A',comment='gift',gift_id=unsafe).gift_id)

    def test_zip_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root/'bad.zip','w') as archive:
                archive.writestr('../outside.png', b'\x89PNG\r\n\x1a\n')
            with self.assertRaises(ValueError):
                importer.import_catalog(root/'bad.zip', root/'output')
            self.assertFalse((root/'outside.png').exists())


if __name__ == '__main__':
    unittest.main()
