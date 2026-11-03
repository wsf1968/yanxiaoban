import copy
import datetime as dt
import json
from pathlib import Path
import sys,tempfile,time,types,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'shared_voice')]
from core import ServiceError
from commercial_flights import FlightProvider,deliver_flights,validate_flight_args
from commercial_voice import CommercialQueries,CommercialVoice,validate_plan,remember_result
from speech_numbers import speech_for_tts


def args(**extra):
    return {'departure':'大兴','arrival':'浦东','departure_scope':'airport','arrival_scope':'airport',
            'date':(dt.date.today()+dt.timedelta(days=2)).isoformat(),'max_segments':1,**extra}


def row(**extra):
    return {'flightNo':'CA8321','airlineName':'中国国际航空','departure':'PKX','arrival':'PVG',
            'departureName':'大兴国际机场','arrivalName':'浦东国际机场','departureDate':args()['date'],
            'arrivalDate':args()['date'],'departureTime':'10:40','arrivalTime':'12:50','transferNum':1,
            'ticketPrice':2285,'isCodeShare':False,**extra}


class Decider:
    def __init__(self,selection=None):self.calls=[];self.selection=selection if selection is not None else [0]
    def ask(self,system,payload,**kwargs):
        self.calls.append((system,payload))
        if '机票交付复核者' in system:return {'ok':True,'issues':[]},{}
        return {'reply':'历史隔离样本：CA8321，10:40大兴出发、12:50到浦东，直飞，参考2285元；税费及可购买性未核实。','selection':self.selection},{}


