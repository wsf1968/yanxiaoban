import base64
import json
from pathlib import Path
import sys
import types
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared_voice'))
from speech_profile import ProfileSpeech, configured_speech
from speech_numbers import speech_for_tts


class SpeechProfileTests(unittest.TestCase):
    def fixture(self):
        self.generated = []
        self.loaded = []
        self.shared = []
        def generate(text, config):
            self.generated.append((text, config.speed, config.silence_scale))
            return types.SimpleNamespace(samples=[0.] * 2205, sample_rate=22050)
        def factory():
            self.loaded.append(True)
            return types.SimpleNamespace(generate=generate), types.SimpleNamespace
        def shared(*args):
            self.shared.append(args)
            return {'state': 'transcribed', 'text': '上午九点出发'}
        self.profile = {'model_root':'/fixture/models', 'speed':.96, 'silence_scale':.3}
        return ProfileSpeech(self.profile, shared, factory=factory)

    def test_actual_render_keeps_number_readings_and_uses_profile_without_shared_tts(self):
        from conversation_bridge import Bridge
        speech=self.fixture()
        bridge=types.SimpleNamespace(call=speech)
        reply={'reply':'G103次，14:05出发。客服电话400-123-4567。','text':'高铁查询','tool':'rail'}
        first=Bridge.render(bridge,reply,'fixture','fixture')
        second=Bridge.render(bridge,reply,'fixture','fixture')
        expected=speech_for_tts(reply['reply'],reply['text'],rail=True)
        self.assertEqual([x[0] for x in self.generated],[expected,expected])
        self.assertTrue(all(x[1:]==(.96,.3) for x in self.generated))
        self.assertEqual(first['reply'],reply['reply'])
        self.assertEqual(first['speechText'],expected)
        self.assertEqual(first['state'],'ready')
        self.assertTrue(base64.b64decode(first['audio']).startswith(b'RIFF'))
        self.assertEqual(len(self.loaded),1)
        self.assertEqual(self.shared,[])

    def test_asr_passes_through_without_loading_another_model(self):
        speech=self.fixture()
        result=speech('transcribe',b'raw-wave','audio/wav','fixture')
        self.assertEqual(result['state'],'transcribed')
        self.assertEqual(self.loaded,[])
        self.assertEqual(self.shared,[('transcribe',b'raw-wave','audio/wav','fixture')])

    def test_travel_paragraphs_render_without_changing_model_content(self):
        from conversation_bridge import Bridge
        speech = self.fixture()
        reply = {'reply': '第一天先游西湖。\n\n第二天看博物馆，09:30出发。\r\n\t具体安排在页面里。',
                 'text': '杭州两日游', 'tool': 'travel'}
        result = Bridge.render(types.SimpleNamespace(call=speech), reply, 'fixture', 'fixture')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['reply'], reply['reply'])
        self.assertEqual(result['spoken'], reply['reply'])
        # Number formatting remains the existing formatter's responsibility.
        import re
        self.assertEqual(self.generated[0][0], re.sub(r'[\r\n\t]+', ' ', result['speechText']).strip())
        self.assertEqual(''.join(self.generated[0][0].split()), ''.join(result['speechText'].split()))
        self.assertEqual(self.generated[0][1:], (.96, .3))
        self.assertTrue(base64.b64decode(result['audio']).startswith(b'RIFF'))
        self.assertEqual(self.shared, [])

    def test_layout_compatibility_does_not_allow_controls_or_overflow(self):
        speech = self.fixture()
        for text in ['正常\x00内容', '正常\x1b内容', '\n\t\r', '甲' * 351]:
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                speech('synthesize', json.dumps({'text': text}).encode(), 'application/json', 'fixture')
        self.assertEqual(self.generated, [])

    def test_invalid_profile_rejected_and_no_unannounced_fallback(self):
        speech=self.fixture()
        for name,value in [('speed',0),('silence_scale',True),('model_root','relative')]:
            with self.subTest(name=name),self.assertRaises(ValueError):
                ProfileSpeech({**self.profile,name:value},None)
        def fail(text,config):raise RuntimeError('fixture')
        speech.warmup();speech.tts.generate=fail
        from conversation_bridge import Bridge
        result=Bridge.render(types.SimpleNamespace(call=speech),{'reply':'语音测试。'},'fixture','fixture')
        self.assertEqual(result['state'],'text_only')
        self.assertEqual(self.shared,[])
        shared=lambda *args:None
        self.assertIs(configured_speech('/fixture/no-profile.json',shared),shared)

if __name__=='__main__':unittest.main()
