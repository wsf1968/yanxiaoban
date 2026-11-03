"""Per-install local TLS and a bounded LAN proxy to the unchanged commercial server."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import html
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import ssl
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def valid_local_hostname(hostname):
    return bool(isinstance(hostname,str) and re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.local',hostname))


def prepare_certificates(directory, hostname, now=None):
    """Keep the installed CA stable; renew only the server certificate when needed."""
    if not valid_local_hostname(hostname): raise ValueError('A valid native Bonjour hostname is required')
    now = now or datetime.now(timezone.utc)
    directory = Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700);directory.chmod(0o700)
    ca_path, key_path = directory/'root-ca.pem', directory/'root-ca-key.pem'
    leaf_path, leaf_key_path = directory/'server.pem', directory/'server-key.pem'
    if ca_path.exists() != key_path.exists(): raise ValueError('Incomplete CA files; original trust identity was not replaced')
    if ca_path.exists():
        authority = x509.load_pem_x509_certificate(ca_path.read_bytes())
        ca_key = serialization.load_pem_private_key(key_path.read_bytes(),password=None)
        if authority.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo) != ca_key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo):
            raise ValueError('CA key mismatch')
        if authority.not_valid_after_utc < now+timedelta(days=365): raise ValueError('CA renewal requires an explicit trust update')
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        identity = uuid.uuid4().hex[:8]
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'言小办本机连接 '+identity)])
        authority = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True,content_commitment=False,key_encipherment=False,
                data_encipherment=False,key_agreement=False,key_cert_sign=True,crl_sign=True,
                encipher_only=False,decipher_only=False),critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),critical=False)
            .sign(ca_key,hashes.SHA256()))
        private_write(key_path,ca_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        private_write(ca_path,authority.public_bytes(serialization.Encoding.PEM))
    renew = True
    if leaf_path.is_file() and leaf_key_path.is_file():
        leaf = x509.load_pem_x509_certificate(leaf_path.read_bytes())
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        if hostname not in names: raise ValueError('Computer hostname changed; existing phone entry was not silently replaced')
        authority.public_key().verify(leaf.signature,leaf.tbs_certificate_bytes,ec.ECDSA(leaf.signature_hash_algorithm))
        renew = leaf.not_valid_after_utc < now+timedelta(days=30)
    if renew:
        key = ec.generate_private_key(ec.SECP256R1())
        leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,hostname)]))
            .issuer_name(authority.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname),x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False)
            .sign(ca_key,hashes.SHA256()))
        private_write(leaf_key_path,key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        private_write(leaf_path,leaf.public_bytes(serialization.Encoding.PEM))
    digest = authority.fingerprint(hashes.SHA256()).hex()
    identifier = 'local.yanxiaoban.connection.'+digest[:24]
    cert_name = authority.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    profile = {'PayloadType':'Configuration','PayloadVersion':1,'PayloadIdentifier':identifier,
        'PayloadUUID':str(uuid.uuid5(uuid.NAMESPACE_URL,identifier)), 'PayloadDisplayName':cert_name,
        'PayloadDescription':'仅安装这台Mac的言小办本机连接证书，不包含设备管理、VPN或代理设置。安装后仍需在证书信任设置中手动开启信任。',
        'PayloadContent':[{'PayloadType':'com.apple.security.root','PayloadVersion':1,
            'PayloadIdentifier':identifier+'.root','PayloadUUID':str(uuid.uuid5(uuid.NAMESPACE_URL,identifier+'.root')),
            'PayloadDisplayName':cert_name,'PayloadCertificateFileName':'yanxiaoban-root-ca.cer',
            'PayloadContent':authority.public_bytes(serialization.Encoding.DER)}]}
    private_write(directory/'root-ca.mobileconfig',plistlib.dumps(profile))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(leaf_path,leaf_key_path)
    return {'context':context,'ca':ca_path,'certificate':leaf_path,'profile':directory/'root-ca.mobileconfig',
            'name':cert_name,'fingerprint_sha256':digest,'server_certificate_renewed':renew}


def lan_peer(address):
    try:
        ip = ipaddress.ip_address(address)
        return ip.is_loopback or any(ip in net for net in (
            ipaddress.ip_network('10.0.0.0/8'),ipaddress.ip_network('172.16.0.0/12'),
            ipaddress.ip_network('192.168.0.0/16'),ipaddress.ip_network('169.254.0.0/16')))
    except ValueError: return False


class LANServer(ThreadingHTTPServer):
    daemon_threads = True
    def verify_request(self,request,client_address): return lan_peer(client_address[0])


class TLSServer(LANServer):
    def __init__(self,address,handler,context):
        self.context = context
        super().__init__(address,handler)
    def get_request(self):
        connection,address = super().get_request()
        connection.settimeout(12)
        # Handshake is deferred to the request thread so a silent LAN client cannot block accept().
        return self.context.wrap_socket(connection,server_side=True,do_handshake_on_connect=False),address


def proxy_handler(origin, backend_port):
    from urllib.parse import urlsplit
    expected_host = urlsplit(origin).netloc
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def error(self,status):
            self.send_response(status);self.send_header('Content-Length','0');self.end_headers()
        def forward(self):
            if self.headers.get('Host') != expected_host: return self.error(403)
            if not self.path.startswith('/') or self.path.startswith('//'): return self.error(400)
            if self.headers.get('Transfer-Encoding'): return self.error(400)
            if self.command=='POST' and self.headers.get('Origin') != origin: return self.error(403)
            try: length = int(self.headers.get('Content-Length','0'))
            except ValueError: return self.error(400)
            if not 0<=length<=1_000_000: return self.error(413)
            body = self.rfile.read(length) if length else None
            if body is not None and len(body)!=length: return self.error(400)
            headers = {'Host':expected_host}
            for name in ['Content-Type','Accept','Origin','Cookie','X-Pi-Conversation','X-Pi-Revision','X-Pi-Turn','X-Pi-Capture-End','X-Pi-Segment','X-Pi-Segments']:
                if self.headers.get(name) is not None: headers[name] = self.headers[name]
            connection = http.client.HTTPConnection('127.0.0.1',backend_port,timeout=155)
            try:
                connection.request(self.command,self.path,body=body,headers=headers)
                reply = connection.getresponse()
                if reply.getheader('Content-Type', '').startswith('application/x-ndjson'):
                    self.send_response(reply.status)
                    for name,value in reply.getheaders():
                        if name.lower() not in ('connection','transfer-encoding','content-length','server','date'):
                            self.send_header(name,value)
                    self.send_header('Connection','close')
                    self.send_header('Permissions-Policy','microphone=(self), camera=(), geolocation=()')
                    self.end_headers(); self.close_connection = True
                    total = 0
                    while True:
                        chunk = reply.readline(10_000_001)
                        if not chunk: break
                        total += len(chunk)
                        if total > 12_000_000: break
                        self.wfile.write(chunk); self.wfile.flush()
                    return
                data = reply.read(10_000_001)
                if len(data)>10_000_000: return self.error(502)
                self.send_response(reply.status)
                for name,value in reply.getheaders():
                    if name.lower() not in ('connection','transfer-encoding','content-length','server','date'):
                        self.send_header(name,value)
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Permissions-Policy','microphone=(self), camera=(), geolocation=()')
                self.send_header('X-Content-Type-Options','nosniff')
                self.end_headers(); self.wfile.write(data)
            except (OSError,http.client.HTTPException):
                self.close_connection = True
            finally: connection.close()
        do_GET = forward
        do_POST = forward
    return Proxy


def qr_data(value):
    import qrcode
    import qrcode.image.svg
    qr = qrcode.make(value,image_factory=qrcode.image.svg.SvgPathImage,box_size=6,border=3)
    out = io.BytesIO();qr.save(out)
    return 'data:image/svg+xml;base64,'+base64.b64encode(out.getvalue()).decode()


def setup_handler(certificate, origin, certificate_url, login_url, port, feature_setup=None):
    esc=html.escape
    class Setup(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def send(self,status,data,content_type):
            self.send_response(status);self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'none'; img-src data:; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers();self.wfile.write(data)
        def do_GET(self):
            if self.path == '/root-ca.mobileconfig':
                return self.send(200,Path(certificate['profile']).read_bytes(),'application/x-apple-aspen-config')
            if self.path == '/root-ca.cer':
                ca=x509.load_pem_x509_certificate(Path(certificate['ca']).read_bytes())
                return self.send(200,ca.public_bytes(serialization.Encoding.DER),'application/pkix-cert')
            if self.path != '/': return self.send(404,b'','text/plain')
            local = ipaddress.ip_address(self.client_address[0]).is_loopback
            if not local or self.headers.get('Host') not in (f'127.0.0.1:{port}',f'localhost:{port}'):
                page='<h1>连接这台 Mac 的言小办</h1><p>请在 Mac 上打开连接说明并扫码；首次需要安装本机连接证书。</p><a href="/root-ca.mobileconfig">下载本机连接证书</a>'
            else:
                page=f'''<h1>iPhone 首次连接说明</h1><p>这是连接说明页。言小办主界面沿用现有商用测试版。</p>
                <p><a href="http://127.0.0.1:18782/">在这台 Mac 上打开言小办主界面</a></p>
                <p>手机和 Mac 先连接同一个 Wi-Fi。</p>
                <h2>1 扫码安装本机连接证书</h2><img alt="安装本机连接证书" src="{qr_data(certificate_url)}">
                <ol><li>用 iPhone 相机扫码，在 Safari 打开并点“允许”下载描述文件。</li>
                <li>切换到 iPhone 的“设置”，点“已下载描述文件”。若没有此项，打开“通用 → VPN与设备管理”。</li>
                <li>选择 <strong>{esc(certificate['name'])}</strong>，点右上角“安装”，输入 iPhone 锁屏密码，再确认安装。</li>
                <li>回到“设置 → 通用 → 关于本机 → 证书信任设置”，开启这份言小办证书的完全信任。</li></ol>
                <p>这份证书仅用于连接这台 Mac；没有安装设备管理、VPN或代理。</p>
                <h2>2 扫码打开言小办</h2><img alt="首次授权并打开言小办" src="{qr_data(login_url() if callable(login_url) else login_url)}">
                <p>首次连接二维码短时有效，请勿转发。进入后允许麦克风；Safari首次播放若需要轻点，请按页面提示。</p>
                <h2>以后直接打开</h2><p><a href="{esc(origin)}/">{esc(origin)}/</a></p>
                <p>此入口使用电脑的固定局域网名称，普通IP变化不需要改网址。手机实际收音、播报和地址变化仍需实测。</p>
                <details><summary>核对证书指纹</summary><p>{esc(certificate['fingerprint_sha256'])}</p></details>'''
                feature_url=feature_setup() if feature_setup else None
                if feature_url:
                    page=f'''<h1>启用家庭记账与系统事项</h1><p>用你的 iPhone 扫码，在现有言小办中确认姓名和账本名称。只需首次授权，日常继续用原来的 Siri 口令。</p><img alt="启用家庭记账与系统事项" src="{qr_data(feature_url)}"><p>此码授予家庭账管理员身份，请只用你自己的手机扫描，不要转发。家人的加入链接在手机设置中另行生成。</p><p>系统事项会保存在此Mac已连接的iCloud“言小办商用提醒”和“言小办商用日程”中，同账户iPhone需开启同步和通知。</p><details><summary>手机基础连接与证书（已连接的手机无需重做）</summary>'''+page+'</details>'
            document=('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>言小办连接手机</title><style>body{font:17px/1.7 system-ui;max-width:760px;margin:32px auto;padding:0 22px;color:#18212d;background:#f8fafc}h1{font-size:28px}h2{margin-top:32px;font-size:21px}img{width:230px;height:230px;background:white;padding:10px;border-radius:12px}p{overflow-wrap:anywhere}a{color:#155bc1}details{font-size:13px;color:#526070}</style>'+page+'</html>').encode()
            self.send(200,document,'text/html; charset=utf-8')
    return Setup
