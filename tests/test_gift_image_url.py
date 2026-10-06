from pathlib import Path
import sys
import unittest
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from lca.models import ChatEvent, safe_gift_image_url
from lca.connectors.tiktok import gift_image_url
URL = 'https://p16-webcast.tiktokcdn.com/img/rose.webp'
class GiftImageTests(unittest.TestCase):
    def test_roundtrip(self):
        event = ChatEvent.create(platform='tiktok', channel_id='creator', author='A', comment='gift', event_type='gift', gift_name='Rose',gift_id=5655,gift_count=10,gift_image_url=URL)
        restored = ChatEvent.from_mapping(event.to_dict())
        self.assertEqual(URL, restored.gift_image_url)
        self.assertEqual(('5655','Rose',10), (restored.gift_id,restored.gift_name,restored.gift_count))
    def test_invalid_urls_are_discarded(self):
        for value in (None, [], {}, True, 'http://tiktokcdn.com/a', 'https://tiktokcdn.com.evil/a', 'https://eviltiktokcdn.com/a', 'https://x@tiktokcdn.com/a', 'https://tiktokcdn.com:443/a', '//tiktokcdn.com/a', 'javascript:alert(1)', 'https://tiktokcdn.com\\@evil/a', 'https://tiktokcdn.com/a\n', 'https://tiktokcdn.com/'+('a'*2048)):
            with self.subTest(value=value):
                self.assertIsNone(safe_gift_image_url(value))
                self.assertIsNone(ChatEvent.create(platform='tiktok',channel_id='creator',author='A',comment='gift',gift_image_url=value).gift_image_url)
    def test_valid_hosts(self):
        for value in (URL, 'https://tiktokcdn.com/a?x=1', 'https://P16.tiktokcdn.com/a'):
            self.assertEqual(value,safe_gift_image_url(value))
    def test_connector_candidates(self):
        self.assertEqual(URL,gift_image_url(NS(image=NS(url_list=['http://evil/a',URL]),icon=NS(url_list=['https://tiktokcdn.com/icon']))))
        self.assertEqual(URL,gift_image_url(NS(image=NS(url_list=['https://evil/a']),icon=NS(url_list=[None,URL]))))
        for value in (None, NS(), NS(image=NS(url_list=URL)),NS(image=NS(url_list=[{}]))):
            self.assertIsNone(gift_image_url(value))
if __name__ == '__main__': unittest.main()
