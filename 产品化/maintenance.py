"""User-owned local maintenance tools. No shell, arbitrary paths or private prompts."""
from contextlib import contextmanager
import base64,datetime,fcntl,hashlib,json,os,platform,plistlib,re,secrets,shutil,signal,socket,sqlite3,subprocess,sys,time,uuid
from pathlib import Path
from installer_model import PROVIDERS,ask

SCHEMA='commercial-v1'
TOOLS={
 'inspect':'查看系统、安装和运行状态（只读）',
 'install':'校验并安装当前用户打开的版本包；更新前备份，失败回到原版本',
 'start':'用用户已保存配置启动本安装的服务',
 'restart':'在当前操作结束后重启本安装，应用用户保存的新配置',
 'stop':'停止本安装登记并核对身份的服务',
 'check':'检查本安装运行库、配置及服务；不使用电脑麦克风、不调用收费机票',
 'test_model':'真实测试用户选定的模型连接',
 'backup':'备份本安装的数据与配置（本机私有目录）',
 'rollback':'切换到已校验的上一版本，保留当前数据',
}
SYSTEM='''你是言小办安装与维护助手。模型优先执行，用户随时接手。你根据user_request、真实state和tools选择下一步，不让用户复制命令。
只返回JSON {"reply":"面向普通用户的简短说明","action":"tools中的一个名字或done"}。一次只选一步，根据工具真实结果继续。
用户未连接模型、系统授权或手机操作需要本人完成时说明并done，不能假称完成。安装包、版本与配置已由用户在界面选择，不能另找软件、改路径、改凭证或虚构检查。
纯诊断不安装或重启。安装/更新通常先检查真实状态、install、check，配置已保存时start、check；你按实际情况决定，不机械重复。install可同时完成初次安装或更新。
工具失败先理解结果，不能无证据声称成功；同一失败不反复尝试。done只代表本轮处理结束，准确说明还需用户完成的事项。检查结果有phone_acceptance=pending时不能称手机已通过。
你不能执行任意系统命令、关闭系统保护、上传数据或恢复旧数据覆盖新账目。诊断只含系统版本、组件状态等脱敏信息，不要求用户提供历史、记忆或Key。用户明确要求回退时才rollback，停止时才stop。
真实核验反馈：你曾漏看model_connection_verified=false，随后把ready=false错误归因于手机授权，复核也误判通过。请依据check.blockers逐项理解和处理；ready/local_ready只表示本机准备状态，phone_acceptance是单独验收项，不能发明失败原因。成功的本轮模型通信也会留下连接验证证据；不要重复测试已验证的同一配置。'''

def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name('.tmp-'+uuid.uuid4().hex)
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def read_json(path,default=None):
    try:return json.loads(Path(path).read_text())
    except FileNotFoundError:return default

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()

