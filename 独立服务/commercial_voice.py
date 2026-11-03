"""Commercial API adapters around the existing Pi voice/conversation components."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import threading

from core import DeepSeek, ServiceError, validate_request
from trains import TrainClient
from commercial_flights import FlightProvider, validate_flight_args, deliver_flights, DIRECTORY as FLIGHT_DIRECTORY
from speech_style import SPEECH_STYLE
from commerce import Commerce, validate_commerce_args

SHARED = Path(__file__).resolve().parent / 'shared_voice'
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))
from model_client import ModelClient, ModelFailure
from general_dialogue import Conversation, Conflict
from conversation_bridge import Bridge, ConversationFailure, air_call
from home_api import handle_home, owner_from_cookie
from memory_usage import delivery_rules, mentions

COMMERCIAL_RULES = '''
以下商用目录覆盖此前所有旧工具说明。Siri仅打开网页，用户接下来由言小办听取并回复。
基础工具为web_search网页搜索、travel旅行攻略、rail高铁动车查询；其它可用工具以payload.enabledExecutors和本轮补充目录为准。没有微信、外卖、手机模拟、加购、下单功能。购物接口由用户自行取得相应权限，购物连接是否可执行以本轮目录为准；机票是否启用及收费边界以本轮机票目录为准，不假称已查询。闲聊和信息不足时正常chat/clarify，不机械凑工具。
返回且仅返回reply、capability、action、query。capability只能none/web_search/travel/rail以及本轮补充目录明确开放的工具；action为chat/clarify/prepare/pause/resume/cancel；query无执行时null。
web_search args只能query，2至75字；只含用户确实询问的内容。搜索返回来源摘要，不读网页全文。
travel args为destination（明确城市，1至24字）、days（1至7整数）、date（YYYY-MM-DD或空字符串），可加preferences（180字以内的用户偏好）。缺目的地才追问；未给天数由你根据需求决定澄清或建议，并说明假设，不强迫用户填写表格。根据本轮和上下文合并同行人、松紧节奏、兴趣及避开事项到preferences，不能每次追问都重置。独立攻略查询用travel，不调用小红书。已有路线只需局部修改时可依据history中的已保存行程直接简短回答；涉及最新票价、开放规则等才重新查询。
rail args必有departure_station、arrival_station、date、filter。出发到达接受城市或车站，保留用户原话，例如北京、上海、北京南、上海虹桥；直接按城市名查询，不擅自替换成某个主站，也不再要求城市用户报具体站名。可加departure_scope/arrival_scope（city或station）：一般“北京到上海”是city，明确“北京站”“只到虹桥站”才station。日期缺失时才clarify；相对日期按payload.today换算，支持15天内。filter为G/D/GD，由你按用户需求决定。
rail可加departure_time_range（凌晨/上午/下午/晚上或空字符串）、departure_start、departure_end（24小时HH:MM；截止可24:00），均按出发时间。上午06:00–12:00，下午12:00–18:00，晚上18:00–24:00，凌晨00:00–06:00；“下午两点到五点”给14:00、17:00；“三点以后”有上午/下午上下文就换算，没有则澄清；“晚上七点后”给19:00、24:00。跨日或含糊时间需确认，不把冲突条件丢弃。“换下午”“三点以后呢”等追问保留原城市/车站、日期和其他未改变条件，同时替换旧时间范围；修改范围时不要同时携带已不兼容的旧departure_time_range。席别、票价、余票和候选排序等由你结合本轮需求及真实返回数据决定；数据未提供的条件才需要说明或澄清，不得擅自丢掉用户限制。票价余票只来自聚合817，不称实时、不提供预订。
执行前reply只作简短说明，不编造结果。用户表示先别查、只记需求，不执行。没有供应商授权的功能只说明当前状态。
'''


MODEL_SELECTION = '''你是言小办，负责执行层的候选选择、排序、席别选择和语音交付。根据用户本轮需求及上下文、完整查询条件与候选数据，自主决定展示哪些班次、多少班、按什么顺序；不要机械取前三班或永远最早一班。
用户反馈：全天结果太多、还得自己找，不合适。你必须把筛选取舍做好，只交付与其目的有关、足以决策的候选；需要更多条件时自然澄清。
监督复查反馈：上一版口播把已排除、不推荐的车次也逐个讲了一遍，太长且分散；不要再让用户听一长串再自己挑。请真正做取舍，口播先给最有帮助的结论与必要替代方案，具体班次细节留在页面；只围绕你最终selection中的推荐交付，不复述未选班次，不重复泛泛提醒。你自行决定数量与顺序，不机械凑前三班。
只返回JSON {"reply":"向用户交付结果的自然口语，最多300字","selection":[{"row":候选row编号,"seat":候选seats中的0起始席别索引}]}。
只可从本次候选中选择，不能捏造班次、票价、余票或更换车站；没有合适候选就selection为空并说明原因或澄清，不扩大到用户未要的时段/车站。
reply由你依据实际数据交付，不要仅说“已整理”。票价余票有缓存延迟，不称实时，不下单。
'''
MODEL_DELIVERY = '''你是言小办。根据本轮请求、资料结果和上下文，决定如何向用户交付；数据不足或失败就如实说明，不编造已完成。只返回JSON {"reply":"简短自然口语，最多300字"}。'''


COMMERCIAL_RULES += SPEECH_STYLE
MODEL_SELECTION += SPEECH_STYLE
MODEL_DELIVERY += SPEECH_STYLE

COMMERCE_DIRECTORY = '''
本轮补充开放只读shopping商品挑选工具，优先于旧jd加购物车等说明。capability=shopping、action=prepare、query={"tool":"shopping","args":{"platform":"已连接平台代号","request":"你合并本轮与历史后确认的完整选购需求"}}。
平台目录及各自实际能力：{platforms}。只有目录中平台可调用；按对应description执行，不声称全网比价、最低价或全站覆盖。不用网页搜索、其它平台或手机操作冒充指定平台API。
需求理解、条件合并、平台选择、是否需要澄清由你决定。真实联调反馈：用户在选品后仅说“预算改成300元以内，不用凑单和会员优惠”，你只回复记住了，未继续查询，这是漏执行。用户修改既有选购条件通常是在继续这项选品任务，应由你合并后继续prepare；只有明确先记住、暂不查询或纯闲聊才不执行。用户改预算、颜色、功能或用途时，只替换修改的条件，其余保留，不能丢掉之前的条件。不要把你推测的偏好添加成用户硬条件；request保留真实数量和否定要求，不在主对话层填频道、页码或商品；后续选品模型会自主决定查询与候选。缺关键信息时自然澄清，不强制填表。
用户明确让找商品、挑选、比较或按新条件重新查时才执行；只是谢谢、好的、先记住或询问已有结果时不能重复查。已有选购结果在history保存，用户问已有款的价格/颜色/链接时可根据保存事实回答，不能捏造新库存、新优惠或已下单。用户明确指定其它未连接平台时不能悄悄换京东。
只返回商品资料、选择理由与用户手点的直达链接，不加购、不自动打开平台、不下单。执行前reply只说正在查询，不说已找到或链接已打开。
'''


def validate_plan(plan, query_mode=True, enabled_tools=()):
    try:
        if not isinstance(plan, dict) or set(plan) != {'reply', 'capability', 'action', 'query'}:
            raise ValueError
        if plan['capability'] not in {'none', 'web_search', 'travel', 'rail', 'flight', 'shopping', 'bookkeeping', 'secretary'}:
            raise ValueError
        if plan['action'] not in {'chat', 'clarify', 'prepare', 'pause', 'resume', 'cancel'}:
            raise ValueError
        reply = plan['reply']
        if not isinstance(reply, str) or not 1 <= len(reply.strip()) <= 300 or any(ord(c) < 32 for c in reply):
            raise ValueError
        query = plan['query']
        if plan['action'] != 'prepare':
            if query is not None:
                raise ValueError
        else:
            if not isinstance(query, dict) or set(query) != {'tool', 'args'}:
                raise ValueError
            if query['tool'] != plan['capability'] or query['tool'] not in enabled_tools:
                raise ValueError
            commercial_request(query)
        return {**plan, 'reply': reply.strip()}
    except (ValueError, TypeError, KeyError, ServiceError):
        # Original conversation supports a schema-repair retry; this commercial
        # failure code deliberately avoids additional paid calls on bad plans.
        raise ModelFailure('commercial_plan_invalid') from None


def commercial_request(query):
    if query['tool'] in ('bookkeeping','secretary'):
        if query['args'] != {}:raise ValueError('Feature tools receive the original conversation from the trusted host')
        return {'mode':query['tool']}
    tool, args = query['tool'], query['args']
    if tool == 'shopping':
        return validate_commerce_args(args)
    if tool == 'flight':
        return {'mode':'flight', **validate_flight_args(args)['request']}
    fields = {'web_search': {'query'}, 'travel': {'destination', 'days', 'date'},
              'rail': {'departure_station', 'arrival_station', 'date', 'filter'}}
    optional = {'web_search': set(), 'travel': {'preferences'},
                'rail': {'departure_scope', 'arrival_scope', 'departure_time_range', 'departure_start', 'departure_end'}}
    if (not isinstance(args, dict) or tool not in fields or not fields[tool] <= set(args)
            or not set(args) <= fields[tool] | optional[tool]):
        raise ValueError('unsupported_query_fields')
    mode = {'web_search': 'search', 'travel': 'travel', 'rail': 'train'}[tool]
    return validate_request({'mode': mode, **args})


class CommercialQueries:
    tools = ['web_search', 'travel', 'rail']

    def __init__(self, search=None, train=None, decider=None, commerce=None, flight_provider=None, root=None):
        self.search = search or DeepSeek(os.environ.get('DEEPSEEK_API_KEY', ''))
        self.train = train or TrainClient(os.environ.get('JUHE_TRAIN_KEY', ''))
        self.decider = decider
        self.commerce = commerce
        self.flights = flight_provider if flight_provider is not None else FlightProvider.from_private(root) if root is not None else FlightProvider()
        self.tools = ['web_search', 'travel', 'rail'] + (['shopping'] if commerce else []) + (['flight'] if self.flights.enabled else [])

    @property
    def rules(self):
        return COMMERCIAL_RULES + FLIGHT_DIRECTORY.replace('{status}',json.dumps(self.flights.status,ensure_ascii=False)) + (COMMERCE_DIRECTORY.replace('{platforms}', json.dumps(
            {k: {'label': v.label, 'description': getattr(v, 'description', '本轮已连接的只读商品资料接口')}
             for k, v in self.commerce.adapters.items()}, ensure_ascii=False)) if self.commerce else '')

    def timeout_for(self, tool):
        return 90 if tool == 'shopping' else 80 if tool == 'flight' else 40

    def execute(self, query, deadline, context):
        request = commercial_request(query)
        if request['mode'] == 'flight':
            try:
                return deliver_flights(self.flights,query['args'],self.decider,deadline,context)
            except (ServiceError,ModelFailure) as error:
                reply=str(error) if isinstance(error,ServiceError) else '机票数据交付模型暂未完成，没有发布未核实推荐；不会自动增加收费查询。'
                return {'status':'error','performed':False,'sources':[],'reply':reply,'error':'flight_query_failed'}
        if request['mode'] == 'shopping':
            if not self.commerce:
                return {'status': 'error', 'sources': [], 'reply': '购物查询当前尚未连接，没有执行。'}
            try:
                return self.commerce.execute(query['args'], deadline, context=context)
            except Exception:
                return {'status': 'error', 'sources': [], 'reply': '这次商品查询暂未完成。没有把未核实的结果当作推荐，你可以稍后重试。'}
        provider = self.train if request['mode'] == 'train' else self.search
        if not provider.key:
            return {'status': 'error', 'sources': [], 'reply': '这项商用接口尚未配置，当前没有执行查询。'}
        try:
            result = (provider.run(request, memory_context=context) if getattr(provider, 'supports_memory_context', False)
                      else provider.run(request))
        except ServiceError as error:
            return {'status': 'error', 'sources': [], 'reply': str(error)[:250]}
        if request['mode'] == 'train':
            return self.rail(result, context)
        summary = result.get('summary', {})
        reply = summary.get('spoken')
        spoken_mentions = result.get('memory_mentioned', [])
        if not reply:
            delivery = self.decide({'request': request, 'result': result,
                                   'context': context}, selection=False)
            reply = delivery['reply']
            spoken_mentions = delivery.get('memory_mentioned', [])
        return {'status': 'complete' if result['state'] == 'complete' else 'partial',
                'reply': reply, 'sources': result.get('sources', []),
                'memory_mentioned': spoken_mentions,
                'commercial': {'kind': request['mode'], 'summary': summary,
                               'request': request,
                               'title': f"{request['destination']} · {request['days']}天行程" if request['mode'] == 'travel' else '',
                               'notice': result.get('notice', ''), 'message': result.get('message', '')}}

    def decide(self, payload, selection=True):
        if self.decider is None:
            raise ServiceError('模型决策器尚未连接。')
        instruction = (MODEL_SELECTION if selection else MODEL_DELIVERY) + delivery_rules(payload.get('context'))
        value, _ = self.decider.ask(instruction, payload, max_tokens=1600, timeout=25)
        if isinstance(value, dict) and value.get('type') == 'json_object':
            value = {k: v for k, v in value.items() if k != 'type'}
        keys = {'reply', 'selection'} if selection else {'reply'}
        if (not isinstance(value, dict) or not keys <= set(value) or not set(value) <= keys | {'memory_mentioned'}
                or not isinstance(value.get('reply'), str) or not 1 <= len(value['reply']) <= 300):
            raise ServiceError('模型交付结果未通过接口格式校验。')
        try:
            mentions(value, payload.get('context'))
        except ValueError:
            raise ServiceError('模型记忆交付回执无效。') from None
        return value

    def rail(self, result, context):
        rows = result.get('trains', [])
        candidates = [[i, r['train_no'], r['departure_station'], r['arrival_station'],
                       r['departure_time'], r['arrival_time'], r['duration'],
                       [[s['seat_name'], s['price'], s['availability']] for s in r.get('seats', [])]]
                      for i, r in enumerate(rows)]
        payload = {'request': result.get('request', {}), 'context': context,
                   'columns': ['row', 'train', 'origin', 'destination', 'departure', 'arrival', 'duration',
                               'seats[name,price,availability]'], 'candidates': candidates,
                   'queried_at': result.get('queried_at'), 'data_notice': result.get('notice')}
        # Resource overflow is reported to DS; no hand-picked subset is sent.
        if len(json.dumps(payload, ensure_ascii=False)) > 25000:
            payload['candidates'] = []
            payload['candidate_count'] = len(rows)
            payload['data_error'] = '数据超出单次模型上下文预算，请向用户澄清更聚焦的条件，本次不要选择班次。'
        decision = self.decide(payload)
        selected = decision['selection']
        if not isinstance(selected, list) or (payload.get('data_error') and selected):
            raise ServiceError('模型班次选择格式不正确。')
        trains, seen = [], set()
        for choice in selected:
            if not isinstance(choice, dict) or set(choice) != {'row', 'seat'}:
                raise ServiceError('模型班次选择格式不正确。')
            index, seat_index = choice['row'], choice['seat']
            if (type(index) is not int or not 0 <= index < len(candidates) or index in seen
                    or type(seat_index) is not int or not 0 <= seat_index < len(rows[index]['seats'])):
                raise ServiceError('模型选择了返回数据中不存在或重复的班次/席别。')
            seen.add(index)
            row = rows[index]; seat = row['seats'][seat_index]
            hours, minutes = map(int, row['duration'].split(':'))
            trains.append({'train': row['train_no'], 'origin': row['departure_station'],
                           'destination': row['arrival_station'], 'depart': row['departure_time'],
                           'arrive': row['arrival_time'], 'durationMinutes': hours * 60 + minutes,
                           'date': result.get('request', {}).get('date', ''), 'seat': seat['seat_name'],
                           'price': seat['price'], 'availability': seat['availability']})
        return {'status': 'complete' if trains or not rows else 'partial', 'reply': decision['reply'],
                'memory_mentioned': decision.get('memory_mentioned', []),
                'sources': result.get('sources', []),
                'rail': {'date': result.get('request', {}).get('date', ''), 'trains': trains},
                'commercial': {'kind': 'train', 'purchase_url': result.get('purchase_url', 'https://www.12306.cn/'),
                               'request': result.get('request', {}), 'total_matches': len(rows),
                               'selection_origin': 'deepseek', 'time_label': result.get('time_label', '全天')}}


def remember_result(result):
    data = result.get('commercial', {})
    context = {'request': data.get('request', {})}
    if data.get('kind') == 'travel':
        context['itinerary'] = [{'day': s['heading'], 'items': [i['text'] for i in s['items']]}
                                for s in data.get('summary', {}).get('sections', [])]
    elif data.get('kind') == 'train':
        context['trains'] = result.get('rail', {}).get('trains', [])
    elif data.get('kind') == 'flight':
        context['flights']=data.get('flights',[])
        context['notice']=data.get('notice','')
        context['evidence']=data.get('evidence',{})
    elif data.get('kind') == 'shopping':
        context['platform'] = data.get('platform')
        context['products'] = [{k: row.get(k) for k in ('id', 'title', 'shop', 'price', 'coupon_price',
            'offer_label', 'offer_notice', 'reason', 'rating_percent', 'review_count', 'link_status', 'queried_at')}
            for row in data.get('products', [])]
    elif data.get('kind') in ('bookkeeping','secretary'):
        context={'feature':data}
    else:
        return result['reply']
    return result['reply'] + '\n已保存的本轮查询条件与结果：' + json.dumps(context, ensure_ascii=False)


class PresentingBridge(Bridge):
    """An observer after the business commit; it cannot block speech on failure."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observer = threading.local()

    def render(self, answer, token, request_id):
        presenter = getattr(self, 'presenter', None)
        owner = getattr(self.observer, 'owner', None)
        if presenter and owner and getattr(self.observer, 'new_reply', False):
            try:presenter.ensure(owner, answer['id'], request_id)
            except Exception:print(json.dumps({'event':'presentation_check','status':'failed','error':'presentation_schedule_failed'}), flush=True)
        callback = getattr(self.observer, 'callback', None)
        if callback:
            try:
                callback(answer)
            except Exception:
                pass
        result = super().render(answer, token, request_id)
        if presenter and owner:
            try:
                layout = presenter.snapshot(owner, answer['id'], request_id)
                if layout:result['presentation'] = layout
            except Exception:pass
        return result


