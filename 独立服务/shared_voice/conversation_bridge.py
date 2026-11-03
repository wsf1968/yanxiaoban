"""ASR -> general GLM conversation -> TTS. Auth is enforced by gateway first."""
import hashlib
import json
import re
import threading
import time
import urllib.request
from http.cookies import SimpleCookie
from general_dialogue import Conflict
from speech_engine import read_pcm
from speech_numbers import speech_for_tts


class ConversationFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def stage_call(stage, request_id, call):
    started = time.monotonic()
    def event(status, code=None):
        row = {'event':'voice_stage','stage':stage,'request':request_id[:8],
               'status':status,'seconds':round(time.monotonic()-started,3)}
        if code: row['error'] = code
        print(json.dumps(row), flush=True)
    event('started')
    try:
        result = call()
        event('completed')
        return result
    except Conflict:
        event('failed','conversation_conflict')
        raise
    except Exception as error:
        allowed = {'model_timeout','model_busy','model_http_error','model_connection_failed',
                   'model_invalid_response','model_failed','memory_capacity'}
        code = getattr(error,'code',None)
        if code not in allowed:
            code = stage + ('_timeout' if isinstance(error,TimeoutError) else '_failed')
            if stage == 'model' and isinstance(error,ValueError): code = 'model_invalid_reply'
        event('failed',code)
        raise ConversationFailure(code) from None


