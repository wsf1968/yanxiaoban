"""Opt-in commercial Juhe 818 adapter. User key only; no free-key fallback.

Model owns intent, conditions, candidate ordering and spoken delivery. This
module validates the wire protocol, actual airport identities and evidence,
and limits charged requests. Tests inject historical data, never call 818.
"""
import copy
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import urllib.request
from urllib.parse import urlencode

from core import ServiceError
from flights import Locations, prepare, select_rows
from private_json import write_private

ENDPOINT = 'https://apis.juhe.cn/flight/query'
DOC = 'https://www.juhe.cn/docs/api/id/818'
NOTICE = '聚合818为按供应商规则计费的可选接口；仅参考票价，税费、舱位、行李、退改、余位与可购买性未核实，不代表最低价。'
DIRECTORY = '''
本轮机票目录覆盖旧的“机票未接入/12306手机操作”说明。flight为聚合818只读API，非网页或手机模拟。
当前状态：{status}。仅当payload.enabledExecutors包含flight才可prepare；未启用就明确不能查询，不调用搜索冒充。
启用由用户自己申请开通、填写自己的Key并首次认可可能计费。日常已启用查询不要求每句重复确认，不代充值。
capability=flight、action=prepare、query={"tool":"flight","args":{...}}。
args必有departure/arrival（用户城市或机场原话）、departure_scope/arrival_scope（city或airport）、date（YYYY-MM-DD）、max_segments（0任意、1直飞、2最多一次中转、3最多两次中转）。按today换算日期，缺关键条件或机场名歧义先clarify，不猜机场。
可加departure_time_range（凌晨/上午/下午/晚上或空）、departure_start/departure_end（HH:MM，同高铁时段；结束可24:00）；flight_no（具体航班号，此时两端必须明确机场scope）、requirements（完整预算/航司/其它用户条件，300字内）、refresh（用户明确要求重新查价才true）。
条件和连续修改由你合并完整输出，保留未改的日期/城市或机场/直飞/起飞时段/预算，不由连接程序猜。城市不得偷偷替成主机场。flight_no不得擅填猜测航班；日期没给才需追问，不能反复要求用户报机场。
所有选择排序与口播取舍由你根据真实候选决定；不能固定前三。无支持数据的条件需说明，不能当作已满足。
ticketPrice只为参考价；transferNum是航段数，1直飞，2一次中转。展示实际返回机场，PKX大兴不能写PEK首都，PVG浦东不能写SHA虹桥。不下单、不补购票链接，不保证含税/实时最低/可按该价购买。
'''
SELECTION = '''你是言小办，负责本次机票候选选择、排序与口播。请求/上下文保留完整条件；candidates是经协议和用户明确机场/时段校验后的真实返回，尚未替你推荐。
仅返回JSON {"reply":"300字以内自然口语","selection":[候选row整数编号,...]}。可选任意数量和顺序，无合适项就空并说明；不固定前三，不改变用户条件。reply只讲最终所选候选及必要限制，不逐条读未选项。
所有航班、机场、日期、时刻、航段数、参考价来自候选。机场代码与名称严格对应，transferNum原义航段数，transfers中转数。不捏造舱位、行李、余位、税费、退改或购买能力。参考价不能称含税总价/最低价/可购报价。候选不足或无匹配时如实说明，不以搜索结果填补。
这是用户自己开通的可选计费查询；若evidence.simulation为true，明确是历史数据隔离核验，不是当前航班查询。条件含供应商未提供字段时不要承诺满足。'''
REVIEW = '''你是机票交付复核者。只返回JSON {"ok":true或false,"issues":[问题描述]}。
依据request/context、selected候选与reply核查：关键条件没有丢失；航班号/真实机场/时间/日期/价格没有编造或错配；航段1没有说成中转1次；ticketPrice明确参考口径，不宣称已含税、最低、余位或可按该价买；不把历史simulation数据说当前查询。不选不存在候选。
不能用泛泛“已核实”代替事实，只有负面说明“未确认含税”等不算虚假保证。风格不佳不是错误，内容无误ok=true。'''


CITY_AIRPORTS={'BJS':{'PEK','PKX'},'SHA':{'SHA','PVG'},'CTU':{'CTU','TFU'}}


