import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from commercial_voice import CommercialVoice, ModelFailure
from core import DeepSeek


class CommercialModelTests(unittest.TestCase):
    def test_default_voice_uses_flash_and_provider_failure_never_switches_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / 'speech-token'
            token.write_text('fixture-only')
            voice = CommercialVoice(root / 'history.db', token)
            model = voice.bridge.conversation.model
            failure = HTTPError(model.url, 429, 'fixture busy', {}, io.BytesIO(b'{}'))
            with patch('urllib.request.urlopen', side_effect=failure) as request:
                with self.assertRaises(ModelFailure):
                    model.ask('test', {'utterance': 'test'})
            self.assertEqual(request.call_count, 1)
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, 'https://api.deepseek.com/chat/completions')
            self.assertEqual(json.loads(sent.data)['model'], 'deepseek-flash')

    def test_search_and_travel_summaries_use_same_flash_model(self):
        for payload in ({'mode': 'search', 'query': '杭州景点'},
                        {'mode': 'travel', 'destination': '杭州', 'days': 3}):
            calls = []
            def transport(path, body):
                calls.append((path, body))
                if path=='/chat/completions' and '搜索规划模型' in body['messages'][0]['content']:
                    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'searches':[{'kind':'experience','query':'杭州 site:you.ctrip.com'}]})}}]}
                if path.endswith('search'):
                    return {'results': [{'url': 'https://you.ctrip.com/a', 'summary': '西湖可以步行游览。'}]}
                return {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}
            DeepSeek('fixture-only', transport=transport).run(payload)
            completions = [body for path, body in calls if path == '/chat/completions']
            self.assertTrue(completions)
            self.assertTrue(all(body['model']=='deepseek-flash' for body in completions))
