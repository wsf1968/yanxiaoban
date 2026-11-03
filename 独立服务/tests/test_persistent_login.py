import hashlib
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import make_server
from session_store import load_sessions
import test_http


class PersistentLoginTests(unittest.TestCase):
    request = test_http.HttpTests.request

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / '.private' / 'sessions.json'
        self.now = [1000]
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.origin = f'http://127.0.0.1:{self.port}'
        self.start()

    def start(self):
        self.server, self.pool = make_server(self.port, test_http.FakeClient(), test_http.FakeTrain(),
            pairing_code='123456789012', clock=lambda: self.now[0],
            lifetime=None, session_path=self.path)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.pool.shutdown(wait=True)

    def tearDown(self):
        self.stop()
        self.directory.cleanup()

    def login(self):
        return self.request('POST', '/api/pair', json.dumps({'code': '123456789012'}),
                            {'Origin': self.origin})

    def test_login_survives_restart_and_cookie_has_90_day_lifetime(self):
        status, headers, _ = self.login()
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        token = cookie.split('=', 1)[1]
        self.assertIn('Max-Age=7776000', headers['Set-Cookie'])
        self.assertIn('SameSite=Lax', headers['Set-Cookie'])
        self.assertNotIn(token, self.path.read_text())
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.stop()
        self.now[0] += 10800
        self.start()
        auth = {'Cookie': cookie}
        status, _, raw = self.request('GET', '/api/status', headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['access'], 'lan')
        self.assertIsNone(json.loads(raw)['expires_at'])
        self.assertIn(b'/app.js', self.request('GET', '/', headers=auth)[2])
        self.assertEqual(self.request('GET', '/api/status', headers={'Cookie': 'pai_session=forged'})[0], 401)
        self.now[0] += 90 * 86400
        self.assertEqual(self.request('GET', '/api/status', headers=auth)[0], 401)

    def test_authenticated_script_load_recovers_cross_app_navigation(self):
        _, headers, _ = self.login()
        cookie = headers['Set-Cookie'].split(';')[0]
        # Initial navigation can omit an old Strict cookie; subsequent subresources include it.
        self.assertIn(b'pair.js', self.request('GET', '/')[2])
        auth = {'Cookie': cookie}
        status, _, script = self.request('GET', '/pair.js?v=remember-login-2', headers=auth)
        self.assertEqual(status, 200)
        self.assertIn(b"fetch('api/status')", script)
        status, refreshed, _ = self.request('GET', '/api/status', headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(refreshed['Set-Cookie'].split(';')[0], cookie)
        self.assertIn('SameSite=Lax', refreshed['Set-Cookie'])
        # Lax cookies do not bypass POST origin validation.
        self.assertEqual(self.request('POST', '/api/tasks', '{}',
            {'Cookie': cookie, 'Origin': 'https://foreign.example'})[0], 403)

    def test_chrome_login_does_not_consume_safari_login_link(self):
        _, chrome, _ = self.login()
        status, safari, _ = self.login()
        self.assertEqual(status, 200)
        self.assertNotEqual(chrome['Set-Cookie'], safari['Set-Cookie'])
        for headers in (chrome, safari):
            cookie = headers['Set-Cookie'].split(';')[0]
            self.assertEqual(self.request('GET', '/api/status', headers={'Cookie': cookie})[0], 200)
        self.assertEqual(len(json.loads(self.path.read_text())), 2)
        self.now[0] += 1801
        self.assertEqual(self.login()[0], 410)
        cookie = safari['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('GET', '/api/status', headers={'Cookie': cookie})[0], 200)

    def test_failed_save_does_not_consume_first_login(self):
        with patch('app.save_sessions', side_effect=OSError('fixture')):
            status, headers, _ = self.login()
        self.assertEqual(status, 503)
        self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(self.login()[0], 200)

    def test_disk_expiry_and_invalid_records_are_not_authorization(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({'bad': 999999999, 'a'*64: 999, 'b'*64: 'infinite'}))
        self.assertEqual(load_sessions(self.path, self.now[0]), {})