class CommercialLocations(Locations):
    def resolve(self,name,scope):
        try:return super().resolve(name,scope)
        except ServiceError:
            if not self.tables['airport'] or not self.tables['city']:raise
            if scope!='airport' or not isinstance(name,str):raise
        cities=dict(self.tables['city']);matches=set();raw=name.strip().upper()
        for code,label in self.tables['airport']:
            labels={label,label.replace('国际机场','机场'),label.removesuffix('国际机场'),label.removesuffix('机场')}
            for city,airports in CITY_AIRPORTS.items():
                if code in airports:
                    labels|={cities[city]+v for v in list(labels) if not v.startswith(cities[city])}
            if raw in labels:matches.add((code,label))
        if len(matches)!=1:raise ServiceError('没有唯一对应的城市或机场，请补充完整名称。')
        code,label=next(iter(matches));return {'code':code,'name':label,'scope':scope}


def validate_flight_args(args):
    if not isinstance(args, dict):
        raise ServiceError('机票条件格式不正确。')
    extra = {'flight_no', 'refresh', 'requirements'}
    base = {k:v for k,v in args.items() if k not in extra}
    prepared = prepare(base,CommercialLocations())
    number = args.get('flight_no', '')
    if not isinstance(number, str) or (number and not re.fullmatch(r'[A-Z0-9]{2}\d{3,4}', number)):
        raise ServiceError('具体航班号格式不正确。')
    if number and any(v['scope'] != 'airport' for v in prepared['locations'].values()):
        raise ServiceError('按具体航班号查询时，请明确出发和到达机场，不能把城市擅换成机场。')
    if type(args.get('refresh', False)) is not bool:
        raise ServiceError('重新查询参数不正确。')
    requirements = args.get('requirements', '')
    if not isinstance(requirements, str) or len(requirements) > 300 or any(ord(c)<32 for c in requirements):
        raise ServiceError('机票补充条件格式不正确。')
    prepared['request'].update(flight_no=number, refresh=args.get('refresh',False), requirements=requirements)
    return prepared


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ServiceError('机票接口出现非预期跳转，未转发凭证。')


