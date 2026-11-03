import copy
import io
from unittest.mock import patch
import json
from pathlib import Path
import tempfile
import time
import unittest

from commerce import Commerce, ModelBudget
from commercial_voice import CommercialQueries
from core import ServiceError
from jd_curated import value_at
from taobao_commerce import TaobaoAdapter, TaobaoSearch, TaobaoAPIError, official_url, model_row, validate_query, pack_facts
from taobao_union import TaobaoClient
import test_commerce_voice

ROW = {'item_id': 'opaque-123', 'item_basic_info': {'title': '主动降噪蓝牙耳机', 'shop_title': '测试店', 'volume': 123},
       'price_promotion_info': {'zk_final_price': '299', 'final_promotion_price': '199',
           'predict_rounding_up_price': '179', 'predict_rounding_up_price_desc': '需凑单',
           'more_promotion_list': {'more_promotion_map_data': [{'promotion_title': '88VIP',
               'promotion_desc': '会员满300减100', 'promotion_start_time': '1792684800000', 'promotion_end_time': '1792771199000'}]}},
       'presale_info': {'presale_deposit': '0'}, 'publish_info': {'click_url': 'https://s.click.taobao.com/fixture', 'income_rate': '50'}}

class Client:
    adzone_id=99
    config={'app_key':'fixture-key','app_secret':'fixture-secret','pid':'mm_1_2_99'}
    def __init__(self,rows=None,error=None):self.rows=rows or [copy.deepcopy(ROW)];self.calls=[];self.error=error
    def call(self,method,args,**kwargs):
        self.calls.append((method,copy.deepcopy(args)))
        if self.error:return {'error_response':self.error}
        return {'tbk_dg_material_optional_upgrade_response':{'total_results':len(self.rows),'result_list':{'map_data':copy.deepcopy(self.rows)}}}

class DS:
    def __init__(self):self.inputs=[]
    def ask(self,system,payload,**kwargs):
        self.inputs.append(copy.deepcopy(payload))
        if 'proposed_request' in payload:return {'request':payload['proposed_request']},{}
        if 'utterance' in payload:
            return {'reply':'正在查淘宝。','capability':'shopping','action':'prepare',
                'memory':{'used':[],'operations':[]},'query':{'tool':'shopping','args':{'platform':'taobao','request':payload['utterance']}}},{}
        if '购物交付复核者' in system:return {'verdict':'pass','issues':[]},{}
        if 'products' in payload:return {'reply':'找到主动降噪耳机，页面报价299元。会员优惠另有条件。'},{}
        base={'reply':'本次候选中找到耳机。','notes':'','retain':[],'query':None,'selection':[]}
        if not payload['queries_so_far']:
            return {**base,'action':'query','query':{'q':'降噪耳机','page_no':1,'page_size':10}},{}
        if payload['queries_so_far'][-1].get('error_code'):
            return {**base,'action':'finish','reply':'本次淘宝接口未完成查询，不能确认商品。'},{}
        if payload['pending_rows']:
            return {**base,'action':'continue'},{}
        identity=payload['latest']['rows'][-1]['id']
        return {**base,'action':'finish','selection':[{'id':identity,'reason':'标题标明主动降噪，页面报价299元。',
            'evidence':[{'field':'item_basic_info.title','value':'主动降噪'},{'field':'price_promotion_info.zk_final_price','value':'299'}]}]},{}

