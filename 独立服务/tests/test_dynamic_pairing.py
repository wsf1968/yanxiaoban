import http.client,json,socket,tempfile,threading,unittest
from pathlib import Path
from app import make_server
from test_http import FakeClient,FakeTrain
class DynamicPairingTests(unittest.TestCase):
 def test_rotated_pairing_link_works_without_losing_existing_phone(self):
  with tempfile.TemporaryDirectory() as folder:
   with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
   current={'code':'123456789012','expires_at':2000}
   server,pool=make_server(port,FakeClient(),FakeTrain(),pairing_code=current['code'],clock=lambda:1000,lifetime=None,session_path=Path(folder)/'sessions.json',pairing_expires_at=2000,pairing_provider=lambda:dict(current))
   threading.Thread(target=server.serve_forever,daemon=True).start()
   def request(path,body=None,cookie=None):
    connection=http.client.HTTPConnection('127.0.0.1',port);headers={'Origin':f'http://127.0.0.1:{port}'}
    if cookie:headers['Cookie']=cookie
    connection.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers);r=connection.getresponse();result=r.status,dict(r.getheaders());r.read();connection.close();return result
   try:
    status,headers=request('/api/pair',{'code':current['code']});self.assertEqual(status,200);cookie=headers['Set-Cookie'].split(';')[0]
    current['code']='987654321098'
    self.assertEqual(request('/api/pair',{'code':'123456789012'})[0],401)
    self.assertEqual(request('/api/pair',{'code':current['code']})[0],200)
    self.assertEqual(request('/api/status',cookie=cookie)[0],200)
    current['expires_at']=500
    self.assertEqual(request('/api/pair',{'code':current['code']})[0],410)
   finally:server.shutdown();server.server_close();pool.shutdown()
