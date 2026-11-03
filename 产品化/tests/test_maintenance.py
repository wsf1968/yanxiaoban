import base64,hashlib,http.client,json,os,sqlite3,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from maintenance import Maintenance,sha,write_json
from installer_server import make_server

class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.key=Ed25519PrivateKey.generate()
        self.public=self.key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()
        self.bundle=self.package('1.0');self.home=self.root/'user';self.fail_version=None
        def run(args,**kwargs):
            if self.fail_version and any('/'+self.fail_version+'/' in str(a) for a in args):raise subprocess.CalledProcessError(1,args)
            return subprocess.CompletedProcess(args,0,stdout='',stderr='')
        self.manager=Maintenance(self.home,self.bundle,run=run)
        self.system=patch('maintenance.platform.system',return_value='Darwin');self.machine=patch('maintenance.platform.machine',return_value='arm64');self.system.start();self.machine.start()
    def tearDown(self):self.system.stop();self.machine.stop();self.tmp.cleanup()
    def package(self,version,marker='ok'):
        root=self.root/('package-'+version+'-'+marker);root.mkdir()
        for name in ['runtime/python/bin/python3','独立服务/installed_runtime.py']:
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(marker)
        (root/'release-public-key.txt').write_text(self.public)
        self.sign(root,version);return root
    def sign(self,root,version):
        entries=[{'path':str(p.relative_to(root)),'kind':'file','sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(root.rglob('*')) if p.is_file() and p.name!='release.json']
        m={'schema':'commercial-v1','platform':'macos-arm64','version':version,'files':entries,'distribution':'internal'}
        m['signature']=base64.b64encode(self.key.sign(json.dumps(m,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())).decode();write_json(root/'release.json',m)
    def install(self):return self.manager.execute('install')
    def test_empty_install_no_model_and_idempotence(self):
        self.assertEqual(self.install()['installed'],'1.0');self.assertFalse(self.install()['changed'])
        self.assertFalse(self.manager.public_settings()['model_key_saved'])
        self.assertEqual(self.manager.current()['version'],'1.0')
    def test_renamed_login_item_only_manages_its_own_installation(self):
        import plistlib
        fake_user=self.root/'fake-os-user'
        own_home=fake_user/'Library/Application Support/言小办开源预览'
        manager=Maintenance(own_home,self.bundle,run=self.manager.run)
        manager.install()
        agents=fake_user/'Library/LaunchAgents';agents.mkdir(parents=True)
        old_item=agents/'org.xiaopai.preview.installed.plist';old_item.write_bytes(b'preserve old installation')
        with patch.object(Path,'home',return_value=fake_user):
            self.assertTrue(manager.login_item(True)['enabled'])
            own_item=agents/'org.yanxiaoban.preview.installed.plist'
            value=plistlib.loads(own_item.read_bytes())
            self.assertIn(str(own_home),value['ProgramArguments'][0])
            self.assertEqual(value['Label'],'org.yanxiaoban.preview.installed')
            self.assertFalse(self.manager.login_item(True)['registered'])
            self.assertFalse(manager.login_item(False)['enabled'])
            self.assertFalse(own_item.exists())
        self.assertEqual(old_item.read_bytes(),b'preserve old installation')
    def test_settings_keys_never_exported_and_vendor_switch_cannot_reuse_key(self):
        self.manager.save_settings({'api_key':'private-fixture','assistant_name':'小满'})
        self.assertNotIn('private-fixture',json.dumps(self.manager.inspect()))
        self.manager.save_settings({'api_key':'','assistant_name':'小满新名'})
        self.assertEqual(self.manager.config()['api_key'],'private-fixture')
        with self.assertRaises(ValueError):self.manager.save_settings({'provider':'glm','api_key':''})
        self.assertEqual(self.manager.config()['provider'],'deepseek')
    def test_first_save_without_model_key_keeps_offline_manual_path(self):
        state=self.manager.save_settings({'assistant_name':'小满','provider':'deepseek','model':'deepseek-flash','api_key':''})
        self.assertEqual(state['assistant_name'],'小满');self.assertFalse(state['model_key_saved'])
        self.assertEqual(self.install()['installed'],'1.0')
    def test_signature_tampering_unlisted_files_and_escaping_links_rejected(self):
        (self.bundle/'extra.py').write_text('malicious')
        with self.assertRaises(ValueError):self.install()
        (self.bundle/'extra.py').unlink();(self.bundle/'独立服务/installed_runtime.py').write_text('changed')
        with self.assertRaises(ValueError):self.install()
        self.sign(self.bundle,'1.0');m=json.loads((self.bundle/'release.json').read_text());m['version']='2.0';write_json(self.bundle/'release.json',m)
        with self.assertRaises(ValueError):self.install()
        self.assertFalse(self.manager.current())
    def test_failed_upgrade_preserves_pointer_data_and_backup(self):
        self.install();data=self.home/'data/history.json';data.write_text('keep user history')
        second=self.package('2.0');self.manager.bundle=second;self.fail_version='2.0'
        with self.assertRaises(subprocess.CalledProcessError):self.install()
        self.assertEqual(self.manager.current()['version'],'1.0');self.assertEqual(data.read_text(),'keep user history')
        self.assertTrue(list((self.home/'backups').glob('*/data/history.json')))
    def test_software_rollback_keeps_new_user_data_and_trust(self):
        self.install();self.manager.bundle=self.package('2.0');self.install()
        data=self.home/'data/new-ledger.txt';data.write_text('new entries after update')
        answer=self.manager.execute('rollback')
        self.assertTrue(answer['current_data_preserved']);self.assertEqual(self.manager.current()['version'],'1.0')
        self.assertEqual(data.read_text(),'new entries after update')
        self.assertEqual(json.loads((self.home/'trust.json').read_text())['release_public_key'],self.public)
    def test_same_version_with_different_payload_is_not_silently_accepted(self):
        self.install();self.manager.bundle=self.package('1.0','different')
        with self.assertRaises(ValueError):self.install()
        self.assertEqual(self.manager.current()['version'],'1.0')
    def test_older_version_collision_also_rejected(self):
        self.install();self.manager.bundle=self.package('2.0');self.install()
        self.manager.bundle=self.package('1.0','different')
        with self.assertRaisesRegex(ValueError,'release_version_conflict'):self.install()
        self.assertEqual(self.manager.current()['version'],'2.0')
    def test_backup_includes_committed_sqlite_wal_and_does_not_change_original(self):
        db=self.home/'data/book.sqlite';c=sqlite3.connect(db);c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE entries(v TEXT)');c.execute('INSERT INTO entries VALUES(?)',('retained',));c.commit()
        result=self.manager.execute('backup');target=self.home/'backups'/result['backup']/'data/book.sqlite'
        with sqlite3.connect(target) as copied:self.assertEqual(copied.execute('SELECT * FROM entries').fetchall(),[('retained',)])
        self.assertEqual(c.execute('SELECT * FROM entries').fetchall(),[('retained',)]);c.close()
    def test_model_failure_and_cancel_leave_manual_controls_available(self):
        self.manager.save_settings({'api_key':'fixture-only'})
        with patch('maintenance.ask',side_effect=ValueError('model_connection_failed')):
            result=self.manager.assist('检查安装')
        self.assertTrue(result['manual_available']);self.assertIn('system',self.manager.execute('inspect'))
        with patch('maintenance.ask',side_effect=AssertionError('Cancelled must not call model')):
            self.assertTrue(self.manager.assist('检查',cancelled=lambda:True)['manual_available'])
    def test_keys_redacted_from_model_payload_and_unsupported_actions_rejected(self):
        self.manager.save_settings({'api_key':'fixture-secret-key'})
        with patch('maintenance.ask',return_value={'reply':'尝试执行','action':'shell'}) as ask:
            answer=self.manager.assist('请检查 fixture-secret-key')
        self.assertNotIn('fixture-secret-key',json.dumps(ask.call_args.args[2]));self.assertTrue(answer['manual_available'])
        with self.assertRaises(ValueError):self.manager.execute('shell')
    def test_cancel_while_model_thinks_does_not_dispatch_a_later_action(self):
        self.manager.save_settings({'api_key':'fixture-only'});cancelled=threading.Event()
        def model(*args):cancelled.set();return {'reply':'开始安装','action':'install'}
        with patch('maintenance.ask',side_effect=model),patch.object(self.manager,'execute') as execute:
            self.assertTrue(self.manager.assist('安装',cancelled=cancelled.is_set)['manual_available']);execute.assert_not_called()
    def test_unverified_completion_is_sent_back_to_model_before_user_sees_it(self):
        self.manager.save_settings({'api_key':'fixture-only'});events=[]
        responses=[{'reply':'安装已经完成','action':'done'},{'verdict':'revise','issues':['实际尚未安装']},
                   {'reply':'还没有安装，可以手动接手。','action':'done'},{'verdict':'pass','issues':[]}]
        with patch('maintenance.ask',side_effect=responses):result=self.manager.assist('检查',events.append)
        self.assertNotIn('安装已经完成',json.dumps(events,ensure_ascii=False));self.assertIn('还没有安装',result['reply'])
    def test_platform_credentials_are_private_unverified_and_do_not_replace_other_platform(self):
        folder=self.home/'data/runtime/commercial/.private';write_json(folder/'commerce.json',{'enabled':True,'platforms':['jd']})
        result=self.manager.save_platform({'platform':'taobao','credentials':{'app_key':'fixture-key','app_secret':'fixture-secret','pid':'mm_1_2_3'}})
        self.assertFalse(result['query_verified']);self.assertEqual(json.loads((folder/'commerce.json').read_text())['platforms'],['jd'])
        self.assertNotIn('fixture-secret',json.dumps(self.manager.inspect()))

    def test_flight_default_is_off_and_enabling_requires_key_and_fee_ack(self):
        path=self.home/'data/runtime/commercial/.private/flight-provider.json'
        self.assertFalse(path.exists());self.assertFalse(self.manager.inspect()['flight']['enabled'])
        with patch('maintenance.ask',side_effect=AssertionError('No model or paid probe on save')),patch('urllib.request.urlopen',side_effect=AssertionError('No outbound probe')):
            with self.assertRaisesRegex(ValueError,'flight_key_required'):
                self.manager.save_flight({'enabled':True,'api_key':'','paid_usage_acknowledged':True})
            with self.assertRaisesRegex(ValueError,'flight_paid_usage_ack_required'):
                self.manager.save_flight({'enabled':True,'api_key':'paid-key-fixture','paid_usage_acknowledged':False})
            with self.assertRaises(ValueError):self.manager.save_flight({'enabled':'true'})
            self.assertFalse(path.exists())

    def test_flight_save_disable_preserves_key_is_private_and_never_claims_live_verified(self):
        path=self.home/'data/runtime/commercial/.private/flight-provider.json'
        before=self.manager.config_digest()
        with patch('maintenance.ask',side_effect=AssertionError('Save must not call model')),patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('Save must not call provider')):
            saved=self.manager.save_flight({'enabled':False,'api_key':'paid-secret-fixture','paid_usage_acknowledged':False})
            self.assertFalse(saved['enabled']);self.assertFalse(saved['live_verified'])
            self.assertTrue(saved['key_saved']);self.assertFalse(saved['provider_called'])
            self.assertNotEqual(before,self.manager.config_digest())
            enabled=self.manager.save_flight({'enabled':True,'api_key':'','paid_usage_acknowledged':True})
            self.assertTrue(enabled['enabled']);self.assertFalse(enabled['live_verified'])
            self.assertEqual(enabled['state'],'configured_not_live_verified')
            disabled=self.manager.save_flight({'enabled':False})
            self.assertFalse(disabled['enabled']);self.assertTrue(disabled['key_saved'])
            self.assertEqual(json.loads(path.read_text())['api_key'],'paid-secret-fixture')
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            path.chmod(0o644)
            self.assertEqual(self.manager.public_flight()['state'],'invalid_configuration')
            self.assertFalse(self.manager.public_flight()['enabled'])
            self.manager.save_flight({'enabled':False})
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertNotIn('paid-secret-fixture',json.dumps(self.manager.inspect()))
            self.assertNotIn('paid-secret-fixture',json.dumps(self.manager.check()))

    def test_flight_saved_secret_is_redacted_before_maintenance_model(self):
        self.manager.save_settings({'api_key':'model-fixture'})
        self.manager.save_flight({'enabled':False,'api_key':'paid-redaction-fixture'})
        with patch('maintenance.ask',return_value={'reply':'unsupported','action':'shell'}) as model:
            self.manager.assist('检查 paid-redaction-fixture')
        self.assertNotIn('paid-redaction-fixture',json.dumps(model.call_args.args[2]))

class HTTPTests(MaintenanceTests):
    def request(self,method,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        data=json.dumps(body) if body is not None else None;c.request(method,path,data,headers or {});r=c.getresponse();answer=r.status,dict(r.getheaders()),r.read();c.close();return answer
    def setUp(self):
        super().setUp();self.server,self.url=make_server(self.manager);threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.headers={'Origin':self.url.split('/#')[0],'Content-Type':'application/json'}
        status,headers,_=self.request('POST','/api/login',{'token':self.url.split('#token=')[1]},self.headers);self.assertEqual(status,200)
        self.headers['Cookie']=headers['Set-Cookie'].split(';')[0]
    def tearDown(self):self.server.shutdown();self.server.server_close();super().tearDown()
    def test_host_origin_login_and_private_path_boundaries(self):
        self.assertEqual(self.request('GET','/api/state')[0],401)
        self.assertEqual(self.request('GET','/api/state',headers={'Host':'attacker.example'})[0],403)
        self.assertEqual(self.request('POST','/api/action',{'action':'install'},{**self.headers,'Origin':'https://attacker.example'})[0],403)
        self.assertEqual(self.request('GET','/data/settings.json',headers=self.headers)[0],404)
        self.assertEqual(self.request('POST','/api/action',{'action':'shell'},self.headers)[0],400)
    def test_model_down_still_allows_manual_install_and_redacted_diagnostics(self):
        self.assertEqual(self.request('POST','/api/action',{'action':'install'},self.headers)[0],200)
        status,_,raw=self.request('POST','/api/settings',{'api_key':'never-echo-key'},self.headers)
        self.assertEqual(status,200);self.assertNotIn(b'never-echo-key',raw)
        self.assertNotIn(b'never-echo-key',self.request('GET','/api/diagnostics',headers=self.headers)[2])

    def test_paid_flight_route_requires_local_session_and_does_not_probe_or_echo_key(self):
        body={'enabled':True,'api_key':'http-paid-fixture','paid_usage_acknowledged':True}
        self.assertEqual(self.request('POST','/api/flight',body,{'Origin':self.headers['Origin'],'Content-Type':'application/json'})[0],401)
        self.assertEqual(self.request('POST','/api/flight',body,{**self.headers,'Origin':'https://foreign.example'})[0],403)
        with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('No paid probe')):
            status,_,raw=self.request('POST','/api/flight',body,self.headers)
            self.assertEqual(status,200);self.assertNotIn(b'http-paid-fixture',raw)
            self.assertFalse(json.loads(raw)['live_verified'])
            status,_,raw=self.request('POST','/api/flight',{'enabled':False},self.headers)
            self.assertEqual(status,200);self.assertFalse(json.loads(raw)['enabled'])
            self.assertNotIn(b'http-paid-fixture',self.request('GET','/api/diagnostics',headers=self.headers)[2])

if __name__=='__main__':unittest.main()
