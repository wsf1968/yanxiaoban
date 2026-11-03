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
from commercial_voice import CommercialVoice, CommercialQueries, validate_plan, ModelFailure


class Search:
    key='fixture'
    def __init__(self): self.calls=[]
    def run(self, request):
        self.calls.append(request)
        return {'state':'complete','sources':[{'id':'1','title':'博物馆官网','url':'https://example.com','kind':'search'}],
                'summary':{'sections':[{'heading':'博物馆','items':[{'text':'成都有博物馆可参观。','evidence':[{'source_id':'1','quote':'成都有博物馆可参观。'}]}]}], 'uncertainties':[], 'spoken':'成都有博物馆可参观，页面附有来源。'}}


class Model:
    def __init__(self):self.calls=0
    def ask(self, system, payload, **kwargs):
        self.calls+=1
        return {'reply':'正在查询。','capability':'web_search','action':'prepare',
                'memory':{'used':[],'operations':[]},
                'query':{'tool':'web_search','args':{'query':payload['utterance']}}}, {}


class SharedVoiceTests(unittest.TestCase):
    request=test_http.HttpTests.request
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        root=Path(self.directory.name);token=root/'token';token.write_text('fixture-only')
        self.search=Search();self.model=Model();self.speech=[]
        buffer=io.BytesIO()
        with wave.open(buffer,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\x01\x00'*16000)
        self.wav=buffer.getvalue()
        def speech(path,body,kind,token):
            self.speech.append(path)
            if path=='transcribe':return {'state':'transcribed','text':'成都有哪些博物馆','tailSilenceSeconds':2}
            return {'audio':base64.b64encode(self.wav).decode(),'mime':'audio/wav'}
        voice=CommercialVoice(root/'history.db',token,model=self.model,
            queries=CommercialQueries(self.search,test_http.FakeTrain()),speech_call=speech)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.server,self.pool=make_server(self.port,voice=voice,public_origin='https://xiaopai.example.invalid')
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        cookie=self.request('GET','/')[1]['Set-Cookie'].split(';')[0]
        self.headers={'Origin':f'http://127.0.0.1:{self.port}','Cookie':cookie,'Content-Type':'application/json'}

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.pool.shutdown(wait=True);self.directory.cleanup()

    def post(self,body):
        status,_,raw=self.request('POST','/api/roundtrip',json.dumps(body),self.headers)
        return status,json.loads(raw)

    def test_original_ui_voice_task_speech_history_and_replay_contract(self):
        status,history=self.post({'action':'history','before':0})
        self.assertEqual((status,history['items']),(200,[]))
        self.assertEqual(self.model.calls,0)
        self.assertIn(b'home-voice-v046.js',self.request('GET','/')[2])
        headers={**self.headers,'Content-Type':'audio/wav','X-Pi-Conversation':'a'*32,
                 'X-Pi-Revision':'0','X-Pi-Turn':'b'*32,'X-Pi-Segment':'0','X-Pi-Segments':'1'}
        status,_,raw=self.request('POST','/api/roundtrip',self.wav,headers)
        answer=json.loads(raw)
        self.assertEqual(status,200,answer)
        self.assertEqual(answer['state'],'ready')
        self.assertTrue(answer['taskExecuted'])
        self.assertIn('成都有博物馆',answer['spoken'])
        self.assertEqual(self.speech,['transcribe','synthesize'])
        self.assertEqual(len(self.search.calls),1)
        self.assertIn('commercial',answer)
        # A repeated audio upload recovers speech without another ASR/model/tool execution.
        self.assertEqual(self.request('POST','/api/roundtrip',self.wav,headers)[0],200)
        self.assertEqual(self.model.calls,1)
        self.assertEqual(len(self.search.calls),1)
        self.assertEqual(self.speech.count('transcribe'),1)
        _,history=self.post({'action':'history','before':0})
        self.assertEqual(len(history['items']),1)
        self.assertIn('commercial',history['items'][0]['result'])
        self.assertEqual(self.post({'action':'replay','id':'a'*32,'requestId':'b'*32})[0],200)
        self.assertEqual(len(self.search.calls),1)
        self.assertEqual(self.post({'action':'delete','id':'a'*32})[0],200)
        self.assertEqual(self.post({'action':'history','before':0})[1]['items'],[])
        self.assertEqual(self.post({'action':'restore','id':'a'*32})[0],200)
        self.assertEqual(len(self.post({'action':'history','before':0})[1]['items']),1)

    def test_cross_origin_audio_cannot_execute(self):
        self.assertEqual(self.request('POST','/api/roundtrip',self.wav,
            {**self.headers,'Origin':'https://foreign.example','Content-Type':'audio/wav'})[0],403)
        self.assertEqual(self.model.calls,0)

    def test_disallowed_tools_and_extra_fields_do_not_reach_any_provider(self):
        for tool,args in [('wechat',{'text':'发送'}),('jd',{'query':'耳机'}),('web_search',{'query':'博物馆','fetch':True})]:
            with self.assertRaises(ModelFailure):
                validate_plan({'reply':'查询','capability':tool,'action':'prepare','query':{'tool':tool,'args':args}},True,['web_search','rail','travel'])
        self.assertEqual(len(self.search.calls),0)
