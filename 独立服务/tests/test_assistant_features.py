import hashlib,http.client,json,socket,sys,tempfile,threading,unittest,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from assistant_features import FeatureAccounts,LocalActual
from feature_accounts import FeatureError
from commercial_voice import validate_plan
from app import make_server
from test_http import FakeClient,FakeTrain

class AccountsTests(unittest.TestCase):
    def setUp(self):
        import shutil
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.accounts=FeatureAccounts(self.root,Path(__file__).resolve().parents[2]/'记账模块',shutil.which('node'))
        self.owner='a'*64;token=self.accounts.bootstrap_token()
        self.accounts.bootstrap(self.owner,token,'测试甲','测试家庭账','测试个人账')
    def tearDown(self):self.tmp.cleanup()
    def test_duplicate_setup_does_not_duplicate_real_actual_books(self):
        token=json.loads((self.root/'bootstrap.json').read_text())['token']
        self.accounts.bootstrap(self.owner,token,'测试甲','测试家庭账','测试个人账')
        member=self.accounts.member(self.owner);books=LocalActual(self.accounts.book_root,member['actor']).books()
        self.assertEqual(len(books),2)
        with self.assertRaises(FeatureError):self.accounts.bootstrap('b'*64,token,'测试乙','别的家庭','个人')
    def test_family_editor_private_isolation_and_revoke_preserves_personal(self):
        invite=self.accounts.invite(self.owner,'editor');self.accounts.join('b'*64,invite['token'],'测试乙')
        owner=self.accounts.member(self.owner);bob=self.accounts.member('b'*64)
        left=LocalActual(self.accounts.book_root,owner['actor']);right=LocalActual(self.accounts.book_root,bob['actor'])
        self.assertEqual(len(right.books()),2)
        private=next(b['id'] for b in left.books() if b['kind']=='personal')
        self.assertFalse(right.execute(private,'denied','catalog',{})['ok'])
        self.accounts.revoke(self.owner,bob['actor'])
        self.assertIsNotNone(self.accounts.member('b'*64));self.assertEqual([b['kind'] for b in right.books()],['personal'])
        self.assertFalse(self.accounts.status('b'*64)['family_member'])
    def test_viewer_cannot_write_and_invitation_is_one_use(self):
        invitation=self.accounts.invite(self.owner,'viewer');self.accounts.join('b'*64,invitation['token'],'测试乙')
        with self.assertRaises(FeatureError):self.accounts.join('c'*64,invitation['token'],'测试丙')
        member=self.accounts.member('b'*64);engine=LocalActual(self.accounts.book_root,member['actor'])
        family=next(b['id'] for b in engine.books() if b['kind']=='family')
        self.assertFalse(engine.execute(family,'cannot-write','create_account',{'name':'不应创建'})['ok'])
        with self.assertRaises(FeatureError):self.accounts.invite('b'*64,'editor')
    def test_join_reuses_same_native_book_after_interruption(self):
        invitation=self.accounts.invite(self.owner,'editor');original=self.accounts.admin_call;calls=[]
        def interrupted(request):
            result=original(request);calls.append(request)
            if len(calls)==1:raise RuntimeError('simulated host interruption after actual book creation')
            return result
        self.accounts.admin_call=interrupted
        with self.assertRaises(RuntimeError):self.accounts.join('b'*64,invitation['token'],'测试乙')
        first=calls[0]['owner'];self.accounts.admin_call=original
        self.accounts.join('b'*64,invitation['token'],'测试乙');self.assertEqual(self.accounts.member('b'*64)['actor'],first)
        self.assertEqual(len(LocalActual(self.accounts.book_root,first).books()),2)
    def test_model_and_browser_cannot_inject_actor_or_grant_owner(self):
        with self.assertRaises(FeatureError):self.accounts.handle(self.owner,{'action':'invite','role':'owner'})
        with self.assertRaises(FeatureError):self.accounts.handle(self.owner,{'action':'invite','role':'editor','actor':'forged'})
        with self.assertRaises(Exception):validate_plan({'reply':'准备执行','capability':'bookkeeping','action':'prepare','query':{'tool':'bookkeeping','args':{'actor':'forged'}}},True,['bookkeeping'])

class FeatureHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name);self.calls=[]
        class Features:
            def __init__(inner,outer):inner.outer=outer
            def status(inner,owner):inner.outer.calls.append(('status',owner));return {'bound':False}
            def handle(inner,owner,body):inner.outer.calls.append(('post',owner,body));return {'bound':False}
        voice=type('Voice',(),{'features':Features(self)})()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.server,self.pool=make_server(self.port,FakeClient(),FakeTrain(),pairing_code='123456789012',session_path=root/'sessions.json',voice=voice)
        threading.Thread(target=self.server.serve_forever,daemon=True).start();self.origin=f'http://127.0.0.1:{self.port}'
    def tearDown(self):self.server.shutdown();self.server.server_close();self.pool.shutdown();self.tmp.cleanup()
    def request(self,method,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port);c.request(method,path,body,{'Origin':self.origin,**(headers or {})});r=c.getresponse();result=(r.status,dict(r.getheaders()),r.read());c.close();return result
    def test_features_auth_origin_and_identity_come_from_session(self):
        self.assertEqual(self.request('GET','/api/features')[0],401)
        self.assertEqual(self.request('POST','/api/features','{}')[0],403)
        _,headers,_=self.request('POST','/api/pair',json.dumps({'code':'123456789012'}));cookie=headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('POST','/api/features','{}',{'Cookie':cookie,'Origin':'https://evil.example'})[0],403)
        self.assertFalse(self.calls)
        self.assertEqual(self.request('GET','/api/features',headers={'Cookie':cookie})[0],200)
        raw=cookie.split('=',1)[1];self.assertEqual(self.calls[0],('status',hashlib.sha256(raw.encode()).hexdigest()))
