"""Read-only shopping integration. Provider selection and all shopping decisions are DS's.

Public cards/history use a common envelope so another approved provider can map
its own factual responses without changing the voice/UI contract. JD and Taobao adapters share the same delivery contract; no shopping-account automation is imported.
"""
import copy
import datetime as dt
import json
from pathlib import Path
import time
import uuid

from core import ServiceError
from jd_curated import CuratedEvaluation, JDClient, REVIEW_RULES, pack_rows, value_at
from taobao_commerce import TaobaoAdapter
from taobao_union import TaobaoClient
from private_json import write_private
from speech_style import SPEECH_STYLE
from shared_voice.memory_usage import delivery_rules, mentions


DELIVERY_RULES = '''你是言小办。这里是你已经完成选品后的口播交付，不再选新商品或改用户条件。
request是你合并上下文后确认的需求；decision是你刚才的选择；products为你选出的真实商品与链接状态；facts为原始字段。只依据这些资料，向用户交付简短、自然、有用的结论。只返回JSON {"reply":"自然中文口播，最多100字"}。这是语音交付长度要求，详细信息由已有卡片承载，连接程序不会替你裁剪文本。
有满足项时先交付你选出的重点和关键价格，理由只用有证据的属性；详细商品、价格口径和链接在卡片里。无需重复标题全文、排除名单、页码、条数、内部复核或多次范围提醒，不为证明你查过而报未选商品。没找到或需要澄清就直说关键原因/问题，不凑数，不把未知说成不支持。
真无线不证明入耳式；功能、佩戴、降噪深度和实测表现不凭常识补写。通勤等用途可以依据无线/主动降噪作建议，不当成效果保证，也不因没有个人实测而否决所有合理推荐。
普通页面价与条件优惠价不能混淆，不反推未知优惠组合；链接失败就如实说明该商品直达链接暂未取得，不能说已打开、已加车、已下单。仅查已提供的平台候选，不称全平台覆盖、全网比价或最低价。
最终这段reply就是用户听到的内容，请真正做好信息取舍和语义断句，避免长串细节和重复提醒。
''' + SPEECH_STYLE

VOICE_FEEDBACK = '''真实联调反馈：上一版单件结果播了36.5秒，空结果播了38.1秒，过长；反复念优惠、未选商品、未知佩戴材质和范围声明，使重点被淹没。请本轮实际改正：以约15秒内能听清的简短交付为目标，由你决定必要信息和断句，不套句数模板。单件优先给是否符合需求、关键价格和理由；空结果说明本次未匹配及关键缺口即可，不重播未选名单。细节已在卡片中，无需反复口头免责。
后续两次仍播了31.4秒和28.2秒，未达到目标；“卡片里的优惠价不是现在能直接拿到的”还把条件未知说成当前不能用，“具体候选都放在卡片里”在空结果时是虚假交付，必须纠正。现在请在100字以内真正取舍重点，不再延伸到耳塞尺寸、到手试用、反复优惠声明或未选商品介绍；products为空就不能说卡片有商品。普通页面价已满足预算时，可以不口头提未采用的优惠。
当前中文音色实测会吞掉Lolli、Pro等拉丁词。口播请你自行用资料中准确的中文品牌或商品类别来指代，完整英文型号留在卡片；不发明中文译名或音译。价格、日期等在reply保持原始阿拉伯数字（如371.37元），由已存在的数字读法层处理，不自行改写为中文数词。此反馈只约束交付表达，不改用户条件或商品选择。'''

DELIVERY_REVIEW_RULES = '''你是购物交付复核者，只核对draft.reply这段最终口播，不复核旁边的旧笔记或替用户重选。
只返回JSON {"verdict":"pass或revise","issues":["需修改的具体问题"]}，通过时issues为空。每个问题必须引用draft.reply中确实出现的短句，指出它与哪一真实字段或明确表达要求冲突；不要把products标题、decision笔记里的英文或旧回复误认成口播。不要将已经满足的要求写成问题。
检查价格、功能和链接状态是否准确，不能捏造优惠、加车、下单、全平台最低价；空结果可以依据decision.notes说明本次未匹配，不需要虚构选中商品。products为空不得声称卡片有候选；优惠条件未知不能断言优惠当前不可用。voice_feedback的改进须落实，避免不必要的长篇优惠/排除名单/未知属性说明。口播必须在100字内。
允许用真实标题中的中文品牌和品类简短指代，完整型号已在卡片，不要求口播逐字复读英文标题。明确标注无线/主动降噪可以支持通勤用途建议，不等于保证实测效果；不能因无实测或未知的非硬条件而要求否决合理推荐。不要把推荐建议误报为性能保证。价格写阿拉伯数字加空格也正确。
若某硬条件确实未证实，可要求准确限定结论；未知不能被当作符合，也不能被当作明确不支持。只纠正实际问题，不加资料没有的证明要求。'''