class TaobaoCommerceTests(unittest.TestCase):
    def test_authorized_query_only_and_supplier_failure_is_not_silent_empty(self):
        client=Client();search=TaobaoSearch(client)
        args={'q':'用户模型给的词','page_no':2,'page_size':7}
        out=search.query(args)
        self.assertEqual(client.calls[0][1],{**args,'adzone_id':99})
        self.assertEqual(out['args'],args)
        for extra in ('adzone_id','app_key','sort','session'):
            with self.assertRaises(ServiceError):search.query({**args,extra:'untrusted'})
        self.assertEqual(len(client.calls),1)
        with self.assertRaises(TaobaoAPIError) as caught:
            TaobaoSearch(Client(error={'code':15,'sub_code':'50001','sub_msg':'无结果 fixture-secret'})).query(args)
        self.assertEqual(caught.exception.code,'15.50001')
        self.assertNotIn('fixture-secret',caught.exception.detail)
        self.assertIn('无结果',caught.exception.detail)

    def test_all_rows_stream_in_provider_order_and_last_row_can_be_selected(self):
        rows=[]
        for n in range(4):
            row=copy.deepcopy(ROW);row['item_id']='id-'+str(n)
            row['price_promotion_info']['full_qualification']=str(n)+'条件'*4500
            rows.append(row)
        client,ds=Client(rows),DS();events=[]
        result=TaobaoAdapter(client,max_pages=1).select('主动降噪耳机300元内',ds,events.append,time.monotonic()+20)
        seen=[]
        for e in events:
            if e['event']=='model' and e['repair']==0:
                table=e['input']['latest']
                seen += [row['id'] for row in table['rows']]
        self.assertEqual(seen,['p1r0','p1r1','p1r2','p1r3'])
        self.assertEqual(result['selected'][0]['original']['item_id'],'id-3')
        self.assertEqual(result['coverage']['pending_rows'],0)
        self.assertEqual(len(client.calls),1)
        self.assertEqual(events[1]['raw_rows'],rows)

    def test_retention_overflow_is_returned_to_ds_for_its_own_correction(self):
        class RetainingDS(DS):
            def ask(self, system, payload, **kwargs):
                out, usage=super().ask(system,payload,**kwargs)
                if out.get('action')=='continue' and not payload.get('protocol_feedback'):
                    out['retain']=[row['id'] for row in payload['latest']['rows']]
                return out,usage
        rows=[]
        for n in range(4):
            row=copy.deepcopy(ROW);row['item_id']='row-'+str(n)
            row['price_promotion_info']['full_qualification']=str(n)+'条件'*4500
            rows.append(row)
        client,ds=Client(rows),RetainingDS();events=[]
        result=TaobaoAdapter(client,max_pages=1).select('主动降噪耳机300元内',ds,events.append,time.monotonic()+20)
        feedbacks=[e['input']['protocol_feedback'] for e in events if e['event']=='model' and e['input'].get('protocol_feedback')]
        self.assertTrue(any('保留' in f['error'] and '预算' in f['error'] for f in feedbacks))
        self.assertEqual(result['selected'][0]['original']['item_id'],'row-3')
        self.assertEqual(result['seen'],4);self.assertEqual(len(client.calls),1)

    def test_prices_conditions_unknown_metrics_and_official_links_survive_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            client,ds=Client(),DS();commerce=Commerce([TaobaoAdapter(client)],ds,folder)
            result=commerce.execute({'platform':'taobao','request':'主动降噪耳机300元内'},time.monotonic()+20)
            self.assertEqual(result['status'],'complete')
            product=result['commercial']['products'][0]
            self.assertEqual((product['price'],product['coupon_price']),('299','199'))
            self.assertIsNone(product['review_count']);self.assertIsNone(product['rating_percent'])
            self.assertEqual(product['price_info'],ROW['price_promotion_info'])
            self.assertIn('会员满300减100',' '.join(product['offer_terms']))
            self.assertIn('需凑单',' '.join(product['offer_terms']))
            self.assertEqual(product['url'],'https://s.click.taobao.com/fixture')
            self.assertNotIn('精选',result['sources'][0]['provider'])
            self.assertIn('关键词',result['commercial']['scope'])
            trace=json.loads(next(Path(folder).glob('*.json')).read_text())
            facts=trace['selection_result']['selected'][0]['facts']
            self.assertNotIn('publish_info',facts)
            self.assertEqual(value_at(facts,'price_promotion_info.zk_final_price'),'299')
            self.assertNotIn('fixture-secret',json.dumps(result))

    def test_errors_cannot_be_reported_as_success_or_substitute_platform(self):
        with tempfile.TemporaryDirectory() as folder:
            ds=DS();commerce=Commerce([TaobaoAdapter(Client(error={'code':15,'sub_code':'2'}))],ds,folder)
            result=commerce.execute({'platform':'taobao','request':'耳机'},time.monotonic()+20)
            self.assertEqual(result['status'],'error')
            self.assertEqual(result['commercial']['state'],'query_failed')
            self.assertEqual(result['commercial']['products'],[])
            self.assertEqual(result['sources'],[])
            with self.assertRaises(ServiceError):commerce.execute({'platform':'jd','request':'耳机'},time.monotonic()+20)

    def test_private_profile_and_directory_reflect_only_explicit_platforms(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.private').mkdir()
            (root/'.private/taobao-union.json').write_text(json.dumps({'app_key':'x','app_secret':'y','pid':'mm_1_2_99'}))
            (root/'.private/commerce.json').write_text('{"enabled":true,"platforms":["taobao"]}')
            commerce=Commerce.from_private(root,DS())
            self.assertEqual(list(commerce.adapters),['taobao'])
            rules=CommercialQueries(commerce=commerce).rules
            self.assertNotIn('淘宝尚未连接',rules);self.assertIn('授权物料关键词',rules)
            (root/'.private/commerce.json').write_text('{"enabled":true,"platforms":["taobao","jd"]}')
            with self.assertRaises(ServiceError):Commerce.from_private(root,DS())

    def test_shared_compression_round_trips_every_condition_and_preserves_field_names(self):
        rows=[model_row(copy.deepcopy(ROW),'p1r'+str(n)) for n in range(3)]
        rows[0]['source_literal']={'$shared':500}
        packed=pack_facts(rows)
        def expand(v):
            if isinstance(v,dict) and set(v)=={packed['reference_key']}:return expand(packed['shared_objects'][v[packed['reference_key']]])
            if isinstance(v,dict):return {k:expand(x) for k,x in v.items()}
            if isinstance(v,list):return [expand(x) for x in v]
            return v
        self.assertEqual(expand(packed['rows']),rows)
        self.assertEqual(packed['rows'][0]['id'],'p1r0')
        self.assertTrue(packed['shared_objects'])

    def test_reasoning_budget_uses_only_protocol_stages_and_keeps_retrieval_fast(self):
        options=[]
        class Model:
            def ask(self,system,payload,**kwargs):options.append(kwargs.get('reasoning_effort'));return {},{}
        adapter=TaobaoAdapter(Client())
        budget=ModelBudget(Model(),time.monotonic()+20,reasoning_effort=adapter.reasoning_effort)
        for payload in ({'latest':{}},{'user_context':{}},{'draft':{}},{'products':[]}):
            budget.ask('规则',payload)
        self.assertEqual(options,[None,'low','low','low'])

    def test_transient_reasoning_failure_retries_same_request_inside_shared_budget(self):
        from model_client import ModelFailure
        calls=[];events=[]
        class Model:
            def ask(self,system,payload,**kwargs):
                calls.append((system,copy.deepcopy(payload),kwargs))
                if len(calls)==1:raise ModelFailure('model_timeout')
                return {'reply':'DS原文'},{}
        budget=ModelBudget(Model(),time.monotonic()+60,reasoning_effort='low');budget.trace=events.append
        result,_=budget.ask('复核规则',{'draft':{'reply':'真实资料'}},timeout=20)
        self.assertEqual(result,{'reply':'DS原文'});self.assertEqual(budget.calls,2)
        self.assertEqual(calls[0][:2],calls[1][:2]);self.assertEqual(calls[0][2]['timeout'],30)
        self.assertEqual(events[0]['error_code'],'model_timeout')

    def test_low_reasoning_is_per_call_and_does_not_change_default_model(self):
        from model_client import ModelClient
        requests=[]
        def transport(request, **kwargs):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"reply":"完成"}'}}]}).encode())
        model=ModelClient({'base_url':'https://api.deepseek.com','api_key':'fixture','model':'deepseek-flash'})
        with patch('model_client.urllib.request.urlopen',transport):
            model.ask('规则',{})
            model.ask('规则',{},reasoning_effort='low')
            model.ask('规则',{})
        self.assertEqual([r['model'] for r in requests],['deepseek-flash']*3)
        self.assertEqual([r['thinking']['type'] for r in requests],['disabled','enabled','disabled'])
        self.assertEqual(requests[1]['reasoning_effort'],'low')
        self.assertEqual(requests[1]['max_tokens'],6144)

    def test_product_links_reject_credentials_lookalike_hosts_and_protocols(self):
        self.assertEqual(official_url('//s.click.taobao.com/x'),'https://s.click.taobao.com/x')
        for value in ['http://taobao.com/x','https://taobao.com.evil.example/x','https://user@taobao.com/x',
                      'https://taobao.com:444/x','javascript:alert(1)','https://taobao.com/\n']:
            with self.assertRaises(ServiceError):official_url(value)

