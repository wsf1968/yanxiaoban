import json
import unittest
from urllib.parse import parse_qs
from taobao_union import TaobaoClient, TaobaoFailure, NoRedirect, signed_params, ENDPOINT

CONFIG={'app_key':'fixture-app-key','app_secret':'fixture-secret','pid':'mm_11_22_33'}
class Response:
    def __init__(self,payload):self.raw=json.dumps(payload).encode()
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,limit):return self.raw[:limit]

class TransportTests(unittest.TestCase):
    def test_selected_request_parameters_are_not_silently_changed(self):
        sent=[]
        def send(req,**kwargs):sent.append(req);return Response({'tbk_dg_material_optional_upgrade_response':{'result_list':{'map_data':[]}}})
        client=TaobaoClient(CONFIG,open_call=send)
        args={'q':'200元内耳机，不要入耳式','adzone_id':client.adzone_id,'has_coupon':False,'page_size':17}
        client.call('taobao.tbk.dg.material.optional.upgrade',args)
        payload=parse_qs(sent[0].data.decode())
        self.assertEqual(payload['q'],[args['q']]);self.assertEqual(payload['page_size'],['17'])
        self.assertEqual(payload['adzone_id'],['33']);self.assertEqual(payload['has_coupon'],['false'])
        self.assertEqual(sent[0].full_url,ENDPOINT);self.assertNotIn(CONFIG['app_secret'].encode(),sent[0].data)
        self.assertNotIn('site_id',payload) # this API documents adzone_id, not media ID substitution
    def test_supplier_failure_stays_error_and_does_not_become_empty_candidates(self):
        error={'error_response':{'code':15,'sub_code':'2','sub_msg':'pid不正确'}}
        client=TaobaoClient(CONFIG,open_call=lambda *a,**k:Response(error))
        self.assertEqual(client.call('taobao.tbk.dg.material.optional.upgrade',{'adzone_id':33}),error)
    def test_mutating_api_and_reserved_auth_overrides_are_rejected_before_network(self):
        def fail(*a,**k):self.fail('Network should not be reached')
        client=TaobaoClient(CONFIG,open_call=fail)
        for method,args in [('taobao.trade.create',{}),('taobao.tbk.dg.material.optional.upgrade',{'app_key':'foreign'})]:
            with self.assertRaises(ValueError):client.call(method,args)
    def test_redirect_does_not_forward_signed_request(self):
        with self.assertRaisesRegex(TaobaoFailure,'redirect_blocked'):
            NoRedirect().redirect_request(None,None,None,None,None,None)
    def test_oversize_or_invalid_response_fails_explicitly(self):
        for raw in [b'x'*2_000_001,b'not-json',b'[]']:
            response=Response({});response.raw=raw
            client=TaobaoClient(CONFIG,open_call=lambda *a,**k:response)
            with self.assertRaises(TaobaoFailure):client.call('taobao.tbk.dg.material.optional.upgrade',{})
    def test_incomplete_pid_never_invents_adzone(self):
        with self.assertRaises(ValueError):TaobaoClient({**CONFIG,'pid':'mm_11_22'})
    def test_unknown_values_do_not_turn_into_fake_string_fields(self):
        with self.assertRaises(ValueError):signed_params(CONFIG,'taobao.tbk.dg.material.optional.upgrade',{'q':None})

if __name__=='__main__':unittest.main(verbosity=2)
