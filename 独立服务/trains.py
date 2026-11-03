"""Juhe 817 query only. No booking, account login or model rewriting."""
import datetime as dt
from decimal import Decimal, InvalidOperation
import json
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from core import ServiceError, validate_request

ENDPOINT = 'https://apis.juhe.cn/fapigw/train/query'
BEIJING = dt.timezone(dt.timedelta(hours=8))


def normalize_rows(rows):
    if not isinstance(rows, list):
        raise ServiceError('高铁接口返回格式异常。')
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            raise ServiceError('高铁车次字段异常，未展示不完整结果。')
        required = ('train_no', 'departure_station', 'arrival_station', 'departure_time', 'arrival_time', 'duration')
        if any(not isinstance(row.get(k), str) or not 1 <= len(row[k]) <= 40 for k in required):
            raise ServiceError('高铁车次字段缺失，未展示不完整结果。')
        for field in ('departure_time', 'arrival_time'):
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', row[field]):
                raise ServiceError('高铁时刻格式异常。')
        if not re.fullmatch(r'\d{1,3}:[0-5]\d', row['duration']):
            raise ServiceError('高铁历时格式异常。')
        prices = row.get('prices')
        if not isinstance(prices, list):
            raise ServiceError('高铁席别字段异常。')
        seats = []
        for seat in prices:
            if not isinstance(seat, dict) or not isinstance(seat.get('seat_name'), str):
                raise ServiceError('高铁席别字段异常。')
            raw_price = seat.get('price')
            price = None
            if raw_price is not None:
                try:
                    amount = Decimal(str(raw_price))
                    if not amount.is_finite() or amount < 0 or amount > 100000:
                        raise InvalidOperation
                    price = format(amount, 'f')
                except InvalidOperation:
                    raise ServiceError('高铁票价格式异常。') from None
            number = seat.get('num')
            seats.append({'seat_name': seat['seat_name'][:30], 'price': price,
                          'availability': str(number)[:30] if number is not None else '未提供'})
        clean.append({**{k: row[k] for k in required}, 'seats': seats,
                      'booking_available': row.get('enable_booking') == 'Y'})
    return clean


class TrainClient:
    def __init__(self, key, *, transport=None):
        self.key = key.strip()
        self.transport = transport
        self.cache = {}
        self.lock = threading.Lock()

    def query(self, body):
        if self.transport:
            return self.transport(body)
        # Keep key out of URLs, logs, task records and browser responses.
        request = urllib.request.Request(ENDPOINT, data=urlencode({**body, 'key': self.key}).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ServiceError('高铁接口响应过大。')
                return json.loads(raw)
        except (OSError, ValueError):
            raise ServiceError('高铁接口连接或返回数据异常，请稍后重试。') from None

    def run(self, payload, progress=lambda _: None):
        request = validate_request(payload)
        if request['mode'] != 'train':
            raise ServiceError('高铁查询参数不正确。')
        if not self.key:
            raise ServiceError('请先在私有配置中填写 JUHE_TRAIN_KEY。')
        # The provider accepts city names and returns the actual stations. Do
        # not guess a city's primary station or fan out into guessed pairs.
        wire = {'search_type': '1', 'date': request['date'], 'filter': request['filter'], 'enable_booking': '2'}
        for field in ('departure_station', 'arrival_station'):
            name = request[field]
            wire[field] = name[:-1] if name.endswith('市') and len(name) > 2 else name.removesuffix('站')
        minute = lambda text: int(text[:2]) * 60 + int(text[3:])
        start, end = minute(request['departure_start']), minute(request['departure_end'])
        for label, low, high in [('凌晨', 0, 360), ('上午', 360, 720), ('下午', 720, 1080), ('晚上', 1080, 1440)]:
            if low <= start < end <= high:
                wire['departure_time_range'] = label
                break
        # Cache the provider's rows, not the user's filtered selection. A
        # follow-up from "afternoon" to "14–17" can reuse the same response.
        identity = json.dumps(wire, sort_keys=True, ensure_ascii=False)
        # Serialize duplicate requests; short memory cache avoids wasting daily quota.
        with self.lock:
            now = time.monotonic()
            self.cache = {k: v for k, v in self.cache.items() if now - v[0] < 60}
            cached = identity in self.cache
            if cached:
                _, rows, queried_at = self.cache[identity]
            else:
                progress('正在查询聚合数据高铁接口')
                data = self.query(wire)
                if not isinstance(data, dict):
                    raise ServiceError('高铁接口返回格式异常。')
                code = data.get('error_code')
                if type(code) is not int or code != 0:
                    messages = {10001: '高铁 key 无效，请检查私有配置。',
                                10002: '此 key 未开通高铁817接口权限。', 10003: '高铁 key 已过期。',
                                10012: '高铁接口额度已用完，请在聚合数据控制台查看。',
                                10013: '高铁试用额度已用完。'}
                    raise ServiceError(messages.get(code, '高铁查询未成功，请检查城市、车站或日期。'))
                rows = normalize_rows(data.get('result'))
                queried_at = dt.datetime.now(BEIJING).isoformat(timespec='seconds')
                if len(self.cache) < 100:
                    self.cache[identity] = (time.monotonic(), rows, queried_at)
        # Exact hours are filtered again locally even if the provider ignores
        # its broad period. Never substitute all-day trains for an empty range.
        trains = [row for row in rows if start <= minute(row['departure_time']) < end]
        for prefix in ('departure', 'arrival'):
            if request[prefix + '_scope'] == 'station':
                name = request[prefix + '_station'].removesuffix('站')
                trains = [row for row in trains if row[prefix + '_station'].removesuffix('站') == name]
        label = '全天' if (start, end) == (0, 1440) else f"{request['departure_start']}–{request['departure_end']}出发"
        return {'state': 'complete' if trains else 'empty', 'request': request, 'sources': [],
                'provider': '聚合数据 · 817', 'trains': trains, 'cached': cached, 'time_label': label,
                'queried_at': queried_at,
                'notice': '票价余票为接口查询快照，可能延迟；以12306购票页为准。按出发时间筛选。',
                'purchase_url': 'https://www.12306.cn/index/',
                'message': '' if trains else f'{label}没有查到符合条件的班次；可以扩大时段或换日期。'}
