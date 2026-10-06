from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from lca import create_app
from tests.support import FakeConnectorFactory
TOKEN='offline-test-token-1234567890'
A='session_A_123456';B='session_B_123456'
class TestEventApiTests(unittest.TestCase):
 def setUp(self):
  self.factory=FakeConnectorFactory();self.app=create_app({'TESTING':True,'LOCAL_AUTH_TOKEN':TOKEN},connector_factory=self.factory);self.http=self.app.test_client();self.sio=self.app.extensions['socketio']
 def headers(self,session=A):return {'X-LCA-Token':TOKEN,'X-LCA-Session':session}
 def test_auth_required(self):
  for headers in ({},{'X-LCA-Token':'wrong','X-LCA-Session':A},self.headers('bad')):
   self.assertEqual(401,self.http.post('/api/events/test',headers=headers,json={'platform':'tiktok'}).status_code)
  self.assertEqual([],self.factory.created)
 def test_invalid_platform(self):
  for platform in (None,[],{},'facebook','TIKTOK'):
   self.assertEqual(400,self.http.post('/api/events/test',headers=self.headers(),json={'platform':platform}).status_code)
  self.assertEqual([],self.factory.created)
 def test_session_isolation_samples_unique_ids_and_no_connector(self):
  a=self.sio.test_client(self.app,auth={'token':TOKEN,'sessionId':A});b=self.sio.test_client(self.app,auth={'token':TOKEN,'sessionId':B});ids=[]
  for platform in ('youtube','tiktok','tiktok'):
   response=self.http.post('/api/events/test',headers=self.headers(),json={'platform':platform,'giftImageUrl':'https://evil/a'})
   self.assertEqual(200,response.status_code);ids.append(response.json['id'])
   messages=[m for m in a.get_received() if m['name']=='chat_message'];self.assertEqual(1,len(messages));msg=messages[0]['args'][0]
   self.assertEqual(platform,msg['platform']);self.assertIn('TEST',msg['author']);self.assertIn('[TEST]',msg['giftName']);self.assertIn('[TEST]',msg['comment']);self.assertEqual([],b.get_received())
   if platform=='tiktok':self.assertEqual('5655',msg['giftId']);self.assertIn('tiktokcdn.com',msg['giftImageUrl'])
   else:self.assertIsNone(msg['giftImageUrl']);self.assertIn('fake',msg['giftValue'])
  self.assertEqual(3,len(set(ids)));self.assertEqual([],self.factory.created);a.disconnect();b.disconnect()
if __name__=='__main__':unittest.main()