class FlightCommercialTests(unittest.TestCase):
    def fixture(self,rows=None,**kwargs):
        self.calls=[]
        def transport(wire):self.calls.append(wire);return {'error_code':0,'result':{'flightInfo':copy.deepcopy([row()] if rows is None else rows)}}
        return FlightProvider(enabled=True,api_key='fixture-user-key',paid_usage_acknowledged=True,
                              transport=transport,simulation=True,**kwargs)

    def test_unconfigured_and_disabled_never_go_out_or_read_env_key(self):
        with tempfile.TemporaryDirectory() as root,patch.dict('os.environ',{'JUHE_FLIGHT_KEY':'do-not-use'}),patch('urllib.request.build_opener',side_effect=AssertionError('no flight network')):
            provider=FlightProvider.from_private(root)
            self.assertFalse(provider.enabled)
            with self.assertRaises(ServiceError):provider.run(args(),time.monotonic()+30)
            runtime=CommercialQueries(flight_provider=provider)
            self.assertNotIn('flight',runtime.tools)
            self.assertFalse(runtime.execute({'tool':'flight','args':args()},time.monotonic()+30,{})['performed'])

    def test_owner_config_paid_ack_and_private_permissions_required(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'.private/flight-provider.json';path.parent.mkdir()
            cfg={'enabled':True,'api_key':'own-key','paid_usage_acknowledged':True}
            path.write_text(json.dumps(cfg));path.chmod(0o600)
            self.assertTrue(FlightProvider.from_private(root).enabled)
            path.write_text(json.dumps({**cfg,'paid_usage_acknowledged':False}))
            self.assertEqual(FlightProvider.from_private(root).status['state'],'invalid_configuration')
            path.write_text(json.dumps(cfg));path.chmod(0o644)
            self.assertFalse(FlightProvider.from_private(root).enabled)

    def test_wire_airport_identity_direct_segments_and_reference_price(self):
        p=self.fixture();result=p.run(args(flight_no='CA8321'),time.monotonic()+30)
        self.assertEqual(self.calls,[{'departure':'PKX','arrival':'PVG','departureDate':args()['date'],'maxSegments':'1','flightNo':'CA8321'}])
        self.assertEqual(result['candidates'][0]['transfers'],0)
        self.assertEqual(result['candidates'][0]['origin_code'],'PKX')
        self.assertEqual(result['candidates'][0]['reference_price'],'2285')
        self.assertNotIn('fixture-user-key',json.dumps(result))
        with self.assertRaises(ServiceError):validate_flight_args(args(departure='北京',departure_scope='city',flight_no='CA8321'))

    def test_provider_city_expansion_never_overrides_explicit_airport(self):
        p=self.fixture()
        result=p.run(args(departure='首都'),time.monotonic()+30)
        self.assertEqual(result['candidates'],[])
        p=self.fixture([row(departureName='首都国际机场')])
        with self.assertRaises(ServiceError):p.run(args(),time.monotonic()+30)

    def test_city_scope_uses_city_codes_and_rejects_wrong_city(self):
        p=self.fixture();p.run(args(departure='北京',arrival='上海',departure_scope='city',arrival_scope='city'),time.monotonic()+30)
        self.assertEqual((self.calls[0]['departure'],self.calls[0]['arrival']),('BJS','SHA'))
        p=self.fixture([row(departure='CTU',departureName='成都双流国际机场')])
        with self.assertRaises(ServiceError):p.run(args(departure='北京',departure_scope='city'),time.monotonic()+30)

    def test_cached_same_provider_query_reuses_data_new_time_filter_without_charge(self):
        p=self.fixture();first=p.run(args(),time.monotonic()+30)
        later=p.run(args(departure_start='15:00',departure_end='16:00'),time.monotonic()+30)
        self.assertEqual(len(self.calls),1);self.assertTrue(later['evidence']['cache_hit'])
        self.assertEqual(later['candidates'],[]);self.assertEqual(first['evidence']['provider_request_attempts'],1);self.assertEqual(first['evidence']['chargeable_request_attempts'],0)

    def test_budget_and_timeout_prevent_extra_charge_and_failures_do_not_retry(self):
        p=self.fixture(daily_request_limit=1)
        p.run(args(),time.monotonic()+30)
        with self.assertRaises(ServiceError):p.run(args(refresh=True),time.monotonic()+30)
        self.assertEqual(len(self.calls),1)
        p=self.fixture()
        with self.assertRaises(ServiceError):p.run(args(),time.monotonic()+1)
        self.assertEqual(self.calls,[])
        p.transport=lambda wire:{'error_code':10012,'reason':'fixture-user-key'}
        with self.assertRaises(ServiceError) as error:p.run(args(),time.monotonic()+30)
        self.assertNotIn('fixture-user-key',str(error.exception))
        self.assertEqual(p.day_total,1)

    def test_model_selects_order_and_count_no_program_top_three_and_history_preserves(self):
        p=self.fixture([row(flightNo='CA'+str(8300+i)) for i in range(5)])
        ds=Decider([4,1,3,0]);runtime=CommercialQueries(flight_provider=p,decider=ds)
        result=runtime.execute({'tool':'flight','args':args()},time.monotonic()+70,{'utterance':'按我的条件选'})
        self.assertEqual([r['row'] for r in result['commercial']['flights']],[4,1,3,0])
        self.assertFalse(result['performed']);self.assertTrue(result['commercial']['simulation'])
        self.assertEqual(len(ds.calls),2);self.assertEqual(len(self.calls),1)
        self.assertIn('CA8304',remember_result(result))
        bad=deliver_flights
        with self.assertRaises(ServiceError):bad(p,args(),Decider([99]),time.monotonic()+70,{})

    def test_delivery_review_rejects_and_retries_model_only_not_paid_provider(self):
        p=self.fixture()
        class BadDS(Decider):
            def ask(self,system,payload,**kwargs):
                self.calls.append((system,payload))
                if '机票交付复核者' in system:return {'ok':False,'issues':['不得把参考价称含税可购。']},{}
                return {'reply':'含税最低2285元，可以购买。','selection':[0]},{}
        ds=BadDS()
        with self.assertRaisesRegex(ServiceError,'口播复核未通过'):
            deliver_flights(p,args(),ds,time.monotonic()+70,{})
        self.assertEqual(len(self.calls),1);self.assertEqual(len(ds.calls),4)
        self.assertEqual(ds.calls[2][1]['feedback'],['不得把参考价称含税可购。'])

    def test_plan_catalog_and_actual_conversation_roundtrip_replay_no_charge(self):
        from general_dialogue import Conversation
        p=self.fixture()
        class MainDS(Decider):
            def ask(self,system,payload,**kwargs):
                if 'utterance' in payload:
                    return {'reply':'正在查询。','capability':'flight','action':'prepare','query':{'tool':'flight','args':args()}},{}
                return super().ask(system,payload,**kwargs)
        ds=MainDS();runtime=CommercialQueries(flight_provider=p,decider=ds)
        with tempfile.TemporaryDirectory() as tmp:
            c=Conversation(Path(tmp)/'history.db',ds,queries=runtime,plan_validator=validate_plan,system_suffix=runtime.rules,history_formatter=remember_result)
            out=c.turn('c'*64,'a'*32,0,'b'*32,'查明天的直飞航班')
            self.assertEqual(out['commercial']['kind'],'flight');self.assertEqual(out['tool'],'flight')
            self.assertEqual(out['reply'],ds.ask('',{})[0]['reply'])
            again=c.turn('c'*64,'a'*32,0,'b'*32,'查明天的直飞航班')
            self.assertEqual(out,again);self.assertEqual(len(self.calls),1)

    def test_bridge_passes_flight_context_without_rewriting_reply_or_history(self):
        from conversation_bridge import Bridge
        calls=[]
        def speech(path,body,kind,token):
            calls.append(json.loads(body)['text'])
            return {'audio':'AA==','mime':'audio/wav'}
        original={'reply':'CA8321，10:40出发，参考2285元。','tool':'flight','text':'航班'}
        rendered=Bridge.render(types.SimpleNamespace(call=speech),original,'local','fixture')
        self.assertEqual(rendered['reply'],original['reply'])
        self.assertEqual(rendered['spoken'],original['reply'])
        self.assertEqual(rendered['speechText'],calls[0])
        self.assertIn('C A 八、三、二、一',calls[0])
        self.assertIn('上午十点四十分',calls[0])

    def test_flight_reading_context_only_and_guards(self):
        text='CA8321、3U8633，10:40出发。PKX PVG A320 B737，参考2285元。'
        expanded=speech_for_tts(text,flight=True)
        self.assertIn('C A 八、三、二、一',expanded);self.assertIn('三 U 八、六、三、三',expanded)
        self.assertIn('PKX PVG A320 B737',expanded)
        self.assertIn('CA8321',speech_for_tts(text))
        self.assertEqual(speech_for_tts('https://example.com/CA8321',flight=True),'https://example.com/CA8321')

if __name__=='__main__':unittest.main()
