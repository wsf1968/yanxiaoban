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


class FakeClient:
    key = 'test-only'
    def run(self, payload, progress):
        return {'state': 'empty', 'sources': [], 'notice': '测试', 'message': '没有资料'}


class FakeTrain:
    key = 'train-test-only'
    def run(self, payload, progress):
        return {'state': 'complete', 'trains': [], 'sources': [], 'notice': '高铁路由测试'}


class HttpTests(unittest.TestCase):
    def setUp(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        self.server, self.pool = make_server(self.port, FakeClient(), FakeTrain())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.pool.shutdown(wait=True)

    def request(self, method, path, body=None, headers=None):
        conn=http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        conn.request(method, path, body, headers or {})
        result=conn.getresponse(); raw=result.read()
        status, response_headers=result.status, dict(result.getheaders()); conn.close()
        return status, response_headers, raw

    def test_foreign_origin_and_host_are_rejected(self):
        self.assertEqual(self.request('GET', '/', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/status')[0], 401)
        cookie=self.request('GET', '/')[1]['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('POST', '/api/tasks', '{}',
            {'Cookie':cookie,'Origin':'https://evil.example'})[0],403)

    def test_browser_task_lifecycle(self):
        cookie=self.request('GET', '/')[1]['Set-Cookie'].split(';')[0]
        headers={'Cookie':cookie,'Origin':f'http://127.0.0.1:{self.port}'}
        status, _, raw=self.request('POST','/api/tasks',json.dumps({'mode':'search','query':'测试问题'}),headers)
        self.assertEqual(status,202); identity=json.loads(raw)['id']
        for _ in range(20):
            status, _, raw=self.request('GET','/api/tasks/'+identity,headers={'Cookie':cookie})
            task=json.loads(raw)
            if task['state']=='empty':break
            time.sleep(.01)
        self.assertEqual(task['state'],'empty')

    def test_train_routes_independently_from_model(self):
        from test_trains import payload
        cookie = self.request('GET', '/')[1]['Set-Cookie'].split(';')[0]
        headers = {'Cookie': cookie, 'Origin': f'http://127.0.0.1:{self.port}'}
        status, _, raw = self.request('POST', '/api/tasks', json.dumps(payload()), headers)
        self.assertEqual(status, 202)
        identity = json.loads(raw)['id']
        for _ in range(20):
            _, _, raw = self.request('GET', '/api/tasks/'+identity, headers={'Cookie': cookie})
            task = json.loads(raw)
            if task.get('result'):
                break
            time.sleep(.01)
        self.assertEqual(task['result']['notice'], '高铁路由测试')


if __name__=='__main__':unittest.main()