class FlightProvider:
    def __init__(self, *, enabled=False, api_key='', paid_usage_acknowledged=False,
                 max_requests_per_minute=6, daily_request_limit=100, private=None,
                 transport=None, simulation=False, configuration_error=''):
        self.key = api_key.strip() if isinstance(api_key,str) else ''
        self.enabled = enabled is True and paid_usage_acknowledged is True and bool(self.key.strip())
        self.per_minute, self.per_day = max_requests_per_minute, daily_request_limit
        self.private = Path(private) if private else None
        self.transport, self.simulation = transport, simulation
        self.configuration_error = configuration_error
        self.lock = threading.Lock(); self.cache = {}; self.attempts = []; self.day = None; self.day_total = 0
        self.locations = CommercialLocations()

    @classmethod
    def from_private(cls, root):
        private = Path(root)/'.private';path=private/'flight-provider.json'
        if not path.exists():
            return cls(private=private)
        try:
            if not path.is_file() or path.stat().st_size>16384 or path.stat().st_mode & 0o077:
                raise ValueError()
            cfg=json.loads(path.read_text())
            allowed={'enabled','api_key','paid_usage_acknowledged','max_requests_per_minute','daily_request_limit'}
            if not isinstance(cfg,dict) or set(cfg)-allowed or type(cfg.get('enabled')) is not bool:
                raise ValueError()
            if not cfg['enabled']:
                return cls(private=private)
            if not isinstance(cfg.get('api_key'),str) or not 1<=len(cfg['api_key'].strip())<=512 or any(ord(c)<32 for c in cfg['api_key']) or cfg.get('paid_usage_acknowledged') is not True:
                raise ValueError()
            for key,default,maximum in [('max_requests_per_minute',6,30),('daily_request_limit',100,10000)]:
                value=cfg.get(key,default)
                if type(value) is not int or not 1<=value<=maximum:raise ValueError()
            return cls(private=private,**cfg)
        except (ValueError,OSError,TypeError):
            return cls(private=private,configuration_error='机票私有配置未通过校验；未启用，也没有调用。')

    @property
    def status(self):
        return {'enabled':self.enabled,'paid_interface':True,'live_verified':False,
                'state':'configured_not_live_verified' if self.enabled else 'invalid_configuration' if self.configuration_error else 'not_enabled'}

    def _reserve(self):
        now=time.time();date=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
        def update(record):
            if not isinstance(record.get('recent',[]),list):raise ServiceError('机票调用预算记录异常，未调用。')
            recent=[x for x in record.get('recent',[]) if type(x) in (int,float) and x>now-60]
            total=record.get('total',0) if record.get('date')==date else 0
            if type(total) is not int or total<0:raise ServiceError('机票调用预算记录异常，未调用。')
            if len(recent)>=self.per_minute or total>=self.per_day:
                raise ServiceError('已达到本机设置的机票计费请求预算，本次未调用；可在用户自己的配置中调整。')
            return {'date':date,'total':total+1,'recent':recent+[now]}
        if self.private:
            self.private.mkdir(parents=True,exist_ok=True,mode=0o700)
            with (self.private/'flight-budget.lock').open('a') as lock:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
                path=self.private/'flight-budget.json'
                try:old=json.loads(path.read_text()) if path.exists() else {}
                except (OSError,ValueError):raise ServiceError('机票调用预算记录异常，未调用。') from None
                if not isinstance(old,dict):raise ServiceError('机票调用预算记录异常，未调用。')
                write_private(path,update(old))
        else:
            record=update({'date':self.day,'total':self.day_total,'recent':self.attempts})
            self.attempts=record['recent']; self.day=record['date']; self.day_total=record['total']

    def _query(self, wire, timeout):
        if self.transport is not None:
            return self.transport(dict(wire))
        request=urllib.request.Request(ENDPOINT,data=urlencode({**wire,'key':self.key}).encode(),
            headers={'Content-Type':'application/x-www-form-urlencoded'})
        try:
            with urllib.request.build_opener(NoRedirect).open(request,timeout=timeout) as response:
                raw=response.read(2_000_001)
                if len(raw)>2_000_000:raise ServiceError('机票接口响应过大。')
                return json.loads(raw)
        except (OSError,ValueError):
            raise ServiceError('机票接口连接或返回异常；本次不自动重试，以免重复计费。') from None

    def _validate_airports(self, data, prepared):
        airports=dict(self.locations.tables['airport'])
        result=data.get('result')
        if not isinstance(result,dict) or not isinstance(result.get('flightInfo'),list):
            raise ServiceError('机票供应商返回格式待核对。')
        for row in result['flightInfo']:
            if not isinstance(row,dict):raise ServiceError('机票字段不完整。')
            if not isinstance(row.get('flightNo'),str) or not re.fullmatch(r'(?:[A-Z]{2}|[A-Z][0-9]|[0-9][A-Z])\d{3,4}(?:/(?:[A-Z]{2}|[A-Z][0-9]|[0-9][A-Z])\d{3,4})*',row['flightNo']):
                raise ServiceError('供应商航班号格式待核对。')
            if row.get('isCodeShare') is not None and type(row['isCodeShare']) is not bool:
                raise ServiceError('供应商代码共享标志格式待核对。')
            for side in ('departure','arrival'):
                code=row.get(side);name=row.get(side+'Name')
                if code not in airports or not isinstance(name,str) or not name.endswith(airports[code]):
                    raise ServiceError('供应商机场编码与名称不能对应，未作为推荐。')
        # Official IATA city aggregates whose airport labels omit their city.
        aggregates=CITY_AIRPORTS
        for side,point in prepared['locations'].items():
            if point['scope']=='airport':continue
            allowed=aggregates.get(point['code'],set())|{code for code,name in airports.items()
                if code==point['code'] or name.startswith(point['name'])}
            if not allowed:raise ServiceError('当前城市机场对应关系未核实，请明确机场；没有扩大付费查询。')
            for row in data.get('result',{}).get('flightInfo',[]):
                if isinstance(row,dict) and row.get(side) not in allowed:
                    raise ServiceError('供应商返回机场与请求城市不符，未作为推荐。')
        return data

    def run(self,args,deadline):
        if not self.enabled:
            raise ServiceError(self.configuration_error or '机票计费接口尚未由你启用，当前不能查；需要使用你自己的聚合818账号和Key。')
        prepared=validate_flight_args(args)
        wire={'departure':prepared['locations']['departure']['code'],'arrival':prepared['locations']['arrival']['code'],
              'departureDate':prepared['date'],'maxSegments':str(prepared['max_segments'])}
        if prepared['request']['flight_no']:wire['flightNo']=prepared['request']['flight_no']
        identity=json.dumps(wire,sort_keys=True)
        with self.lock:
            now=time.monotonic();cached=self.cache.get(identity)
            hit=bool(cached and now-cached[0]<300 and not prepared['request']['refresh'])
            if deadline-now<3:raise ServiceError('本轮机票查询等待已到时，未发起新的计费调用。')
            if hit:data=copy.deepcopy(cached[1])
            else:
                self._reserve()
                data=self._query(wire,min(20,deadline-now))
                if not isinstance(data,dict) or type(data.get('error_code')) is not int or data['error_code']!=0:
                    raise ServiceError('机票供应商没有返回成功结果；请核对自己的接口开通、额度和参数。本次不自动重试。')
                self.cache[identity]=(time.monotonic(),copy.deepcopy(data))
        self._validate_airports(data,prepared)
        result=select_rows(data,prepared)
        number=prepared['request']['flight_no']
        if number:result['candidates']=[r for r in result['candidates'] if number in r['flight'].split('/')]
        rows=result['candidates']
        for index,row in enumerate(rows):row['row']=index
        evidence={'provider':'Juhe 818','endpoint':ENDPOINT,'request':wire,'queried_at':dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
                  'response_sha256':hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                  'cache_hit':hit,'provider_request_attempts':0 if hit else 1,
                  'chargeable_request_attempts':0 if hit or self.simulation else 1,
                  'billing_notice':'是否扣费以用户自己的供应商账户记录为准，非本机推定扣费金额。','simulation':self.simulation}
        return {**result,'state':'complete' if rows else 'empty','matched':len(rows),'request':prepared['request'],
                'candidates':rows,'notice':NOTICE,'evidence':evidence}


