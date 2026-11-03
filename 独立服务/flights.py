"""Offline flight query flow. No network client, key, or production registration.

Only an explicitly injected fixture transport can return rows in this version.
Provider fields follow Juhe 818; live execution remains disconnected.
"""
import datetime as dt
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from core import ServiceError, departure_window

DATA_PATH = Path(__file__).parent/'data/flight-locations.json'


class Locations:
    def __init__(self):
        path = DATA_PATH
        if not path.is_file():
            path = Path(__file__).resolve().parents[1]/'.build/flight-data/flight-locations.json'
        self.tables = json.loads(path.read_text()) if path.is_file() else {'city': [], 'airport': []}
        self.city_names = {name for _, name in self.tables['city'] if len(name) >= 2}

    def resolve(self, name, scope):
        if not self.tables['city'] or not self.tables['airport']:
            raise ServiceError('机票地点资料尚未准备，请先按构建说明下载供应商资料；本次未调用收费查询。')
        if not isinstance(name, str) or scope not in ('city', 'airport'):
            raise ServiceError('请说明城市，或明确指定哪个机场。')
        raw = name.strip().upper()
        matches = []
        for code, label in self.tables[scope]:
            aliases = {code, label}
            if scope == 'city':
                aliases.add(label + '市')
            else:
                aliases |= {label.removesuffix('国际机场'), label.removesuffix('机场')}
                for length in range(2, len(label)):
                    if label[:length] in self.city_names:
                        short = label[length:]
                        aliases |= {short, short.removesuffix('国际机场'), short.removesuffix('机场')}
                # Short airport names already occur in the official table,
                # e.g. 首都国际机场 -> 首都, 浦东国际机场 -> 浦东.
            if raw in aliases:
                matches.append((code, label))
        matches = sorted(set(matches))
        if len(matches) != 1:
            raise ServiceError('没有唯一对应的城市或机场，请补充完整名称。')
        return {'code': matches[0][0], 'name': matches[0][1], 'scope': scope}


def prepare(payload, locations=None):
    """Validate the complete request chosen by DS, without inferring user intent."""
    if not isinstance(payload, dict):
        raise ServiceError('机票条件格式不正确。')
    allowed = {'departure', 'arrival', 'departure_scope', 'arrival_scope', 'date',
               'departure_time_range', 'departure_start', 'departure_end', 'max_segments'}
    if not set(payload) <= allowed:
        raise ServiceError('有尚不支持的机票筛选条件。')
    merged = dict(payload)
    locations = locations or Locations()
    resolved = {}
    for prefix in ('departure', 'arrival'):
        point = locations.resolve(merged.get(prefix), merged.get(prefix + '_scope'))
        resolved[prefix] = point
    if resolved['departure']['code'] == resolved['arrival']['code']:
        raise ServiceError('出发和到达地点不能相同。')
    try:
        date = dt.date.fromisoformat(merged.get('date', ''))
    except (ValueError, TypeError):
        raise ServiceError('请说明出发日期。') from None
    if date < dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date():
        raise ServiceError('不能查询已经过去的出发日期。')
    segments = merged.get('max_segments')
    if type(segments) is not int or segments not in (0, 1, 2, 3):
        raise ServiceError('请确认只看直飞，还是接受中转。')
    window = departure_window(merged)
    return {'request': {**{k: v for k, v in merged.items() if k in allowed}, **window},
            'locations': resolved, 'date': date.isoformat(), 'max_segments': segments,
            'window': window}


