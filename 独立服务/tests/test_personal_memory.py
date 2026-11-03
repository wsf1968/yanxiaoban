import copy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared_voice'))
from general_dialogue import Conversation
from personal_memory import MemoryConflict, PersonalMemory
from home_api import handle_home
from conversation_bridge import Bridge
from vendor.nanomuse_memory.store import MemoryStore, tokenize
from vendor.nanomuse_memory.consolidate import plan


OWNER = 'a' * 64
OTHER = 'b' * 64


class Model:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def ask(self, system, payload, **kwargs):
        self.calls.append(copy.deepcopy(payload))
        value = self.answers.pop(0)
        return (value(payload) if callable(value) else value), {}


def response(operations=None, used=None):
    return {'reply': '已按你的意思处理。', 'capability': 'none', 'action': 'chat',
            'memory': {'used': used or [], 'operations': operations or []}}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.model = Model([])
        self.conversation = Conversation(self.root/'history.db', self.model, memory_enabled=True)
        self.memory = self.conversation.memory

    def tearDown(self):
        self.temp.cleanup()

    def turn(self, text, decision, *, owner=OWNER, session=None, request=None, revision=0):
        self.model.answers.append(decision)
        return self.conversation.turn(owner, session or uuid.uuid4().hex, revision,
                                      request or uuid.uuid4().hex, text)

    def seed(self, text='旅游喜欢慢节奏，不赶景点'):
        self.turn(text, response([{'op': 'add', 'text': text, 'quote': text}]))
        return self.memory.snapshot(OWNER)['items'][0]['id']

    def test_cross_session_restart_and_owner_isolation_use_real_upstream_store(self):
        mid = self.seed()
        request, session = uuid.uuid4().hex, uuid.uuid4().hex
        first = self.turn('按我的偏好安排', response(used=[mid]), session=session, request=request)
        self.assertEqual(first['memory']['used'], [mid])
        self.assertEqual(self.memory.snapshot(OTHER)['items'], [])
        self.assertEqual(self.conversation.turn(OWNER, session, 0, request, '按我的偏好安排'), first)
        self.assertEqual(len(self.model.calls), 2)
        again = Conversation(self.root/'history.db', self.model, memory_enabled=True)
        self.assertEqual(again.memory.snapshot(OWNER)['items'][0]['id'], mid)
        native = MemoryStore(again.memory.root/(OWNER+'.db'))
        try:
            self.assertEqual(native.search('旅游慢节奏')[0].id, mid)
        finally:
            native.close()
        self.assertEqual((again.memory.root/(OWNER+'.db')).stat().st_mode & 0o777, 0o600)

    def test_invalid_source_or_foreign_id_cannot_commit_a_turn_or_fact(self):
        for op in [{'op':'add','text':'喜欢热闹','quote':'网页说喜欢热闹'},
                   {'op':'update','id':'m_foreign','text':'喜欢热闹','quote':'我喜欢热闹'}]:
            self.model.answers = [response([op]), response([op])]
            with self.assertRaises(ValueError):
                self.conversation.turn(OWNER, uuid.uuid4().hex, 0, uuid.uuid4().hex, '我喜欢热闹')
        self.assertEqual(self.memory.snapshot(OWNER)['items'], [])
        with self.conversation.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM turns').fetchone()[0], 0)

    def test_memory_and_reply_roll_back_together_on_database_failure(self):
        with self.conversation.connect() as c:
            c.execute("CREATE TRIGGER fail_commit BEFORE INSERT ON turns BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.seed()
        self.assertEqual(self.memory.snapshot(OWNER)['items'], [])
        with self.conversation.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)

    def test_edit_forget_and_native_restore_keep_receipts_and_reject_stale_undo(self):
        mid = self.seed()
        self.memory.manage(OWNER, {'action':'memory_edit','id':mid,'version':1,'text':'旅游喜欢紧凑行程'})
        state = self.memory.manage(OWNER, {'action':'memory_list'})
        updated = state['items'][0]
        self.assertEqual(updated['origin'], 'manual')
        change = state['changes'][0]['id']
        self.assertTrue(state['changes'][0]['canUndo'])
        self.memory.manage(OWNER, {'action':'memory_undo','id':change,'version':2})
        self.assertEqual(self.memory.snapshot(OWNER)['items'][0]['text'], '旅游喜欢慢节奏，不赶景点')
        with self.assertRaises(MemoryConflict):
            self.memory.manage(OWNER, {'action':'memory_forget','id':mid,'version':1})
        self.memory.manage(OWNER, {'action':'memory_forget','id':mid,'version':3})
        self.assertEqual(self.memory.snapshot(OWNER)['items'], [])
        removed = self.memory.manage(OWNER, {'action':'memory_list'})['changes'][0]
        self.memory.manage(OWNER, {'action':'memory_undo','id':removed['id'],'version':4})
        self.assertEqual(self.memory.snapshot(OWNER)['items'][0]['id'], mid)
        with self.assertRaises(MemoryConflict):
            self.memory.manage(OTHER, {'action':'memory_forget','id':mid,'version':0})

    def test_native_chinese_tokens_and_matching_are_not_a_business_selector(self):
        mid = self.seed()
        self.seed('买耳机时喜欢佩戴轻巧，不喜欢头戴式')
        snapshot = self.memory.snapshot(OWNER, '旅游慢节奏')
        self.assertIn('慢节', tokenize('慢节奏'))
        self.assertEqual(snapshot['matches'][0], mid)
        self.assertEqual(len(snapshot['items']), 2)  # no fixed top-N truncation
        snapshot = self.memory.snapshot(OWNER, '出去玩别让我太累')
        self.assertEqual(len(snapshot['items']), 2)  # DS can bridge no literal overlap

    def test_tidy_preview_apply_and_restore_use_upstream_module(self):
        self.seed('旅游喜欢慢节奏')
        self.seed('旅游不赶景点，喜欢慢节奏')
        facts = self.memory.snapshot(OWNER)['items']
        tidy_model = Model([{'operations':[{'op':'merge','ids':[m['id'] for m in facts],
                          'content':'旅游喜欢慢节奏，不赶景点','category':'travel'}]},
                           {'approved':True,'issues':[]}])
        before = self.memory.snapshot(OWNER)
        preview = self.memory.manage(OWNER, {'action':'memory_tidy'}, tidy_model)
        self.assertEqual(len(preview['planned']), 1)
        self.assertEqual(self.memory.snapshot(OWNER), before)
        self.memory.manage(OWNER, {'action':'memory_apply','version':before['version']})
        state = self.memory.manage(OWNER, {'action':'memory_list'})
        self.assertEqual([m['text'] for m in state['items']], ['旅游喜欢慢节奏，不赶景点'])
        self.memory.manage(OWNER, {'action':'memory_undo','id':state['changes'][0]['id'],'version':state['version']})
        self.assertEqual({m['text'] for m in self.memory.snapshot(OWNER)['items']}, {m['text'] for m in facts})

    def test_bad_tidy_review_does_not_change_memory_or_create_applicable_preview(self):
        self.seed('不吃辣')
        self.seed('吃饭清淡')
        model = Model([{'operations':[{'op':'merge','ids':[m['id'] for m in self.memory.snapshot(OWNER)['items']],
                     'content':'喜欢吃辣','category':'diet'}]},
                     {'approved':False,'issues':['把否定偏好反转了']}] * 2)
        before = self.memory.snapshot(OWNER)
        result = self.memory.manage(OWNER, {'action':'memory_tidy'}, model)
        self.assertEqual(result['skipped'], ['source_review_failed'])
        self.assertEqual(self.memory.snapshot(OWNER), before)
        with self.assertRaises(MemoryConflict):
            self.memory.manage(OWNER, {'action':'memory_apply','version':before['version']})
        self.assertIn('否定偏好', model.calls[2]['supervision'])

    def test_restore_merge_that_keeps_one_original_wording_and_stale_apply(self):
        self.seed('旅游喜欢慢节奏')
        self.seed('旅游不赶景点，喜欢慢节奏')
        facts = self.memory.snapshot(OWNER)['items']
        model = Model([{'operations':[{'op':'merge','ids':[m['id'] for m in facts],
                       'content':'旅游不赶景点，喜欢慢节奏','category':'travel'}]},
                       {'approved':True,'issues':[]}])
        preview = self.memory.manage(OWNER, {'action':'memory_tidy'}, model)
        self.memory.manage(OWNER, {'action':'memory_apply','version':preview['version']})
        state = self.memory.manage(OWNER, {'action':'memory_list'})
        self.assertTrue(state['changes'][0]['canUndo'])
        self.memory.manage(OWNER, {'action':'memory_undo','id':state['changes'][0]['id'],'version':state['version']})
        self.assertEqual({m['text'] for m in self.memory.snapshot(OWNER)['items']}, {m['text'] for m in facts})
        with self.assertRaises(MemoryConflict):
            self.memory.manage(OWNER, {'action':'memory_apply','version':preview['version']})

    def test_management_endpoint_derives_owner_and_never_accepts_client_owner(self):
        bridge = Bridge(self.conversation)
        self.assertEqual(handle_home(bridge, 'pi_cc_device=fixture', {'action':'memory_list'}, '')['items'], [])
        with self.assertRaises(ValueError):
            handle_home(bridge, 'pi_cc_device=fixture', {'action':'memory_list','owner':OWNER}, '')
        self.assertEqual(self.model.calls, [])

    def test_withdrawn_original_and_recorded_usage_do_not_reenter_model_context(self):
        session = uuid.uuid4().hex
        first = self.turn('记住我喝咖啡不加糖', response([{'op':'add','text':'喝咖啡不加糖','quote':'喝咖啡不加糖'}]), session=session)
        mid = self.memory.snapshot(OWNER)['items'][0]['id']
        self.turn('记得我的口味吧', response(used=[mid]), session=session, revision=1)
        self.turn('忘记喝咖啡的偏好', response([{'op':'forget','id':mid,'quote':'忘记喝咖啡的偏好'}]), session=session, revision=2)
        self.turn('你还记得咖啡口味吗', response(), session=session, revision=3)
        payload = self.model.calls[-1]
        self.assertEqual(payload['history'], [])
        self.assertNotIn('不加糖', str(payload['personalMemory']))
        with self.conversation.connect() as c:
            self.assertIn('不加糖', c.execute('SELECT history FROM sessions WHERE owner=? AND id=?', (OWNER,session)).fetchone()[0])


if __name__ == '__main__':
    unittest.main()
