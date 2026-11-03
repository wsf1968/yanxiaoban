import copy
import json
from pathlib import Path
import tempfile
import unittest

from commercial_voice import CommercialQueries, CommercialVoice, commercial_request
from core import DeepSeek, ServiceError, validate_request
from test_trains import ROW, payload
from trains import TrainClient


class TrainFilterTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.rows = [{**copy.deepcopy(ROW), 'train_no': f'G{i}', 'departure_time': time,
                      'arrival_station': station}
                     for i, (time, station) in enumerate([
                         ('11:59', '上海虹桥'), ('12:00', '上海虹桥'),
                         ('14:00', '上海'), ('15:30', '上海虹桥'), ('17:00', '上海'), ('18:00', '上海')])]
        def transport(body):
            self.calls.append(body)
            # Deliberately ignore provider-side period: local filter must still hold.
            return {'error_code': 0, 'result': self.rows}
        self.client = TrainClient('fixture', transport=transport)
        self.query = {**payload(), 'departure_station': '北京', 'arrival_station': '上海',
                      'departure_scope': 'city', 'arrival_scope': 'city'}

    def test_city_query_forwards_names_without_guessing_stations(self):
        result = self.client.run({**self.query, 'departure_time_range': '下午'})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['departure_station'], '北京')
        self.assertEqual(self.calls[0]['arrival_station'], '上海')
        self.assertEqual(self.calls[0]['departure_time_range'], '下午')
        self.assertEqual([x['departure_time'] for x in result['trains']], ['12:00', '14:00', '15:30', '17:00'])
        self.assertEqual({x['arrival_station'] for x in result['trains']}, {'上海', '上海虹桥'})

    def test_exact_time_refinement_reuses_rows_and_does_not_return_outside_window(self):
        self.client.run({**self.query, 'departure_time_range': '下午'})
        result = self.client.run({**self.query, 'departure_start': '14:00', 'departure_end': '17:00'})
        self.assertTrue(result['cached'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([x['departure_time'] for x in result['trains']], ['14:00', '15:30'])
        self.assertEqual(result['time_label'], '14:00–17:00出发')
        narrowed = self.client.run({**self.query, 'departure_start': '16:00', 'departure_end': '16:30'})
        self.assertEqual(narrowed['trains'], [])
        self.assertIn('没有查到', narrowed['message'])
        self.assertEqual(len(self.calls), 1)

    def test_explicit_station_filter_does_not_include_same_city_other_stations(self):
        result = self.client.run({**self.query, 'arrival_station': '上海站', 'arrival_scope': 'station'})
        self.assertEqual({x['arrival_station'] for x in result['trains']}, {'上海'})
        self.assertEqual(self.calls[0]['arrival_station'], '上海')

    def test_time_window_invalid_values_fail_before_provider(self):
        for values in [{'departure_start': None}, {'departure_end': 17},
                       {'departure_start': '24:00'}, {'departure_end': '25:00'},
                       {'departure_start': '17:00', 'departure_end': '14:00'},
                       {'departure_time_range': '上午', 'departure_start': '14:00'}]:
            with self.assertRaises(ServiceError):
                self.client.run({**self.query, **values})
        self.assertEqual(self.calls, [])

    def test_model_selects_count_order_and_seat_without_first_three_fallback(self):
        class Decision:
            def ask(inner, system, data, **kwargs):
                return {'reply':'根据你的条件，这四班可供比较。',
                        'selection':[{'row':index,'seat':1} for index in [3,1,0,2]]}, {}
        runtime=CommercialQueries(train=self.client,decider=Decision())
        result=runtime.rail(self.client.run(self.query), {'utterance':'帮我比较班次'})
        self.assertEqual(len(result['rail']['trains']),4)
        self.assertEqual([t['train'] for t in result['rail']['trains']], ['G3','G1','G0','G2'])
        self.assertTrue(all(t['seat']=='一等座' for t in result['rail']['trains']))
        class Invalid:
            def ask(inner,*args,**kwargs):
                return {'reply':'错误候选','selection':[{'row':999,'seat':0}]},{}
        with self.assertRaises(ServiceError):
            CommercialQueries(decider=Invalid()).rail(self.client.run(self.query),{})

    def test_followup_retains_request_scope_and_time_in_real_conversation_history(self):
        calls = []
        class Model:
            def ask(inner, system, request, **kwargs):
                calls.append(request)
                if 'candidates' in request:
                    return {'reply':'按本轮需求选择这些班次。','selection':[{'row':i,'seat':0} for i in reversed(range(len(request['candidates'])))]},{}
                args = {k: v for k, v in self.query.items() if k != 'mode'}
                args.update(departure_start='14:00', departure_end='17:00')
                return {'reply': '正在查。', 'capability': 'rail', 'action': 'prepare',
                        'memory': {'used': [], 'operations': []},
                        'query': {'tool': 'rail', 'args': args}}, {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); token = root/'token'; token.write_text('fixture')
            voice = CommercialVoice(root/'history.db', token, model=Model(),
                                    queries=CommercialQueries(train=self.client))
            conversation = voice.bridge.conversation
            first = conversation.turn('f'*64, 'a'*32, 0, 'b'*32, '明天下午北京到上海')
            self.assertEqual(first['commercial']['total_matches'], 2)
            self.assertEqual(len(first['rail']['trains']), 2)
            conversation.turn('f'*64, 'a'*32, 1, 'c'*32, '改成三点以后')
            history = [call for call in calls if 'utterance' in call][1]['history'][-1]['content']
            self.assertIn('14:00', history)
            self.assertIn('arrival_scope', history)
            self.assertIn('上海', history)


class TravelFocusTests(unittest.TestCase):
    def test_feedback_reaches_model_and_model_output_is_not_programmatically_cut(self):
        bodies = []
        def transport(path, body):
            if path=='/chat/completions' and '搜索规划模型' in body['messages'][0]['content']:
                return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'searches':[{'kind':'experience','query':'杭州 亲子 site:you.ctrip.com'}]})}}]}
            if path.endswith('search'):
                self.assertLessEqual(len(body['query']), 128)
                return {'results': [{'url': 'https://you.ctrip.com/a', 'summary': '西湖可以步行游览。'}]}
            bodies.append(body)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
                'sections': [{'heading': f'第{day}天', 'items': [
                    {'text': '西湖轻松游览。', 'evidence_ids': ['1-1']} for _ in range(5)]} for day in range(1, 5)],
                'uncertainties': ['核对预约', '核对交通', '不要显示第三条']})}}]}
        result = DeepSeek('fixture', transport=transport).run({'mode': 'travel', 'destination': '杭州',
                    'days': 3, 'preferences': '带孩子，轻松一点，避开爬山'})
        self.assertEqual(json.loads(bodies[0]['messages'][1]['content'])['request']['preferences'], '带孩子，轻松一点，避开爬山')
        self.assertIn('用户实际反馈',bodies[0]['messages'][0]['content'])
        # An intentionally verbose fixture proves the transport does not fake
        # model improvement by cutting sections, sentences, items or warnings.
        self.assertEqual(len(result['summary']['sections']), 4)
        self.assertTrue(all(len(section['items']) == 5 for section in result['summary']['sections']))
        self.assertEqual(len(result['summary']['uncertainties']), 3)

    def test_voice_tool_passes_preferences_and_precise_time_fields(self):
        travel = commercial_request({'tool': 'travel', 'args': {'destination': '成都', 'days': 3,
                    'date': '', 'preferences': '亲子，不赶路'}})
        self.assertEqual(travel['preferences'], '亲子，不赶路')
        args = {k: v for k, v in payload().items() if k != 'mode'}
        request = commercial_request({'tool': 'rail', 'args': {**args, 'departure_time_range': '下午',
                    'departure_start': '14:00', 'departure_end': '17:00'}})
        self.assertEqual(request['departure_start'], '14:00')
