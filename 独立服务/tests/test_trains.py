import datetime as dt
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import ServiceError, validate_request
from trains import TrainClient, normalize_rows, BEIJING


def payload():
    return {'mode': 'train', 'departure_station': '北京南', 'arrival_station': '上海虹桥',
            'date': (dt.datetime.now(BEIJING).date()+dt.timedelta(days=1)).isoformat(), 'filter': 'G'}


ROW = {'train_no': 'G25', 'departure_station': '北京南', 'arrival_station': '上海虹桥',
       'departure_time': '18:04', 'arrival_time': '22:32', 'duration': '04:28',
       'enable_booking': 'Y', 'prices': [{'seat_name': '二等座', 'price': 627.5, 'num': '无'},
                                        {'seat_name': '一等座', 'price': None, 'num': None}]}


class TrainTests(unittest.TestCase):
    def test_date_station_filter_validation(self):
        self.assertEqual(validate_request(payload())['mode'], 'train')
        today = dt.datetime.now(BEIJING).date()
        for change in [{'date': (today+dt.timedelta(days=15)).isoformat()},
                       {'date': (today-dt.timedelta(days=1)).isoformat()},
                       {'arrival_station': '北京南'}, {'departure_station': 'https://bad.test'},
                       {'filter': 'K'}, {'date': None}]:
            with self.assertRaises(ServiceError):
                validate_request({**payload(), **change})

    def test_structured_prices_no_inventory_or_booking_invented(self):
        result = normalize_rows([copy.deepcopy(ROW)])[0]
        self.assertEqual(result['seats'][0], {'seat_name': '二等座', 'price': '627.5', 'availability': '无'})
        self.assertIsNone(result['seats'][1]['price'])
        self.assertEqual(result['seats'][1]['availability'], '未提供')
        self.assertNotIn('purchase_url', result)
        row = {**ROW, 'departure_time': '25:00'}
        with self.assertRaises(ServiceError):
            normalize_rows([row])
        with self.assertRaises(ServiceError):
            normalize_rows([{**ROW, 'prices': [{'seat_name': '二等座', 'price': 'NaN'}]}])

    def test_one_query_short_cache_and_empty_result(self):
        calls = []
        def transport(body):
            calls.append(body)
            return {'error_code': 0, 'result': [copy.deepcopy(ROW)]}
        client = TrainClient('test-only', transport=transport)
        first, second = client.run(payload()), client.run(payload())
        self.assertFalse(first['cached']); self.assertTrue(second['cached'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['enable_booking'], '2')
        self.assertNotIn('key', calls[0])
        self.assertNotIn('test-only', str(first))
        result = TrainClient('test', transport=lambda _: {'error_code': 0, 'result': []}).run(payload())
        self.assertEqual(result['state'], 'empty')

    def test_quota_and_upstream_reason_never_leak_credentials(self):
        calls = []
        def transport(body):
            calls.append(body)
            return {'error_code': 10012, 'reason': 'secret-key-must-not-leak'}
        with self.assertRaisesRegex(ServiceError, '额度已用完') as error:
            TrainClient('test', transport=transport).run(payload())
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ServiceError):
            TrainClient('', transport=transport).run(payload())
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
