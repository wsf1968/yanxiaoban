import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs,urlsplit
from unittest.mock import patch

from xiaopai_secretary import Secretary, Journal, Scope, BoundaryError
from xiaopai_secretary.bridge import shortcut_url
from xiaopai_secretary.journal import digest
from xiaopai_secretary.model import deepseek_model
from xiaopai_secretary.engine import protocol_errors

PAYLOAD={'request_id':'test-turn','user':'明早八点提醒我买票。','history':[], 'sources':[], 'existing_items':[]}
VALID={'decision':'prepare','reply':'准备创建明早八点的买票提醒。',
    'actions':[{'tool':'create_reminder','operation_id':'ds-id','source_ids':[],
        'authorization':PAYLOAD['user'],'selection_explanation':'明确的独立提醒',
        'title':'买票','remind_at':'2026-10-22T08:00:00+08:00','notes':None,'presentation_ref':None}],
    'booking':{'state':'unknown','evidence':None},'reason':'用户要求'}
CAPS={'create_event','create_reminder','update_item','delete_item','booking_metadata','readback','operation_marker',
      'nullable_event_end','undated_reminder','no_alert'}

class FakeModel:
    def __init__(self,replies):self.replies=iter(replies);self.calls=[]
    def ask(self,system,payload,**kwargs):
        self.calls.append((system,payload))
        return copy.deepcopy(next(self.replies)),{'model':'offline-replay','tokens':0}


def receipt(job,native_id='fixture-item',fields=None,exists=True):
    action=job['action']
    if fields is None and exists:
        keys=('title','start_at','end_at','remind_at','location','notes') if action['tool']=='create_event' else ('title','remind_at','notes')
        fields={k:action[k] for k in keys}
    return {'protocol_version':1,'operation_id':job['operation_id'],'action_sha256':job['action_sha256'],
            'outcome':'succeeded','native_id':native_id,
            'readback':{'operation_id':job['operation_id'],'exists':exists,'fields':fields,'booking':job['booking']}}

