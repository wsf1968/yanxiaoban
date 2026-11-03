const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/../web_shared/home-app-v050.js','utf8');
const functions=source.slice(source.indexOf('  function resultTitle('),source.indexOf('  function renderCurrent('));
const context=vm.createContext({URL,timeLabel:()=> '测试时间',icon:()=> '',
 esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))});
vm.runInContext(functions,context);
test('city route heading, precise time and matching count appear with real station cards',()=>{
 const result={text:'明天下午北京到上海',rail:{date:'2026-10-21',trains:[{train:'G1',origin:'北京南',destination:'上海虹桥',depart:'14:00',arrive:'18:30',date:'2026-10-21',seat:'二等座',availability:'有',price:'500',durationMinutes:270}]},commercial:{kind:'train',request:{departure_station:'北京',arrival_station:'上海'},time_label:'14:00–17:00出发',total_matches:9}};
 const html=context.bodyHTML(result);
 assert.match(html,/北京.*上海/);assert.match(html,/14:00–17:00出发/);assert.match(html,/符合条件9班/);assert.match(html,/上海虹桥/);
 const empty=context.bodyHTML({...result,reply:'指定时段没有车次',rail:{date:result.rail.date,trains:[]}});
 assert.match(empty,/指定时段没有车次/);assert.doesNotMatch(empty,/selected-train/);
});
test('travel presents the sources selected by model citations and folds uncited sources; unsafe URLs are not links',()=>{
 const result={commercial:{kind:'travel',summary:{sections:[{items:[{evidence:[{source_id:'2'},{source_id:'5'}]}]}]}},sources:Array.from({length:8},(_,i)=>({id:String(i+1),title:'来源'+i,url:i===0?'javascript:alert(1)':'https://example.com/'+i}))};
 const html=context.sourceHTML(result),visible=html.split('<details>')[0];
 assert.equal((visible.match(/result-source/g)||[]).length,2);
 assert.match(html,/其他检索来源（6）/);assert.doesNotMatch(html,/href="javascript:/);
});
