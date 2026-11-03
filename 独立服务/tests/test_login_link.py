import json
from pathlib import Path
import tempfile
import unittest
from mobile import login_link


class LoginLinkTests(unittest.TestCase):
    def test_link_survives_restart_without_extending_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.private'
            first = login_link(path, 1000)
            self.assertEqual(first, login_link(path, 1500))
            self.assertEqual(first['expires_at'], 2800)
            self.assertEqual((path/'login-link.json').stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(first['code'], login_link(path, 2801)['code'])

    def test_upgrade_preserves_current_qr_link(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.private'
            path.mkdir()
            note = path/'手机配对说明.txt'
            note.write_text('http://192.168.50.108:18768/#login=123456789012\n')
            os.utime(note, (1000, 1000))
            grant = login_link(path, 1500)
            self.assertEqual(grant, {'code': '123456789012', 'expires_at': 2800})
