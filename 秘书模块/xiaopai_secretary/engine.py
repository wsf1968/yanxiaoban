"""Validated DS decision workflow, extracted without semantic changes from accepted runner.
No private config loading, HTTP listener, action execution, or live assistant import.
"""
from datetime import datetime
import json
from importlib.resources import files
import time
from jsonschema import Draft202012Validator

RESOURCES=files(__package__).joinpath("resources")
SCHEMA=json.loads(RESOURCES.joinpath("schema.json").read_text(encoding="utf-8"))
Draft202012Validator.check_schema(SCHEMA)
VALIDATOR=Draft202012Validator(SCHEMA)

def user_evidence(payload):
    return [payload['user']]+[h['content'] for h in payload.get('history',[])
        if h.get('role')=='user' and isinstance(h.get('content'),str)]

def protocol_errors(out,payload):
    errors=[str(list(e.absolute_path))+': '+e.message for e in VALIDATOR.iter_errors(out)]
    if errors:return errors
    texts=user_evidence(payload)
    actions=out['actions']
    if (out['decision']=='prepare')!=bool(actions):errors.append('decision/action envelope inconsistent')
    quote=out['booking']['evidence']
    if quote and not any(quote in text for text in texts):errors.append('booking evidence is not an exact user quote')
    known={s['id'] for s in payload.get('sources',[])}|{s['id'] for s in payload.get('existing_items',[])}|{payload['request_id']}
    item_ids={s['id'] for s in payload.get('existing_items',[])}
    ids=[]
    for action in actions:
        ids.append(action['operation_id'])
        if not any(action['authorization'] in text for text in texts):errors.append('authorization is not an exact user quote')
        if set(action['source_ids'])-known:errors.append('source_ids contain absent source')
        ref=action['presentation_ref']
        items=payload.get('presentation',{}).get('items',[])
        if ref is not None:
            matching=[item for item in items if item['index']==ref['index']]
            if len(matching)!=1 or matching[0]['source_id']!=ref['source_id']:
                errors.append('presentation_ref does not match actual displayed index/source binding: '+json.dumps(items,ensure_ascii=False))
            if ref['source_id'] not in action['source_ids']:errors.append('presentation_ref source must be in source_ids')
        elif action['tool']=='create_event' and items and set(action['source_ids'])&{i['source_id'] for i in items}:
            errors.append('event selecting a displayed source must declare presentation_ref')
        if 'target_id' in action and action['target_id'] not in item_ids:errors.append('target_id is absent from existing_items')
        params=action.get('changes',action)
        for key in ('start_at','end_at','remind_at'):
            if params.get(key) is not None:
                try:
                    if datetime.fromisoformat(params[key]).tzinfo is None:errors.append(key+' has no timezone')
                except (TypeError,ValueError):errors.append(key+' invalid ISO8601')
    if len(ids)!=len(set(ids)):errors.append('duplicate operation_id in one output')
    return errors

def ask_logged(model,system,payload,kind,trace):
    start=time.monotonic()
    out,usage=model.ask(system,payload,max_tokens=2200,timeout=55)
    trace.append({'kind':kind,'raw_output':out,'usage':usage,'seconds':round(time.monotonic()-start,3)})
    return out

def decide(model,payload,policy,auditor,trace):
    feedback=[]
    for attempt in range(3):
        request={'input':payload}
        if feedback:request['validation_feedback']=feedback
        out=ask_logged(model,policy,request,'decision',trace)
        errors=protocol_errors(out,payload)
        trace[-1]['protocol_errors']=errors
        if errors:
            feedback=[{'candidate':out,'issues':errors}]
            continue
        audit=ask_logged(model,auditor,{'input':payload,'candidate':out},'audit',trace)
        valid=(set(audit)=={'verdict','issues'} and audit.get('verdict') in ['accept','revise']
               and isinstance(audit.get('issues'),list) and all(isinstance(x,str) for x in audit['issues']))
        if valid and audit['verdict']=='accept' and not audit['issues']:
            return out
        feedback=[{'candidate':out,'issues':audit.get('issues') if valid else ['invalid auditor protocol']}]
    return None  # Fail closed; never programmatically substitute or "fix" DS content.


class Secretary:
    """Inject the existing model interface; call prepare with actual context only.

    The caller persists trace privately if required. A None result means no approved
    decision; this class never creates a replacement answer or dispatches an action.
    """
    def __init__(self, model, *, environment=None):
        self.model=model
        policy=RESOURCES.joinpath("policy.txt").read_text(encoding="utf-8")
        if environment:
            policy=environment+'\n'+policy.partition('\n')[2]
        self.policy=policy+"\n输出JSON Schema：\n"+json.dumps(SCHEMA,ensure_ascii=False)
        self.auditor=policy+"\n"+RESOURCES.joinpath("auditor.txt").read_text(encoding="utf-8")

    def prepare(self, payload, trace=None):
        if trace is None: trace=[]
        return decide(self.model,payload,self.policy,self.auditor,trace)