class CommercialVoice:
    def __init__(self, db, token_file, *, model=None, queries=None, speech_call=None, memory_enabled=True):
        self.token = Path(token_file).read_text().strip()
        if not self.token:
            raise ValueError('Missing local speech credential')
        model = model or ModelClient({'base_url': 'https://api.deepseek.com',
                                      'api_key': os.environ.get('DEEPSEEK_API_KEY', ''),
                                      'model': 'deepseek-flash'})
        private = Path(db).parent
        root = private.parent if private.name == '.private' else private
        runtime = queries or CommercialQueries(decider=model, commerce=Commerce.from_private(root, model), root=root)
        if runtime.decider is None:
            runtime.decider = model
        self.runtime = runtime
        conversation = Conversation(db, model, queries=runtime,
                                    plan_validator=validate_plan, system_suffix=runtime.rules,
                                    history_formatter=remember_result, memory_enabled=memory_enabled)
        if speech_call is None:
            # The preview must never fall back to a developer's personal service.
            def speech_call(*args, **kwargs):
                raise RuntimeError('local_speech_provider_required')
        self.bridge = PresentingBridge(conversation, call=speech_call)
        from presentation import Presenter
        self.presenter = Presenter(conversation)
        self.bridge.presenter = self.presenter

    def handle_progress(self, cookie, raw, headers, audio, emit):
        self.bridge.observer.callback = lambda answer: emit({'event': 'result', 'result': answer})
        try:
            emit({'event': 'complete', 'result': self.handle(cookie, raw, headers, audio=audio)})
        finally:
            self.bridge.observer.callback = None

    def handle(self, cookie, raw, headers, audio=False):
        self.bridge.observer.owner = owner_from_cookie(cookie)
        try:
            body = None if audio else json.loads(raw)
            self.bridge.observer.new_reply = audio or (isinstance(body,dict) and body.get('action')=='text')
            if audio:
                return self.bridge.reply(cookie, raw, self.token,
                    headers.get('X-Pi-Conversation', ''), headers.get('X-Pi-Revision', ''),
                    headers.get('X-Pi-Turn', ''), capture_limited=headers.get('X-Pi-Capture-End') == 'limit',
                    segment_index=int(headers.get('X-Pi-Segment', '0')),
                    segment_count=int(headers.get('X-Pi-Segments', '1')))
            return handle_home(self.bridge,cookie,body,self.token)
        finally:
            self.bridge.observer.owner=None;self.bridge.observer.new_reply=False
