"""Installed profiles must not change the existing commercial entry or cookies."""
import http.client,json,socket,tempfile,threading,unittest
from pathlib import Path
import test_http
from test_common_voice import Model,Search
from app import make_server
from commercial_voice import CommercialVoice,CommercialQueries

class InstalledProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();r=Path(self.tmp.name);token=r/'token';token.write_text('fixture-only')
        voice=CommercialVoice(r/'history.db',token,model=Model(),queries=CommercialQueries(Search(),test_http.FakeTrain()),speech_call=lambda *a:{'audio':'AA==','mime':'audio/wav'})
        voice.install_profile={'assistant_name':'言小办A','user_name':'小客','model':'deepseek-flash','capabilities':['web_search'],'cookie_name':'pai_session_012345abcdef'}
        voice.welcome_path=r/'ready.wav';voice.welcome_path.write_bytes(b'fixture-handshake')
        with socket.socket() as s:s.bind(('127.0.0.1',0));self.port=s.getsockname()[1]
        self.server,self.pool=make_server(self.port,voice=voice);threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.pool.shutdown(wait=True);self.tmp.cleanup()
    def request(self,path,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5);c.request('GET',path,headers=headers or {});r=c.getresponse();result=r.status,dict(r.getheaders()),r.read();c.close();return result
    def test_installed_name_greeting_and_cookie_are_separate_and_escaped(self):
        status,headers,body=self.request('/')
        self.assertEqual(status,200);self.assertIn('pai_session_012345abcdef=',headers['Set-Cookie'])
        html=body.decode();self.assertIn('言小办A',html);self.assertIn('小客，我在',html);self.assertNotIn('你好',html);self.assertNotIn('言小办AA',html)
        cookie=headers['Set-Cookie'].split(';')[0]
        _,_,raw=self.request('/api/status',{'Cookie':cookie});self.assertEqual(json.loads(raw)['capabilities'],['web_search'])
        self.assertEqual(self.request('/api/status',{'Cookie':'pai_session=old-unrelated-device'})[0],401)
        self.assertEqual(self.request('/ready-v035.wav',{'Cookie':cookie})[2],b'fixture-handshake')