DELIVERY_REVIEW_RULES += '''
最新手机失败必须纠正：你把“入耳形态资料没标明”判成无依据肯定属性，又因未复读“蓝牙”而拒绝正确商品，最终用户拿不到已找到的结果。这是复核误报。明确说某属性未知，不等于断言它存在或不存在；口播不需逐字复读全部筛选条件。请只把事实矛盾、捏造承诺或违反明确硬条件作为revise，不能仅因表达偏好或一句非必要但真实的未知说明而阻断交付。简洁自然的目标仍须监督，但不得把风格建议伪装成事实错误。'''


def validate_commerce_args(args):
    if (not isinstance(args, dict) or set(args) != {'platform', 'request'}
            or not isinstance(args['platform'], str) or not 1 <= len(args['platform']) <= 32
            or not isinstance(args['request'], str) or not 1 <= len(args['request'].strip()) <= 1600
            or any(ord(c) < 32 for c in args['request'])):
        raise ServiceError('购物需求格式不正确。')
    return {'mode': 'shopping', 'platform': args['platform'], 'request': args['request'].strip()}


class ModelBudget:
    def __init__(self, model, deadline, *, max_calls=24, max_chars=350000, reasoning_effort=None):
        self.model, self.deadline = model, deadline
        self.reasoning_effort = reasoning_effort
        self.max_calls, self.max_chars = max_calls, max_chars
        self.calls, self.input_chars = 0, 0

    def ask(self, system, payload, **kwargs):
        size = len(system) + len(json.dumps(payload, ensure_ascii=False))
        effort = self.reasoning_effort(payload) if callable(self.reasoning_effort) else self.reasoning_effort
        requested_timeout = kwargs.get('timeout', 45)
        if effort is not None:
            kwargs['reasoning_effort'] = effort
            requested_timeout = max(30, requested_timeout)
        for attempt in range(2):
            remaining = self.deadline-time.monotonic()
            if remaining < 1 or self.calls >= self.max_calls or self.input_chars + size > self.max_chars:
                raise ServiceError('本轮购物查询已达到处理预算，未再追加调用。')
            self.calls += 1
            self.input_chars += size
            kwargs['timeout'] = min(requested_timeout, remaining)
            try:
                return self.model.ask(system, payload, **kwargs)
            except Exception as error:
                code = getattr(error, 'code', None)
                if (attempt or effort is None or code not in
                        ('model_timeout', 'model_busy', 'model_connection_failed', 'model_invalid_response')):
                    raise
                # One bounded protocol/transport retry of the identical DS request.
                # No supplier call, selection rewrite or global voice-mode change.
                if hasattr(self, 'trace'):
                    self.trace({'event':'model_transport_retry','error_code':code,'retry':1})


class JDAdapter:
    platform, label = 'jd', '京东'
    description = '京东联盟精选商品池及直达链接；无关键词全站搜索，不保证任意型号可找到或最低价。'
    scope = '仅基于本次查看的精选候选，价格与优惠以商品页及结算为准。'
    source_label = '京东联盟精选'

    def __init__(self, client, *, max_pages=6):
        self.client, self.max_pages = client, max_pages

    def select(self, request, model, trace, deadline):
        # Full fetched data, DS-controlled channels/pages/retention/order/choices.
        client = self.client
        class DeadlineClient:
            @property
            def api_calls(self):
                return client.api_calls
            def observed_channels(self):
                return client.observed_channels()
            def observed_queries(self):
                return client.observed_queries()
            def query(self, args):
                remaining = deadline-time.monotonic()
                if remaining < 1:
                    raise ServiceError('本轮购物查询已到处理时限，未追加查询。')
                return client.query(args, timeout=min(25, remaining))
        result = CuratedEvaluation(DeadlineClient(), model, max_pages=self.max_pages, trace=trace).run(request)
        for selected in result['selected']:
            page = int(selected['facts']['id'].split('r', 1)[0][1:])-1
            selected['queried_at'] = result['queries'][page]['queried_at']
        return result

    def product(self, selected, deadline):
        facts, original = selected['facts'], selected['original']
        url, link_status = None, 'unavailable'
        if deadline-time.monotonic() >= 1:
            try:
                url = self.client.promotion(original, timeout=min(25, deadline-time.monotonic()))
                link_status = 'ready'
            except Exception:
                pass
        price = facts.get('priceInfo', {})
        return {'id': str(original.get('skuId', facts['id'])), 'source_ref': facts['id'],
                'platform': self.platform, 'platform_label': self.label,
                'title': facts.get('skuName', ''), 'shop': facts.get('shopName'),
                'price': price.get('price'), 'coupon_price': price.get('lowestCouponPrice'),
                'price_info': price, 'coupon_terms': facts.get('coupons', []),
                'purchase_price_info': facts.get('purchasePriceInfo'),
                'rating_percent': facts.get('goodCommentsShare'), 'review_count': facts.get('comments'),
                'reason': selected['decision']['reason'], 'url': url, 'link_status': link_status,
                'queried_at': selected.get('queried_at')}


