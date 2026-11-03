/* Optional presentation only. Trusted result data, existing card renderers, no eval. */
(() => {
  'use strict';
  const kinds={text:'reply',product_cards:'product',train_comparison:'train',itinerary:'section',sections:'section',ledger:'ledger',agenda:'agenda',details:'details'};
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const key=r=>r.id+':'+r.requestId;
  const cache=new Map(), pending=new Map();
  function catalog(r) {
    const c=r.commercial||{},out={reply:r.reply||''};
    for(const [prefix,rows] of [['product',c.products],['train',r.rail?.trains],['section',c.summary?.sections],['ledger',c.details],['agenda',c.items]])
      (Array.isArray(rows)?rows:[]).forEach((row,i)=>{out[prefix+':'+i]=row;});
    if(Object.keys(out).length===1)out.details={};
    return out;
  }
  function valid(layout,r) {
    if(!layout||layout.version!==1||!['plain','components'].includes(layout.mode)||!Array.isArray(layout.blocks)||layout.blocks.length>12)return false;
    if(layout.mode==='plain'&&layout.blocks.length)return false;
    const available=catalog(r),seen=new Set();
    for(const block of layout.blocks) {
      if(!block||!Object.hasOwn(kinds,block.component)||!Array.isArray(block.refs)||!block.refs.length)return false;
      for(const ref of block.refs) {
        if(typeof ref!=='string'||!Object.hasOwn(available,ref)||seen.has(ref)||ref.split(':')[0]!==kinds[block.component])return false;
        seen.add(ref);
      }
    }
    return !(layout.complete&&layout.mode==='components'&&!layout.blocks.length);
  }
  async function events(response,notify) {
    // A JSON response is supported for old servers and buffered transports.
    if(!response.headers?.get('Content-Type')?.startsWith('application/x-ndjson'))return response.json();
    let buffer='',total=0,last=null;
    function lines(text,flush=false) {
      buffer+=text;
      if(buffer.length>12000000)throw new Error('回复超过显示容量，原结果仍已保存。');
      const rows=buffer.split('\n');buffer=rows.pop();
      if(flush&&buffer){rows.push(buffer);buffer='';}
      for(const row of rows)if(row.trim()) {
        const event=JSON.parse(row);
        if(event.event==='error'){
          const error=new Error('这次请求未完成，原结果仍然保留。');error.code=event.error;
          error.status=event.error==='conversation_conflict'?409:event.error==='result_not_found'?404:event.error==='invalid_home_request'?400:503;
          throw error;
        }
        if(event.event==='complete')last=event.result;
        if(notify) {try{notify(event);}catch(_){/* Display cannot suppress speech. */}}
      }
    }
    if(response.body?.getReader) {
      const reader=response.body.getReader(),decoder=new TextDecoder();
      try {
        while(true) {
          const {value,done}=await reader.read();if(done)break;
          total+=value.byteLength;if(total>12000000)throw new Error('回复超过显示容量，原结果仍已保存。');
          lines(decoder.decode(value,{stream:true}));
        }
        lines(decoder.decode(),true);
      } catch(error) {await reader.cancel().catch(()=>{});throw error;}
      finally {reader.releaseLock();}
    } else lines(await response.text(),true);
    return last;
  }
  async function roundtrip(response,onResult) {
    const streaming=response.headers?.get('Content-Type')?.startsWith('application/x-ndjson');
    const result=await events(response,event=>{if(event.event==='result')onResult?.(event.result);});
    if(streaming&&!result)throw new Error('连接中断；可在历史记录查看已经保存的结果。');
    return result;
  }
  function start(r,notify,cachedOnly=false) {
    const id=key(r);
    if(cache.has(id)){notify(cache.get(id));return;}
    if(pending.has(id))return;
    const controller=new AbortController();pending.set(id,controller);
    const timer=setTimeout(()=>controller.abort(),12000);
    (async()=>{
      let completed=false;
      try {
        const response=await fetch(new URL('api/presentation',document.baseURI).pathname,{
          method:'POST',credentials:'same-origin',signal:controller.signal,
          headers:{'Content-Type':'application/json','X-PI-Request':'1'},
          body:JSON.stringify({id:r.id,requestId:r.requestId,...(cachedOnly?{cached:true}:{})})});
        if(!response.ok)throw new Error('presentation_unavailable');
        await events(response,event=>{
          if(event.event==='layout'&&valid(event.layout,r)) {
            notify(event.layout);
            if(event.layout.complete){completed=true;cache.set(id,event.layout);}
          } else if(event.event==='fallback'||event.event==='layout'&&!event.layout) {completed=true;notify(null);}
        });
        if(!completed)notify(null);
      } catch(_) {notify(null);}
      finally {clearTimeout(timer);if(pending.get(id)===controller)pending.delete(id);if(cache.size>60)cache.delete(cache.keys().next().value);}
    })();
  }
  function cancel() {for(const controller of pending.values())controller.abort();pending.clear();}
  const money=n=>{const v=BigInt(n),a=v<0n?-v:v;return (v<0n?'-':'')+String(a/100n)+'.'+String(a%100n).padStart(2,'0');};
  const date=v=>{const d=new Date(v);return Number.isFinite(d.getTime())?d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+'（北京时间）':v;};
  function ledger(data) {
    const r=data.result||{},s=r.summary,rows=r.rows||(r.transaction?[r.transaction]:[]);
    return `${s?`<div class="presentation-totals"><p>收入 <strong>¥${esc(money(s.income_fen))}</strong></p><p>净支出 <strong>¥${esc(money(s.expense_fen))}</strong></p><p>未分类入账 <strong>¥${esc(money(s.unclassified_inflow_fen))}</strong></p><p>结余 <strong>¥${esc(money(s.net_fen))}</strong></p></div>`:''}${rows.length?`<div class="presentation-table"><table class="detail-table"><thead><tr><th>日期</th><th>金额</th><th>说明</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${esc(row.date)}</td><td>¥${esc(money(row.amount_fen))}</td><td>${esc(row.notes||'流水')}</td></tr>`).join('')}</tbody></table></div>`:''}`;
  }
  function agenda(item) {
    return `<article class="presentation-agenda"><h4>${esc(item.title)}</h4>${[['开始',item.start_at],['结束',item.end_at],['提醒',item.remind_at]].filter(([,v])=>v).map(([label,value])=>`<p>${label}：${esc(date(value))}</p>`).join('')}${item.notes?`<p class="document-copy">${esc(item.notes)}</p>`:''}</article>`;
  }
  function notices(r) {
    const c=r.commercial||{},items=[c.scope,c.message,c.notice,...(c.summary?.uncertainties||[])];
    if(r.rail)items.push('聚合数据查询快照，票价余票以购票页为准。');
    if(c.kind==='secretary')items.push('系统事项通过iCloud同步。是否已同步到手机及到点通知，请在iPhone提醒事项或日历中核对。');
    return items.filter(v=>typeof v==='string'&&v).map(v=>`<p class="data-warning">${esc(v)}</p>`).join('');
  }
  function render(r,legacy,helpers) {
    const layout=r.presentation;if(!valid(layout,r))return legacy();
    try {
      const values=catalog(r),hasFacts=Object.keys(values).some(k=>k!=='reply'&&k!=='details');
      if(layout.mode==='plain'&&!hasFacts)return legacy();
      let blocks=[];
      if(layout.mode==='components')blocks=layout.blocks.map(b=>{
        const rows=b.refs.map(ref=>values[ref]);
        switch(b.component) {
          case 'text':return `<p class="document-copy">${esc(r.reply)}</p>`;
          case 'product_cards':return `<div class="product-list">${rows.map(helpers.productCard).join('')}</div>`;
          case 'train_comparison':return `<div class="presentation-trains">${rows.map(row=>helpers.trainCard(row,true)).join('')}</div>`;
          case 'sections':
          case 'itinerary':return rows.map(s=>`<section class="presentation-day"><h4>${esc(s.heading)}</h4>${(s.items||[]).map(i=>`<p class="document-copy">${esc(i.text)} ${[...new Set((i.evidence||[]).map(e=>e.source_id))].map(id=>'['+esc(id)+']').join('')}</p>`).join('')}</section>`).join('');
          case 'ledger':return rows.map(ledger).join('');
          case 'agenda':return rows.map(agenda).join('');
          case 'details':return legacy();
          default:return '';
        }
      });
      if(!layout.blocks.some(b=>b.refs.includes('reply')||b.component==='details'))blocks.unshift(`<p class="document-copy">${esc(r.reply)}</p>`);
      if(!blocks.length)return legacy();
      const full=layout.blocks.some(b=>b.component==='details')?'':`<details class="presentation-original"><summary>查看完整结果</summary>${legacy()}</details>`;
      const context=r.rail?`<p class="query-context">${esc(r.rail.date)} · ${esc(r.commercial?.time_label||'全天')}${r.commercial?.total_matches!=null?' · 符合条件'+esc(r.commercial.total_matches)+'班':''}</p>`:'';
      return `<div class="presentation-view"><h3 class="document-title">${esc(helpers.title(r))}</h3>${context}${r.text?`<p class="followup-note">你说：${esc(r.text)}</p>`:''}${blocks.map(html=>`<div class="presentation-block">${html}</div>`).join('')}${notices(r)}${full}</div>`;
    } catch(_) {return legacy();}
  }
  window.PiPresentation={catalog,valid,render,events,roundtrip,start,cancel};
})();