class Maintenance:
    def __init__(self,home,bundle,*,run=subprocess.run,clock=time.time):
        os.umask(0o077)
        self.home=Path(home).expanduser().absolute();self.bundle=Path(bundle).absolute();self.run=run;self.clock=clock
        if self.home.is_symlink() or (self.home.exists() and any(self.home.iterdir()) and not (self.home/'identity.json').exists()):raise ValueError('unsafe_install_home')
        self.home.mkdir(parents=True,exist_ok=True,mode=0o700);self.home.chmod(0o700)
        for folder in ['releases','data','backups','logs']:
            p=self.home/folder
            if p.is_symlink():raise ValueError('unsafe_install_home')
            p.mkdir(exist_ok=True,mode=0o700);p.chmod(0o700)
        if not (self.home/'identity.json').exists():write_json(self.home/'identity.json',{'id':uuid.uuid4().hex})

    @contextmanager
    def lock(self):
        p=self.home/'maintenance.lock';f=p.open('a');p.chmod(0o600)
        try:fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);yield
        finally:f.close()

    def current(self):return read_json(self.home/'current.json',{})
    def config(self):return read_json(self.home/'data/settings.json',{})
    def config_digest(self):
        folder=self.home/'data/runtime/commercial/.private'
        files={name:sha(folder/name) if (folder/name).exists() else None for name in ('commerce.json','jd-union.json','taobao-union.json','flight-provider.json')}
        return hashlib.sha256(json.dumps({'settings':self.config(),'shopping':files},sort_keys=True).encode()).hexdigest()
    def model_digest(self,config=None):
        c=config if config is not None else self.config()
        return hashlib.sha256(json.dumps({k:c.get(k) for k in ('provider','model','api_key')},sort_keys=True).encode()).hexdigest()
    def release(self):
        version=self.current().get('version')
        if not version or not all(x.isalnum() or x in '.-_' for x in version):raise ValueError('not_installed')
        return self.home/'releases'/version

    def public_settings(self):
        c=self.config()
        proof=read_json(self.home/'data/model-check.json',{})
        train_verified=bool(c.get('train_key')) and c.get('train_verified_digest')==hashlib.sha256(c['train_key'].encode()).hexdigest()
        return {k:c.get(k) for k in ['assistant_name','user_name','provider','model','autostart']}|{'model_key_saved':bool(c.get('api_key')),'train_key_saved':bool(c.get('train_key')),'train_connection_verified':train_verified,'model_connection_verified':proof.get('digest')==self.model_digest()}

    def flight_config(self):
        path=self.home/'data/runtime/commercial/.private/flight-provider.json'
        value=read_json(path,{})
        if not isinstance(value,dict) or set(value)-{'enabled','api_key','paid_usage_acknowledged','max_requests_per_minute','daily_request_limit'}:raise ValueError('invalid_settings')
        return value

    def public_flight(self):
        try:
            value=self.flight_config();key=value.get('api_key','');ack=value.get('paid_usage_acknowledged',False)
            valid_key=isinstance(key,str) and len(key)<=512 and not any(ord(c)<32 for c in key)
            valid=(type(value.get('enabled',False)) is bool and type(ack) is bool and valid_key)
            path=self.home/'data/runtime/commercial/.private/flight-provider.json'
            if path.exists() and ('enabled' not in value or not path.is_file() or path.stat().st_mode&0o077 or path.stat().st_size>16384):valid=False
            for name,maximum in [('max_requests_per_minute',30),('daily_request_limit',10000)]:
                if name in value and (type(value[name]) is not int or not 1<=value[name]<=maximum):valid=False
            enabled=value.get('enabled') is True and ack is True and valid_key and bool(key.strip())
            if value.get('enabled') is True and not enabled:valid=False
            return {'enabled':bool(enabled) and valid,'key_saved':bool(key.strip()) if valid_key else False,
                    'paid_usage_acknowledged':ack is True,'live_verified':False,
                    'state':'invalid_configuration' if not valid else 'configured_not_live_verified' if enabled else 'not_enabled'}
        except (ValueError,OSError,UnicodeError):
            return {'enabled':False,'key_saved':False,'paid_usage_acknowledged':False,
                    'live_verified':False,'state':'invalid_configuration'}

    def save_flight(self,body):
        if (not isinstance(body,dict) or not {'enabled'}<=set(body)
                or set(body)-{'enabled','api_key','paid_usage_acknowledged'}
                or type(body['enabled']) is not bool):raise ValueError('invalid_settings')
        value=self.flight_config();key=body.get('api_key','')
        if not isinstance(key,str) or len(key)>512 or any(ord(c)<32 for c in key):raise ValueError('invalid_settings')
        # Empty password input preserves the user's private saved key, including on disable.
        key=key.strip() or value.get('api_key','')
        if not isinstance(key,str) or len(key)>512 or any(ord(c)<32 for c in key):raise ValueError('invalid_settings')
        ack=body.get('paid_usage_acknowledged',value.get('paid_usage_acknowledged',False))
        if type(ack) is not bool:raise ValueError('invalid_settings')
        if body['enabled'] and not key.strip():raise ValueError('flight_key_required')
        if body['enabled'] and not ack:raise ValueError('flight_paid_usage_ack_required')
        for name,maximum in [('max_requests_per_minute',30),('daily_request_limit',10000)]:
            if name in value and (type(value[name]) is not int or not 1<=value[name]<=maximum):raise ValueError('invalid_settings')
        value.update(enabled=body['enabled'],api_key=key,paid_usage_acknowledged=ack)
        write_json(self.home/'data/runtime/commercial/.private/flight-provider.json',value)
        # Saving and diagnostics never contact the paid provider. First-query evidence is separate.
        return self.public_flight()|{'saved':True,'provider_called':False,'restart_to_apply':True}

    def verify_train(self,request):
        if not isinstance(request,str) or not 2<=len(request)<=300:raise ValueError('invalid_settings')
        config=self.config()
        if not config.get('train_key'):raise ValueError('train_key_required')
        if config['provider']!='deepseek':raise ValueError('platform_model_adaptation_pending')
        sys.path.insert(0,str(self.release()/'独立服务'))
        from commercial_voice import commercial_request
        from trains import TrainClient
        value=ask(config,'''根据用户这句高铁查询决定真实接口参数，不猜城市主站，不改变条件。只返回JSON {"action":"query","args":{"departure_station":"城市或站名","arrival_station":"城市或站名","date":"YYYY-MM-DD","filter":"G、D或GD","departure_scope":"city或station","arrival_scope":"city或station","departure_time_range":"全天、凌晨、上午、下午或晚上"}}，精确时段可加departure_start、departure_end，格式HH:MM。当前接口仅支持北京时间今天起15天内。信息不足则返回 {"action":"clarify","reply":"简短追问"}，不要填猜测值。这次只核验用户自己的只读接口连接，不订票。''',{'request':request,'now':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()})
        if isinstance(value,dict) and set(value)=={'action','reply'} and value['action']=='clarify' and isinstance(value['reply'],str) and 1<=len(value['reply'])<=300:
            return {'query_verified':False,'reply':value['reply']}
        if not isinstance(value,dict) or set(value)!={'action','args'} or value['action']!='query':raise ValueError('train_query_not_verified')
        args=commercial_request({'tool':'rail','args':value['args']})
        result=TrainClient(config['train_key']).run(args)
        if result.get('cached') is not False or result.get('state') not in ('complete','empty'):raise ValueError('train_query_not_verified')
        current=self.config()
        if current.get('train_key')!=config['train_key']:raise ValueError('train_query_not_verified')
        current['train_verified_digest']=hashlib.sha256(config['train_key'].encode()).hexdigest();write_json(self.home/'data/settings.json',current)
        return {'query_verified':True,'request':result['request'],'returned_count':len(result['trains']),'restart_to_apply':True}

    def save_platform(self,body):
        platform_name=body.get('platform');fields={'app_key','app_secret','site_id'} if platform_name=='jd' else {'app_key','app_secret','pid'}
        if platform_name not in ('jd','taobao') or set(body)!={'platform','credentials'} or not isinstance(body['credentials'],dict) or set(body['credentials'])!=fields:raise ValueError('invalid_settings')
        values=body['credentials']
        if any(not isinstance(v,str) or not 1<=len(v.strip())<=256 or not re.fullmatch(r'[A-Za-z0-9_-]+',v.strip()) for v in values.values()):raise ValueError('invalid_settings')
        values={k:v.strip() for k,v in values.items()}
        if platform_name=='jd' and not values['site_id'].isdigit():raise ValueError('invalid_settings')
        if platform_name=='taobao' and not re.fullmatch(r'mm_[1-9][0-9]*_[1-9][0-9]*_[1-9][0-9]*',values['pid']):raise ValueError('invalid_settings')
        folder=self.home/'data/runtime/commercial/.private';path=folder/(platform_name+'-union.json')
        if path.exists():write_json(folder/(platform_name+'-before-'+uuid.uuid4().hex+'.json'),read_json(path))
        write_json(path,values)
        # Saving is not verification. Disable only this changed platform until
        # its own read-only test succeeds; keep the other connected platform.
        profile=read_json(folder/'commerce.json',{'enabled':False,'platforms':[]})
        profile['platforms']=[p for p in profile['platforms'] if p!=platform_name];profile['enabled']=bool(profile['platforms'])
        write_json(folder/'commerce.json',profile)
        write_json(folder/(platform_name+'-verification.json'),{'query_verified':False})
        return {'saved':True,'platform':platform_name,'query_verified':False}

    def verify_platform(self,platform_name,request):
        if platform_name not in ('jd','taobao') or not isinstance(request,str) or not 2<=len(request)<=300:raise ValueError('invalid_settings')
        if self.config()['provider']!='deepseek':raise ValueError('platform_model_adaptation_pending')
        release=self.release();sys.path.insert(0,str(release/'独立服务'))
        from commercial_voice import ModelClient
        from commerce import Commerce
        folder=self.home/'data/runtime/commercial/.private';path=folder/(platform_name+'-union.json')
        if not path.exists():raise ValueError('platform_credentials_required')
        testroot=self.home/'data/connection-tests'/uuid.uuid4().hex;testprivate=testroot/'.private'
        write_json(testprivate/(platform_name+'-union.json'),read_json(path));write_json(testprivate/'commerce.json',{'enabled':True,'platforms':[platform_name]})
        model=ModelClient({'base_url':'https://api.deepseek.com','api_key':self.config()['api_key'],'model':self.config()['model']})
        commerce=Commerce.from_private(testroot,model);client=commerce.adapters[platform_name].client
        # Both adapters expose parsed, validated query pages. Observe this
        # contract instead of the different private HTTP-client nesting.
        original=client.query;observed=[]
        def record(*args,**kwargs):
            value=original(*args,**kwargs)
            if isinstance(value,dict) and isinstance(value.get('rows'),list) and value.get('cached') is False:observed.append(True)
            return value
        client.query=record
        result=commerce.execute({'platform':platform_name,'request':request},time.monotonic()+80,context={'utterance':request,'history':[]})
        if not observed:raise ValueError('platform_query_not_verified')
        profile=read_json(folder/'commerce.json',{'enabled':False,'platforms':[]})
        if platform_name not in profile['platforms']:profile['platforms'].append(platform_name)
        profile['enabled']=True;write_json(folder/'commerce.json',profile)
        write_json(folder/(platform_name+'-verification.json'),{'query_verified':True,'all_permissions_verified':False,'at':self.clock()})
        return {'platform':platform_name,'query_verified':True,'all_permissions_verified':False,'reply':result.get('reply',''),'restart_to_apply':True}

    def save_settings(self,value):
        if not isinstance(value,dict) or set(value)-{'assistant_name','user_name','provider','model','api_key','train_key','autostart'}:raise ValueError('invalid_settings')
        c=self.config();c.update(value)
        c.setdefault('assistant_name','言小办');c.setdefault('user_name','');c.setdefault('provider','deepseek');c.setdefault('autostart',False)
        if c['provider'] not in PROVIDERS:raise ValueError('unsupported_provider')
        c.setdefault('model',PROVIDERS[c['provider']]['model'])
        for key,limit,empty in [('assistant_name',32,False),('user_name',40,True),('model',80,False),('api_key',512,True),('train_key',512,True)]:
            v=c.get(key,'')
            if not isinstance(v,str) or len(v)>limit or (not empty and not v.strip()) or any(ord(x)<32 for x in v):raise ValueError('invalid_settings')
            c[key]=v.strip()
        if type(c['autostart']) is not bool:raise ValueError('invalid_settings')
        # An empty password field keeps the private saved value; keys never return to the page.
        old=self.config()
        if old.get('provider') and old['provider']!=c['provider'] and not value.get('api_key'):raise ValueError('new_provider_key_required')
        for field in ('api_key','train_key'):
            if not value.get(field) and old.get(field):c[field]=old[field]
        write_json(self.home/'data/settings.json',c)
        if self.current() and 'autostart' in value:self.login_item(c['autostart'])
        return self.public_settings()

    def manifest(self,source=None):
        source=Path(source or self.bundle);m=read_json(source/'release.json')
        if not m or m.get('schema')!=SCHEMA or m.get('platform')!='macos-arm64':raise ValueError('unsupported_release')
        version=m.get('version','')
        if not version or len(version)>64 or not all(x.isalnum() or x in '.-_' for x in version):raise ValueError('invalid_release_version')
        return m

    def verify(self,source=None):
        source=Path(source or self.bundle);m=self.manifest(source)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        trust=read_json(self.home/'trust.json',{})
        public=trust.get('release_public_key') or (self.bundle/'release-public-key.txt').read_text().strip()
        canonical={k:v for k,v in m.items() if k!='signature'}
        try:Ed25519PublicKey.from_public_bytes(bytes.fromhex(public)).verify(base64.b64decode(m['signature']),json.dumps(canonical,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
        except Exception:raise ValueError('release_signature_failed') from None
        expected={'release.json'};seen=set()
        for entry in m['files']:
            rel=Path(entry['path'])
            if rel.is_absolute() or '..' in rel.parts or not rel.parts or any(x.startswith('.private') for x in rel.parts):raise ValueError('unsafe_release_path')
            if entry['path'] in seen:raise ValueError('invalid_release_manifest')
            seen.add(entry['path']);expected.add(entry['path']);p=source/rel
            if not p.resolve().is_relative_to(source.resolve()):raise ValueError('unsafe_release_link')
            if entry.get('kind')=='symlink':
                if not p.is_symlink() or os.readlink(p)!=entry['target']:raise ValueError('release_integrity_failed')
            elif entry.get('kind')!='file' or not p.is_file() or p.is_symlink() or sha(p)!=entry['sha256']:raise ValueError('release_integrity_failed')
        actual={str(p.relative_to(source)) for p in source.rglob('*') if p.is_file() or p.is_symlink()}
        if actual!=expected:raise ValueError('unexpected_release_files')
        return m

    def service(self):
        value=read_json(self.home/'data/runtime/process.json',{})
        if not value.get('pid'):return None
        try:
            expected=str(self.release()/'独立服务/installed_runtime.py')
            out=self.run(['/bin/ps','-p',str(value['pid']),'-o','command='],capture_output=True,text=True,timeout=3,check=True).stdout
            if expected not in out:return None
            os.kill(value['pid'],0);return value
        except (OSError,subprocess.SubprocessError,ValueError):return None

    def inspect(self):
        current=self.current();c=self.public_settings()
        info=read_json(self.home/'data/runtime/service-info.json',{})
        return {'system':platform.system(),'macos':platform.mac_ver()[0],'architecture':platform.machine(),
                'free_disk_gb':round(shutil.disk_usage(self.home).free/1073741824,1),
                'bundle_version':self.manifest()['version'],'installed_version':current.get('version'),
                'previous_version':current.get('previous'),'running':bool(self.service()),
                'configuration_pending_restart':bool(self.service()) and info.get('configuration_digest')!=self.config_digest(),
                'settings':c,'shopping_platforms':read_json(self.home/'data/runtime/commercial/.private/commerce.json',{}).get('platforms',[]),
                'flight':self.public_flight(),
                'phone_acceptance':'pending','distribution_ready':False,'release_stage':self.manifest().get('distribution','internal'),
                'manual_tools':list(TOOLS)}

    def stop(self):
        value=self.service()
        if not value:return {'stopped':False,'reason':'this_install_not_running'}
        os.kill(value['pid'],signal.SIGTERM)
        for _ in range(1800):
            try:
                pid,_=os.waitpid(value['pid'],os.WNOHANG)
                if pid:return {'stopped':True}
            except ChildProcessError:pass
            try:os.kill(value['pid'],0)
            except ProcessLookupError:return {'stopped':True}
            time.sleep(.1)
        raise ValueError('service_stop_timeout')

    def backup(self):
        # Callers quiesce this installation first. Never copy a live SQLite WAL as a loose file.
        was_running=bool(self.service())
        if was_running:self.stop()
        folder=self.home/'backups'/(datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3))
        folder.mkdir(mode=0o700)
        try:
            if any(p.is_symlink() for p in (self.home/'data').rglob('*')):raise ValueError('unsafe_data_link')
            shutil.copytree(self.home/'data',folder/'data',symlinks=False)
            from urllib.parse import quote
            for path in (self.home/'data').rglob('*'):
                if not path.is_file():continue
                with path.open('rb') as f:header=f.read(16)
                if header!=b'SQLite format 3\x00':continue
                target=folder/'data'/path.relative_to(self.home/'data')
                for suffix in ('-wal','-shm'):
                    side=Path(str(target)+suffix)
                    if side.exists():side.unlink()
                src=sqlite3.connect('file:'+quote(str(path))+'?mode=ro',uri=True);dst=sqlite3.connect(target)
                try:src.backup(dst)
                finally:dst.close();src.close()
            write_json(folder/'backup.json',{'schema':SCHEMA,'version':self.current().get('version'),'created':self.clock(),'contains_private_data':True})
        finally:
            if was_running:self.start()
        return {'backup':folder.name,'local_private_backup':True}

    def install(self):
        m=self.verify();version=m['version'];old=self.current();was_running=bool(self.service())
        if old.get('version')==version:
            if sha(self.release()/'release.json')!=sha(self.bundle/'release.json'):raise ValueError('release_version_conflict')
            return {'installed':version,'changed':False}
        if platform.system()!='Darwin' or platform.machine()!='arm64':raise ValueError('this_build_requires_apple_silicon_mac')
        if was_running:self.stop()
        try:
            self.backup()
            target=self.home/'releases'/version
            if target.exists():
                self.verify(target)
                if sha(target/'release.json')!=sha(self.bundle/'release.json'):raise ValueError('release_version_conflict')
            else:
                staging=self.home/'releases'/('.staging-'+uuid.uuid4().hex)
                try:shutil.copytree(self.bundle,staging,symlinks=True);self.verify(staging);os.replace(staging,target)
                finally:
                    if staging.exists():shutil.rmtree(staging)
            # Check runtime imports from the copied path, with no developer environment.
            env=self.environment(target)
            self.run([str(target/'runtime/python/bin/python3'),'-I','-B','-c','import sherpa_onnx,numpy,soundfile,cryptography,jsonschema,qrcode'],env=env,capture_output=True,timeout=45,check=True)
            write_json(self.home/'current.json',{'version':version,'previous':old.get('version'),'schema':SCHEMA})
            if not (self.home/'trust.json').exists():write_json(self.home/'trust.json',{'release_public_key':(self.bundle/'release-public-key.txt').read_text().strip()})
            if self.config().get('autostart'):self.login_item(True)
            if was_running:self.start()
            return {'installed':version,'changed':True,'previous':old.get('version')}
        except Exception:
            if old:
                write_json(self.home/'current.json',old)
                if self.config().get('autostart'):self.login_item(True)
                if was_running:self.start()
            raise

    def environment(self,release=None):
        release=Path(release or self.release())
        return {'HOME':str(Path.home()),'PATH':str(release/'runtime/node/bin')+':/usr/bin:/bin:/usr/sbin:/sbin',
                'PYTHONNOUSERSITE':'1','PAI_INSTALL_HOME':str(self.home),'PAI_DATA_DIR':str(self.home/'data/runtime'),
                'PYTHONDONTWRITEBYTECODE':'1',
                'LANG':'en_US.UTF-8','LC_ALL':'en_US.UTF-8'}

    def start(self):
        if self.service():return {'running':True,'already_running':True}
        release=self.release()
        if not self.config().get('api_key'):raise ValueError('model_key_required')
        runtime=self.home/'data/runtime';runtime.mkdir(exist_ok=True,mode=0o700)
        with (self.home/'logs/service.log').open('a') as log:
            child=subprocess.Popen([str(release/'runtime/python/bin/python3'),'-u',str(release/'独立服务/installed_runtime.py')],cwd=release/'独立服务',env=self.environment(release),stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        write_json(runtime/'process.json',{'pid':child.pid})
        for _ in range(200):
            if child.poll() is not None:raise ValueError('service_start_failed')
            info=read_json(runtime/'service-info.json',{})
            if info.get('pid')==child.pid:
                return {'running':True,'local_https':True,'phone_acceptance':'pending'}
            time.sleep(.1)
        child.terminate();child.wait(timeout=180);raise ValueError('service_start_timeout')

    def restart(self):
        self.stop();return self.start()

    def login_item(self,enabled):
        # Explicit user setting only; isolated acceptance roots never change the
        # real account's Login Items. The plist takes effect on the next login.
        standard=Path.home()/'Library/Application Support/言小办开源预览'
        if self.home.resolve()!=standard.resolve():return {'registered':False,'isolated_acceptance_root':True}
        path=Path.home()/'Library/LaunchAgents/org.yanxiaoban.preview.installed.plist'
        if not enabled:
            if path.exists():path.unlink()
            return {'enabled':False,'effective':'next_login'}
        release=self.release();env=self.environment(release)
        value={'Label':'org.yanxiaoban.preview.installed','ProgramArguments':[str(release/'runtime/python/bin/python3'),'-u',str(release/'独立服务/installed_runtime.py')],
               'EnvironmentVariables':env,'WorkingDirectory':str(release/'独立服务'),'RunAtLoad':True,'KeepAlive':False,
               'StandardOutPath':str(self.home/'logs/service.log'),'StandardErrorPath':str(self.home/'logs/service.log')}
        path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.tmp');temp.write_bytes(plistlib.dumps(value));temp.chmod(0o600);os.replace(temp,path)
        return {'enabled':True,'effective':'next_login'}

    def check(self):
        result=self.inspect()
        if not result['installed_version']:return result|{'ready':False,'local_ready':False,'blockers':['not_installed'],'next':'install'}
        release=self.release()
        try:
            self.run([str(release/'runtime/python/bin/python3'),'-I','-B','-c','import sherpa_onnx,numpy,soundfile,cryptography,qrcode,jsonschema'],env=self.environment(),capture_output=True,timeout=30,check=True)
            result['dependencies_ready']=True
        except (OSError,subprocess.SubprocessError):result['dependencies_ready']=False
        result['local_http_ready']=False;result['local_tls_ready']=False
        if result['running']:
            import http.cookiejar,http.client,ssl,urllib.request
            info=read_json(self.home/'data/runtime/service-info.json',{})
            try:
                client=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                base='http://127.0.0.1:'+str(info['desktop_port'])+'/'
                with client.open(base,timeout=5) as r:result['local_http_ready']=r.status==200
                with client.open(base+'api/status',timeout=5) as r:result['runtime_capabilities']=json.load(r)['capabilities']
                ca=self.home/'data/runtime/certificates/root-ca.pem'
                connection=http.client.HTTPSConnection('localhost',info['https_port'],context=ssl.create_default_context(cafile=str(ca)),timeout=5)
                connection.request('GET','/',headers={'Host':info['origin'].split('://')[1]});response=connection.getresponse();response.read();result['local_tls_ready']=response.status==200;connection.close()
            except Exception:pass
        result['ready']=result['running'] and result['dependencies_ready'] and result['local_http_ready'] and result['local_tls_ready'] and result['settings']['model_connection_verified'] and not result['configuration_pending_restart']
        checks={'service_not_running':result['running'],'runtime_dependencies_missing':result['dependencies_ready'],
                'local_http_failed':result['local_http_ready'],'local_tls_failed':result['local_tls_ready'],
                'model_connection_not_verified':result['settings']['model_connection_verified'],
                'configuration_pending_restart':not result['configuration_pending_restart']}
        result['blockers']=[key for key,passed in checks.items() if not passed];result['local_ready']=result['ready']
        return result

    def rollback(self):
        old=self.current();version=old.get('previous')
        if not version:raise ValueError('no_previous_release')
        target=self.home/'releases'/version;self.verify(target)
        was_running=bool(self.service())
        if was_running:self.stop()
        self.backup()
        try:
            write_json(self.home/'current.json',{'version':version,'previous':old['version'],'schema':SCHEMA})
            if self.config().get('autostart'):self.login_item(True)
            if was_running:self.start()
        except Exception:
            write_json(self.home/'current.json',old)
            if self.config().get('autostart'):self.login_item(True)
            if was_running:self.start()
            raise
        return {'version':version,'current_data_preserved':True}

    def test_model(self):
        config=self.config();value=ask(config,'只返回JSON {"connected":true}。本次仅测试连接，不执行任务。',{'test':'connection'})
        if value!={'connected':True}:raise ValueError('model_connection_not_verified')
        write_json(self.home/'data/model-check.json',{'digest':self.model_digest(config),'at':self.clock(),'evidence':'explicit_connection_test'})
        return {'model_connected':True,'provider':self.config()['provider']}

    def execute(self,action):
        if action not in TOOLS:raise ValueError('unsupported_maintenance_action')
        with self.lock():
            result=getattr(self,action)()
            write_json(self.home/'logs'/('action-'+uuid.uuid4().hex+'.json'),{'action':action,'result':result,'at':self.clock()})
            return result

    def assist(self,user_request,emit=lambda _:None,cancelled=lambda:False):
        if not isinstance(user_request,str) or not 1<=len(user_request)<=500:raise ValueError('invalid_request')
        history=[];last=None
        for key in ('api_key','train_key'):
            secret=self.config().get(key)
            if secret:user_request=user_request.replace(secret,'[凭证已隐藏]')
        try:flight_secret=self.flight_config().get('api_key')
        except (ValueError,OSError,UnicodeError):flight_secret=None
        if isinstance(flight_secret,str) and flight_secret:user_request=user_request.replace(flight_secret,'[凭证已隐藏]')
        for _ in range(8):
            if cancelled():return {'reply':'自动流程已停止，你可以使用手动操作继续。','manual_available':True,'state':self.inspect()}
            state=self.inspect()
            try:
                config=self.config();value=ask(config,SYSTEM,{'user_request':user_request,'state':state,'tools':TOOLS,'results':history})
                if cancelled():return {'reply':'自动流程已停止，你可以使用手动操作继续。','manual_available':True,'state':self.inspect()}
                if set(value)!={'reply','action'} or value['action'] not in set(TOOLS)|{'done'} or not isinstance(value['reply'],str) or not 1<=len(value['reply'])<=400:raise ValueError('model_invalid_maintenance_plan')
                write_json(self.home/'data/model-check.json',{'digest':self.model_digest(config),'at':self.clock(),'evidence':'validated_real_maintenance_model_reply'})
                if value['action']=='done':
                    audit=ask(self.config(),'你是安装交付复核者。对照真实状态核验候选reply是否准确。只返回JSON {"verdict":"pass或revise","issues":["问题"]}。ready/local_ready和blockers指本机，不把本机未就绪归因于手机未确认；手机验收独立。特别复核模型连接、依赖和HTTP/TLS，禁止发明失败原因。distribution_ready=false不等于正式交付全部完成。允许准确说本机已启动或软件安装完毕，同时说明待用户确认项。不能把尚未安装、运行库失败或工具报错说成已完成。',{'state':self.inspect(),'candidate':value,'results':history})
                    if audit!={'verdict':'pass','issues':[]}:
                        history.append({'candidate':value,'review':audit});continue
                    emit({'event':'plan','reply':value['reply'],'action':'done'})
                    return {'reply':value['reply'],'state':self.inspect(),'model_used':True}
                emit({'event':'plan','reply':value['reply'],'action':value['action']})
                if last==value['action'] and history[-1].get('error'):raise ValueError('repeated_failed_action')
                last=value['action']
                try:result={'action':last,'result':self.execute(last)}
                except Exception as error:result={'action':last,'error':safe_error(error)}
                history.append(result);emit({'event':'receipt',**result})
            except Exception as error:
                return {'reply':'自动处理暂未完成，可以使用下方的手动操作继续。','error':safe_error(error),'manual_available':True,'state':self.inspect()}
        return {'reply':'已到本轮处理上限，请核对当前状态；手动操作仍可使用。','manual_available':True,'state':self.inspect()}

def safe_error(error):
    allowed={'unsupported_maintenance_action','model_key_required','new_provider_key_required','not_installed','this_build_requires_apple_silicon_mac','invalid_settings','unsupported_provider','release_integrity_failed','release_signature_failed','unexpected_release_files','unsafe_data_link','unsafe_release_path','unsafe_release_link','unsupported_release','invalid_release_version','service_start_failed','service_start_timeout','service_stop_timeout','no_previous_release','model_connection_failed','model_connection_not_verified','model_invalid_maintenance_plan','model_invalid_reply','repeated_failed_action','model_incomplete','release_version_conflict','platform_model_adaptation_pending','platform_credentials_required','platform_query_not_verified','train_key_required','train_query_not_verified','flight_key_required','flight_paid_usage_ack_required'}
    text=str(error)
    return text if text in allowed else 'operation_not_completed'
