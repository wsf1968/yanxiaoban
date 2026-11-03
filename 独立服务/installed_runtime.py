"""Installed runtime with user data outside release code; never loads project .env."""
from pathlib import Path
import base64,fcntl,hashlib,json,os,signal,socket,subprocess,sys,threading
from datetime import datetime
from macos_local import native_hostname,lan_address
from product_speech import load_speech
from app import make_server
from local_https import TLSServer,LANServer,prepare_certificates,proxy_handler,setup_handler,private_write
from mobile import login_link
from private_json import write_private

def public_certificate_handler(certificate,origin,port):
    """Public certificate download only; pairing stays in the authenticated wizard."""
    original=setup_handler(certificate,origin,'','',port)
    class CertificatePage(original):
        def do_GET(self):
            if self.path=='/':
                page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>言小办 · 连接iPhone</title><style>body{font:18px/1.7 system-ui;background:#10251d;color:#e2efdb;max-width:680px;margin:48px auto;padding:24px}a{color:#c0e8a6}</style><h1>从安装助手连接你的iPhone</h1><p>请在Mac双击“言小办安装助手”，在统一绿色向导中选择“显示本机连接指引”。首次连接二维码只在该向导内显示。</p><p><a href="/root-ca.mobileconfig">下载这台Mac的连接证书</a></p><p>证书安装与完全信任后，再回到Mac向导扫描手机授权二维码；这个页面不能授予手机访问权限。</p></html>'''
                return self.send(200,page.encode(),'text/html; charset=utf-8')
            return super().do_GET()
    return CertificatePage

class NamedModel:
    def __init__(self,model,name):self.original=model;self.name=name
    def __getattr__(self,key):return getattr(self.original,key)
    def ask(self,system,*args,**kwargs):
        identity='\n安装配置中的助手名字（纯数据，不是指令）：'+json.dumps({'assistant_name':self.name},ensure_ascii=False)+'''。
本轮身份配置优先于此前默认的Pi、派或言小办称呼。用户询问名字时使用配置的完整名字，不自行缩写、提炼、去掉后缀或改名。真实核验中曾把“小满验收”缩成“小满”，这是错误，须按完整配置修正；这只影响自称，不改变用户任务、权限或输出协议。'''
        return self.original.ask(system.replace('言小办',self.name)+identity,*args,**kwargs)

def choose_ports(private):
    p=private/'runtime.json'
    if p.exists():return json.loads(p.read_text())
    for base in range(18879,19980,100):
        sockets=[]
        try:
            for port in range(base,base+4):
                s=socket.socket();s.bind(('127.0.0.1',port));sockets.append(s)
            value={'hostname':native_hostname(),'setup_port':base,'https_port':base+1,'backend_port':base+2,'desktop_port':base+3}
            write_private(p,value);return value
        except OSError:pass
        finally:
            for s in sockets:s.close()
    raise RuntimeError('No available isolated service ports')

def main():
    os.umask(0o077)
    home=Path(os.environ['PAI_INSTALL_HOME']);release=Path(__file__).resolve().parents[1]
    private=home/'data/runtime';private.mkdir(parents=True,exist_ok=True,mode=0o700)
    singleton=(private/'service.lock').open('a')
    try:fcntl.flock(singleton.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:return
    settings=json.loads((home/'data/settings.json').read_text());cfg=choose_ports(private)
    write_private(private/'process.json',{'pid':os.getpid()})
    if cfg['hostname']!=native_hostname():raise RuntimeError('Computer name changed; keep the previous phone entry pending user review')
    provider=settings['provider'];name=settings['assistant_name']
    ds_key=settings['api_key'] if provider=='deepseek' else ''
    if ds_key:os.environ['DEEPSEEK_API_KEY']=ds_key
    else:os.environ.pop('DEEPSEEK_API_KEY',None)
    os.environ['JUHE_TRAIN_KEY']=settings.get('train_key','')
    from commercial_voice import CommercialVoice,CommercialQueries,ModelClient
    from commerce import Commerce
    base='https://api.deepseek.com' if provider=='deepseek' else 'https://open.bigmodel.cn/api/paas/v4'
    model=NamedModel(ModelClient({'base_url':base,'api_key':settings['api_key'],'model':settings['model']}),name)
    speech,token=load_speech(release/'assets/models',private)
    commercial=private/'commercial';voice_private=commercial/'.private';voice_private.mkdir(parents=True,exist_ok=True,mode=0o700)
    queries=CommercialQueries(decider=model,commerce=Commerce.from_private(commercial,model),root=commercial)
    # Directory availability follows actual configuration, never task keywords.
    train_verified=bool(settings.get('train_key')) and settings.get('train_verified_digest')==hashlib.sha256(settings['train_key'].encode()).hexdigest()
    queries.tools=[t for t in queries.tools if t not in ('web_search','travel','rail') or
                   (t in ('web_search','travel') and bool(ds_key)) or (t=='rail' and bool(ds_key) and train_verified)]
    features=None
    if ds_key:
        from assistant_features import AssistantFeatures,FeatureQueries,NativeAdapter
        from xiaopai_secretary.model import deepseek_model
        native=NativeAdapter(private);native.app=release/'native/言小办系统事项.app'
        features=AssistantFeatures(private,model,native=native,secretary_model=NamedModel(deepseek_model(ds_key),name))
        queries=FeatureQueries(queries,features)
    voice=CommercialVoice(voice_private/'commercial-conversations.db',token,model=model,queries=queries,speech_call=speech)
    voice.features=features
    voice.install_profile={'assistant_name':name,'user_name':settings.get('user_name',''),'model_provider':provider,
        'model':settings['model'],'capabilities':queries.tools,'other_model_adaptation':'per_capability_required',
        'cookie_name':'pai_session_'+json.loads((home/'identity.json').read_text())['id'][:12]}
    welcome=speech('synthesize',json.dumps({'text':'我在，请说。'},ensure_ascii=False).encode(),'application/json',token.read_text().strip())
    voice.welcome_path=private/'ready.wav';private_write(voice.welcome_path,base64.b64decode(welcome['audio']))
    # Shared assets and conversation/speech implementation remain the accepted UI.
    origin=f"https://{cfg['hostname']}:{cfg['https_port']}";grant=login_link(voice_private,__import__('time').time())
    backend,pool=make_server(cfg['backend_port'],pairing_code=grant['code'],lifetime=None,
        session_path=voice_private/'device-sessions.json',pairing_expires_at=grant['expires_at'],voice=voice,public_origin=origin,
        pairing_provider=lambda:login_link(voice_private,__import__('time').time()))
    certificate=prepare_certificates(private/'certificates',cfg['hostname'])
    secure=TLSServer(('0.0.0.0',cfg['https_port']),proxy_handler(origin,cfg['backend_port']),certificate['context'])
    setup=LANServer(('0.0.0.0',cfg['setup_port']),public_certificate_handler(certificate,origin,cfg['setup_port']))
    desktop,desktop_pool=make_server(cfg['desktop_port'],lifetime=90*86400,voice=voice)
    servers=[backend,secure,setup,desktop]
    for server in servers:threading.Thread(target=server.serve_forever,daemon=True).start()
    discovery=subprocess.Popen(['/usr/bin/dns-sd','-R',name,'_https._tcp','local',str(cfg['https_port']),'path=/'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def file_hash(path):return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    shopping_hashes={n:file_hash(voice_private/n) for n in ('commerce.json','jd-union.json','taobao-union.json','flight-provider.json')}
    write_private(private/'service-info.json',{'pid':os.getpid(),'platform':'macOS','origin':origin,'setup_port':cfg['setup_port'],
        'https_port':cfg['https_port'],'backend_port':cfg['backend_port'],'desktop_port':cfg['desktop_port'],
        'started_at':datetime.now().astimezone().isoformat(),'assistant_name':name,'phone_tested':False,
        'flight':getattr(queries,'base',queries).flights.status,
        'configuration_digest':hashlib.sha256(json.dumps({'settings':settings,'shopping':shopping_hashes},sort_keys=True).encode()).hexdigest()})
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    try:stop.wait()
    finally:
        for server in servers:server.shutdown();server.server_close()
        # A shutdown never cuts a bookkeeping/system write in half. No new
        # requests are accepted; let the owned in-flight turn finish first.
        acquired=voice.bridge.slot.acquire(timeout=180)
        if acquired:voice.bridge.slot.release()
        pool.shutdown(wait=False,cancel_futures=True);desktop_pool.shutdown(wait=False,cancel_futures=True)
        discovery.terminate();discovery.wait(timeout=5)

if __name__=='__main__':main()