class Commerce:
    def __init__(self, adapters, model, trace_dir):
        self.adapters = {adapter.platform: adapter for adapter in adapters}
        self.model, self.trace_dir = model, Path(trace_dir)

    @classmethod
    def from_private(cls, root, model):
        root = Path(root)
        profile = root / '.private/commerce.json'
        if not profile.is_file():
            return None
        config = json.loads(profile.read_text())
        if not isinstance(config, dict) or set(config) != {'enabled', 'platforms'} or type(config['enabled']) is not bool:
            raise ServiceError('购物配置格式不正确。')
        if not config['enabled']:
            return None
        platforms = config['platforms']
        if (not isinstance(platforms, list) or not platforms
                or any(not isinstance(p, str) or p not in ('jd', 'taobao') for p in platforms)
                or len(set(platforms)) != len(platforms)):
            raise ServiceError('购物平台目录格式不正确或尚未实现。')
        adapters = []
        try:
            for platform in platforms:
                if platform == 'jd':
                    adapters.append(JDAdapter(JDClient.from_private(root / '.private/jd-union.json')))
                else:
                    adapters.append(TaobaoAdapter(TaobaoClient.from_private(root / '.private/taobao-union.json')))
        except (OSError, ValueError, TypeError):
            raise ServiceError('已启用的购物平台私有配置不完整，未替换为其它平台。') from None
        return cls(adapters, model, root / '.private/commerce-traces')

    def execute(self, args, deadline, context=None):
        request = validate_commerce_args(args)
        adapter = self.adapters.get(request['platform'])
        if adapter is None:
            raise ServiceError('该购物平台尚未连接，当前未查询。')
        trace_path = self.trace_dir / (uuid.uuid4().hex + '.json')
        record = {'platform': request['platform'], 'request': request['request'],
                  'started_at': dt.datetime.now().astimezone().isoformat(), 'trace': []}
        started = time.monotonic()
        budget = ModelBudget(self.model, deadline, reasoning_effort=getattr(adapter, 'reasoning_effort', None))
        def trace(event):
            record['trace'].append(copy.deepcopy(event))
            write_private(trace_path, record)
        budget.trace = trace
        try:
            if context and hasattr(adapter, 'prepare_request'):
                request['request'] = adapter.prepare_request(request['request'], context, budget, trace)
                record['confirmed_request'] = request['request']
            selection = adapter.select(request['request'], budget, trace, deadline)
            record['selection_result'] = selection
            products = [adapter.product(item, deadline) for item in selection['selected']]
            # Record true lookup times; cached data retain their original times.
            times = [q['queried_at'] for q in selection['queries'] if q['returned']]
            queried_at = max(times) if times else None
            decision = selection['decision']
            # Keep the original selection trace; the oral pass needs the decision,
            # not a second copy of the superseded verbose draft.
            delivery_decision = {key: decision[key] for key in ('action', 'selection', 'notes')}
            scope = getattr(adapter, 'scope', '仅基于本次实际查询的候选，价格与优惠以商品页及结算为准。')
            source_label = getattr(adapter, 'source_label', adapter.label+'联盟商品')
            voice_feedback = VOICE_FEEDBACK + getattr(adapter, 'delivery_feedback', '')
            review_rules = DELIVERY_REVIEW_RULES + getattr(adapter, 'delivery_review_extra', '')
            delivery = {'request': request['request'], 'decision': delivery_decision,
                        'products': [{k: v for k, v in p.items() if k not in
                                      ('price_info', 'coupon_terms', 'purchase_price_info', 'offer_terms')} for p in products],
                        'facts': getattr(adapter, 'pack_rows', pack_rows)([item['facts'] for item in selection['selected']]),
                        'scope': scope,
                        'source_evidence': [{'id': x['facts']['id'], 'evidence': [
                            {'field': e['field'], 'value': value_at(x['facts'], e['field'])}
                            for e in x['decision']['evidence']]} for x in selection['selected']],
                        'coverage': selection.get('coverage', {'retrieved': selection['retrieved'], 'seen': selection['seen']}),
                        'voice_feedback': voice_feedback}
            if delivery_rules(context):
                delivery['memoryContext'] = context['memoryContext']
                delivery['utterance'] = context.get('utterance', '')
            reply = None
            spoken_mentions = []
            try:
                for attempt in range(2):
                    output, usage = budget.ask(DELIVERY_RULES + delivery_rules(context), delivery, max_tokens=900, timeout=20)
                    trace({'event': 'delivery', 'input': delivery, 'raw_output': output, 'usage': usage})
                    if isinstance(output, dict) and output.get('type') == 'json_object':
                        output = {k: v for k, v in output.items() if k != 'type'}
                    if (not isinstance(output, dict) or not {'reply'} <= set(output)
                            or not set(output) <= {'reply', 'memory_mentioned'}
                            or not isinstance(output['reply'], str) or not 1 <= len(output['reply'].strip()) <= 100):
                        delivery['feedback'] = '口播必须是仅含reply的JSON，reply在100字以内；请你自行取舍重新交付，程序不会裁剪。'
                        continue
                    try:
                        output_mentions = mentions(output, context)
                    except ValueError:
                        delivery['feedback'] = 'memory_mentioned必须只含本轮memoryContext中实际提起的id；不提起就[]。请重新交付。'
                        continue
                    review_input = {'request': request['request'],
                        'draft': {'reply': output['reply'], 'notes': '', 'selection': decision['selection']},
                        'facts': delivery['facts'], 'source_evidence': delivery['source_evidence'], 'facts_scope': '本轮DS选中的商品，不是全部历史候选。',
                        'queries': selection['queries'], 'coverage': delivery['coverage'],
                        'products': delivery['products'], 'voice_feedback': voice_feedback,
                        'decision': delivery_decision}
                    if delivery_rules(context):
                        review_input['memoryContext'] = context['memoryContext']
                        review_input['utterance'] = context.get('utterance', '')
                    memory_review = ('''\n本轮memoryContext是主会话已采用的用户记忆，可支持关于用户偏好或称呼的自然表达，不必在商品字段里重复证明这类用户信息。它不能证明商品属性、价格或性能。仍核对人物、当次要求及不主动提及设置，不要求为了个性化而必须提起记忆。'''
                                     if delivery_rules(context) else '')
                    verdict, review_usage = budget.ask(review_rules + memory_review,
                                                      review_input, max_tokens=900, timeout=20)
                    trace({'event': 'delivery_review', 'input': review_input, 'raw_output': verdict, 'usage': review_usage})
                    if (isinstance(verdict, dict) and set(verdict) == {'verdict', 'issues'}
                            and verdict['verdict'] == 'pass' and verdict['issues'] == []):
                        reply = output['reply'].strip()
                        spoken_mentions = output_mentions
                        break
                    delivery['feedback'] = {'rejected_output': output, 'review': verdict,
                                             'instruction': '请你依据原资料自行纠正；连接程序不改写你的答案。'}
            except Exception as error:
                trace({'event': 'delivery_failure', 'error_type': type(error).__name__})
            if reply is None:
                # Selection/card facts have already passed independently. A failed
                # oral draft must not erase them or masquerade as no matches.
                reply = '商品查询已返回，语音交付尚未通过核对，请查看页面结果。'
                record['delivery_incomplete'] = True
            queries_failed = bool(selection['queries']) and all('error_code' in q for q in selection['queries'])
            state = ('query_failed' if queries_failed else 'delivery_pending' if record.get('delivery_incomplete') else
                     'clarify' if decision['action'] == 'clarify' else 'complete')
            result = {'status': 'error' if state == 'query_failed' else 'partial' if state != 'complete' else 'complete', 'reply': reply,
                      'memory_mentioned': spoken_mentions,
                      'sources': [{'provider': source_label, 'title': source_label+'商品资料',
                                   'queriedAt': queried_at}] if queried_at else [],
                      'commercial': {'kind': 'shopping', 'platform': adapter.platform,
                          'platform_label': adapter.label, 'title': adapter.label+'商品挑选', 'state': state,
                          'request': request, 'products': products, 'queried_at': queried_at,
                          'scope': scope}}
            record['result'] = result
            return result
        except Exception as error:
            record['error_type'] = type(error).__name__
            if isinstance(error, ServiceError):
                record['validation_error'] = str(error)
            raise
        finally:
            record.update(model_calls=budget.calls, input_chars=budget.input_chars,
                          elapsed_seconds=round(time.monotonic()-started, 3))
            write_private(trace_path, record)