class EngineTests(unittest.TestCase):
    @unittest.skip("Private historical model traces are intentionally excluded from the clean source")
    def test_original_87_complete_traces_replay_without_semantic_changes(self):
        base=Path(os.environ.get('SECRETARY_EVIDENCE',Path(__file__).parent/'fixtures'))
        count=0
        for name in ['acceptance-a-low','acceptance-b-low','holdout-low']:
            report=json.loads((base/name/'results.json').read_text())
            for row in report['results']:
                with self.subTest(run=name,case=row['id'],repeat=row['repeat']):
                    fake=FakeModel([t['raw_output'] for t in row['trace']])
                    trace=[]
                    actual=Secretary(fake).prepare(row['input'],trace)
                    self.assertEqual(actual,row['final'])
                    self.assertEqual(len(fake.calls),len(row['trace']))
                    self.assertEqual(actual,trace[-2]['raw_output'])
                    self.assertFalse(protocol_errors(actual,row['input']))
                    count+=1
        self.assertEqual(count,87)

    def test_resources_equal_frozen_accepted_version(self):
        base=Path(os.environ.get('SECRETARY_EVIDENCE',Path(__file__).parent/'fixtures'))
        dest=Path(__file__).resolve().parents[1]/'xiaopai_secretary'/'resources'
        for name in ['schema.json','policy.txt','auditor.txt']:
            self.assertEqual((base/name).read_bytes(),(dest/name).read_bytes())

    def test_default_native_adapter_stays_disabled(self):
        config=json.loads((Path(__file__).resolve().parents[1]/'native-adapter.example.json').read_text())
        self.assertIs(config['enabled'],False)
        self.assertEqual(config['capabilities'],[])
        self.assertEqual(config['verified_devices'],[])

    def test_bounded_invalid_output_stops_without_business_fallback(self):
        fake=FakeModel([{}, {}, {}])
        self.assertIsNone(Secretary(fake).prepare(PAYLOAD))
        self.assertEqual(len(fake.calls),3)
        self.assertIn('validation_feedback',fake.calls[1][1])

    def test_rejected_output_feedback_reaches_ds(self):
        revised=copy.deepcopy(VALID);revised['reply']='准备添加这个提醒。'
        fake=FakeModel([VALID,{'verdict':'revise','issues':['fixture correction']},revised,{'verdict':'accept','issues':[]}])
        self.assertEqual(Secretary(fake).prepare(PAYLOAD),revised)
        self.assertEqual(fake.calls[2][1]['validation_feedback'][0]['candidate'],VALID)

    def test_dedicated_client_same_ds_and_low_thinking(self):
        sent=[]
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def read(self,*args):return json.dumps({'model':'deepseek-flash','choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}).encode()
        def send(req,**kwargs):sent.append(json.loads(req.data));return Response()
        with patch('xiaopai_secretary._accepted_client.urllib.request.urlopen',send):
            deepseek_model('fixture-not-real').ask('test',{})
        self.assertEqual(sent[0]['model'],'deepseek-flash')
        self.assertEqual(sent[0]['thinking'],{'type':'enabled'})
        self.assertEqual(sent[0]['reasoning_effort'],'low')

class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'isolated'/ 'journal.db'
        self.journal=Journal(self.path)
        self.scope=Scope('fixture-owner','fixture-phone',True)
        self.payload=copy.deepcopy(PAYLOAD);self.decision=copy.deepcopy(VALID)
    def tearDown(self): self.tmp.cleanup()
    def stage(self,request=None,decision=None):
        payload=copy.deepcopy(self.payload)
        if request:payload['request_id']=request
        return self.journal.stage(self.scope,payload['request_id'],payload,decision or self.decision)[0]['id']
    def dispatch(self):
        oid=self.stage()
        return self.journal.claim(self.scope,oid,CAPS)
    def succeed(self):
        job=self.dispatch();ack=receipt(job)
        self.journal.acknowledge(self.scope,job['operation_id'],job['receipt_token'],ack)
        return job,ack
    def target_decision(self,tool,changes=None):
        d=copy.deepcopy(self.decision)
        d['actions']=[{k:v for k,v in d['actions'][0].items() if k in ('operation_id','source_ids','authorization','selection_explanation','presentation_ref')}]
        d['actions'][0].update(tool=tool,target_id='fixture-item')
        if changes is not None:d['actions'][0]['changes']=changes
        self.payload['existing_items']=self.journal.existing_items(self.scope)
        return d

    def test_raw_model_ids_preserved_but_execution_ids_host_allocated(self):
        before=copy.deepcopy(self.decision);oid=self.stage()
        self.assertNotEqual(oid,'ds-id')
        self.assertEqual(self.journal.get(self.scope,oid)['model_id'],'ds-id')
        self.assertEqual(before,self.decision)
    def test_same_request_idempotent_and_conflict_rejected(self):
        oid=self.stage();self.assertEqual(oid,self.stage())
        d=copy.deepcopy(self.decision);d['actions'][0]['title']='不同'
        with self.assertRaisesRegex(BoundaryError,'request_conflict'):self.stage(decision=d)
    def test_restart_preserves_dispatch_no_second_token(self):
        job=self.dispatch();reopened=Journal(self.path)
        with self.assertRaisesRegex(BoundaryError,'already_dispatched'):
            reopened.claim(self.scope,job['operation_id'],CAPS)
        self.assertFalse(reopened.model_receipt(self.scope,job['operation_id'])['verified_native_readback'])
    def test_competing_dispatches_only_one_wins(self):
        oid=self.stage()
        def claim(_):
            try:self.journal.claim(self.scope,oid,CAPS);return 'claimed'
            except BoundaryError:return 'blocked'
        with ThreadPoolExecutor(max_workers=4) as pool: answers=list(pool.map(claim,range(4)))
        self.assertEqual(answers.count('claimed'),1)
    def test_unknown_blocks_new_request_id_retry(self):
        job=self.dispatch();self.journal.mark_uncertain(self.scope,job['operation_id'])
        other=self.stage('new-turn')
        with self.assertRaisesRegex(BoundaryError,'reconcile'):self.journal.claim(self.scope,other,CAPS)
    def test_callback_success_cannot_report_native_success(self):
        job=self.dispatch();oid=job['operation_id']
        self.journal.mark_uncertain(self.scope,oid)
        self.assertEqual(self.journal.get(self.scope,oid)['state'],'unknown')
        self.assertFalse(self.journal.model_receipt(self.scope,oid)['verified_native_readback'])
    def test_targeted_readback_can_resolve_unknown(self):
        job=self.dispatch();self.journal.mark_uncertain(self.scope,job['operation_id'])
        actual=self.journal.acknowledge(self.scope,job['operation_id'],job['receipt_token'],receipt(job))
        self.assertEqual(actual['state'],'succeeded')
    def test_token_is_not_enough_for_other_device_or_revoked_device(self):
        job=self.dispatch()
        for scope in [Scope('fixture-owner','other-phone',True),Scope('other-owner','fixture-phone',True),Scope('fixture-owner','fixture-phone',False)]:
            with self.assertRaises(BoundaryError):self.journal.acknowledge(scope,job['operation_id'],job['receipt_token'],receipt(job))
    def test_bad_token_rejected(self):
        job=self.dispatch()
        with self.assertRaisesRegex(BoundaryError,'invalid_receipt_token'):
            self.journal.acknowledge(self.scope,job['operation_id'],'wrong',receipt(job))
    def test_missing_readback_or_wrong_action_cannot_succeed(self):
        job=self.dispatch()
        for change in ['missing','action','operation','target-marker','time','booking']:
            r=receipt(job)
            if change=='missing':r.pop('readback')
            elif change=='action':r['action_sha256']='wrong'
            elif change=='operation':r['operation_id']='wrong'
            elif change=='target-marker':r['readback']['operation_id']='wrong'
            elif change=='time':r['readback']['fields']['remind_at']='2026-10-23T08:00:00+08:00'
            else:r['readback']['booking']={'state':'purchased','evidence':'fake'}
            with self.subTest(change=change),self.assertRaises(BoundaryError):
                self.journal.acknowledge(self.scope,job['operation_id'],job['receipt_token'],r)
        self.assertEqual(self.journal.get(self.scope,job['operation_id'])['state'],'dispatched')
    def test_duplicate_receipt_idempotent_conflicting_receipt_rejected(self):
        job,r=self.succeed()
        actual=self.journal.acknowledge(self.scope,job['operation_id'],job['receipt_token'],r)
        self.assertEqual(actual['state'],'succeeded')
        r['native_id']='other'
        with self.assertRaisesRegex(BoundaryError,'receipt_conflict'):
            self.journal.acknowledge(self.scope,job['operation_id'],job['receipt_token'],r)
    def test_prepared_cancel_never_dispatched(self):
        oid=self.stage();self.journal.cancel_prepared(self.scope,oid)
        with self.assertRaises(BoundaryError):self.journal.claim(self.scope,oid,CAPS)
        self.assertFalse(self.journal.model_receipt(self.scope,oid)['verified_native_readback'])
    def test_cannot_locally_cancel_after_phone_delivery(self):
        job=self.dispatch()
        with self.assertRaises(BoundaryError):self.journal.cancel_prepared(self.scope,job['operation_id'])
    def test_unverified_adapter_disabled_without_parameter_changes(self):
        oid=self.stage()
        with self.assertRaisesRegex(BoundaryError,'unsupported_adapter_capability'):
            self.journal.claim(self.scope,oid,set())
        self.assertEqual(self.journal.get(self.scope,oid)['state'],'prepared')
    def test_nullable_fields_require_explicit_adapter_support(self):
        for missing,tool in [('nullable_event_end','create_event'),('undated_reminder','create_reminder'),('no_alert','create_reminder')]:
            d=copy.deepcopy(self.decision);a=d['actions'][0];a['tool']=tool;a['remind_at']=None
            if tool=='create_event':a.update(start_at='2026-10-22T08:00:00+08:00',end_at=None,location=None)
            oid=self.stage(request=missing,decision=d)
            with self.assertRaisesRegex(BoundaryError,missing):self.journal.claim(self.scope,oid,CAPS-{missing})
    def test_unowned_target_rejected_even_if_input_claims_it_exists(self):
        d=self.target_decision('delete_item');self.payload['existing_items']=[{'id':'fixture-item'}]
        oid=self.stage(decision=d)
        with self.assertRaisesRegex(BoundaryError,'target_not_owned'):self.journal.claim(self.scope,oid,CAPS)
    def test_create_update_delete_only_owned_objects_with_readback(self):
        self.succeed()
        d=self.target_decision('update_item',{'title':'新标题','remind_at':None})
        oid=self.stage('update-turn',d);job=self.journal.claim(self.scope,oid,CAPS)
        fields={'title':'新标题','remind_at':None,'notes':None}
        self.journal.acknowledge(self.scope,oid,job['receipt_token'],receipt(job,fields=fields))
        self.assertEqual(self.journal.existing_items(self.scope)[0]['title'],'新标题')
        d=self.target_decision('delete_item');oid=self.stage('delete-turn',d)
        job=self.journal.claim(self.scope,oid,CAPS)
        self.journal.acknowledge(self.scope,oid,job['receipt_token'],receipt(job,exists=False))
        self.assertEqual(self.journal.existing_items(self.scope),[])
    def test_reminder_cannot_accept_event_only_fields(self):
        self.succeed();d=self.target_decision('update_item',{'location':'车站'})
        oid=self.stage('update-turn',d)
        with self.assertRaisesRegex(BoundaryError,'unsupported_target_field'):self.journal.claim(self.scope,oid,CAPS)
    def test_multi_action_request_is_staged_atomically_and_sequential(self):
        d=copy.deepcopy(self.decision);d['actions'].append(copy.deepcopy(d['actions'][0]))
        d['actions'][1]['operation_id']='second'
        ops=self.journal.stage(self.scope,PAYLOAD['request_id'],self.payload,d)
        with self.assertRaisesRegex(BoundaryError,'prior_operation'):self.journal.claim(self.scope,ops[1]['id'],CAPS)
        job=self.journal.claim(self.scope,ops[0]['id'],CAPS)
        self.journal.acknowledge(self.scope,job['operation_id'],job['receipt_token'],receipt(job))
        self.assertEqual(self.journal.claim(self.scope,ops[1]['id'],CAPS)['action']['operation_id'],'second')
    def test_invalid_decision_no_partial_database_stage(self):
        d=copy.deepcopy(self.decision);d['actions'].append({})
        with self.assertRaises(BoundaryError):self.stage(decision=d)
        self.stage()  # same request remains available, not partially inserted
    def test_event_booking_metadata_cannot_disappear_in_native_adapter(self):
        d=copy.deepcopy(self.decision);a=d['actions'][0]
        a.update(tool='create_event',start_at='2026-10-22T08:00:00+08:00',end_at=None,location='车站')
        oid=self.stage(decision=d);job=self.journal.claim(self.scope,oid,CAPS)
        r=receipt(job);r['readback']['booking']=None
        with self.assertRaisesRegex(BoundaryError,'native_readback_mismatch'):
            self.journal.acknowledge(self.scope,oid,job['receipt_token'],r)
    def test_native_no_write_proof_resolves_unknown_without_claiming_saved(self):
        job=self.dispatch();oid=job['operation_id'];self.journal.mark_uncertain(self.scope,oid)
        r={'protocol_version':1,'operation_id':oid,'action_sha256':job['action_sha256'],
           'outcome':'not_applied','native_id':None,'readback':{'operation_id':oid,'execution_state':'not_started',
           'exists':None,'fields':None,'booking':job['booking']}}
        bad=copy.deepcopy(r);bad['readback']['execution_state']='maybe_started'
        with self.assertRaisesRegex(BoundaryError,'missing_native_no_write_proof'):
            self.journal.acknowledge(self.scope,oid,job['receipt_token'],bad)
        self.journal.acknowledge(self.scope,oid,job['receipt_token'],r)
        self.assertEqual(self.journal.model_receipt(self.scope,oid)['status'],'error')
        self.assertFalse(self.journal.model_receipt(self.scope,oid)['verified_native_readback'])
        next_id=self.stage('after-proof')
        self.journal.claim(self.scope,next_id,CAPS)

    def test_model_receipt_uses_accepted_saved_protocol(self):
        job,_=self.succeed()
        actual=self.journal.model_receipt(self.scope,job['operation_id'])
        self.assertEqual(actual['id'],'ds-id')
        self.assertEqual(actual['status'],'saved')
        self.assertTrue(actual['verified_native_readback'])

    def test_db_file_private_on_posix(self):
        if os.name=='posix': self.assertEqual(self.path.stat().st_mode & 0o777,0o600)

class BridgeTests(unittest.TestCase):
    def test_url_contains_only_opaque_id_not_credentials_or_business_payload(self):
        oid='a'*32;url=shortcut_url('言小办 秘书',oid,'https://example.invalid/secretary/return')
        q=parse_qs(urlsplit(url).query)
        self.assertEqual(q['name'],['言小办 秘书'])
        self.assertEqual(json.loads(q['text'][0]),{'protocol_version':1,'operation_id':oid})
        self.assertNotIn('receipt_token',url)
        self.assertEqual(parse_qs(urlsplit(q['x-success'][0]).query)['transport'],['finished'])
    def test_no_plain_http_or_credential_return_url(self):
        for url in ['http://example.invalid/x','https://user:password@example.invalid/x','https://example.invalid/x?token=bad']:
            with self.assertRaises(ValueError):shortcut_url('言小办','a'*32,url)

if __name__=='__main__':unittest.main(verbosity=2)
