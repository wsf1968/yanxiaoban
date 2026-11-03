const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const root={TextDecoder,AbortController,setTimeout,clearTimeout,URL,document:{baseURI:'https://example.com/cloud-computer/commercial-test/'}};root.window=root;
vm.runInNewContext(fs.readFileSync(__dirname+'/../web_shared/presentation-v1.js','utf8'),root);
const ui=root.PiPresentation;
const result={id:'a'.repeat(32),requestId:'b'.repeat(32),reply:'DS原文',commercial:{kind:'shopping',products:[{title:'<甲>',price:'199'},{title:'乙',price:'299'}],scope:'仅已授权候选'}};
const layout={version:1,mode:'components',blocks:[{component:'product_cards',refs:['product:1','product:0']}],complete:true};
test('model order survives; every original detail and scope remain accessible',()=>{
 const html=ui.render({...result,presentation:layout},()=>'<p>完整原始结果与条件</p>',{title:()=> '商品挑选',productCard:p=>p.title.replace('<','&lt;').replace('>','&gt;')});
 assert.ok(html.indexOf('乙')<html.indexOf('&lt;甲&gt;'));
 assert.match(html,/DS原文/);assert.match(html,/仅已授权候选/);assert.match(html,/完整原始结果与条件/);assert.match(html,/查看完整结果/);
});
test('bad references, invented components and duplicate refs fall back without changing facts',()=>{
 for(const blocks of [[{component:'script',refs:['reply']}],[{component:'product_cards',refs:['product:9']}],[{component:'text',refs:['product:0']}],[{component:'text',refs:['reply','reply']}]])
  assert.equal(ui.render({...result,presentation:{...layout,blocks}},()=> '原始回答',{}),'原始回答');
});
test('plain mode adds no cards but keeps complete structured records available',()=>{
 const html=ui.render({...result,presentation:{version:1,mode:'plain',blocks:[],complete:true}},()=> '原始详细金额与商品', {title:()=> '结果'});
 assert.match(html,/DS原文/);assert.match(html,/原始详细金额与商品/);assert.doesNotMatch(html,/product-list/);
});
test('arbitrary UTF8 boundaries render only full events and do not wait for audio to show committed result',async()=>{
 const encoder=new TextEncoder(),bytes=encoder.encode(JSON.stringify({event:'result',result:{reply:'汉字'}})+'\n'+JSON.stringify({event:'complete',result:{state:'ready',audio:'AAA'}})+'\n');
 const seen=[];
 const response={headers:{get:()=> 'application/x-ndjson'},body:new ReadableStream({start(c){for(let i=0;i<bytes.length;i+=2)c.enqueue(bytes.slice(i,i+2));c.close();}})};
 const final=await ui.roundtrip(response,r=>seen.push(r.reply));assert.deepEqual(seen,['汉字']);assert.equal(final.state,'ready');
});
test('interrupted transport never fabricates a final audio reply',async()=>{
 const response={headers:{get:()=> 'application/x-ndjson'},text:async()=>JSON.stringify({event:'result',result:{reply:'已保存'}})+'\n'};
 await assert.rejects(ui.roundtrip(response,()=>{}),/连接中断/);
});
test('large currency values remain exact; calendar times have explicit timezone; untrusted titles escaped',()=>{
 const r={reply:'查询结果',commercial:{kind:'bookkeeping',details:[{result:{rows:[{date:'2026-10-27',amount_fen:'9007199254740991',notes:'<script>'}]}}]},presentation:{version:1,mode:'components',blocks:[{component:'ledger',refs:['ledger:0']}],complete:true}};
 const html=ui.render(r,()=> '原始',{title:()=> '账目'});assert.match(html,/90071992547409\.91/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
 const a={reply:'实际回执',commercial:{kind:'secretary',items:[{title:'会议',start_at:'2026-10-28T01:00:00Z',end_at:'2026-10-28T02:00:00Z'}]},presentation:{version:1,mode:'components',blocks:[{component:'agenda',refs:['agenda:0']}],complete:true}};
 const agenda=ui.render(a,()=> '完整',{title:()=> '日程'});assert.match(agenda,/9:00:00|09:00:00/);assert.match(agenda,/10:00:00/);assert.match(agenda,/北京时间/);assert.doesNotMatch(agenda,/已通知/);
});
