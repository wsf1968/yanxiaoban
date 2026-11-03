"""Read-only 12306 web query adapter; fixed host, bounded calls, no login/order.

The website's current data format is not a promised public API. Fail closed on
schema changes. Per request: init + station catalog + one query + <=3 prices.
"""
import datetime as dt
import http.cookiejar
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
BASE = 'https://kyfw.12306.cn'
INIT = '/otn/leftTicket/init'
SEATS = {'second': ('二等座', 30, 'O'), 'first': ('一等座', 31, 'M'),
         'business': ('商务座', 32, 'A9')}
ERRORS = {
    'rail_date': '目前只查询今天起十五天内的高铁、动车直达车次，请换个日期。',
    'rail_place': '没有准确对应到铁路城市或车站，请补充或确认站名。',
    'rail_parameters': '这项铁路查询条件还不明确，请确认日期、车站或时段。',
    'rail_restricted': '12306这次要求验证或限制访问，查询已停止，暂时不能判断有无车票。',
    'rail_login': '12306这次要求登录，尚未取得车票信息。',
    'rail_timeout': '12306查询超时了，暂时不能判断有无车票。',
    'rail_unavailable': '12306查询暂时未成功，不能把这次失败当作没有车票。',
    'rail_schema': '12306返回的数据格式暂时无法核验，这次不能给出可靠的车票信息。',
}


