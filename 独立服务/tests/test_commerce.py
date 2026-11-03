import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from commerce import Commerce, JDAdapter, ModelBudget, validate_commerce_args
from commercial_voice import CommercialQueries, ModelFailure, remember_result, validate_plan
from core import ServiceError
from jd_curated import model_row


ROW = {'skuId': 123, 'skuName': '真无线主动降噪蓝牙耳机 奶釉白',
       'priceInfo': {'price': 371.37, 'lowestCouponPrice': 323.09},
       'shopInfo': {'shopName': '测试店'}, 'comments': 100, 'goodCommentsShare': 99,
       'materialUrl': 'https://jingfen.jd.com/fixture'}


def selected(row=ROW, identity='p1r0'):
    return {'original': copy.deepcopy(row), 'facts': model_row(row, identity), 'queried_at': '2026-10-21T10:00:00+08:00',
            'decision': {'id': identity, 'reason': '主动降噪，页面价在预算内。',
                         'evidence': [{'field': 'priceInfo.price', 'value': 371.37}]}}


class Adapter:
    platform, label = 'jd', '京东'
    def __init__(self, empty=False, clarify=False, failed_links=False):
        self.calls = 0; self.empty = empty; self.clarify = clarify
        self.failed_links = failed_links
    def select(self, request, model, trace, deadline):
        self.calls += 1
        choices = [] if self.empty or self.clarify else [selected()]
        return {'decision': {'action': 'clarify' if self.clarify else 'finish',
            'reply': '原选品回复', 'notes': '内部判断', 'retain': [], 'query': None,
            'selection': [x['decision'] for x in choices]}, 'selected': choices,
            'queries': [{'queried_at': '2026-10-21T10:00:00+08:00', 'returned': 1}], 'retrieved': 1, 'seen': 1}
    def product(self, item, deadline):
        adapter = JDAdapter(type('Client', (), {'promotion': lambda _, row, **kw: 'https://union-click.jd.com/fixture'})())
        product = adapter.product(item, deadline)
        if self.failed_links:
            product.update(url=None, link_status='unavailable')
        return product


class Model:
    def __init__(self, revise=False):
        self.inputs = []; self.revise = revise; self.reviews = 0
    def ask(self, system, payload, **kwargs):
        self.inputs.append(copy.deepcopy(payload))
        if '购物交付复核者' in system:
            self.reviews += 1
            return ({'verdict': 'revise', 'issues': ['有一处没有事实依据，请纠正。']}
                    if self.revise and self.reviews == 1 else {'verdict': 'pass', 'issues': []}), {}
        return {'reply': 'DS自行修正后的原始口播。' if payload.get('feedback') else 'DS原始口播。'}, {}


class CommerceTests(unittest.TestCase):
    def test_oral_rejection_or_failure_does_not_erase_already_approved_cards(self):
        for fail in (False, True):
            class RejectingModel(Model):
                def ask(self, system, payload, **kwargs):
                    if fail: raise TimeoutError('voice delivery timeout')
                    if '购物交付复核者' in system:
                        return {'verdict':'revise','issues':['表达偏好']}, {}
                    return {'reply':'模型草稿'}, {}
            with tempfile.TemporaryDirectory() as folder:
                result=Commerce([Adapter()],RejectingModel(),folder).execute(
                    {'platform':'jd','request':'耳机500元内'},time.monotonic()+10)
                self.assertEqual(result['status'],'partial')
                self.assertEqual(result['commercial']['state'],'delivery_pending')
                self.assertEqual(result['commercial']['products'][0]['price'],371.37)
                self.assertNotIn('没找到',result['reply'])

    def test_tool_directory_disabled_by_default_and_schema_boundary(self):
        self.assertNotIn('shopping', CommercialQueries().tools)
        plan = {'reply': '正在查', 'capability': 'shopping', 'action': 'prepare',
                'query': {'tool': 'shopping', 'args': {'platform': 'jd', 'request': '耳机500元内'}}}
        with self.assertRaises(ModelFailure): validate_plan(plan, True, ['web_search'])
        validate_plan(plan, True, ['shopping'])
        for args in ({'platform': 'jd', 'request': '耳机', 'auto_cart': True},
                     {'platform': 'jd', 'request': ''}, {'platform': 'jd', 'request': '耳机\n下单'}):
            with self.assertRaises(ServiceError): validate_commerce_args(args)

    def test_selected_order_facts_links_and_ds_authored_delivery_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, model = Adapter(), Model(revise=True)
            commerce = Commerce([adapter], model, folder)
            result = commerce.execute({'platform': 'jd', 'request': '500元内主动降噪耳机'}, time.monotonic()+10)
            self.assertEqual(result['reply'], 'DS自行修正后的原始口播。')
            product = result['commercial']['products'][0]
            self.assertEqual((product['price'], product['coupon_price']), (371.37, 323.09))
            self.assertEqual(product['url'], 'https://union-click.jd.com/fixture')
            self.assertEqual(product['queried_at'], '2026-10-21T10:00:00+08:00')
            trace = json.loads(next(Path(folder).glob('*.json')).read_text())
            self.assertEqual([e['event'] for e in trace['trace']], ['delivery', 'delivery_review', 'delivery', 'delivery_review'])
            self.assertNotIn('feedback', trace['trace'][0]['input'])
            self.assertIn('feedback', trace['trace'][2]['input'])
            self.assertEqual(adapter.calls, 1)
            history = remember_result({'reply': result['reply'], **result})
            self.assertIn('500元内主动降噪耳机', history)
            self.assertIn('371.37', history)
            self.assertNotIn(product['url'], history)

    def test_link_failure_is_kept_and_reported_to_ds_not_replaced_with_fake_url(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Model(); commerce = Commerce([Adapter(failed_links=True)], model, folder)
            result = commerce.execute({'platform': 'jd', 'request': '耳机'}, time.monotonic()+10)
            product = result['commercial']['products'][0]
            self.assertIsNone(product['url'])
            self.assertEqual(model.inputs[0]['products'][0]['link_status'], 'unavailable')

    def test_empty_and_clarification_do_not_invent_products_or_complete_purchase(self):
        for clarify in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                result = Commerce([Adapter(empty=True, clarify=clarify)], Model(), folder).execute(
                    {'platform': 'jd', 'request': '宝宝纸尿裤'}, time.monotonic()+10)
                self.assertEqual(result['commercial']['products'], [])
                self.assertEqual(result['status'], 'partial' if clarify else 'complete')
                self.assertNotIn('shopping', result)

    def test_provider_registration_and_resource_bounds_do_not_choose_a_substitute(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Model(); adapter = Adapter(); commerce = Commerce([adapter], model, folder)
            with self.assertRaises(ServiceError):
                commerce.execute({'platform': 'taobao', 'request': '耳机'}, time.monotonic()+10)
            self.assertEqual(adapter.calls, 0)
        with self.assertRaises(ServiceError):
            ModelBudget(model, time.monotonic()-1).ask('x', {})
        self.assertEqual(model.inputs, [])

    def test_profile_requires_explicit_enablement_and_supported_adapter(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); self.assertIsNone(Commerce.from_private(root, Model()))
            (root/'.private').mkdir(); (root/'.private/commerce.json').write_text('{"enabled":false,"platforms":[]}')
            self.assertIsNone(Commerce.from_private(root, Model()))
            (root/'.private/commerce.json').write_text('{"enabled":true,"platforms":["taobao"]}')
            with self.assertRaises(ServiceError): Commerce.from_private(root, Model())


if __name__ == '__main__': unittest.main()