class TaobaoVoiceTests(unittest.TestCase):
    setUp=test_commerce_voice.CommerceVoiceTests.setUp
    tearDown=test_commerce_voice.CommerceVoiceTests.tearDown
    request=test_commerce_voice.CommerceVoiceTests.request
    post=test_commerce_voice.CommerceVoiceTests.post

    def test_taobao_reaches_existing_http_voice_cards_history_replay(self):
        client=Client();ds=DS()
        self.ds.ask=ds.ask  # Existing Bridge still uses this model object.
        commerce=Commerce([TaobaoAdapter(client)],self.ds,Path(self.temp.name)/'tb-traces')
        self.voice.runtime.commerce=commerce
        self.voice.bridge.conversation.system_suffix=self.voice.runtime.rules
        body={'action':'text','id':'1'*32,'revision':0,'requestId':'2'*32,'text':'淘宝300元内主动降噪蓝牙耳机'}
        status,result=self.post(body)
        self.assertEqual(status,200,result);self.assertEqual(result['queryStatus'],'complete')
        self.assertEqual(result['commercial']['platform'],'taobao')
        self.assertEqual(result['commercial']['products'][0]['price'],'299')
        self.assertEqual(self.speech,['synthesize'])
        before=len(ds.inputs)
        self.assertEqual(self.post(body)[0],200)
        self.assertEqual(self.post({'action':'replay','id':'1'*32,'requestId':'2'*32})[0],200)
        self.assertEqual(len(client.calls),1);self.assertEqual(len(ds.inputs),before)
        _,history=self.post({'action':'history','before':0})
        self.assertEqual(history['items'][0]['result']['commercial']['products'][0]['id'],'opaque-123')
        self.assertNotIn('fixture-secret',json.dumps(history))

if __name__=='__main__':unittest.main()
