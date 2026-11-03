"""Authenticated homepage operations over the existing same-origin endpoint."""
import hashlib
import json
import re
import uuid
from datetime import datetime
from http.cookies import SimpleCookie
from general_dialogue import Conflict
from conversation_bridge import stage_call
from query_runtime import TZ


def owner_from_cookie(cookie):
    cookies = SimpleCookie(); cookies.load(cookie)
    device = cookies.get('pi_cc_device')
    if not device or not device.value:
        raise ValueError('missing_device')
    return hashlib.sha256(device.value.encode()).hexdigest()


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('invalid_identity')
    return value


def public_result(raw):
    result = json.loads(raw)
    fields = ('id','revision','requestId','text','reply','action','capability','state',
              'taskExecuted','tool','queryStatus','sources','missing','rail','weather','route','handoff','wechat','shopping','research','createdAt','commercial','memory')
    return {key:result[key] for key in fields if key in result}


def task_rows(bridge, owner, task_id, deleted=False):
    with bridge.conversation.connect() as c:
        hidden = bool(c.execute('SELECT 1 FROM deleted_sessions WHERE owner=? AND id=?',
                                (owner, task_id)).fetchone())
        if hidden != deleted: return []
        return [public_result(row['response']) for row in c.execute(
            'SELECT response FROM turns WHERE owner=? AND id=? ORDER BY rowid', (owner, task_id))]


def handle_home(bridge, cookie, body, token):
    if not isinstance(body, dict): raise ValueError('invalid_home_request')
    owner = owner_from_cookie(cookie)
    action = body.get('action')
    required = {'history': {'action','before'}, 'task': {'action','id'},
                'trash': {'action','before'}, 'delete': {'action','id'}, 'restore': {'action','id'},
                'text': {'action','id','revision','requestId','text'},
                'replay': {'action','id','requestId'},'wechat_status':{'action','id','requestId'},
                'shopping_status':{'action','id','requestId'},'research_status':{'action','id','requestId'}}
    required.update(memory_list={'action'}, memory_edit={'action','id','version','text'},
                    memory_forget={'action','id','version'}, memory_undo={'action','id','version'},
                    memory_tidy={'action'}, memory_apply={'action','version'})
    if action not in required or set(body) != required[action]: raise ValueError('invalid_home_request')
    if action.startswith('memory_'):
        memory = getattr(bridge.conversation, 'memory', None)
        if memory is None:
            raise ValueError('memory_unavailable')
        # Same ordering as voice/text; a management edit cannot race a model
        # turn and turn its acknowledgement into an uncommitted promise.
        if not bridge.slot.acquire(blocking=False): raise Conflict('conversation_busy')
        try:
            if not bridge.conversation.slot.acquire(blocking=False): raise Conflict('conversation_busy')
            try:
                return memory.manage(owner, body, bridge.conversation.model)
            finally:
                bridge.conversation.slot.release()
        finally:
            bridge.slot.release()
    if action in {'history', 'trash'}:
        before = body['before']
        if type(before) is not int or not 0 <= before <= 9223372036854775807: raise ValueError('invalid_cursor')
        with bridge.conversation.connect() as c:
            groups = c.execute('''SELECT t.id,MAX(t.rowid) AS position FROM turns t
                LEFT JOIN deleted_sessions d ON d.owner=t.owner AND d.id=t.id
                WHERE t.owner=? AND (d.id IS NOT NULL)=?
                GROUP BY t.id HAVING MAX(t.rowid) < ? ORDER BY position DESC LIMIT 21''',
                (owner, action == 'trash', before or 9223372036854775807)).fetchall()
        items = []
        for group in groups[:20]:
            rows = task_rows(bridge, owner, group['id'], deleted=action == 'trash')
            if not rows: continue
            latest = next((r for r in reversed(rows) if r.get('tool')), rows[-1])
            items.append({'id':group['id'], 'revision':rows[-1]['revision'],
                          'title':rows[0]['text'], 'result':latest})
        pending=[]
        if action=='history':
            with bridge.conversation.connect() as c:
                pending=[public_result(row[0]) for row in c.execute('''SELECT t.response FROM turns t
                    LEFT JOIN deleted_sessions d ON d.owner=t.owner AND d.id=t.id
                    WHERE t.owner=? AND d.id IS NULL
                    AND (json_extract(t.response,'$.shopping.state') IN ('queued','running','cancelling')
                         OR json_extract(t.response,'$.research.state') IN ('queued','running','cancelling')
                         OR json_extract(t.response,'$.shopping.needsDecision')=1
                         OR json_extract(t.response,'$.research.needsDecision')=1)
                    ORDER BY t.rowid DESC LIMIT 20''',(owner,))]
        return {'items':items, 'pending':pending, 'nextCursor':groups[19]['position'] if len(groups) > 20 else None}
    task_id = identity(body['id'])
    if action in {'delete', 'restore'}:
        # Same lock order as text/voice. A pending turn must finish before hiding it.
        if not bridge.slot.acquire(blocking=False): raise Conflict('conversation_busy')
        try:
            if not bridge.conversation.slot.acquire(blocking=False): raise Conflict('conversation_busy')
            try:
                with bridge.conversation.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if not c.execute('SELECT 1 FROM turns WHERE owner=? AND id=? LIMIT 1',
                                     (owner, task_id)).fetchone(): raise LookupError('result_not_found')
                    if action == 'delete':
                        c.execute('INSERT OR IGNORE INTO deleted_sessions VALUES(?,?,?)',
                                  (owner, task_id, datetime.now(TZ).isoformat()))
                    else:
                        c.execute('DELETE FROM deleted_sessions WHERE owner=? AND id=?', (owner, task_id))
                return {'id':task_id, 'deleted':action == 'delete'}
            finally: bridge.conversation.slot.release()
        finally: bridge.slot.release()
    if action == 'task':
        rows = task_rows(bridge, owner, task_id)
        if not rows: raise LookupError('result_not_found')
        return {'id':task_id, 'revision':rows[-1]['revision'], 'turns':rows}
    request_id = identity(body['requestId'])
    if action=='wechat_status':return refresh_wechat_receipt(bridge,cookie,owner,task_id,request_id)
    if action=='research_status':return refresh_shopping_receipt(bridge,cookie,owner,task_id,request_id,kind='research')
    if action=='shopping_status':return refresh_shopping_receipt(bridge,cookie,owner,task_id,request_id)
    if action == 'text':
        if type(body['revision']) is not int or body['revision'] < 0: raise ValueError('invalid_revision')
        text = body['text']
        if not isinstance(text,str) or not 1 <= len(text.strip()) <= 20000 or any(ord(c)<32 for c in text): raise ValueError('invalid_text')
    if not bridge.slot.acquire(blocking=False): raise Conflict('conversation_busy')
    try:
        if action == 'text':
            answer = stage_call('model', request_id, lambda: bridge.conversation.turn(
                owner, task_id, body['revision'], request_id, body['text'], cookie=cookie))
        else:
            bridge.conversation.ensure_active(owner, task_id)
            with bridge.conversation.connect() as c:
                row = c.execute('SELECT response FROM turns WHERE owner=? AND id=? AND request_id=?',
                                (owner,task_id,request_id)).fetchone()
            if not row: raise LookupError('result_not_found')
            answer = public_result(row['response'])
        return bridge.render(answer, token, request_id)
    finally:
        bridge.slot.release()