class RailError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_args(args, now=None):
    required = {'origin', 'destination', 'date'}
    optional = {'seat', 'after', 'before', 'train_code', 'sort', 'train_type'}
    if not isinstance(args, dict) or not required <= set(args) <= required | optional:
        raise RailError('rail_parameters')
    for name in ('origin', 'destination'):
        if not isinstance(args[name], str) or not re.fullmatch(r'[\u3400-\u9fffA-Za-z·]{1,30}', args[name]):
            raise RailError('rail_parameters')
    if args.get('seat', 'second') not in SEATS or args.get('sort', 'departure') not in {'departure', 'duration'}:
        raise RailError('rail_parameters')
    if args.get('train_type', 'GD') not in {'G', 'D', 'GD'}:
        raise RailError('rail_parameters')
    if not isinstance(args['date'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args['date']):
        raise RailError('rail_date')
    try:
        date = dt.date.fromisoformat(args['date'])
    except ValueError:
        raise RailError('rail_date') from None
    if now and not now.date() <= date <= now.date() + dt.timedelta(days=14):
        raise RailError('rail_date')
    for name in ('after', 'before'):
        if name in args and (not isinstance(args[name], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', args[name])):
            raise RailError('rail_parameters')
    if args.get('after', '00:00') > args.get('before', '23:59'):
        raise RailError('rail_parameters')
    if 'train_code' in args and (not isinstance(args['train_code'], str) or not re.fullmatch(r'[GD]\d{1,5}', args['train_code'])):
        raise RailError('rail_parameters')
    if args.get('train_code') and args['train_code'][0] not in args.get('train_type', 'GD'):
        raise RailError('rail_parameters')
    return dict(args)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OfficialClient:
    def __init__(self):
        self.deadline = time.monotonic() + 16
        self.calls = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path, params=None, limit=2_000_000):
        if not (path == INIT or path == '/otn/leftTicket/queryTicketPrice'
                or re.fullmatch(r'/otn/leftTicket/query[A-Za-z]*', path)
                or re.fullmatch(r'/otn/resources/js/framework/station_name\.js(?:\?station_version=[\d.]+)?', path)):
            raise RailError('rail_parameters')
        if self.calls >= 6:
            raise RailError('rail_parameters')
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RailError('rail_timeout')
        self.calls += 1
        url = BASE + path + ('?' + urllib.parse.urlencode(params) if params else '')
        request = urllib.request.Request(url, headers={'Referer': BASE + INIT, 'Accept-Encoding': 'identity'})
        try:
            with self.opener.open(request, timeout=min(5, remaining)) as response:
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise RailError('rail_schema')
                return raw.decode('utf-8')
        except urllib.error.HTTPError as error:
            status = error.code
            location = error.headers.get('Location', '')
            error.close()
            if status in (403, 429):
                raise RailError('rail_restricted') from None
            if status == 401 or (status in (301, 302, 303, 307, 308) and 'login' in location.lower()):
                raise RailError('rail_login') from None
            raise RailError('rail_unavailable') from None
        except (TimeoutError, socket.timeout):
            raise RailError('rail_timeout') from None
        except (OSError, UnicodeError):
            raise RailError('rail_unavailable') from None


def payload(text):
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        if isinstance(text, str) and re.search(r'captcha|安全验证|人机验证|访问验证|访问过于频繁', text[:2000], re.I):
            raise RailError('rail_restricted') from None
        raise RailError('rail_schema') from None
    if not isinstance(data, dict) or data.get('status') is not True or not isinstance(data.get('data'), dict):
        raise RailError('rail_unavailable')
    if data['data'].get('is_login') in ('N', False):
        raise RailError('rail_login')
    return data['data']


def catalog(text):
    stations = {}
    for entry in text.split('@')[1:]:
        parts = entry.split('|')
        if len(parts) < 8:
            raise RailError('rail_schema')
        name, code, city = parts[1], parts[2], parts[7]
        if not re.fullmatch(r'[A-Z]{3}', code) or not name or not city:
            raise RailError('rail_schema')
        if code in stations and stations[code] != (name, city):
            raise RailError('rail_schema')
        stations[code] = (name, city)
    if not stations:
        raise RailError('rail_schema')
    return stations


def resolve(name, stations):
    explicit_station = name.endswith('站')
    clean = name[:-1] if explicit_station or name.endswith('市') else name
    city_codes = {code for code, row in stations.items() if row[1] == clean}
    exact = [code for code, row in stations.items() if row[0] == clean]
    if not explicit_station and city_codes:
        # City query is scoped to the official returned rows, not a promise that
        # one station query exhaustively enumerates every city-pair service.
        anchor = exact[0] if len(exact) == 1 else sorted(city_codes)[0]
        return {'name': clean, 'scope': 'city', 'codes': city_codes, 'anchor': anchor}
    if len(exact) != 1:
        raise RailError('rail_place')
    return {'name': clean, 'scope': 'station', 'codes': set(exact), 'anchor': exact[0]}


def minute(value):
    if not re.fullmatch(r'\d{2,3}:[0-5]\d', value):
        raise RailError('rail_schema')
    hours, minutes = map(int, value.split(':'))
    return hours * 60 + minutes


def availability(value, sale_flag, button):
    if sale_flag not in {'Y', 'N'}:
        raise RailError('rail_schema')
    if sale_flag != 'Y':
        if '起售' in button or '未开售' in button:
            return '未开售'
        if '停运' in button:
            return '停运'
        return '暂不可售'
    if value in {'有', '无', '候补'}:
        return value
    if value in {'', '--'}:
        return '未提供'
    if re.fullmatch(r'\d{1,4}', value):
        return '无' if int(value) == 0 else str(int(value)) + '张'
    raise RailError('rail_schema')


def parse_rows(data, stations, origin, destination, args, now):
    rows = data.get('result')
    if data.get('flag') not in ('1', 1) or not isinstance(rows, list) or len(rows) > 2000:
        raise RailError('rail_schema')
    seat = SEATS[args.get('seat', 'second')]
    results = []
    seen = set()
    for row in rows:
        if not isinstance(row, str) or len(row) > 10000:
            raise RailError('rail_schema')
        fields = row.split('|')
        if len(fields) < 40:
            raise RailError('rail_schema')
        if fields[6] not in origin['codes'] or fields[7] not in destination['codes']:
            continue
        code = fields[3]
        if not re.fullmatch(r'[GD]\d{1,5}', code):
            continue
        if code[0] not in args.get('train_type', 'GD'):
            continue
        if args.get('train_code') and args['train_code'] != code:
            continue
        depart, arrive, duration = minute(fields[8]), minute(fields[9]), minute(fields[10])
        if depart >= 1440 or arrive >= 1440 or not 0 < duration <= 4320 or (depart + duration) % 1440 != arrive:
            raise RailError('rail_schema')
        if not args.get('after', '00:00') <= fields[8] <= args.get('before', '23:59'):
            continue
        if args['date'] == now.date().isoformat() and fields[8] <= now.strftime('%H:%M'):
            continue
        if not re.fullmatch(r'[A-Za-z0-9]{8,20}', fields[2]) or not re.fullmatch(r'\d{2}', fields[16]) or not re.fullmatch(r'\d{2}', fields[17]) or not re.fullmatch(r'[A-Za-z0-9]{1,30}', fields[35]):
            raise RailError('rail_schema')
        identity = (code, fields[6], fields[7])
        if identity in seen:
            continue
        seen.add(identity)
        results.append({'train': code, 'origin': stations[fields[6]][0], 'destination': stations[fields[7]][0],
            'date': args['date'], 'depart': fields[8], 'arrive': fields[9],
            'arriveDate': (dt.date.fromisoformat(args['date']) + dt.timedelta(days=(depart + duration) // 1440)).isoformat(),
            'durationMinutes': duration, 'seat': seat[0], 'availability': availability(fields[seat[1]], fields[11], fields[1]),
            'price': None, '_priceArgs': {'train_no': fields[2], 'from_station_no': fields[16],
                'to_station_no': fields[17], 'seat_types': fields[35], 'train_date': args['date']}})
    results.sort(key=lambda r: (r['durationMinutes'], r['depart'], r['train']) if args.get('sort') == 'duration' else (r['depart'], r['durationMinutes'], r['train']))
    # One city query may include several stops on the same physical train.
    # Return one clearly named station pair per train, using the selected order.
    unique = {}
    for result in results:
        unique.setdefault(result['train'], result)
    return list(unique.values())


def read_price(data, seat):
    value = data.get(SEATS[seat][2])
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[¥￥]\d{1,5}(?:\.\d{1,2})?', value):
        raise RailError('rail_schema')
    amount = Decimal(value[1:])
    if amount <= 0 or amount > 100000:
        raise RailError('rail_schema')
    return format(amount.normalize(), 'f')


def query(args, now=None, client=None):
    now = now or dt.datetime.now(TZ)
    args = validate_args(args, now)
    client = client or OfficialClient()
    html = client.get(INIT, limit=1_000_000)
    endpoint = re.search(r"var CLeftTicketUrl\s*=\s*'(?P<path>leftTicket/query[A-Za-z]*)'", html)
    asset = re.search(r'["\'](?P<path>/otn/resources/js/framework/station_name\.js(?:\?station_version=[\d.]+)?)["\']', html)
    if not endpoint or not asset:
        raise RailError('rail_schema')
    stations = catalog(client.get(asset['path']))
    origin, destination = resolve(args['origin'], stations), resolve(args['destination'], stations)
    if origin['codes'] == destination['codes']:
        raise RailError('rail_parameters')
    data = payload(client.get('/otn/' + endpoint['path'], {
        'leftTicketDTO.train_date': args['date'], 'leftTicketDTO.from_station': origin['anchor'],
        'leftTicketDTO.to_station': destination['anchor'], 'purpose_codes': 'ADULT'}))
    rows = parse_rows(data, stations, origin, destination, args, now)
    selected = rows[:3]
    failures = []
    price_calls = 0
    for row in selected:
        params = row.pop('_priceArgs')
        if failures and failures[-1] in {'rail_restricted', 'rail_login', 'rail_timeout'}:
            failures.append(failures[-1])
            continue
        try:
            price_calls += 1
            row['price'] = read_price(payload(client.get('/otn/leftTicket/queryTicketPrice', params, limit=64000)), args.get('seat', 'second'))
            if row['price'] is None:
                failures.append('rail_price_missing')
        except RailError as error:
            failures.append(error.code)
    stamp = dt.datetime.now(TZ).strftime('%H:%M')
    seat = SEATS[args.get('seat', 'second')][0]
    # Explicitly limit the scope of city aggregation and ranking.
    prefix = args['date'][5:].replace('-', '月') + '日，' + seat + '。'
    if not selected:
        reply = prefix + '12306本次返回中，没有匹配这段起讫站、时段和车次条件的高铁或动车；不代表所有同城站点或中转都无车。'
    else:
        order = '按耗时' if args.get('sort') == 'duration' else '按出发时间'
        reply = ('你问的 ' + args['train_code'] + '，' + prefix) if args.get('train_code') else (
            prefix + '本次匹配' + str(len(rows)) + '班，' + order + '列出' + str(len(selected)) + '班：')
        for row in selected:
            day = '次日' if row['arriveDate'] != row['date'] else ''
            if (dt.date.fromisoformat(row['arriveDate']) - dt.date.fromisoformat(row['date'])).days > 1:
                day = row['arriveDate'][5:].replace('-', '月') + '日'
            price = row['price'] + '元' if row['price'] is not None else '票价未取得'
            train_label = '' if args.get('train_code') else row['train'] + '，'
            reply += f"{train_label}{row['origin']}{row['depart']}到{row['destination']}{day}{row['arrive']}，{price}，{row['availability']}。"
    reply += '12306 ' + stamp + '查询，票价余票会变。'
    if len(reply) > 300:
        raise RailError('rail_schema')
    return {'status': 'partial' if failures or not selected else 'complete', 'reply': reply,
        'missing': (['部分票价未取得'] if failures else ['没有匹配车次，未穷尽同城站点及中转'] if not selected else []),
        'sources': [{'provider': '12306', 'title': '12306车次余票查询', 'url': BASE + INIT,
                     'reporttime': dt.datetime.now(TZ).isoformat(), 'location': origin['name'] + '→' + destination['name']}],
        'rail': {'date': args['date'], 'originScope': origin['scope'], 'destinationScope': destination['scope'],
                 'matchedCount': len(rows), 'trains': selected, 'scope': 'official_returned_direct_G_D_only'},
        'diagnostics': {'provider': '12306', 'queryCalls': 1, 'priceCalls': price_calls, 'priceErrors': failures}}
