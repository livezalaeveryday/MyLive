from pathlib import Path
import sys
import tempfile
import socket
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from lca.desktop_identity import load_desktop_identity, DesktopIdentityError
from lca.config import Settings
from server import desktop_launch_url, require_available_port
class DesktopIdentityTests(unittest.TestCase):
 def test_two_launches_identical_credentials_and_url(self):
  with tempfile.TemporaryDirectory() as root:
   first=load_desktop_identity(Path(root),explicit_token='');second=load_desktop_identity(Path(root),explicit_token='')
   self.assertEqual(first,second);self.assertGreaterEqual(len(first['token']),32)
   self.assertEqual(desktop_launch_url(first['token'],first['session'],5000),desktop_launch_url(second['token'],second['session'],5000))
   self.assertTrue(desktop_launch_url(first['token'],first['session'],5000).startswith('http://127.0.0.1:5000/'))
 def test_explicit_token_and_port_override(self):
  with tempfile.TemporaryDirectory() as root:
   saved=load_desktop_identity(Path(root),explicit_token='');override='explicit-test-token-1234567890'
   changed=load_desktop_identity(Path(root),explicit_token=override)
   self.assertEqual(override,changed['token']);self.assertEqual(saved['session'],changed['session'])
   self.assertEqual(saved,load_desktop_identity(Path(root),explicit_token=''))
   with patch.dict('os.environ',{'LCA_PORT':'8768'}):self.assertEqual(8768,Settings.from_env().port)
   self.assertIn(':8768/',desktop_launch_url(override,changed['session'],8768))
 def test_occupied_port_fails_without_fallback(self):
  with socket.socket() as occupied:
   occupied.bind(('127.0.0.1',0));occupied.listen();port=occupied.getsockname()[1]
   with self.assertRaisesRegex(RuntimeError,'port was not changed'):require_available_port(port,'127.0.0.1')
 def test_corrupt_profile_is_preserved(self):
  with tempfile.TemporaryDirectory() as root:
   path=Path(root)/'MyLive'/'desktop-identity.json';path.parent.mkdir();path.write_text('{invalid',encoding='utf-8')
   with self.assertRaisesRegex(DesktopIdentityError,'not replaced'):load_desktop_identity(Path(root),explicit_token='')
   self.assertEqual('{invalid',path.read_text())
 def test_concurrent_first_launch_reads_winner(self):
  from concurrent.futures import ThreadPoolExecutor
  with tempfile.TemporaryDirectory() as root:
   with ThreadPoolExecutor(max_workers=4) as pool:
    identities=list(pool.map(lambda _:load_desktop_identity(Path(root),explicit_token=''),range(4)))
   self.assertTrue(all(identity==identities[0] for identity in identities))
 def test_explicit_lan_bind_url_is_reachable_host(self):
  self.assertTrue(desktop_launch_url('t'*32,'s'*32,5000,'192.168.1.5').startswith('http://192.168.1.5:5000/'))
 def test_bad_explicit_token_fails(self):
  with tempfile.TemporaryDirectory() as root:
   with self.assertRaises(DesktopIdentityError):load_desktop_identity(Path(root),explicit_token='short')
if __name__=='__main__':unittest.main()
