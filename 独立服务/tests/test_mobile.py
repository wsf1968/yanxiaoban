import http.client
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import make_server
import test_http as http_fixtures
from test_http import FakeClient, FakeTrain


class MobileTests(unittest.TestCase):
    request = http_fixtures.HttpTests.request

    def setUp(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        self.now = [1000]
        self.code = '123456789012'  # Fixture only, not a real pairing code.
        self.server, self.pool = make_server(self.port, FakeClient(), FakeTrain(),
                    pairing_code=self.code, clock=lambda: self.now[0])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.pool.shutdown(wait=True)

    def pair(self, code=None, origin=None):
        return self.request('POST', '/api/pair', json.dumps({'code': code or self.code}),
                            {'Origin': origin or self.origin})

    def test_guest_cannot_get_session_or_query(self):
        status, headers, raw = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertNotIn('Set-Cookie', headers)
        self.assertNotIn(self.code.encode(), raw)
        for path in ('/api/status', '/api/tasks/unknown', '/app.js', '/.env'):
            self.assertEqual(self.request('GET', path)[0], 401)
        self.assertEqual(self.request('POST', '/api/tasks', '{}', {'Origin': self.origin})[0], 403)

    def test_origin_pairing_one_use_and_session(self):
        self.assertEqual(self.pair(origin='https://evil.example')[0], 403)
        self.assertEqual(self.pair(code='000000000000')[0], 401)
        status, headers, _ = self.pair()
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(self.pair()[0], 410)
        status, _, raw = self.request('GET', '/api/status', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['access'], 'lan-test')
        status, _, raw = self.request('POST', '/api/tasks',
            json.dumps({'mode': 'search', 'query': '测试问题'}), {'Cookie': cookie, 'Origin': self.origin})
        self.assertEqual(status, 202)
        identity = json.loads(raw)['id']
        for _ in range(20):
            status, _, raw = self.request('GET', '/api/tasks/'+identity, headers={'Cookie': cookie})
            if json.loads(raw).get('result'):
                break
            time.sleep(.01)
        self.assertEqual(status, 200)
        self.assertNotIn('_owner', json.loads(raw))
        self.assertEqual(self.request('GET', '/api/tasks/'+identity,
                         headers={'Cookie': 'pai_session=forged'})[0], 401)
        self.now[0] += 7201
        self.assertEqual(self.request('GET', '/api/status', headers={'Cookie': cookie})[0], 410)

    def test_pair_expiry_rate_limit_and_interface_guard(self):
        for _ in range(10):
            self.assertEqual(self.pair(code='000000000000')[0], 401)
        self.assertEqual(self.pair()[0], 429)
        self.now[0] += 1801
        self.assertEqual(self.pair()[0], 410)
        for address in ('0.0.0.0', '8.8.8.8'):
            with self.assertRaises(ValueError):
                make_server(self.port, FakeClient(), FakeTrain(), bind_host=address, pairing_code=self.code)
        with self.assertRaises(ValueError):
            make_server(self.port, FakeClient(), FakeTrain(), bind_host='192.168.50.115')


if __name__ == '__main__':
    unittest.main()