def deliver_flights(provider,args,model,deadline,context):
    result=provider.run(args,deadline)
    if model is None:raise ServiceError('机票模型决策器尚未连接，没有以程序排序替代模型。')
    feedback=[];usage=[]
    for attempt in range(2):
        left=deadline-time.monotonic()
        if left<2:raise ServiceError('机票模型交付超时，已保留本次返回，不自动追加计费查询。')
        payload={'request':result['request'],'context':context,'candidates':result['candidates'],
            'notice':result['notice'],'evidence':result['evidence'],'feedback':feedback}
        if len(json.dumps(payload,ensure_ascii=False))>28000:
            raise ServiceError('航班候选超过本轮模型输入预算，未按固定数量裁掉候选；请缩小出发时段或机场条件。')
        decision,cost=model.ask(SELECTION,payload,max_tokens=1500,timeout=min(25,left))
        usage.append(cost)
        if not isinstance(decision,dict) or set(decision)!={'reply','selection'} or not isinstance(decision['reply'],str) or not 1<=len(decision['reply'])<=300 or not isinstance(decision['selection'],list):
            raise ServiceError('机票模型交付格式不正确，没有用程序代写结果。')
        indices=decision['selection'];rows=result['candidates']
        if any(type(i) is not int or not 0<=i<len(rows) for i in indices) or len(set(indices))!=len(indices):
            raise ServiceError('机票模型选了不存在或重复的候选，未作为推荐。')
        selected=[rows[i] for i in indices]
        left=deadline-time.monotonic()
        if left<2:raise ServiceError('机票交付复核超时，未发布未核验口播。')
        review,cost=model.ask(REVIEW,{'request':result['request'],'context':context,'selected':selected,
            'reply':decision['reply'],'notice':NOTICE,'evidence':result['evidence']},max_tokens=700,timeout=min(15,left))
        usage.append(cost)
        if isinstance(review,dict) and review.get('ok') is True and isinstance(review.get('issues'),list) and not review['issues']:
            return {'status':'complete','performed':not provider.simulation,'reply':decision['reply'],
                    'sources':[{'id':'flight-provider','title':'聚合818航班查询接口说明','url':DOC}],
                    'commercial':{'kind':'flight','request':result['request'],'flights':selected,'total_matches':result['matched'],
                        'notice':NOTICE,'evidence':result['evidence'],'simulation':provider.simulation,
                        'selection_origin':'assistant_model','delivery_review':'pass','usage':usage}}
        feedback=review.get('issues',['口播复核未通过，请根据真实候选修正。']) if isinstance(review,dict) else ['复核格式异常。']
    raise ServiceError('机票口播复核未通过，没有把未核实内容当作推荐。')
