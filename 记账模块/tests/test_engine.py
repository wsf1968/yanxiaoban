import copy
from pathlib import Path
import tempfile
import unittest

from xiaopai_bookkeeping import Bookkeeping,BookkeepingFailure

PASS={'verdict':'pass','issues':[]}
def tool(operation,args=None):
    return {'decision':'tool','reply':'正在处理。','tool':{'book_id':'family','operation':operation,'args':args or {}}}
def reply(text='本次测试已记入家庭账。'):
    return {'decision':'reply','reply':text,'tool':None}

class Model:
    def __init__(self,outputs):self.outputs=list(outputs);self.inputs=[]
    def ask(self,system,payload,**kwargs):
        self.inputs.append(copy.deepcopy(payload))
        return copy.deepcopy(self.outputs.pop(0)),{'model':'fixture'}

class Backend:
    actor='fixture-alice'
    def __init__(self,root):self.root=Path(root);self.calls=[]
    def books(self):return [{'id':'family','name':'家庭账','kind':'family','role':'owner'}]
    def execute(self,book,request,operation,args):
        self.calls.append((book,request,operation,copy.deepcopy(args)))
        return {'ok':True,'result':{'status':'saved','transaction':{'id':'provider-id','amount_fen':-3550}}}

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.backend=Backend(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def run_turn(self,model,text='家庭账午饭35.5元',request='request-1'):
        return Bookkeeping(model,self.backend).turn(text,[],request,'2026-10-26T19:00:00+08:00')
    def test_raw_model_args_and_reply_are_not_rewritten(self):
        args={'account_id':'real-account','date':'2026-10-26','amount_fen':-3550,'category_id':None,'notes':'午饭'}
        model=Model([tool('add',args),PASS,reply(),PASS]);result=self.run_turn(model)
        self.assertTrue(result['complete']);self.assertEqual(self.backend.calls[0][3],args)
        self.assertEqual(result['reply'],'本次测试已记入家庭账。')
    def test_before_write_rejection_makes_ds_correct_its_own_amount(self):
        bad={'account_id':'real','date':'2026-10-26','amount_fen':-355,'category_id':None,'notes':'午饭'}
        good={**bad,'amount_fen':-3550}
        model=Model([tool('add',bad),{'verdict':'revise','issues':['35.5元是3550分']},tool('add',good),PASS,reply(),PASS])
        result=self.run_turn(model)
        self.assertTrue(result['complete']);self.assertEqual(len(self.backend.calls),1)
        self.assertEqual(self.backend.calls[0][3]['amount_fen'],-3550)
        self.assertEqual(result['trace'][0]['raw_output']['tool']['args']['amount_fen'],-355)
    def test_transport_retry_reuses_whole_turn_without_model_or_provider_write(self):
        model=Model([tool('add'),PASS,reply(),PASS]);first=self.run_turn(model)
        second=self.run_turn(Model([]))
        self.assertTrue(second['replayed']);self.assertEqual(first['reply'],second['reply']);self.assertEqual(len(self.backend.calls),1)
    def test_reused_request_with_changed_text_is_rejected(self):
        self.run_turn(Model([reply('需要选择记在哪本账。'),PASS]))
        with self.assertRaisesRegex(BookkeepingFailure,'request_id_conflict'):
            self.run_turn(Model([]),text='换一个请求')
    def test_source_ambiguity_is_clarified_without_any_write(self):
        model=Model([{'decision':'clarify','reply':'这笔要记家庭账还是个人账？','tool':None},PASS])
        result=self.run_turn(model,text='午饭35元');self.assertEqual(result['decision'],'clarify');self.assertEqual(self.backend.calls,[])
    def test_final_review_correction_does_not_programmatically_rewrite_reply(self):
        model=Model([reply('已付款。'),{'verdict':'revise','issues':['工具没有付款能力。']},reply('还没有执行付款。'),PASS])
        result=self.run_turn(model);self.assertEqual(result['reply'],'还没有执行付款。')
        self.assertEqual(result['trace'][0]['raw_output']['reply'],'已付款。')
    def test_model_failure_after_real_receipt_is_persisted_without_repeating(self):
        model=Model([tool('add'),PASS]);first=self.run_turn(model)
        self.assertFalse(first['complete']);self.assertEqual(len(first['tools']),1)
        second=self.run_turn(Model([]));self.assertTrue(second['replayed']);self.assertEqual(len(self.backend.calls),1)
    def test_unknown_provider_outcome_stops_further_writes(self):
        self.backend.execute=lambda *args:{'ok':False,'error':'write_outcome_unknown_do_not_retry'}
        result=self.run_turn(Model([tool('add'),PASS]))
        self.assertFalse(result['complete']);self.assertEqual(result['error'],'write_outcome_unknown_do_not_retry')

if __name__=='__main__':unittest.main()
