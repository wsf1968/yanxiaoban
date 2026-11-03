const fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/../web_shared/home-app-v050.js','utf8');
const functions=source.slice(source.indexOf('  function resultTitle('),source.indexOf('  function renderCurrent('));
const context=vm.createContext({URL,timeLabel:()=> '测试时间',icon:()=> '',
 esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))});
vm.runInContext(functions,context);
const product={title:'<耳机>',shop:'测试店',platform:'jd',platform_label:'京东',price:371.37,coupon_price:323.09,
 rating_percent:99,review_count:100000,reason:'来自模型的选择理由',url:'https://union-click.jd.com/fixture',link_status:'ready'};
test('read-only product card keeps prices distinct, real metrics and user-tapped link',()=>{
 const result={reply:'模型口播',commercial:{kind:'shopping',title:'京东商品挑选',products:[product],scope:'仅本次候选'}};
 const html=context.bodyHTML(result);
 assert.match(html,/¥371.37/);assert.match(html,/优惠参考 ¥323.09/);assert.match(html,/需满足商品页优惠条件/);
 assert.match(html,/好评率 99%/);assert.match(html,/100,000/);assert.match(html,/到京东查看/);
 assert.match(html,/&lt;耳机&gt;/);assert.doesNotMatch(html,/已加车|购物车|<耳机>/);
});
test('unknown metrics are omitted, failed or unsafe links do not become purchase buttons',()=>{
 for(const change of [{url:'javascript:alert(1)'},{url:'https://user:pass@jd.com/'},{link_status:'unavailable'}]){
  const html=context.productCard({...product,...change,rating_percent:null,review_count:null});
  assert.doesNotMatch(html,/product-link" href=/);assert.match(html,/暂未取得/);assert.doesNotMatch(html,/好评率|评价 /);
 }
});
test('generic card envelope supports another registered provider without JD wording',()=>{
 const html=context.productCard({...product,platform:'taobao',platform_label:'淘宝',url:'https://example.com/fixture'});
 assert.match(html,/到淘宝查看/);assert.doesNotMatch(html,/京东|加车/);
});
test('commerce export stays plain text and does not execute or duplicate a purchase',()=>{
 const exportSource=source.slice(source.indexOf('  function exportResult('),source.indexOf('  async function openExport('));
 vm.runInContext(exportSource,context);context.statusName=()=> '查询完成';
 const out=context.exportResult({text:'耳机',reply:'DS口播',commercial:{kind:'shopping',title:'京东商品挑选',products:[product],scope:'本次候选'}});
 assert.match(out,/查看商品：https:\/\/union-click.jd.com/);assert.match(out,/页面报价：¥371.37/);
 assert.doesNotMatch(out,/<h3|已加车|购物车/);
});

test('Taobao conditional offers preserve labels and escaped source terms in card and export',()=>{
 const p={...product,platform:'taobao',platform_label:'淘宝',price:'299',coupon_price:'199',
  rating_percent:null,review_count:null,offer_label:'条件优惠参考',offer_notice:'会员、补贴有门槛',
  offer_terms:['88VIP：满300减100','<活动说明>','预估凑单参考：¥179；需凑单']};
 const html=context.productCard(p);
 assert.match(html,/页面报价/);assert.match(html,/条件优惠参考 ¥199/);assert.match(html,/会员、补贴有门槛/);
 assert.match(html,/平台优惠说明（不代表可叠加）/);assert.match(html,/满300减100/);
 assert.match(html,/&lt;活动说明&gt;/);assert.doesNotMatch(html,/好评率|评价 /);
 const out=context.exportResult({reply:'模型原文',commercial:{kind:'shopping',products:[p]}});
 assert.match(out,/条件优惠参考：¥199/);assert.match(out,/需凑单/);
});
