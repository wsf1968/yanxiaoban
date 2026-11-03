import copy
import datetime as dt
import unittest
from unittest.mock import patch

from core import ServiceError
from flights import FlightFlow, Locations, prepare


def request():
    return {'departure': '北京', 'arrival': '上海', 'date': (dt.date.today()+dt.timedelta(days=2)).isoformat(),
            'departure_time_range': '下午','departure_scope':'city','arrival_scope':'city','max_segments':1}


def row(number='CA0001', time='14:00', origin='PKX', destination='PVG', segments=1):
    return {'flightNo': number, 'airlineName': '模拟航司', 'departure': origin, 'arrival': destination,
            'departureName': '模拟出发机场', 'arrivalName': '模拟到达机场',
            'departureDate': request()['date'], 'arrivalDate': request()['date'],
            'departureTime': time, 'arrivalTime': '19:00', 'transferNum': segments,
            'ticketPrice': 535, 'isCodeShare': False}


class FlightFlowTests(unittest.TestCase):
    def fixture(self, rows):
        self.calls=[]
        def transport(body):
            self.calls.append(body)
            return {'error_code':0, 'result': {'flightInfo': rows}}
        return FlightFlow(transport,lambda prepared,data:{'reply':'模拟模型选择结果','selection':list(reversed(range(len(data['candidates']))))})

    def test_city_and_explicit_airport_are_distinct(self):
        locations=Locations()
        self.assertEqual(locations.resolve('北京','city')['code'],'BJS')
        self.assertEqual(locations.resolve('上海','city')['code'],'SHA')
        self.assertEqual(locations.resolve('首都','airport')['code'],'PEK')
        self.assertEqual(locations.resolve('虹桥','airport')['code'],'SHA')
        self.assertEqual(locations.resolve('大兴','airport')['code'],'PKX')
        with self.assertRaises(ServiceError):locations.resolve('未确认的城市','city')

    def test_default_is_disconnected_and_mock_flow_never_opens_network(self):
        with patch('urllib.request.urlopen',side_effect=AssertionError('No flight calls allowed')):
            self.assertEqual(FlightFlow().run(request())['state'],'not_connected')
            result=self.fixture([row()]).run(request())
            self.assertTrue(result['simulation'])
            self.assertEqual(result['matched'],1)

    def test_time_filter_obeys_model_direct_choice_and_preserves_model_candidate_order(self):
        rows=[row('CA'+str(i),time) for i,time in enumerate(['11:59','12:00','14:00','16:00','17:59','18:00'])]
        rows.append(row('CA9999','15:00',segments=2))
        result=self.fixture(rows).run(request())
        self.assertEqual(result['matched'],4)
        self.assertEqual(len(result['flights']),4)
        self.assertEqual(result['flights'][0]['departure'],'17:59')
        self.assertTrue(all(f['transfers']==0 for f in result['flights']))
        self.assertEqual(self.calls[0]['departure'],'BJS')
        self.assertEqual(self.calls[0]['maxSegments'],'1')

    def test_provider_airport_expansion_does_not_override_user_airport(self):
        flow=self.fixture([row(origin='PKX'),row('CA0002',origin='PEK')])
        result=flow.run({**request(),'departure':'首都','departure_scope':'airport'})
        self.assertEqual(result['matched'],1)
        self.assertEqual(result['flights'][0]['origin_code'],'PEK')
        empty=flow.run({**request(),'departure':'首都','departure_scope':'airport','departure_start':'16:00','departure_end':'17:00'})
        self.assertEqual(empty['state'],'empty')
        self.assertEqual(empty['flights'],[])

    def test_full_followup_request_is_supplied_by_model_not_inferred_in_adapter(self):
        initial=prepare(request())['request']
        later=prepare({**initial,'departure_start':'15:00','departure_time_range':''})
        self.assertEqual(later['window']['departure_end'],'18:00')
        with self.assertRaises(ServiceError):prepare({'departure_start':'15:00'})
        with self.assertRaises(ServiceError):prepare({**request(),'departure_scope':None})

    def test_reference_price_unknown_fields_and_malformed_data_are_not_fabricated(self):
        result=self.fixture([row()]).run(request())['flights'][0]
        self.assertEqual(result['reference_price'],'535')
        for field in ('tax','baggage','refund','purchase_url','availability'):
            self.assertNotIn(field,result)
        with self.assertRaises(ServiceError):
            self.fixture([{**row(),'ticketPrice':'NaN'}]).run(request())
        with self.assertRaises(ServiceError):
            self.fixture([{**row(),'transferNum':0}]).run(request())
