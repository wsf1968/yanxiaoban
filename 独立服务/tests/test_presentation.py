"""Presentation boundary, real HTTP streaming and exactly-once business regression."""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

import test_http
from test_common_voice import SharedVoiceTests
from presentation import Layout, Presenter, catalog, model_lines


class LayoutTests(unittest.TestCase):
    def test_reference_validation_cannot_inject_facts_or_executable_ui(self):
        result={'reply':'原文','commercial':{'products':[{'price':'99'}]}}
        for row in [
            {'component':'product_cards','refs':['product:8']},
            {'component':'text','refs':['product:0']},
            {'component':'script','refs':['reply']},
            {'component':'text','refs':['reply'],'text':'已经付款'},
            {'component':'product_cards','refs':['product:0','product:0']},
        ]:
            layout=Layout(catalog(result));layout.accept({'mode':'components'})
            with self.assertRaises(ValueError):layout.accept(row)
        layout=Layout(catalog(result));layout.accept({'mode':'plain'});layout.accept({'done':True})
        self.assertEqual(layout.value()['blocks'],[])

    def test_other_owner_deleted_task_and_changed_receipt_never_receive_cached_layout(self):
        from general_dialogue import Conversation
        from general_dialogue import Conflict
        with tempfile.TemporaryDirectory() as directory:
            conversation=Conversation(Path(directory)/'test.db',None)
            result={'id':'a'*32,'requestId':'b'*32,'reply':'真实回执','revision':1}
            owner='c'*64;args=(owner,result['id'],result['requestId'])
            with conversation.connect() as c:
                c.execute('INSERT INTO turns VALUES(?,?,?,?,?)',(*args,'fixture',json.dumps(result)))
            calls=[]
            def lines(payload):
                calls.append(payload);yield {'mode':'plain'};yield {'done':True}
            presenter=Presenter(conversation,lines);events=[]
            presenter.ensure(*args);presenter.stream(*args,events.append)
            self.assertTrue(events[-1]['layout']['complete'])
            with self.assertRaises(LookupError):presenter.load('d'*64,*args[1:])
            result['reply']='新的实际回执'
            with conversation.connect() as c:
                c.execute('UPDATE turns SET response=?',(json.dumps(result),))
            events=[];presenter.stream(*args,events.append,cached_only=True)
            self.assertEqual(events,[{'event':'fallback'}]);self.assertEqual(len(calls),1)
            with conversation.connect() as c:c.execute('INSERT INTO deleted_sessions VALUES(?,?,?)',(*args[:2],'now'))
            with self.assertRaises(Conflict):presenter.load(*args)


