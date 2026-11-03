const fs=require('fs'),vm=require('vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/../web_shared/home-voice-v046.js','utf8');
function env(fetch){const root={Blob,AbortController,setTimeout,clearTimeout,setInterval,clearInterval,fetch,crypto:{randomUUID:()=> '012345ab-cdef-4567-89ab-012345abcdef'}};root.window=root;vm.runInNewContext(source,root);return root;}
test('65 seconds covers all samples; retries keep identity; intermediate responses cannot execute',async()=>{
 const calls=[];let fail=true;
 const r=env(async(url,o)=>{calls.push(o);if(fail){fail=false;throw new Error('network')};const part=+o.headers['X-Pi-Segment'];return {ok:true,json:async()=>part<2?{state:'segment_received',segment:part,taskExecuted:false}:{state:'ready'}}});
 const p=new r.PiVoiceProbe.VoiceProbe('/test',()=>{});const cap={id:'a',revision:30,requestId:'same',limited:false};
 await p.sendSegments(new Float32Array(65*16000),cap,0);
 assert.deepEqual(calls.map(c=>c.headers['X-Pi-Segment']),['0','0','1','2']);assert.ok(calls.every(c=>c.headers['X-Pi-Turn']==='same'&&c.headers['X-Pi-Revision']==='30'));
 assert.deepEqual(calls.slice(1).map(c=>(c.body.byteLength-44)/2/16000),[25,25,15]);
});
test('continuous speech not ended by old 20 second or 60 second idle timer',async()=>{
 const timers=[];const r=env();r.setTimeout=(fn,ms)=>{timers.push({fn,ms});return timers.length};r.clearTimeout=()=>{};
 r.navigator={mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop(){}}]})}};
 r.AudioContext=class{constructor(){this.state='running'}resume(){return Promise.resolve()}suspend(){return Promise.resolve()}};
 r.MediaRecorder=class{constructor(){this.state='inactive';this.mimeType='audio/mp4'}start(){this.state='recording'}stop(){this.state='inactive';this.onstop()}};
 const p=new r.PiVoiceProbe.VoiceProbe('/test',()=>{});p.watchPause=()=>{p.gate={heard:true}};let processed=0;p.process=()=>processed++;
 await p.start(false,false,{id:'a'.repeat(32),revision:30});timers.find(t=>t.ms===60000).fn();assert.equal(p.phase,'recording');assert.equal(processed,0);p.finish();assert.equal(processed,1);
});
test('segment cancellation never sends later segments',async()=>{
 let p,calls=0;const r=env(async()=>{calls++;p.rev++;return{ok:true,json:async()=>({state:'segment_received',segment:0,taskExecuted:false})}});p=new r.PiVoiceProbe.VoiceProbe('/test',()=>{});
 await assert.rejects(p.sendSegments(new Float32Array(60*16000),{id:'a',revision:0,requestId:'r'},0));assert.equal(calls,1);
});
test('retry retains original capture revision and id',async()=>{
 const r=env();const p=new r.PiVoiceProbe.VoiceProbe('/test',()=>{});const blob=new Blob(['voice']);p.ctx={resume:async()=>{}};p.conversationId='a';p.conversationRevision=7;p.pendingCapture={blob,id:'a',revision:6,requestId:'old'};p.retryAvailable=true;p.cleanup=()=>{};p.process=async(b)=>{assert.equal(b,blob);assert.equal(p.pendingCapture.requestId,'old');assert.equal(p.pendingCapture.revision,6)};await p.retryLast();
});
async function processFixture(response){
 const r=env();r.atob=s=>Buffer.from(s,'base64').toString('binary');
 r.OfflineAudioContext=class{constructor(){this.destination={}}createBufferSource(){return {connect(){},start(){}}}async startRendering(){return {getChannelData:()=>new Float32Array(16000)}}};
 const p=new r.PiVoiceProbe.VoiceProbe('/test',()=>{});p.ctx={state:'running',decodeAudioData:async()=>({duration:1})};p.conversationId='a'.repeat(32);p.conversationRevision=30;p.play=()=>{};p.sendSegments=async(s,c)=>({...response,id:c.id,requestId:c.requestId,revision:c.revision+1});
 await p.process(new Blob(['audio']),0);return p;
}
test('completed research and shopping receipts reach playback, not false mismatch',async()=>{
 for(const tool of ['xiaohongshu','flight','taobao_instant','meituan','jd']){
  const p=await processFixture({state:'ready',spoken:'已经完成',text:'查询进度',mime:'audio/wav',audio:'AAAA',queryStatus:'complete',taskExecuted:true,tool,research:{completed:true},shopping:{completed:true}});
  assert.equal(p.phase,'ready',tool);assert.equal(p.conversationRevision,31);assert.equal(p.pendingCapture,null);
 }
});
test('inconsistent completion remains rejected and recording retained',async()=>{
 const p=await processFixture({state:'ready',spoken:'完成',text:'查询',mime:'audio/wav',audio:'AAAA',queryStatus:'complete',taskExecuted:true,tool:'flight',research:{completed:false}});
 assert.equal(p.phase,'error');assert.ok(p.pendingCapture);assert.equal(p.retryAvailable,true);
});
test('TTS failure preserves capture for same-turn audio recovery',async()=>{
 const p=await processFixture({state:'text_only',spoken:'回答',text:'问题',message:'声音暂不可用',taskExecuted:false});
 assert.equal(p.conversationRevision,31);assert.equal(p.pendingCapture.revision,30);assert.equal(p.retryAvailable,true);
});