def air_call(path, body, content_type, token, port=18862):
    req = urllib.request.Request('http://127.0.0.1:%d/v1/%s' % (port, path), data=body,
        headers={'Content-Type': content_type, 'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError('oversized_response')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('invalid_response')
        return result


class Bridge:
    def __init__(self, conversation, call=air_call):
        self.conversation = conversation
        self.call = call
        self.slot = threading.Lock()
        with conversation.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS recordings
                (owner TEXT,id TEXT,request_id TEXT,fingerprint TEXT,text TEXT,
                 PRIMARY KEY(owner,id,request_id))''')
            if 'endpoint_complete' not in {r[1] for r in c.execute('PRAGMA table_info(recordings)')}:
                c.execute('ALTER TABLE recordings ADD COLUMN endpoint_complete INTEGER NOT NULL DEFAULT 1')

        with conversation.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS recording_segments (owner TEXT,id TEXT,request_id TEXT,idx INTEGER,count INTEGER,revision TEXT,fingerprint TEXT,text TEXT,tail REAL,PRIMARY KEY(owner,id,request_id,idx))')

    def reply(self, cookie, raw, token, identity, revision, request_id, *, capture_limited=False, segment_index=0, segment_count=1):
        if any(not isinstance(x, str) or not re.fullmatch(r'[a-f0-9]{32}', x) for x in (identity, request_id)):
            raise ValueError('invalid_identity')
        if not isinstance(revision, str) or not re.fullmatch(r'\d{1,12}', revision):
            raise ValueError('invalid_revision')
        cookies = SimpleCookie(); cookies.load(cookie)
        device = cookies.get('pi_cc_device')
        if not device or not device.value:
            raise ValueError('missing_device')
        owner = hashlib.sha256(device.value.encode()).hexdigest()
        fingerprint = hashlib.sha256(revision.encode() + b':' + raw + (b':limit' if capture_limited else b'')).hexdigest()
        if type(segment_index) is not int or type(segment_count) is not int or not 0 <= segment_index < segment_count <= 1000:
            raise ValueError('invalid_segment')
        read_pcm(raw)
        if not self.slot.acquire(blocking=False):
            raise Conflict('conversation_busy')
        try:
            self.conversation.ensure_active(owner, identity)
            with self.conversation.connect() as c:
                record = c.execute('SELECT * FROM recordings WHERE owner=? AND id=? AND request_id=?',
                                   (owner, identity, request_id)).fetchone()
                session = c.execute('SELECT revision FROM sessions WHERE owner=? AND id=?', (owner, identity)).fetchone()
            if record and segment_index==segment_count-1 and record['fingerprint'] != fingerprint:
                raise Conflict('request_id_reused')
            if not record and ((session and session['revision'] != int(revision)) or (not session and int(revision) != 0)):
                raise Conflict('stale_revision')
            assembled=None
            if segment_count>1:
                with self.conversation.connect() as c:
                    parts=c.execute('SELECT * FROM recording_segments WHERE owner=? AND id=? AND request_id=? ORDER BY idx',(owner,identity,request_id)).fetchall()
                if any(x['count']!=segment_count or x['revision']!=revision for x in parts):raise Conflict('segment_metadata_changed')
                part=next((x for x in parts if x['idx']==segment_index),None)
                if part and part['fingerprint']!=fingerprint:raise Conflict('segment_changed')
                if not part:
                    if segment_index!=len(parts):raise Conflict('segment_out_of_order')
                    result=stage_call('asr',request_id,lambda:self.call('transcribe',raw,'audio/wav',token))
                    text=result.get('text');tail=result.get('tailSilenceSeconds')
                    if result.get('state') not in ('transcribed','no_speech') or not isinstance(text,str) or len(text)>20000:raise ValueError('invalid_transcript')
                    with self.conversation.connect() as c:
                        c.execute('INSERT INTO recording_segments VALUES(?,?,?,?,?,?,?,?,?)',(owner,identity,request_id,segment_index,segment_count,revision,fingerprint,text,tail))
                if segment_index<segment_count-1:
                    return {'state':'segment_received','segment':segment_index,'taskExecuted':False}
                with self.conversation.connect() as c:
                    parts=c.execute('SELECT * FROM recording_segments WHERE owner=? AND id=? AND request_id=? ORDER BY idx',(owner,identity,request_id)).fetchall()
                if len(parts)!=segment_count:raise Conflict('segments_incomplete')
                assembled={'state':'transcribed','text':' '.join(x['text'].strip() for x in parts).strip(),'tailSilenceSeconds':parts[-1]['tail']}
                if not assembled['text']:assembled['state']='no_speech'
            if record:
                text = record['text']
                endpoint_complete = bool(record['endpoint_complete'])
            else:
                result = assembled if assembled is not None else stage_call('asr', request_id, lambda: self.call('transcribe', raw, 'audio/wav', token))
                text = result.get('text')
                if result.get('state') == 'no_speech' and text == '':
                    return {'state': 'no_speech', 'text': '', 'audio': None,
                            'message': '没有识别到清楚的人声，请再说一次。', 'taskExecuted': False}
                if result.get('state') != 'transcribed' or not isinstance(text, str) or not 1 <= len(text.strip()) <= 20000:
                    raise ValueError('invalid_transcript')
                tail=result.get('tailSilenceSeconds')
                endpoint_complete = not capture_limited or (type(tail) in (int,float) and .8 <= tail <= 30)
                with self.conversation.connect() as c:
                    c.execute('INSERT INTO recordings(owner,id,request_id,fingerprint,text,endpoint_complete) VALUES(?,?,?,?,?,?)', (owner, identity, request_id, fingerprint, text, int(endpoint_complete)))
            answer = stage_call('model', request_id,
                lambda: self.conversation.turn(owner, identity, int(revision), request_id, text, cookie=cookie, capture_incomplete=not endpoint_complete))
            return self.render(answer, token, request_id)
        finally:
            self.slot.release()

    def render(self, answer, token, request_id):
        # Commit text before speech synthesis. Retrying the same request can
        # recover audio without asking GLM again or advancing history twice.
        # Speech-only formatting layer: reply/text/history stay untouched,
        # only the text sent to TTS is rewritten deterministically.
        speech_text = speech_for_tts(answer['reply'], answer.get('text') or '',
                                     rail=(answer.get('tool') == 'rail'), flight=(answer.get('tool') == 'flight'))
        # Engine accepts at most 350 chars per synthesize call. If the reply
        # itself fits but speech-only expansion overflows (number-dense long
        # replies), fall back to the original reply so TTS still runs in one
        # call with the engine's own reading; never truncate or split.
        if len(answer['reply']) <= 350 and len(speech_text) > 350:
            speech_text = answer['reply']
        try:
            audio = stage_call('tts', request_id, lambda: self.call('synthesize',
                json.dumps({'text': speech_text}, ensure_ascii=False).encode(), 'application/json', token))
            if not isinstance(audio.get('audio'), str) or not audio['audio'] or audio.get('mime') != 'audio/wav':
                raise ValueError('invalid_audio_reply')
            return {**answer, 'audio': audio['audio'], 'mime': audio['mime'],
                    'state': 'ready', 'spoken': answer['reply'], 'speechText': speech_text}
        except Exception:
            return {**answer, 'state': 'text_only', 'audio': None, 'spoken': answer['reply'],
                    'message': '回复已生成，但声音暂不可用；请查看文字。'}