class PresentationHTTPTests(SharedVoiceTests):
    def test_stream_result_precedes_tts_and_retries_never_execute_again(self):
        from unittest.mock import patch
        from commercial_voice import PresentingBridge
        release, entered = threading.Event(), threading.Event()
        original=PresentingBridge.render
        def slow_speech(bridge,answer,token,request_id):
            old=bridge.call
            def call(path,*args):
                if path=='synthesize':
                    entered.set();release.wait(3)
                return old(path,*args)
            bridge.call=call
            try:return original(bridge,answer,token,request_id)
            finally:bridge.call=old
        body={'action':'text','id':'a'*32,'revision':0,'requestId':'b'*32,'text':'成都有哪些博物馆'}
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        try:
            with patch.object(PresentingBridge,'render',slow_speech):
                conn.request('POST','/api/roundtrip',json.dumps(body),{**self.headers,'Accept':'application/x-ndjson'})
                response=conn.getresponse()
                self.assertEqual(response.status,200)
                self.assertEqual(response.getheader('X-Accel-Buffering'),'no')
                first=json.loads(response.readline())
                self.assertEqual(first['event'],'result')
                self.assertNotIn('audio',first['result'])
                self.assertTrue(entered.wait(1))
                # Still synthesizing, yet a committed result is already visible.
                self.assertFalse(release.is_set())
                release.set()
                final=json.loads(response.readline())
                self.assertEqual(final['result']['state'],'ready')
                self.assertEqual(first['result']['reply'],final['result']['spoken'])
                self.assertEqual(response.read(),b'')
            self.assertEqual(self.post(body)[0],200)
            self.assertEqual(len(self.search.calls),1)
            self.assertEqual(self.model.calls,1)
        finally:release.set();conn.close()

    def test_local_https_proxy_does_not_buffer_progress_until_tts_finishes(self):
        from unittest.mock import patch
        from http.server import ThreadingHTTPServer
        from local_https import proxy_handler
        from commercial_voice import PresentingBridge
        release=threading.Event();original=PresentingBridge.render
        def delayed(bridge,answer,token,request_id):
            callback=getattr(bridge.observer,'callback',None)
            if callback:callback(answer)
            release.wait(3)
            return original(bridge,answer,token,request_id)
        proxy=ThreadingHTTPServer(('127.0.0.1',0),proxy_handler('https://xiaopai.example.invalid',self.port))
        threading.Thread(target=proxy.serve_forever,daemon=True).start()
        conn=http.client.HTTPConnection('127.0.0.1',proxy.server_port,timeout=5)
        body={'action':'text','id':'1'*32,'revision':0,'requestId':'2'*32,'text':'成都有哪些博物馆'}
        try:
            with patch.object(PresentingBridge,'render',delayed):
                conn.request('POST','/api/roundtrip',json.dumps(body),{**self.headers,'Host':'xiaopai.example.invalid','Origin':'https://xiaopai.example.invalid','Accept':'application/x-ndjson'})
                response=conn.getresponse()
                self.assertEqual(json.loads(response.readline())['event'],'result')
                self.assertFalse(release.is_set());release.set()
                self.assertIn(b'"complete"',response.read())
        finally:release.set();conn.close();proxy.shutdown();proxy.server_close()

    def test_presentation_endpoint_never_invokes_tools_and_cannot_read_other_owner(self):
        from unittest.mock import patch
        from presentation import Presenter
        calls=[]
        def lines(model,payload,timeout=10):
            calls.append(payload)
            yield {'mode':'components'}
            yield {'component':'itinerary','refs':['section:0']}
            yield {'done':True}
        body={'action':'text','id':'c'*32,'revision':0,'requestId':'d'*32,'text':'成都有哪些博物馆'}
        request=json.dumps({'id':body['id'],'requestId':body['requestId']})
        with patch('presentation.model_lines',lines):
            self.assertEqual(self.post(body)[0],200)
            for _ in range(2):
                status,_,raw=self.request('POST','/api/presentation',request,self.headers)
                events=[json.loads(row) for row in raw.splitlines()]
                self.assertEqual(status,200)
                self.assertTrue(events[-1]['layout']['complete'])
        self.assertEqual(len(calls),1)
        self.assertEqual(len(self.search.calls),1)
        self.assertEqual(self.model.calls,1)
        self.assertNotIn('personalMemory',calls[0])
        self.assertEqual(self.request('POST','/api/presentation',request,{**self.headers,'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('POST','/api/presentation',request,{'Content-Type':'application/json','Origin':self.headers['Origin']})[0],403)
        self.assertEqual(self.request('POST','/api/presentation',json.dumps({'id':'f'*32,'requestId':body['requestId']}),self.headers)[0],404)
        self.assertEqual(self.post({'action':'delete','id':body['id']})[0],200)
        self.assertEqual(self.request('POST','/api/presentation',request,self.headers)[0],400)

    def test_cached_history_does_not_trigger_new_model_and_bad_layout_is_fallback(self):
        from unittest.mock import patch
        body={'action':'text','id':'e'*32,'revision':0,'requestId':'f'*32,'text':'成都有哪些博物馆'}
        req={'id':body['id'],'requestId':body['requestId']}
        def bad(*args,**kwargs):
            yield {'mode':'components'}
            yield {'component':'text','refs':['reply']}
            yield {'component':'itinerary','refs':['section:999']}
        with patch('presentation.model_lines',bad) as model:
            self.post(body)
            status,_,raw=self.request('POST','/api/presentation',json.dumps({**req,'cached':True}),self.headers)
            self.assertIn(json.loads(raw)['event'],('fallback','layout'))
            status,_,raw=self.request('POST','/api/presentation',json.dumps(req),self.headers)
            self.assertEqual(json.loads(raw.splitlines()[-1])['event'],'fallback')
        _,history=self.post({'action':'history','before':0})
        self.assertIn('成都有博物馆',history['items'][0]['result']['reply'])
        self.assertEqual(self.model.calls,1)


class SSETests(unittest.TestCase):
    def test_model_stream_requires_complete_sse_finish_not_just_model_done_claim(self):
        from unittest.mock import patch
        class Model:url='https://api.deepseek.com/chat/completions';key='fixture';model='deepseek-flash'
        class Response:
            def __init__(self,rows):self.rows=iter(rows)
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def readline(self,*args):return next(self.rows,b'')
        def event(text,reason=None):
            return b'data: '+json.dumps({'choices':[{'delta':{'content':text},'finish_reason':reason}]}).encode()+b'\n'
        rows=[event('{"mode":"plain"}\n'),event('{"done":true}', 'stop')]
        with patch('urllib.request.urlopen',return_value=Response(rows)):
            with self.assertRaises(ValueError):list(model_lines(Model(),{}))
        with patch('urllib.request.urlopen',return_value=Response(rows+[b'data: [DONE]\n'])):
            self.assertEqual(list(model_lines(Model(),{})),[{'mode':'plain'},{'done':True}])


if __name__=='__main__':unittest.main()
