"""Every new reply, no presentation HTTP request, no format trigger words."""
import hashlib,json,sqlite3,threading,time
from pathlib import Path
from unittest.mock import patch
from test_common_voice import SharedVoiceTests


class AutomaticTests(SharedVoiceTests):
    def checks(self):
        with sqlite3.connect(Path(self.directory.name)/'history.db') as c:
            return c.execute('SELECT state FROM presentation_checks').fetchall()

    def test_json_and_audio_automatically_check_without_any_browser_subscription(self):
        calls=[]
        def lines(model,payload,timeout=10):
            calls.append(payload);yield {'mode':'components'}
            yield {'component':'sections','refs':['section:0']};yield {'done':True}
        with patch('presentation.model_lines',lines):
            body={'action':'text','id':'3'*32,'revision':0,'requestId':'4'*32,'text':'成都有哪些博物馆'}
            self.assertEqual(self.post(body)[0],200)
            headers={**self.headers,'Content-Type':'audio/wav','X-Pi-Conversation':'5'*32,
                     'X-Pi-Revision':'0','X-Pi-Turn':'6'*32,'X-Pi-Segment':'0','X-Pi-Segments':'1'}
            self.assertEqual(self.request('POST','/api/roundtrip',self.wav,headers)[0],200)
            for _ in range(100):
                states=self.checks()
                if len(states)==2 and all(s[0]=='complete' for s in states):break
                time.sleep(.01)
            self.assertEqual(states,[('complete',),('complete',)])
            self.assertEqual(len(calls),2)
            self.assertTrue(all('卡片' not in p['utterance'] for p in calls))
            self.assertTrue(all('allowed_refs' in p['components']['sections'] for p in calls))
            self.post(body);self.post({'action':'history','before':0})
            self.post({'action':'replay','id':body['id'],'requestId':body['requestId']})
            self.assertEqual(len(calls),2);self.assertEqual(len(self.search.calls),2)

    def test_slow_layout_does_not_delay_audio_or_need_a_subscriber(self):
        entered,release=threading.Event(),threading.Event()
        def lines(*args,**kwargs):
            entered.set();release.wait(4);yield {'mode':'plain'};yield {'done':True}
        body={'action':'text','id':'9'*32,'revision':0,'requestId':'a'*32,'text':'成都有哪些博物馆'}
        try:
            with patch('presentation.model_lines',lines):
                status,answer=self.post(body)
                self.assertEqual((status,answer['state']),(200,'ready'))
                self.assertTrue(entered.wait(1));self.assertFalse(release.is_set())
                release.set()
                for _ in range(100):
                    states=self.checks()
                    if states==[('complete',)]:break
                    time.sleep(.01)
                self.assertEqual(states,[('complete',)])
        finally:release.set()

    def test_browser_cannot_start_a_check_for_unchecked_old_record(self):
        owner=hashlib.sha256(self.headers['Cookie'].split('=',1)[1].encode()).hexdigest()
        r={'id':'7'*32,'requestId':'8'*32,'revision':1,'text':'旧结果','reply':'已保存'}
        with sqlite3.connect(Path(self.directory.name)/'history.db') as c:
            c.execute('INSERT INTO turns VALUES(?,?,?,?,?)',(owner,r['id'],r['requestId'],'fixture',json.dumps(r)))
        with patch('presentation.model_lines',side_effect=AssertionError('Browser cannot start DS')):
            status,_,raw=self.request('POST','/api/presentation',json.dumps({'id':r['id'],'requestId':r['requestId']}),self.headers)
        self.assertEqual(status,200);self.assertEqual(json.loads(raw)['event'],'fallback')
