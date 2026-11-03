"""Loopback-only unified setup and maintenance UI, independent of the voice service."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit
import argparse,base64,hashlib,hmac,io,json,os,secrets,subprocess,sys,threading,time
from maintenance import Maintenance,read_json,write_json,safe_error

ROOT=Path(__file__).resolve().parent

def connection_guide(manager):
    private=manager.home/'data/runtime';info=read_json(private/'service-info.json',{})
    if not info or not manager.service():raise ValueError('service_start_failed')
    release=manager.release();sys.path.insert(0,str(release/'独立服务'))
    from mobile import login_link
    from local_https import qr_data
    from macos_local import lan_address
    import qrcode # Verified bundled library, never loaded from a user's site-packages.
    login=login_link(private/'commercial/.private',time.time())['code']
    url=info['origin']+'/?entry=siri#login='+login
    config=read_json(private/'runtime.json')
    profile=f"http://{lan_address()}:{config['setup_port']}/root-ca.mobileconfig"
    answer={'origin':info['origin'],'siri_url':info['origin']+'/?entry=siri','profile_qr':qr_data(profile),'phone_qr':qr_data(url),
        'certificate_instructions':['iPhone和Mac连接同一Wi-Fi。','扫码下载本机连接证书，在设置→通用→VPN与设备管理中安装。','设置→通用→关于本机→证书信任设置，开启这份证书的完全信任。','扫描手机连接二维码，允许麦克风，按提示完成第一次播放。'],
        'phone_acceptance':'pending'}
    if manager.config()['provider']=='deepseek':
        from feature_accounts import FeatureAccounts
        accounts=FeatureAccounts(private/'features',release/'记账模块',str(release/'runtime/node/bin/node'))
        token=accounts.bootstrap_token()
        if token:answer['feature_qr']=qr_data(info['origin']+'/#login='+login+'&feature_setup='+token)
    return answer

def make_server(manager,port=0):
    token=secrets.token_urlsafe(32);session=secrets.token_urlsafe(32);busy=threading.Lock();cancel=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def send(self,status,value,kind='application/json; charset=utf-8',cookie=False):
            raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if cookie:self.send_header('Set-Cookie','pai_install_session='+session+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400')
            self.end_headers()
            try:self.wfile.write(raw)
            except OSError:pass
        def host_ok(self):return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'
        def authorized(self):
            try:return hmac.compare_digest(SimpleCookie(self.headers.get('Cookie',''))['pai_install_session'].value,session)
            except (KeyError,ValueError):return False
        def do_GET(self):
            if not self.host_ok():return self.send(403,{'error':'local_page_required'})
            path=urlsplit(self.path).path
            assets={'/':('installer.html','text/html; charset=utf-8'),'/installer.js':('installer.js','text/javascript'),'/installer.css':('installer.css','text/css')}
            if path in assets:
                name,kind=assets[path];return self.send(200,(ROOT/'web'/name).read_bytes(),kind)
            if not self.authorized():return self.send(401,{'error':'open_installer_app'})
            try:
                if path=='/api/state':return self.send(200,manager.inspect()|{'operation_in_progress':busy.locked(),'cancel_requested':cancel.is_set() and busy.locked()})
                if path=='/api/connection':return self.send(200,connection_guide(manager))
                if path=='/api/diagnostics':return self.send(200,manager.check())
            except Exception as error:return self.send(400,{'error':safe_error(error)})
            self.send(404,{'error':'not_found'})
        def do_POST(self):
            origin=f'http://127.0.0.1:{self.server.server_port}'
            if not self.host_ok() or self.headers.get('Origin')!=origin:return self.send(403,{'error':'local_page_required'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 1<=length<=8192 or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('invalid_request')
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict):raise ValueError('invalid_request')
            except (ValueError,UnicodeError):return self.send(400,{'error':'invalid_request'})
            path=urlsplit(self.path).path
            if path=='/api/login':
                valid=set(body)=={'token'} and isinstance(body['token'],str) and hmac.compare_digest(body['token'],token)
                return self.send(200 if valid else 401,{'connected':valid},cookie=valid)
            if not self.authorized():return self.send(401,{'error':'open_installer_app'})
            if path=='/api/cancel':cancel.set();return self.send(200,{'stopping_after_current_step':True})
            if not busy.acquire(blocking=False):return self.send(409,{'error':'operation_in_progress'})
            try:
                if path=='/api/settings':return self.send(200,manager.save_settings(body))
                if path=='/api/flight':return self.send(200,manager.save_flight(body))
                if path=='/api/train-test':
                    if set(body)!={'request'}:raise ValueError('invalid_request')
                    return self.send(200,manager.verify_train(body['request']))
                if path=='/api/platform':return self.send(200,manager.save_platform(body))
                if path=='/api/platform-test':
                    if set(body)!={'platform','request'}:raise ValueError('invalid_request')
                    return self.send(200,manager.verify_platform(body['platform'],body['request']))
                if path=='/api/action':
                    if set(body)!={'action'}:raise ValueError('invalid_request')
                    return self.send(200,manager.execute(body['action']))
                if path=='/api/acceptance':
                    if body!={'phone_confirmed':True}:raise ValueError('invalid_request')
                    write_json(manager.home/'data/phone-acceptance.json',{'phone_confirmed_by_user':True,'provenance':'manual_user_confirmation','at':time.time()})
                    return self.send(200,{'phone_confirmed_by_user':True})
                if path=='/api/assist':
                    if set(body)!={'request'} or not isinstance(body['request'],str) or not 1<=len(body['request'])<=500:raise ValueError('invalid_request')
                    cancel.clear();self.send_response(200);self.send_header('Content-Type','application/x-ndjson; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Connection','close');self.end_headers();self.close_connection=True
                    connected=True
                    def emit(value):
                        nonlocal connected
                        if connected:
                            try:self.wfile.write(json.dumps(value,ensure_ascii=False).encode()+b'\n');self.wfile.flush()
                            except OSError:connected=False
                    result=manager.assist(body['request'],emit,cancelled=cancel.is_set)
                    emit({'event':'complete','result':result});return
                return self.send(404,{'error':'not_found'})
            except Exception as error:self.send(400,{'error':safe_error(error),'manual_available':True})
            finally:busy.release()
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    return server,f'http://127.0.0.1:{server.server_port}/#token='+token

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--home',required=True);parser.add_argument('--bundle',required=True);parser.add_argument('--headless-start',action='store_true');args=parser.parse_args()
    manager=Maintenance(args.home,args.bundle)
    if args.headless_start:manager.start();return
    previous=read_json(manager.home/'installer-session.json',{})
    if previous.get('bundle_signature')==manager.manifest().get('signature') and Path(previous.get('bundle_path','/nonexistent')).exists():
        try:
            parsed=urlsplit(previous['url'])
            if parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port:
                import socket
                with socket.create_connection(('127.0.0.1',parsed.port),timeout=2):pass
                subprocess.run(['/usr/bin/open',previous['url']],check=True);return
        except (OSError,KeyError,ValueError):pass
    server,url=make_server(manager)
    write_json(manager.home/'installer-session.json',{'pid':os.getpid(),'url':url,'bundle_path':str(manager.bundle),'bundle_signature':manager.manifest().get('signature')})
    # Only a local bootstrap link, never a model/API key. Not printed to logs.
    subprocess.run(['/usr/bin/open',url],check=True)
    server.serve_forever()

if __name__=='__main__':main()
