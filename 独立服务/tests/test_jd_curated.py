import copy
import json
from pathlib import Path
import tempfile
import unittest

from core import ServiceError
from jd_curated import JDClient, QUERY, CuratedEvaluation, model_row, pack_rows, validate_completion, validate_decision, validate_query


CONFIG = {'app_key': 'fixture-public', 'app_secret': 'fixture-private', 'site_id': 'fixture-site'}
ROW = {'skuName': '测试降噪耳机', 'priceInfo': {'price': 399, 'lowestCouponPrice': 369},
       'comments': 50, 'goodCommentsShare': 98, 'materialUrl': 'https://jingfen.jd.com/detail/fixture.html',
       'commissionInfo': {'commission': 99}, 'couponInfo': {'couponList': [{'discount': 30,
           'quota': 399, 'useEndTime': 1799999999000, 'link': 'https://coupon.m.jd.com/fixture'}]}}


def decision(action='finish', **changes):
    return {'action': action, 'reply': '测试回复', 'notes': '', 'query': None,
            'retain': [], 'selection': [], **changes}


class CuratedTests(unittest.TestCase):
    def test_historical_page_index_survives_restart_without_product_or_price_recommendations(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'catalog.json'
            client=JDClient(CONFIG,catalog_path=path)
            args={'eliteId':24,'pageIndex':9,'pageSize':3}
            rows=[{**ROW,'categoryInfo':{'cid3Name':'耳机'}},
                  {**ROW,'categoryInfo':{'cid3Name':'耳机'}},
                  {**ROW,'categoryInfo':{'cid3Name':'水壶'}}]
            client.remember_page({'args':args,'rows':rows,'totalCount':300,
                                  'queried_at':'2026-10-21T10:00:00+08:00'})
            restored=JDClient(CONFIG,catalog_path=path)
            index=restored.observed_queries()
            self.assertEqual(index['pages'][0]['args'],args)
            self.assertEqual(index['pages'][0]['category_counts'],{'耳机':2,'水壶':1})
            self.assertEqual(restored.cache,{})
            self.assertNotIn('skuName',json.dumps(index))
            self.assertNotIn('priceInfo',json.dumps(index))
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_keyword_and_undocumented_params_rejected_before_network(self):
        calls = []
        client = JDClient(CONFIG, transport=lambda *args: calls.append(args))
        for extra in ({'keyword': '耳机'}, {'pageSize': 51}, {'eliteId': 999999}, {'pageIndex': True}, {'groupId': 1}):
            with self.assertRaises(ServiceError):
                client.query({'eliteId': 24, 'pageIndex': 1, 'pageSize': 20, **extra})
        self.assertEqual(calls, [])

    def test_signed_post_response_and_exact_query_cache_no_product_sorting(self):
        sent = []
        rows = [copy.deepcopy(ROW), {**ROW, 'skuName': '另一商品', 'priceInfo': {'price': 9}}]
        def wire(method, params):
            sent.append((method, params))
            return {'jd_union_open_goods_jingfen_query_responce': {'code': '0',
                'queryResult': json.dumps({'code': 200, 'data': rows, 'totalCount': 2})}}
        client = JDClient(CONFIG, transport=wire)
        args = {'eliteId': 24, 'pageIndex': 1, 'pageSize': 2}
        first, second = client.query(args), client.query(args)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], QUERY)
        self.assertEqual(len(sent[0][1]['sign']), 32)
        self.assertEqual(json.loads(sent[0][1]['360buy_param_json']), {'goodsReq': args})
        self.assertEqual(first['rows'], rows)
        self.assertTrue(second['cached'])

    def test_projection_preserves_unknown_and_conditional_prices_without_secrets_links_or_commission(self):
        row = model_row(ROW, 'p1r0')
        self.assertEqual(row['priceInfo'], ROW['priceInfo'])
        self.assertEqual(row['coupons'][0]['quota'], 399)
        self.assertNotIn('brandName', row)
        self.assertNotIn('commissionInfo', row)
        self.assertNotIn('link', row['coupons'][0])
        self.assertNotIn('materialUrl', row)

    def test_purchase_price_coupon_composition_is_not_dropped(self):
        source = {**ROW, 'purchasePriceInfo': {'purchasePrice': 289.52, 'thresholdPrice': 371.37,
            'couponList': [{'couponStyle': 3, 'discount': 0.95, 'quota': 99,
                            'platformType': 0, 'link': 'https://coupon.m.jd.com/private'},
                           {'couponStyle': 3, 'discount': 0.87, 'quota': 1}],
            'promotionLabelInfoList': [{'promotionLabel': '满1件减15元'}]}}
        result = model_row(source, 'p1r0')
        info = result['purchasePriceInfo']
        self.assertEqual(info['couponList'], [
            {'couponStyle': 3, 'discount': 0.95, 'quota': 99, 'platformType': 0},
            {'couponStyle': 3, 'discount': 0.87, 'quota': 1}])
        self.assertEqual(info['promotionLabelInfoList'], source['purchasePriceInfo']['promotionLabelInfoList'])
        self.assertEqual(info['purchasePrice'], 289.52)
        self.assertIn('link', source['purchasePriceInfo']['couponList'][0])
        packed = pack_rows([result])
        column = packed['columns'].index('purchasePriceInfo')
        unpacked = dict(zip(packed['object_columns']['purchasePriceInfo'], packed['rows'][0][column]))
        self.assertEqual(unpacked, info)

    def test_bad_upstream_messages_do_not_leak_key(self):
        def wire(*args):
            return {'error_response': {'zh_desc': 'fixture-private fixture-public'}}
        with self.assertRaises(ServiceError) as error:
            JDClient(CONFIG, transport=wire).query({'eliteId': 24, 'pageIndex': 1, 'pageSize': 1})
        self.assertNotIn('fixture', str(error.exception))

    def test_final_choice_must_cite_existing_unchanged_facts(self):
        rows = {'p1r0': model_row(ROW, 'p1r0')}
        choice = {'id': 'p1r0', 'reason': '标题写有降噪，价格399元',
                  'evidence': [{'field': 'skuName', 'value': '降噪'}, {'field': 'priceInfo.price', 'value': 399}]}
        self.assertEqual(validate_decision(decision(selection=[choice]), rows)['selection'], [choice])
        for changed in ({'id': 'invented'}, {'evidence': [{'field': 'priceInfo.price', 'value': 299}]},
                        {'evidence': [{'field': 'inventory', 'value': '有货'}]}):
            with self.assertRaises(ServiceError):
                validate_decision(decision(selection=[{**choice, **changed}]), rows)
        # Numeric string / integer / JSON float are protocol representations of
        # the same amount; accepting them must not change the price.
        equal = {**choice, 'evidence': [{'field': 'priceInfo.price', 'value': '399.00'}]}
        validate_decision(decision(selection=[equal]), rows)

    def test_tabular_encoding_preserves_all_products_and_coupon_conditions(self):
        rows = [model_row(ROW, 'p1r0'), model_row({**ROW, 'skuName': '第二件'}, 'p1r1')]
        packed = pack_rows(rows)
        unpacked = []
        for values in packed['rows']:
            row = dict(zip(packed['columns'], values))
            row['coupons'] = [dict(zip(packed['coupon_columns'], x)) for x in row['coupons']]
            for key, fields in packed['object_columns'].items():
                if row[key] is not None:
                    row[key] = dict(zip(fields, row[key]))
            unpacked.append(row)
        self.assertEqual(rows, unpacked)

    def test_large_page_streams_all_rows_once_without_requery_or_cropping(self):
        class Client:
            api_calls = 0
            def query(self, args):
                self.api_calls += 1
                rows = [{**ROW, 'skuName': '商品' + str(i),
                         'purchasePriceInfo': {'terms': '完整条款' * 1000}} for i in range(10)]
                return {'args': args, 'rows': rows, 'totalCount': 10, 'queried_at': 'fixture', 'cached': False}
        class Model:
            n = 0
            seen = []
            def ask(self, system, payload, **kwargs):
                self.n += 1
                table = payload['latest']
                if table['rows']:
                    idx = table['columns'].index('skuName')
                    self.seen.extend(row[idx] for row in table['rows'])
                if self.n == 1:
                    return decision('query', query={'eliteId': 24, 'pageIndex': 1, 'pageSize': 50}), {}
                return decision('continue' if payload['pending_rows'] else 'finish'), {}
        client, model = Client(), Model()
        result = CuratedEvaluation(client, model, review_final=False).run('测试完整分包')
        self.assertEqual(model.seen, ['商品' + str(i) for i in range(10)])
        self.assertEqual(client.api_calls, 1)
        self.assertEqual(result['seen'], 10)
        self.assertEqual(result['queries'][0]['delivered_rows'], 10)
        self.assertTrue(result['queries'][0]['delivered_to_model'])

    def test_empty_finish_cannot_skip_unread_rows_but_clarification_is_model_choice(self):
        with self.assertRaisesRegex(ServiceError, '3条'):
            validate_completion(decision(), 3)
        validate_completion(decision(), 0)
        validate_completion(decision('clarify', reply='需要确认尺寸'), 3)
        validate_completion(decision(selection=[{'id': 'fixture'}]), 3)

    def test_empty_results_receive_review_instead_of_bypassing_it(self):
        class Client:
            api_calls = 0
        class Model:
            reviews = 0
            def ask(self, system, payload, **kwargs):
                if '购物交付复核者' in system:
                    self.reviews += 1
                    return {'verdict': 'pass', 'issues': []}, {}
                return decision(reply='本次未查到匹配，范围有限'), {}
        model = Model()
        result = CuratedEvaluation(Client(), model).run('测试空结果复核')
        self.assertEqual(model.reviews, 1)
        self.assertEqual(result['selected'], [])

    def test_negative_review_receives_real_budget_and_current_evidence(self):
        class Client:
            api_calls = 0
            def query(self, args):
                self.api_calls += 1
                return {'args': args, 'rows': [ROW], 'totalCount': 1168,
                        'queried_at': 'fixture', 'cached': False}
        class Model:
            def ask(self, system, payload, **kwargs):
                if '购物交付复核者' in system:
                    self.review_input = payload
                    return {'verdict': 'pass', 'issues': []}, {}
                if payload['pages_remaining']:
                    return decision('query', query={'eliteId': 24, 'pageIndex': 1, 'pageSize': 1}), {}
                return decision(reply='本次查看的候选没有明确满足项'), {}
        model = Model()
        CuratedEvaluation(Client(), model, max_pages=1).run('测试空结果证据')
        review = model.review_input
        self.assertEqual(review['coverage']['pages_remaining'], 0)
        self.assertEqual(review['coverage']['pending_rows'], 0)
        self.assertEqual(review['coverage']['retrieved'], 1)
        self.assertEqual(review['queries'][0]['totalCount'], 1168)
        self.assertEqual(len(review['facts']['rows']), 1)
        self.assertIn('skuName', review['facts']['columns'])

    def test_queries_and_final_order_are_model_decisions_not_fixed_first_rows(self):
        class Client:
            api_calls = 0
            def query(self, args):
                self.api_calls += 1
                return {'args': args, 'rows': [ROW, {**ROW, 'skuName': '模型选择的第二件'}],
                        'totalCount': 2, 'queried_at': 'fixture', 'cached': False}
        class Model:
            n = 0
            def ask(self, system, payload, **kwargs):
                self.n += 1
                if self.n == 1:
                    return decision('query', query={'eliteId': 24, 'pageIndex': 3, 'pageSize': 2}), {}
                return decision(selection=[{'id': 'p1r1', 'reason': '标题证据',
                    'evidence': [{'field': 'skuName', 'value': '模型选择的第二件'}]}]), {}
        result = CuratedEvaluation(Client(), Model(), review_final=False).run('测试')
        self.assertEqual(result['queries'][0]['args']['pageIndex'], 3)
        self.assertEqual([x['facts']['skuName'] for x in result['selected']], ['模型选择的第二件'])

    def test_clarification_makes_no_jd_call_and_repeated_pages_stop(self):
        class Client:
            api_calls = 0
            def query(self, args):
                self.api_calls += 1
                return {'args': args, 'rows': [], 'totalCount': 0, 'queried_at': 'fixture', 'cached': False}
        class Model:
            def ask(self, *args, **kwargs):
                return decision('clarify', reply='请问需要什么尺寸？'), {}
        client = Client()
        result = CuratedEvaluation(client, Model()).run('纸尿裤')
        self.assertEqual(result['decision']['action'], 'clarify')
        self.assertEqual(client.api_calls, 0)
        class Repeater:
            def ask(self, *args, **kwargs):
                return decision('query', query={'eliteId': 24, 'pageIndex': 1, 'pageSize': 1}), {}
        with self.assertRaisesRegex(ServiceError, '重复'):
            CuratedEvaluation(client, Repeater()).run('耳机')
        self.assertEqual(client.api_calls, 1)

    def test_semantic_feedback_returns_to_ds_without_program_selecting_or_rewriting(self):
        class Client:
            api_calls = 0
            def query(self, args):
                self.api_calls += 1
                return {'args': args, 'rows': [ROW], 'totalCount': 1, 'queried_at': 'fixture', 'cached': False}
        class Model:
            plan_calls = 0
            review_calls = 0
            def ask(self, system, payload, **kwargs):
                if '购物交付复核者' in system:
                    self.review_calls += 1
                    if self.review_calls == 1:
                        return {'verdict': 'revise', 'issues': ['没有证据支持该适配条件，请重新决策。']}, {}
                    return {'verdict': 'pass', 'issues': []}, {}
                self.plan_calls += 1
                if self.plan_calls == 1:
                    return decision('query', query={'eliteId': 24, 'pageIndex': 1, 'pageSize': 1}), {}
                if self.plan_calls == 2:
                    return decision(reply='不合格的模型原回复', selection=[{'id': 'p1r0', 'reason': '测试理由',
                        'evidence': [{'field': 'skuName', 'value': '降噪'}]}]), {}
                self.feedback = payload['protocol_feedback']
                return decision('clarify', reply='模型重新决定先澄清'), {}
        model, events = Model(), []
        result = CuratedEvaluation(Client(), model, trace=events.append).run('测试适配需求')
        self.assertIn('没有证据', model.feedback['error'])
        self.assertEqual(result['decision']['reply'], '模型重新决定先澄清')
        self.assertEqual(result['selected'], [])
        self.assertTrue(any(e.get('raw_output', {}).get('reply') == '不合格的模型原回复' for e in events))

    def test_trace_preserves_actual_inputs_before_later_history_and_repairs(self):
        class Client:
            api_calls = 0
            def query(self, args):
                self.api_calls += 1
                return {'args': args, 'rows': [ROW], 'totalCount': 1,
                        'queried_at': 'fixture', 'cached': False}
        class Model:
            n = 0
            def ask(self, system, payload, **kwargs):
                self.n += 1
                if self.n == 1:
                    return decision('query', query={'eliteId': 24, 'pageIndex': 1, 'pageSize': 1}), {}
                if self.n == 2:
                    return decision('continue'), {}  # invalid: nothing remains
                return decision(), {}
        events = []
        CuratedEvaluation(Client(), Model(), review_final=False, trace=events.append).run('审计快照')
        initial, rejected, repaired = [e for e in events if e['event'] == 'model']
        self.assertEqual(initial['input']['history'], [])
        self.assertEqual(initial['input']['queries_so_far'], [])
        self.assertNotIn('protocol_feedback', rejected['input'])
        self.assertIn('protocol_feedback', repaired['input'])
        page = next(e for e in events if e['event'] == 'page')
        self.assertEqual(page['metadata']['delivered_rows'], 0)
        self.assertFalse(page['metadata']['delivered_to_model'])

    def test_promotion_uses_only_existing_material_and_registered_site(self):
        calls = []
        def wire(method, params):
            calls.append(json.loads(params['360buy_param_json']))
            return {'jd_union_open_promotion_common_get_responce': {'getResult': json.dumps({
                'code': 200, 'data': {'clickURL': 'https://union-click.jd.com/fixture'}})}}
        client = JDClient(CONFIG, transport=wire)
        self.assertEqual(client.promotion(ROW), 'https://union-click.jd.com/fixture')
        self.assertEqual(calls[0]['promotionCodeReq']['siteId'], 'fixture-site')
        with self.assertRaises(ServiceError):
            client.promotion({**ROW, 'materialUrl': 'https://jd.com.evil.test/steal'})
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