def refresh_wechat_receipt(bridge,cookie,owner,task_id,request_id):
    """Read one frozen send receipt. Never prepares/confirms or creates a dialogue turn."""
    conversation=bridge.conversation
    conversation.ensure_active(owner,task_id)
    with conversation.connect() as c:
        row=c.execute('SELECT response FROM turns WHERE owner=? AND id=? AND request_id=?',(owner,task_id,request_id)).fetchone()
    if not row:raise LookupError('result_not_found')
    answer=json.loads(row['response']);previous=answer.get('wechat',{})
    client=getattr(conversation,'wechat',None)
    if not client or previous.get('state') not in ('queued','sending') or not previous.get('draftId'):
        return public_result(row['response'])
    receipt=client.execute(cookie,task_id,uuid.uuid4().hex,'receipt','',draft=previous)
    # A transient transport error is not a terminal send result. Keep polling safely.
    if receipt.get('draftId')!=previous['draftId'] or receipt.get('state') not in ('queued','sending','sent','uncertain','cancelled','expired'):
        return public_result(row['response'])
    if receipt['state'] in ('queued','sending'):return public_result(row['response'])
    answer.update(wechat=receipt,reply=receipt['reply'],taskExecuted=receipt['state']=='sent',queryStatus='complete' if receipt['state']=='sent' else 'blocked')
    with conversation.connect() as c:
        c.execute('UPDATE turns SET response=? WHERE owner=? AND id=? AND request_id=? AND response=?',(json.dumps(answer,ensure_ascii=False),owner,task_id,request_id,row['response']))
        actual=c.execute('SELECT response FROM turns WHERE owner=? AND id=? AND request_id=?',(owner,task_id,request_id)).fetchone()
    return public_result(actual['response'])


def refresh_shopping_receipt(bridge,cookie,owner,task_id,request_id,kind='shopping'):
    """Poll the exact saved task. Viewing/replaying can never create another cart job."""
    conversation=bridge.conversation
    conversation.ensure_active(owner,task_id)
    with conversation.connect() as c:
        row=c.execute('SELECT response FROM turns WHERE owner=? AND id=? AND request_id=?',
                      (owner,task_id,request_id)).fetchone()
    if not row:raise LookupError('result_not_found')
    answer=json.loads(row['response']);previous=answer.get(kind,{})
    client=getattr(conversation,kind,None)
    if not client or (previous.get('state') not in ('queued','running','cancelling') and not previous.get('needsDecision')) or not previous.get('taskId'):
        return public_result(row['response'])
    receipt=client.execute(cookie,task_id,uuid.uuid4().hex,'status',task_id=previous['taskId'])
    if receipt.get('taskId')!=previous['taskId'] or receipt.get('state')=='uncertain':
        return public_result(row['response'])
    pending=receipt['state'] in ('queued','running','cancelling')
    answer[kind]=receipt
    if kind=='research':answer['sources']=receipt.get('sources',[])
    answer.update(reply=receipt['reply'],taskExecuted=receipt.get('completed') is True,
                  queryStatus='pending' if pending else 'complete' if receipt.get('completed') else 'partial')
    with conversation.connect() as c:
        c.execute('UPDATE turns SET response=? WHERE owner=? AND id=? AND request_id=? AND response=?',
                  (json.dumps(answer,ensure_ascii=False),owner,task_id,request_id,row['response']))
        actual=c.execute('SELECT response FROM turns WHERE owner=? AND id=? AND request_id=?',
                         (owner,task_id,request_id)).fetchone()
    return public_result(actual['response'])
