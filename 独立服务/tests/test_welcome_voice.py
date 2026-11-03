import base64
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
import wave

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import ROOT,make_server
from macos_local import WELCOME_TEXT,prepare_welcome
import test_http as http_fixtures
from test_http import FakeClient,FakeTrain


def pcm_wav():
    result=io.BytesIO()
    with wave.open(result,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000)
        audio.writeframes(b'\x01\x00'*2400)
    return result.getvalue()


class FakeSpeech:
    backend_name='qwen3'
    def __init__(self):self.calls=[];self.wav=pcm_wav()
    def __call__(self,route,body,kind,token):
        self.calls.append((route,json.loads(body),kind,token))
        return {'mime':'audio/wav','audio':base64.b64encode(self.wav).decode()}


class WelcomeVoiceTests(unittest.TestCase):
    request=http_fixtures.HttpTests.request

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.private=Path(self.directory.name)
        self.token=self.private/'token';self.token.write_text('fixture-only')

    def test_qwen_greeting_is_private_uses_original_words_and_preserves_shared_wav(self):
        shared=ROOT/'web_shared/ready-v035.wav';original=shared.read_bytes()
        voice=SimpleNamespace();speech=FakeSpeech()
        path=prepare_welcome(voice,speech,self.token,self.private)
        self.assertEqual(speech.calls,[('synthesize',{'text':'我在，请说。'},'application/json','fixture-only')])
        self.assertEqual(WELCOME_TEXT,'我在，请说。')
        self.assertEqual(path,self.private/'ready-qwen3.wav')
        self.assertEqual(path.read_bytes(),speech.wav)
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(voice.welcome_path,path)
        self.assertEqual(shared.read_bytes(),original)

    def test_other_backends_keep_existing_greeting(self):
        voice=SimpleNamespace();speech=FakeSpeech();speech.backend_name='matcha'
        self.assertIsNone(prepare_welcome(voice,speech,self.token,self.private))
        self.assertFalse(speech.calls)
        self.assertFalse(hasattr(voice,'welcome_path'))

    def test_mobile_greeting_uses_qwen_without_install_profile_and_keeps_access_guards(self):
        voice=SimpleNamespace();speech=FakeSpeech()
        prepare_welcome(voice,speech,self.token,self.private)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.server,self.pool=make_server(self.port,FakeClient(),FakeTrain(),
            pairing_code='welcome-fixture-code',voice=voice)
        def close():
            self.server.shutdown();self.server.server_close();self.pool.shutdown(wait=True)
        self.addCleanup(close)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.assertEqual(self.request('GET','/ready-v035.wav')[0],401)
        self.assertEqual(self.request('GET','/ready-v035.wav',headers={'Host':'evil.example'})[0],403)
        status,headers,_=self.request('POST','/api/pair',json.dumps({'code':'welcome-fixture-code'}),
                                     {'Origin':f'http://127.0.0.1:{self.port}'})
        self.assertEqual(status,200)
        cookie=headers['Set-Cookie'].split(';')[0]
        status,headers,raw=self.request('GET','/ready-v035.wav',headers={'Cookie':cookie})
        self.assertEqual(status,200)
        self.assertEqual(raw,speech.wav)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn(headers['Content-Type'],('audio/wav','audio/x-wav'))


if __name__=='__main__':unittest.main()