def select_rows(data, prepared):
    if not isinstance(data, dict) or data.get('error_code') != 0:
        raise ServiceError('机票数据未返回有效结果。')
    rows = data.get('result', {}).get('flightInfo')
    if not isinstance(rows, list):
        raise ServiceError('机票返回格式待核对。')
    stamp = lambda text: int(text[:2]) * 60 + int(text[3:])
    start = stamp(prepared['window']['departure_start']); end = stamp(prepared['window']['departure_end'])
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ServiceError('机票字段不完整。')
        required = ('flightNo', 'departure', 'arrival', 'departureName', 'arrivalName',
                    'departureDate', 'arrivalDate', 'departureTime', 'arrivalTime')
        if any(not isinstance(row.get(k), str) or not row[k] for k in required):
            raise ServiceError('机票字段不完整。')
        for field in ('departureTime', 'arrivalTime'):
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', row[field]):
                raise ServiceError('机票时间字段待核对。')
        try:
            if dt.date.fromisoformat(row['arrivalDate']) < dt.date.fromisoformat(row['departureDate']):
                raise ValueError
        except ValueError:
            raise ServiceError('机票起降日期待核对。') from None
        if any(not re.fullmatch(r'[A-Z]{3}', row[key]) for key in ('departure', 'arrival')):
            raise ServiceError('机票机场编码待核对。')
        count = row.get('transferNum')
        if type(count) is not int or count < 1:
            raise ServiceError('机票航段数待核对。')
        if row['departureDate'] != prepared['date'] or not start <= stamp(row['departureTime']) < end:
            continue
        if prepared['max_segments'] and count > prepared['max_segments']:
            continue
        if any(point['scope'] == 'airport' and row[side] != point['code']
               for side, point in prepared['locations'].items()):
            continue
        price = row.get('ticketPrice')
        if price is not None:
            try:
                amount = Decimal(str(price))
                if not amount.is_finite() or amount < 0:
                    raise InvalidOperation
                price = format(amount, 'f')
            except InvalidOperation:
                raise ServiceError('参考票价格式待核对。') from None
        identity = tuple(row[k] for k in ('flightNo', 'departure', 'arrival', 'departureDate', 'departureTime'))
        if identity in seen:
            continue
        seen.add(identity)
        result.append({'flight': row['flightNo'], 'airline': row.get('airlineName', ''),
                       'origin': row['departureName'], 'origin_code': row['departure'],
                       'destination': row['arrivalName'], 'destination_code': row['arrival'],
                       'date': row['departureDate'], 'arrival_date': row['arrivalDate'],
                       'departure': row['departureTime'], 'arrival': row['arrivalTime'],
                       'segments': count, 'transfers': count - 1,
                       'code_share': row.get('isCodeShare'), 'reference_price': price})
    return {'state': 'complete' if result else 'empty', 'matched': len(result), 'candidates': result,
            'notice': '参考票价；税费、行李、退改及可购买性未核实。',
            'message': '' if result else '指定机场和时段没有匹配航班；可调整条件，不自动扩大查询。'}


class FlightFlow:
    def __init__(self, fixture_transport=None, fixture_decider=None):
        self.fixture_transport = fixture_transport
        self.fixture_decider = fixture_decider

    def run(self, payload):
        prepared = prepare(payload)
        if self.fixture_transport is None:
            return {'state': 'not_connected', 'request': prepared['request'], 'flights': [],
                    'message': '机票收费数据尚未接通，本次没有查询航班。'}
        body = {'departure': prepared['locations']['departure']['code'],
                'arrival': prepared['locations']['arrival']['code'], 'departureDate': prepared['date'],
                'maxSegments': str(prepared['max_segments'])}
        collected = select_rows(self.fixture_transport(body), prepared)
        if self.fixture_decider is None:
            return {**collected, 'state': 'awaiting_model_decision', 'simulation': True,
                    'request': prepared['request']}
        decision = self.fixture_decider(prepared, collected)
        if (not isinstance(decision, dict) or set(decision) != {'reply', 'selection'}
                or not isinstance(decision['reply'], str) or not isinstance(decision['selection'], list)):
            raise ServiceError('机票模型选择格式不正确。')
        selected = decision['selection']
        if (any(type(index) is not int or not 0 <= index < len(collected['candidates']) for index in selected)
                or len(set(selected)) != len(selected)):
            raise ServiceError('机票模型选择了不存在或重复的候选。')
        return {'state': collected['state'], 'matched': collected['matched'],
                'flights': [collected['candidates'][index] for index in selected],
                'reply': decision['reply'], 'notice': collected['notice'],
                'simulation': True, 'selection_origin': 'model', 'request': prepared['request']}
