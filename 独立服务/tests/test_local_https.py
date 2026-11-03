from datetime import datetime,timedelta,timezone
import http.client
import json
from pathlib import Path
import plistlib
import socket
import ssl
import tempfile
import threading
import unittest

from cryptography import x509
from app import make_server
from local_https import (prepare_certificates,valid_local_hostname,lan_peer,TLSServer,LANServer,
                         proxy_handler,setup_handler)
from test_http import FakeClient,FakeTrain


def free_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


class CertificateTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_ca_identity_unique_and_preserved_on_restart(self):
        first=prepare_certificates(self.root/'one','fixture.local')
        second=prepare_certificates(self.root/'one','fixture.local')
        other=prepare_certificates(self.root/'two','fixture.local')
        self.assertEqual(first['fingerprint_sha256'],second['fingerprint_sha256'])
        self.assertNotEqual(first['fingerprint_sha256'],other['fingerprint_sha256'])
        self.assertFalse(second['server_certificate_renewed'])
        self.assertEqual((self.root/'one/root-ca-key.pem').stat().st_mode&0o777,0o600)
    def test_leaf_renewal_preserves_the_phone_trust_root(self):
        now=datetime.now(timezone.utc)
        first=prepare_certificates(self.root,'fixture.local',now)
        serial=x509.load_pem_x509_certificate(first['certificate'].read_bytes()).serial_number
        renewed=prepare_certificates(self.root,'fixture.local',now+timedelta(days=340))
        self.assertEqual(first['fingerprint_sha256'],renewed['fingerprint_sha256'])
        self.assertTrue(renewed['server_certificate_renewed'])
        self.assertNotEqual(serial,x509.load_pem_x509_certificate(renewed['certificate'].read_bytes()).serial_number)
    def test_hostname_changes_are_not_silently_applied(self):
        prepare_certificates(self.root,'fixture.local')
        with self.assertRaisesRegex(ValueError,'hostname changed'):prepare_certificates(self.root,'other.local')
        for value in ['example.com','*.local','a.local.evil','https://a.local','a_b.local']:
            self.assertFalse(valid_local_hostname(value))
    def test_profile_contains_only_a_public_root_certificate(self):
        certificate=prepare_certificates(self.root,'fixture.local')
        profile=plistlib.loads(certificate['profile'].read_bytes())
        self.assertEqual(len(profile['PayloadContent']),1)
        payload=profile['PayloadContent'][0]
        self.assertEqual(payload['PayloadType'],'com.apple.security.root')
        self.assertNotIn(b'PRIVATE KEY',certificate['profile'].read_bytes())
        self.assertTrue(x509.load_der_x509_certificate(payload['PayloadContent']).extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    def test_incomplete_trust_root_is_not_regenerated(self):
        (self.root/'root-ca-key.pem').write_text('fixture partial root')
        with self.assertRaisesRegex(ValueError,'Incomplete'):prepare_certificates(self.root,'fixture.local')
    def test_lan_boundary_rejects_public_and_proxy_benchmark_addresses(self):
        for ip in ['127.0.0.1','192.168.2.10','10.2.3.4','172.16.1.2','169.254.1.2']:self.assertTrue(lan_peer(ip))
        for ip in ['8.8.8.8','203.0.113.1','198.18.0.1','0.0.0.0','invalid']:self.assertFalse(lan_peer(ip))


class LocalTLSTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.hostname='fixture.local';self.tls_port=free_port();self.backend_port=free_port();self.setup_port=free_port()
        self.origin=f'https://{self.hostname}:{self.tls_port}'
        self.certificate=prepare_certificates(self.root/'certificates',self.hostname)
        self.backend,self.pool=make_server(self.backend_port,FakeClient(),FakeTrain(),
            pairing_code='123456789012',lifetime=None,session_path=self.root/'sessions.json',public_origin=self.origin)
        self.tls=TLSServer(('127.0.0.1',self.tls_port),proxy_handler(self.origin,self.backend_port),self.certificate['context'])
        self.setup=LANServer(('127.0.0.1',self.setup_port),setup_handler(self.certificate,self.origin,
            f'http://192.168.50.152:{self.setup_port}/root-ca.mobileconfig',self.origin+'/#login=123456789012',self.setup_port))
        for server in [self.backend,self.tls,self.setup]:threading.Thread(target=server.serve_forever,daemon=True).start()
        self.context=ssl.create_default_context(cafile=str(self.certificate['ca']))
    def tearDown(self):
        for server in [self.backend,self.tls,self.setup]:server.shutdown();server.server_close()
        self.pool.shutdown(wait=True);self.temp.cleanup()
    def request(self,method,path,body=None,headers=None):
        connection=http.client.HTTPSConnection('127.0.0.1',self.tls_port,context=self.context,timeout=5)
        connection.connect()
        hdr={'Host':f'{self.hostname}:{self.tls_port}',**(headers or {})}
        connection.request(method,path,body=body,headers=hdr)
        response=connection.getresponse();data=response.read();result=(response.status,dict(response.getheaders()),data)
        connection.close();return result
    def pair(self):
        return self.request('POST','/api/pair',json.dumps({'code':'123456789012'}),{'Origin':self.origin,'Content-Type':'application/json'})
    def test_authenticated_https_uses_secure_cookie_and_existing_session_contract(self):
        self.assertEqual(self.request('GET','/')[0],200)
        self.assertEqual(self.request('GET','/api/status')[0],401)
        status,headers,_=self.pair();self.assertEqual(status,200)
        self.assertIn('Secure',headers['Set-Cookie']);self.assertIn('HttpOnly',headers['Set-Cookie'])
        cookie=headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('GET','/api/status',headers={'Cookie':cookie})[0],200)
        self.assertEqual(self.request('GET','/.private/root-ca-key.pem',headers={'Cookie':cookie})[0],404)
        self.assertTrue((self.root/'sessions.json').is_file())
    def test_wrong_host_origin_and_chunked_requests_are_rejected(self):
        self.assertEqual(self.request('GET','/',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('POST','/api/pair','{}',{'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('POST','/api/pair','{}',{'Origin':self.origin,'Transfer-Encoding':'chunked'})[0],400)
    def test_untrusted_certificate_is_rejected_by_a_normal_tls_client(self):
        connection=http.client.HTTPSConnection('127.0.0.1',self.tls_port,timeout=5)
        with self.assertRaises(ssl.SSLCertVerificationError):connection.connect()
        connection.close()
    def test_bootstrap_serves_certificate_but_does_not_reveal_login_to_remote_hosts(self):
        connection=http.client.HTTPConnection('127.0.0.1',self.setup_port,timeout=5)
        connection.request('GET','/',headers={'Host':f'192.168.50.152:{self.setup_port}'})
        response=connection.getresponse();page=response.read()
        self.assertNotIn(b'123456789012',page);self.assertNotIn(b'data:image/svg',page)
        connection.request('GET','/root-ca.mobileconfig');response=connection.getresponse()
        self.assertEqual(response.status,200);self.assertEqual(plistlib.loads(response.read())['PayloadType'],'Configuration')
        connection.request('GET','/',headers={'Host':f'127.0.0.1:{self.setup_port}'})
        response=connection.getresponse();page=response.read();self.assertIn(b'data:image/svg',page)
        connection.close()


if __name__=='__main__':unittest.main()
