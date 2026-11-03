"""State/protocol regressions; model semantics are verified separately with DS."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared_voice'))
from general_dialogue import Conversation
from memory_usage import mentions
from commercial_voice import COMMERCIAL_RULES, CommercialQueries, validate_plan
from core import DeepSeek
from test_personal_memory import Model, response, OWNER, OTHER


class NaturalMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.model = Model([])
        self.conversation = Conversation(Path(self.temp.name)/'history.db', self.model, memory_enabled=True)
        self.memory = self.conversation.memory

    def tearDown(self):
        self.temp.cleanup()

    def turn(self, text, value, **kwargs):
        self.model.answers.append(value)
        return self.conversation.turn(OWNER, kwargs.pop('session', uuid.uuid4().hex),
            kwargs.pop('revision', 0), kwargs.pop('request', uuid.uuid4().hex), text, **kwargs)

    def seed(self, text='我旅游喜欢慢节奏', subject='本人'):
        info = {'subject': subject, 'kind': 'preference', 'scope': 'long_term', 'time': ''}
        result = self.turn(text, response([{'op': 'add', 'text': text, 'quote': text, 'context': info}]))
        return result['memory']['affected'][0]

    def policy(self, mid, enabled=False):
        quote = '这件事以后别主动问了' if not enabled else '这件事可以主动问我'
        return self.turn(quote, response([{'op': 'set_proactive', 'id': mid, 'enabled': enabled, 'quote': quote}]))

    def test_source_scope_and_subject_survive_restart_without_reading_other_owners(self):
        mid = self.seed('我爱人旅游喜欢紧凑行程', '爱人')
        item = self.memory.snapshot(OWNER)['items'][0]
        self.assertEqual(item['context']['subject'], '爱人')
        self.assertEqual(item['source']['quote'], '我爱人旅游喜欢紧凑行程')
        self.assertTrue(item['source']['recorded_at'])
        self.assertEqual(self.memory.snapshot(OTHER)['items'], [])
        restarted = Conversation(Path(self.temp.name)/'history.db', self.model, memory_enabled=True)
        self.assertEqual(restarted.memory.snapshot(OWNER)['items'][0]['id'], mid)
        self.assertEqual(restarted.memory.snapshot(OWNER)['items'][0]['context'], item['context'])

    def test_use_and_mention_are_separate_idempotent_and_persistent(self):
        mid = self.seed()
        self.turn('按我的偏好', response(used=[mid]))
        state = self.memory.snapshot(OWNER)['items'][0]['interaction']
        self.assertTrue(state['last_used_at'])
        self.assertNotIn('last_mentioned_at', state)
        session, request = uuid.uuid4().hex, uuid.uuid4().hex
        value = {**response(used=[mid]), 'memory': {'used': [mid], 'operations': [], 'mentioned': [mid]}}
        answer = self.turn('怎么安排', value, session=session, request=request)
        replay = self.conversation.turn(OWNER, session, 0, request, '怎么安排')
        self.assertEqual(replay, answer)
        state = self.memory.snapshot(OWNER)['items'][0]['interaction']
        self.assertEqual(state['mention_count'], 1)
        self.assertTrue(state['last_mentioned_at'])

    def test_unspoken_draft_does_not_count_and_tool_only_sees_model_selected_ids(self):
        mid = self.seed()
        self.seed('我喝咖啡不加糖')
        model = self.model
        class Queries:
            tools = ['travel']
            def execute(inner, query, deadline, context):
                inner.context = copy.deepcopy(context)
                return {'status': 'complete', 'sources': [], 'reply': '行程已返回。'}
        queries = Queries()
        conv = Conversation(Path(self.temp.name)/'history.db', model, queries=queries,
                            memory_enabled=True, system_suffix=COMMERCIAL_RULES, plan_validator=validate_plan)
        value = {'reply': '按你喜欢的慢节奏安排。', 'capability': 'travel', 'action': 'prepare',
                 'query': {'tool': 'travel', 'args': {'destination': '成都', 'days': 2, 'date': ''}},
                 'memory': {'used': [mid], 'mentioned': [mid], 'operations': []}}
        model.answers.append(value)
        result = conv.turn(OWNER, uuid.uuid4().hex, 0, uuid.uuid4().hex, '安排成都两天游')
        self.assertEqual([x['id'] for x in queries.context['memoryContext']['items']], [mid])
        self.assertEqual(result['memory']['used'], [mid])
        self.assertEqual(result['memory']['mentioned'], [])
        self.assertNotIn('last_mentioned_at', self.memory.snapshot(OWNER)['items'][0]['interaction'])

    def test_stop_followup_is_not_forget_and_carries_through_edit_tidy_and_undo(self):
        mid = self.seed()
        self.policy(mid)
        item = self.memory.snapshot(OWNER)['items'][0]
        self.assertFalse(item['interaction']['proactive_allowed'])
        self.assertEqual(item['text'], '我旅游喜欢慢节奏')
        version = self.memory.snapshot(OWNER)['version']
        self.memory.manage(OWNER, {'action': 'memory_edit', 'id': mid, 'version': version, 'text': '旅游喜欢休闲游'})
        changed = self.memory.snapshot(OWNER)['items'][0]
        self.assertFalse(changed['interaction']['proactive_allowed'])
        self.assertIsNone(changed['context'])  # hand edit is not auto-classified
        self.seed('旅游不赶景点')
        before = self.memory.snapshot(OWNER)
        tidy = Model([{'operations': [{'op': 'merge', 'ids': [x['id'] for x in before['items']],
                         'content': '旅游喜欢休闲游，不赶景点', 'category': 'travel'}]},
                      {'approved': True, 'issues': []}])
        preview = self.memory.manage(OWNER, {'action': 'memory_tidy'}, tidy)
        self.memory.manage(OWNER, {'action': 'memory_apply', 'version': preview['version']})
        merged = self.memory.snapshot(OWNER)['items'][0]
        self.assertFalse(merged['interaction']['proactive_allowed'])
        state = self.memory.manage(OWNER, {'action': 'memory_list'})
        self.memory.manage(OWNER, {'action': 'memory_undo', 'id': state['changes'][0]['id'], 'version': state['version']})
        restored = next(m for m in self.memory.snapshot(OWNER)['items'] if m['id'] == changed['id'])
        self.assertFalse(restored['interaction']['proactive_allowed'])

    def test_policy_and_telemetry_roll_back_with_failed_conversation(self):
        mid = self.seed()
        before = self.memory.snapshot(OWNER)
        with self.conversation.connect() as c:
            c.execute("CREATE TRIGGER fail_turn BEFORE INSERT ON turns BEGIN SELECT RAISE(ABORT,'fixture'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.policy(mid)
        self.assertEqual(self.memory.snapshot(OWNER), before)
        with self.assertRaises(sqlite3.IntegrityError):
            self.turn('记得吗', {'reply': '记得你喜欢慢节奏。', 'capability': 'none', 'action': 'chat',
                                 'memory': {'used': [mid], 'mentioned': [mid], 'operations': []}})
        self.assertEqual(self.memory.snapshot(OWNER), before)

    def test_invalid_context_quotes_and_foreign_mentions_are_rejected(self):
        mid = self.seed()
        snap = self.memory.snapshot(OWNER)
        for value in [
            {'used': [mid], 'mentioned': ['foreign'], 'operations': []},
            {'used': [], 'mentioned': [mid], 'operations': []},
            {'used': [], 'operations': [{'op': 'set_proactive', 'id': mid, 'enabled': False, 'quote': '不是本轮原话'}]},
            {'used': [], 'operations': [{'op': 'add', 'text': '住成都', 'quote': '住成都',
                                         'context': {'subject': '本人', 'kind': 'place', 'scope': 'current', 'time': ''}}]}]:
            with self.assertRaises(ValueError):
                self.memory.decision(value, snap, '住成都')
        with self.assertRaises(ValueError):
            mentions({'memory_mentioned': ['foreign']}, {'memoryContext': {'items': [{'id': mid}]}})

    def test_undo_cannot_reenable_a_topic_silenced_after_the_edit(self):
        mid = self.seed()
        self.memory.manage(OWNER, {'action': 'memory_edit', 'id': mid, 'version': 1, 'text': '旅游偏好休闲游'})
        replacement = self.memory.snapshot(OWNER)['items'][0]['id']
        self.policy(replacement)
        state = self.memory.manage(OWNER, {'action': 'memory_list'})
        self.memory.manage(OWNER, {'action': 'memory_undo', 'id': state['changes'][0]['id'], 'version': state['version']})
        restored = self.memory.snapshot(OWNER)['items'][0]
        self.assertEqual(restored['id'], mid)
        self.assertFalse(restored['interaction']['proactive_allowed'])

    def test_shopping_delivery_receipt_is_from_final_model_output_with_known_transport_metadata(self):
        from commerce import Commerce
        from test_commerce import Adapter
        mid = self.seed('我买耳机不喜欢头戴式')
        item = self.memory.snapshot(OWNER)['items'][0]
        outputs = Model([
            {'type': 'json_object', 'reply': '你不喜欢头戴式，这款真无线耳机页面价371.37元。', 'memory_mentioned': [mid]},
            {'verdict': 'pass', 'issues': []}])
        result = Commerce([Adapter()], outputs, Path(self.temp.name)/'shopping').execute(
            {'platform': 'jd', 'request': '500元内降噪耳机，不要头戴式'}, time.monotonic()+10,
            {'utterance': '挑耳机', 'memoryContext': {'items': [item]}})
        self.assertEqual(result['memory_mentioned'], [mid])
        self.assertEqual(outputs.calls[1]['memoryContext']['items'][0]['id'], mid)
        self.assertEqual(result['reply'], '你不喜欢头戴式，这款真无线耳机页面价371.37元。')

    def test_travel_delivery_uses_context_in_existing_summary_call_and_keeps_actual_receipt(self):
        mid = self.seed()
        item = self.memory.snapshot(OWNER)['items'][0]
        calls = []
        def transport(path, body):
            calls.append((path, copy.deepcopy(body)))
            if path.endswith('/search'):
                return {'results': [{'url': 'https://you.ctrip.com/fixture', 'summary': '成都公园可以散步，适合放慢节奏。'}]}
            if '搜索规划模型' in body['messages'][0]['content']:
                value = {'searches': [{'kind': 'experience', 'query': '成都公园散步'}]}
            else:
                payload = json.loads(body['messages'][1]['content'])
                self.assertEqual(payload['memoryContext']['items'][0]['id'], mid)
                value = {'sections': [{'heading': '第一天', 'items': [{'text': '公园散步', 'evidence_ids': ['1-1']}]}],
                         'uncertainties': [], 'spoken': '按你喜欢的慢节奏，先去公园散步。', 'memory_mentioned': [mid]}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}
        provider = DeepSeek('fixture', transport=transport)
        runtime = CommercialQueries(search=provider)
        context = {'utterance': '成都两天怎么玩', 'memoryContext': {'items': [item]}}
        result = runtime.execute({'tool': 'travel', 'args': {'destination': '成都', 'days': 2, 'date': '',
                                                           'preferences': '慢节奏'}}, time.monotonic()+40, context)
        self.assertEqual(result['memory_mentioned'], [mid])
        self.assertEqual(len(calls), 3)  # plan, search, summary; no extra personalization call


if __name__ == '__main__':
    unittest.main()
