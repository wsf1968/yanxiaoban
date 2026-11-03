import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'shared_voice'))
from qwen_speech import QwenWorker, QwenSpeech, QwenSpeechError, B_INSTRUCTION, provider_settings
from speech_numbers import speech_for_tts

FAKE = '''import sys,json,time,base64,io,wave
count=0
{startup}
for line in sys.stdin:
 r=json.loads(line);count+=1
 {response}
'''
VALID = '''buf=io.BytesIO()
 with wave.open(buf,'wb') as w:
  w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\\x01\\x00'*2400)
 print(json.dumps({'id':r['id'],'mime':'audio/wav','audio':base64.b64encode(buf.getvalue()).decode(),'metrics':{'count':count,'text':r['text'],'instruction':r['instruction']}}),flush=True)'''


class QwenSpeechTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.model = self.path / 'local model'
        self.model.mkdir();(self.model/'config.json').write_text('{}')
        (self.model/'model.safetensors').write_bytes(b'fixture')
        (self.model/'speech_tokenizer').mkdir()

    def worker(self, response=VALID, startup="print(json.dumps({'state':'ready','speaker':'Vivian'}),flush=True)", **kwargs):
        script=self.path/'fake.py';script.write_text(FAKE.format(startup=startup,response=response))
        worker=QwenWorker(sys.executable,self.model,worker_script=script,**kwargs)
        self.addCleanup(worker.close)
        return worker

    def test_one_resident_worker_preserves_formatted_text_and_b_parameters(self):
        worker=self.worker();speech=QwenSpeech(lambda *a:None,worker)
        speech.warmup();pid=worker.process.pid
        for n in range(2):
            reply='G8646，11:34出发，二等座80元。'
            text=speech_for_tts(reply,rail=True)
            result=speech('synthesize',json.dumps({'text':text}).encode(),'application/json','local')
            self.assertEqual(worker.process.pid,pid)
            self.assertEqual(worker.last_metrics['count'],n+1)
            self.assertEqual(worker.last_metrics['text'],text)
            self.assertIn('高八、六、四、六',text)
            self.assertIn('上午十一点三十四分',text)
            self.assertEqual(worker.last_metrics['instruction'],B_INSTRUCTION)
            self.assertTrue(base64.b64decode(result['audio']).startswith(b'RIFF'))

    def test_asr_does_not_wait_for_or_start_worker(self):
        seen=[];worker=self.worker();speech=QwenSpeech(lambda *a:seen.append(a) or {'text':'测试'},worker)
        worker.slot.acquire()
        try:
            self.assertEqual(speech('transcribe',b'wave','audio/wav','token'),{'text':'测试'})
        finally:worker.slot.release()
        self.assertIsNone(worker.process)
        self.assertEqual(seen,[('transcribe',b'wave','audio/wav','token')])

    def test_busy_is_immediate_not_a_queue_of_turns(self):
        worker=self.worker();worker.slot.acquire();started=time.monotonic()
        try:
            with self.assertRaisesRegex(QwenSpeechError,'qwen_busy'):worker.synthesize('测试')
        finally:worker.slot.release()
        self.assertLess(time.monotonic()-started,.2)
        self.assertIsNone(worker.process)

    def test_timeout_terminates_only_owned_child_and_can_explicitly_retry(self):
        worker=self.worker(response='time.sleep(10)',timeout=.05)
        with self.assertRaisesRegex(QwenSpeechError,'qwen_timeout'):worker.synthesize('测试')
        self.assertIsNone(worker.process)
        self.assertFalse(worker.slot.locked())

    def test_load_failure_and_invalid_wav_are_explicit_without_fallback(self):
        for startup,response,code in [
            ("print(json.dumps({'error':'qwen_load_failed'}),flush=True)",VALID,'qwen_load_failed'),
            ("print(json.dumps({'state':'ready','speaker':'Vivian'}),flush=True)","print(json.dumps({'id':r['id'],'mime':'audio/wav','audio':'bm90IHdhdg=='}),flush=True)",'qwen_invalid_audio')]:
            worker=self.worker(startup=startup,response=response)
            shared=[];speech=QwenSpeech(lambda *a:shared.append(a),worker)
            with self.subTest(code=code),self.assertRaisesRegex(QwenSpeechError,code):
                speech('synthesize',b'{"text":"test"}','application/json','token')
            self.assertEqual(shared,[])
            self.assertIsNone(worker.process)

    def test_invalid_requests_never_reach_worker_and_whitespace_only_is_adapted(self):
        worker=self.worker();speech=QwenSpeech(None,worker)
        for body in [{'text':'甲'*351},{'text':'\n\t'},{'text':'正常\x00内容'},{'text':'正常','speed':2},[]]:
            with self.subTest(body=body),self.assertRaises(ValueError):
                speech('synthesize',json.dumps(body).encode(),'application/json','')
        self.assertIsNone(worker.process)
        speech('synthesize',json.dumps({'text':'第一句。\n\n第二句。'}).encode(),'application/json','')
        self.assertEqual(worker.last_metrics['text'],'第一句。 第二句。')

    def test_worker_rejects_token_exhaustion_and_budget_breach_without_partial_audio(self):
        import numpy as np
        from qwen_tts_worker import generate_wav, TtsFailure
        def model(count, samples=100):
            return types.SimpleNamespace(generate_custom_voice=lambda **kwargs:iter([types.SimpleNamespace(audio=np.zeros(samples),sample_rate=24000,token_count=count)]))
        mx=types.SimpleNamespace(random=types.SimpleNamespace(seed=lambda value:None),reset_peak_memory=lambda:None,get_peak_memory=lambda:1e9)
        with self.assertRaisesRegex(TtsFailure,'qwen_token_limit'):
            generate_wav(model(2048),'完整文字',B_INSTRUCTION,mx,np,6)
        mx.get_peak_memory=lambda:7e9
        with self.assertRaisesRegex(TtsFailure,'qwen_resource_limit'):
            generate_wav(model(12),'完整文字',B_INSTRUCTION,mx,np,6)
        mx.get_peak_memory=lambda:1e9
        # A 61-second result is legal: no inherited 60-second truncation.
        raw,metrics=generate_wav(model(800,24000*61),'完整文字',B_INSTRUCTION,mx,np,6)
        self.assertEqual(metrics['seconds'],61)
        self.assertEqual(len(raw),44+24000*61*2)

    def test_persisted_provider_survives_restart_env_overrides_and_bad_config_fails(self):
        config=self.path/'tts-provider.json'
        self.assertEqual(provider_settings(self.path,default_backend='matcha',environ={})[0],'matcha')
        config.write_text(json.dumps({'backend':'qwen3','qwen_python':sys.executable,'qwen_model':str(self.model)}))
        name,env=provider_settings(self.path,environ={})
        self.assertEqual(name,'qwen3')
        worker=QwenWorker.from_env(env);self.addCleanup(worker.close)
        self.assertEqual(str(worker.model),str(self.model))
        self.assertEqual(provider_settings(self.path,environ={'PAI_TTS_BACKEND':'matcha'})[0],'matcha')
        config.write_text('{invalid')
        with self.assertRaisesRegex(QwenSpeechError,'invalid_tts_provider_config'):
            provider_settings(self.path,environ={})
        config.write_text('{"backend":"qwen3"}')
        with self.assertRaisesRegex(QwenSpeechError,'qwen_configuration_missing'):
            provider_settings(self.path,environ={})

    def test_both_commercial_entrypoints_use_the_same_explicit_hook(self):
        import product_speech
        import macos_local
        marker=(object(),Path('/fixture/token'))
        with patch.dict('os.environ',{'PAI_TTS_BACKEND':'qwen3','PAI_QWEN_PYTHON':sys.executable,'PAI_QWEN_MODEL':str(self.model)}), patch('qwen_speech.load_qwen_speech',return_value=marker) as loader:
            self.assertIs(product_speech.load_speech('/models','/private'),marker)
            self.assertIs(macos_local.local_speech('/models','/private'),marker)
            self.assertEqual(loader.call_count,2)
        with patch.dict('os.environ',{'PAI_TTS_BACKEND':'typo'}):
            with self.assertRaisesRegex(ValueError,'unsupported_product_tts_backend'):
                macos_local.local_speech('/unused','/unused')

    def test_paths_required_and_missing_qwen_never_selects_old_voice(self):
        for python,model,error in [('relative',self.model,'qwen_python_missing'),(sys.executable,self.path/'absent','qwen_model_missing')]:
            with self.subTest(error=error),self.assertRaisesRegex(QwenSpeechError,error):QwenWorker(python,model)
        with self.assertRaisesRegex(QwenSpeechError,'qwen_configuration_missing'):QwenWorker.from_env({})
        from product_speech import load_speech
        with patch.dict('os.environ',{'PAI_TTS_BACKEND':'qwen3'},clear=True):
            with self.assertRaisesRegex(QwenSpeechError,'qwen_configuration_missing'):load_speech('/unused','/unused')

    def test_reminder_label_clock_is_repaired_only_for_tts_and_guards_remain(self):
        from conversation_bridge import Bridge
        worker=self.worker();speech=QwenSpeech(None,worker)
        reply={'reply':'已设置好：11:41提醒你喝水。','tool':'secretary','text':'提醒'}
        result=Bridge.render(types.SimpleNamespace(call=speech),reply,'token','fixture')
        self.assertEqual(result['reply'],reply['reply'])
        self.assertEqual(result['spoken'],reply['reply'])
        self.assertEqual(result['state'],'ready')
        self.assertIn('上午十一点四十一分',worker.last_metrics['text'])
        self.assertEqual(result['speechText'],worker.last_metrics['text'])
        for raw in ('比分3:00','https://a.example/11:41','2026-10-27T11:41','比例3:00'):
            self.assertEqual(speech_for_tts(raw),raw)


if __name__=='__main__':unittest.main()
