import base64
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import wave

import test_http
from app import make_server
from commerce import Commerce, JDAdapter
from commercial_voice import CommercialQueries, CommercialVoice
from jd_curated import JDClient, QUERY


class DS:
    def __init__(self): self.calls = 0
    def ask(self, system, payload, **kwargs):
        self.calls += 1
        if 'utterance' in payload:
            return {'reply': '正在找商品。', 'capability': 'shopping', 'action': 'prepare',
                    'memory': {'used': [], 'operations': []},
                    'query': {'tool': 'shopping', 'args': {'platform': 'jd', 'request': payload['utterance']}}}, {}
        if '购物交付复核者' in system:
            return {'verdict': 'pass', 'issues': []}, {}
        if 'products' in payload:
            return {'reply': '找到一款耳机，页面报价399元，直达链接在卡片里。'}, {}
        common = {'reply': '选品原始回复', 'notes': '', 'retain': [], 'query': None, 'selection': []}
        if not payload['latest']['rows']:
            return {**common, 'action': 'query', 'query': {'eliteId': 24, 'pageIndex': 1, 'pageSize': 2}}, {}
        return {**common, 'action': 'finish', 'selection': [{'id': 'p1r1', 'reason': '主动降噪，399元在预算内。',
                'evidence': [{'field': 'skuName', 'value': '主动降噪'}, {'field': 'priceInfo.price', 'value': 399}]}]}, {}


class CommerceVoiceTests(unittest.TestCase):
    request = test_http.HttpTests.request
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();root = Path(self.temp.name)
        self.ds, self.wire, self.speech = DS(), [], []
        def transport(method, params):
            self.wire.append(method)
            if method == QUERY:
                return {'jd_union_open_goods_jingfen_query_responce': {'queryResult': json.dumps({'code': 200,
                    'data': [{'skuId': 1, 'skuName': '显示器', 'priceInfo': {'price': 999}},
                             {'skuId': 2, 'skuName': '真无线主动降噪蓝牙耳机', 'priceInfo': {'price': 399},
                              'materialUrl': 'https://jingfen.jd.com/fixture'}], 'totalCount': 2})}}
            return {'jd_union_open_promotion_common_get_responce': {'getResult': json.dumps({'code': 200,
                    'data': {'clickURL': 'https://union-click.jd.com/fixture'}})}}
        client = JDClient({'app_key': 'fixture-public', 'app_secret': 'fixture-secret', 'site_id': 'fixture-site'}, transport=transport)
        commerce = Commerce([JDAdapter(client, max_pages=1)], self.ds, root/'traces')
        token = root/'token';token.write_text('fixture-speech')
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\x01\x00'*16000)
        self.wav = buffer.getvalue()
        def speech(path, body, kind, credential):
            self.speech.append(path)
            if path == 'transcribe':
                return {'state': 'transcribed', 'text': '京东500元内主动降噪耳机', 'tailSilenceSeconds': 2}
            return {'audio': base64.b64encode(self.wav).decode(), 'mime': 'audio/wav'}
        self.voice = CommercialVoice(root/'history.db', token, model=self.ds,
            queries=CommercialQueries(search=test_http.FakeClient(), train=test_http.FakeTrain(),
                                      decider=self.ds, commerce=commerce), speech_call=speech)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0));self.port = sock.getsockname()[1]
        self.server, self.pool = make_server(self.port, voice=self.voice)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        cookie = self.request('GET', '/')[1]['Set-Cookie'].split(';')[0]
        self.headers = {'Origin': f'http://127.0.0.1:{self.port}', 'Cookie': cookie, 'Content-Type': 'application/json'}

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.pool.shutdown(wait=True);self.temp.cleanup()

    def post(self, body):
        status, _, raw = self.request('POST', '/api/roundtrip', json.dumps(body), self.headers)
        return status, json.loads(raw)

    def test_actual_query_adapter_voice_result_history_and_replay_contract(self):
        body = {'action': 'text', 'id': 'c'*32, 'revision': 0, 'requestId': 'd'*32, 'text': '京东500元内主动降噪耳机'}
        status, result = self.post(body)
        self.assertEqual(status, 200, result)
        self.assertEqual((result['tool'], result['queryStatus'], result['taskExecuted']), ('shopping', 'complete', True))
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['commercial']['products'][0]['id'], '2')
        self.assertEqual(result['commercial']['products'][0]['url'], 'https://union-click.jd.com/fixture')
        self.assertNotIn('shopping', result)  # never the legacy cart receipt
        self.assertNotIn('fixture-secret', json.dumps(result))
        calls = len(self.wire);model_calls = self.ds.calls
        self.assertEqual(calls, 2)
        self.assertEqual(self.post(body)[0], 200)
        self.assertEqual(self.post({'action': 'replay', 'id': 'c'*32, 'requestId': 'd'*32})[0], 200)
        self.assertEqual((len(self.wire), self.ds.calls), (calls, model_calls))
        _, history = self.post({'action': 'history', 'before': 0})
        self.assertEqual(history['items'][0]['result']['commercial']['products'][0]['price'], 399)

    def test_audio_uses_existing_asr_and_tts_without_legacy_cart_executor(self):
        headers = {**self.headers, 'Content-Type': 'audio/wav', 'X-Pi-Conversation': 'a'*32,
                   'X-Pi-Revision': '0', 'X-Pi-Turn': 'b'*32, 'X-Pi-Segment': '0', 'X-Pi-Segments': '1'}
        status, _, raw = self.request('POST', '/api/roundtrip', self.wav, headers)
        result = json.loads(raw)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.speech, ['transcribe', 'synthesize'])
        self.assertEqual((result['tool'], result['commercial']['kind']), ('shopping', 'shopping'))
        self.assertEqual(result['spoken'], '找到一款耳机，页面报价399元，直达链接在卡片里。')

    def test_rejected_oral_draft_still_reaches_phone_and_history_as_partial_with_product(self):
        original = self.ds.ask
        def reject_oral(system, payload, **kwargs):
            if system.startswith('你是购物交付复核者'):
                return {'verdict':'revise','issues':['风格建议不应吞掉商品卡']}, {}
            return original(system,payload,**kwargs)
        self.ds.ask = reject_oral
        status,result=self.post({'action':'text','id':'e'*32,'revision':0,
                                 'requestId':'f'*32,'text':'500元内主动降噪耳机'})
        self.assertEqual(status,200,result)
        self.assertEqual(result['queryStatus'],'partial')
        self.assertEqual(result['commercial']['state'],'delivery_pending')
        self.assertEqual(result['commercial']['products'][0]['price'],399)
        self.assertNotIn('没找到',result['reply'])
        _,history=self.post({'action':'history','before':0})
        self.assertEqual(history['items'][0]['result']['commercial']['products'][0]['id'],'2')


if __name__ == '__main__': unittest.main()
