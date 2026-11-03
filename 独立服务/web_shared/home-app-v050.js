(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const endpoint = new URL('api/roundtrip',document.baseURI).pathname;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const labels = {bookkeeping:'家庭记账',secretary:'言小办秘书',shopping:'商品挑选',flight:'航班查询',jd:'京东',xiaohongshu:'小红书攻略',taobao_instant:'淘宝闪购',meituan:'美团',wechat:'微信',rail:'铁路出行',weather:'天气查询',route:'驾车路线',traffic:'道路路况',travel:'旅游攻略',web_search:'网页查询',amap:'高德地点交接'};
  const icons = {bookkeeping:'bag',secretary:'history',flight:'search',jd:'history',xiaohongshu:'search',taobao_instant:'history',meituan:'history',wechat:'history',rail:'train',weather:'sun',route:'map',traffic:'map',travel:'search',web_search:'search',amap:'map'};
  const activePhases = new Set(['recording','permission','processing','playing']);
  let tasks = [], nextCursor = null, current = null, currentTurns = [], session = null;
  let epoch = 0, historyEpoch = 0, operation = null, toastTimer = null, lastSignal = null;
  let historySearch = '';
  let connectionConfirmed = false;
  let receiptTimer=null,receiptBusy=false;
  let backgroundReceipts=[];
  let voicePhase='idle';
  let staged=null;
  const receiptNotices=new Map();
  const receiptSpoken=new Set();
  const hiddenConversations=new Set();
  function bounded(promise,ms,message) {
    let timer;
    return Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error(message)),ms);})]).finally(()=>clearTimeout(timer));
  }
  function toast(message) {
    clearTimeout(toastTimer); $('toast').textContent = message; $('toast').hidden = false;
    toastTimer = setTimeout(() => $('toast').hidden = true, 5000);
  }
  const errors = {
    device_connection_required:'请先连接言小办，再打开这个页面。',same_origin_required:'请在言小办的 Safari 页面中操作。',
    conversation_conflict:'这段对话正在处理或已变化，已保留原结果；请刷新后重试。',
    result_not_found:'没有找到这条结果，它可能属于另一台连接的设备。',
    model_timeout:'言小办理解这句话超时了，请稍后重试。',model_busy:'言小办暂时繁忙，请稍后再说。',
    speech_unavailable:'暂时没有连上语音服务，请稍后重试。',model_invalid_reply:'这次未能生成有效回答，请换个说法。',
    invalid_home_request:'这次输入未能提交，请重新输入一句话。',
    memory_changed:'记忆已有新的修改，请重新打开记忆页面再操作。',
    memory_capacity:'记忆已接近本轮容量，请先在“言小办记住的事”中检查整理；这次未保存修改。'
  };
  async function api(body, signal, timeoutMs=180000) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) abort(); else signal?.addEventListener('abort',abort,{once:true});
    const timer = setTimeout(abort,timeoutMs);
    try {
      const response = await fetch(endpoint,{method:'POST',credentials:'same-origin',signal:controller.signal,
        headers:{'Content-Type':'application/json','X-PI-Request':'1',...(window.PiPresentation&&body.action==='text'?{'Accept':'application/x-ndjson'}:{})},body:JSON.stringify(body)});
      let data; try { data = window.PiPresentation&&body.action==='text'?await window.PiPresentation.roundtrip(response,result=>{
        if(!controller.signal.aborted&&result.id===body.id&&result.requestId===body.requestId&&result.revision===body.revision+1)progress(result);
      }):await response.json(); } catch (error) { if(error.code)throw error;throw new Error('暂时未取得有效回复，请稍后再试。'); }
      if (!response.ok && response.status===401) {connectionConfirmed=false;stopAll();}
      if (!response.ok) {const error=new Error(errors[data.error] || (response.status===401 ? errors.device_connection_required : '这次请求未完成，原结果仍然保留。'));error.code=data.error;error.status=response.status;throw error;}
      return data;
    } catch(error) {
      if(error.code&&errors[error.code])error.message=errors[error.code];
      if(error.name==='AbortError' && body.action==='history')throw new Error('连接检查超时，请稍后再试。');
      throw error;
    } finally { clearTimeout(timer); signal?.removeEventListener('abort',abort); }
  }
  function statusName(result) {
    if(result.commercial?.kind==='shopping' && result.commercial.state==='delivery_pending')return '商品资料已返回 · 口播待核对';
    if(result.commercial?.kind==='shopping' && result.commercial.state==='clarify')return '等你补充';
    if(result.research)return ({queued:'已接收',running:'正在查询',cancelling:'正在停止',cancelled:'已停止',waiting:'需要处理',partial:'部分结果',complete:'已完成',interrupted:'任务已中断',failed:'未能完成',uncertain:'状态待确认'})[result.research.state] || '攻略查询';
    if(result.shopping)return ({queued:'已接收',running:'正在筛选核对',cancelling:'正在停止',cancelled:'已停止',waiting:'需要处理',partial:'核对结果',complete:'已完成',interrupted:'任务已中断',failed:'未能完成',uncertain:'状态待确认'})[result.shopping.state] || '购物任务';
    if(result.wechat)return ({choosing:'请选择收件人',draft:'待确认发送',queued:'等待微信处理',sending:'微信处理中',sent:'已发送',cancelled:'已取消',expired:'草稿已过期',uncertain:'请核对微信记录',blocked:'尚未提交',missing:'待补充信息'})[result.wechat.state] || '微信消息';
    if(result.handoff)return '待打开高德';
    return result.queryStatus === 'complete' ? '查询完成' : result.queryStatus === 'partial' ? '部分结果' :
      result.queryStatus === 'error' ? '未查到结果' : result.action === 'prepare' ? '尚未执行' : '已回复';
  }
  function taskInProgress(result) {
    return ['queued','running','cancelling','waiting'].includes((result?.shopping||result?.research)?.state)
      || ['choosing','draft','queued','sending'].includes(result?.wechat?.state);
  }
  function keepTaskVisible(previous,incoming) {
    return previous?.id===incoming.id && taskInProgress(previous)
      && !incoming.tool && !incoming.shopping && !incoming.research && !incoming.wechat && !incoming.handoff;
  }
  function currentStatus(result) {
    if (taskInProgress(result)) return statusName(result);
    return ({permission:'正在准备',recording:'正在听',processing:'正在处理',playing:'正在播报'})[voicePhase] || statusName(result);
  }
  function updateCurrentStatus() {
    const tag=$('current-content').querySelector('.sample-tag');
    if(tag && current)tag.textContent=currentStatus(current);
  }
  function timeLabel(result) {
    const source = (result.sources || []).find(s => s.reporttime || s.queriedAt);
    const raw = result.createdAt || source?.reporttime || source?.queriedAt;
    if (!raw) return '早期记录 · 未记录时间';
    const match = String(raw).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    return match ? `${match[2]}月${match[3]}日 ${match[4]}:${match[5]}` : '查询时间见来源';
  }
  function resultTitle(result) {
    if(result.shopping)return result.shopping.brief?.productName || '购物任务';
    if(result.wechat)return result.wechat.recipient ? '给'+result.wechat.recipient+'的微信' : '微信消息';
    if(result.handoff)return result.handoff.destination || '高德地点交接';
    if(result.commercial?.title)return result.commercial.title;
    if(result.commercial?.kind==='train' && result.commercial.request){const q=result.commercial.request;return `${q.departure_station} → ${q.arrival_station}`;}
    const train = result.rail?.trains?.[0];
    if (train) return `${train.origin} → ${train.destination}`;
    if (result.weather) return `${result.weather.city} · ${result.weather.date.slice(5)}`;
    return (result.text || '这段对话').slice(0,48);
  }
  function sourceHTML(result) {
    const sources = (Array.isArray(result.sources) ? result.sources : []);
    if (!sources.length) return `<div class="result-source">${esc(timeLabel(result))} · 已保存</div>`;
    const links=sources.map(s => {
      let url = null;
      try { const candidate = new URL(s.url); if (['https:','http:'].includes(candidate.protocol) && !candidate.username && !candidate.password) url=candidate.href; } catch (_) {}
      const title=esc((s.id?'['+s.id+'] ':'')+(s.title || s.provider || '资料来源'));
      return `<div class="result-source"><span class="source-dot"></span><span>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${title} ↗</a>` : title}${s.reporttime || s.queriedAt ? `<br>${esc(s.reporttime || s.queriedAt)}` : ''}</span></div>`;
    });
    if(result.commercial?.kind==='travel') {
      const chosen=new Set((result.commercial.summary?.sections||[]).flatMap(s=>(s.items||[]).flatMap(i=>(i.evidence||[]).map(e=>e.source_id))));
      const main=links.filter((_,i)=>chosen.has(sources[i].id));
      const extra=links.filter((_,i)=>!chosen.has(sources[i].id));
      return `<div class="sources-list">${main.join('')}${extra.length?`<details><summary>其他检索来源（${extra.length}）</summary>${extra.join('')}</details>`:''}</div>`;
    }
    return `<div class="sources-list">${links.join('')}</div>`;
  }
  function trainCard(t, primary) {
    const availability = t.availability === '无' ? '无票' : t.availability === '有' ? '有票' : t.availability;
    const price = t.price == null ? '票价未取得' : `¥${t.price}`;
    const minutes = Number(t.durationMinutes);
    const duration = Number.isFinite(minutes) ? `${Math.floor(minutes/60)}小时${minutes%60}分` : '';
    return `<section class="selected-train${primary?'':' additional-train'}"><div class="train-top"><div><span class="train-code">${esc(t.train)}</span><span class="train-rank">${esc(t.seat)}</span></div><span class="seat-available">${esc(availability)}</span></div><div class="journey"><div><strong>${esc(t.depart)}</strong><span>${esc(t.origin)}</span></div><div class="journey-line"><span>${esc(duration)}</span><div><i></i><b></b><i></i></div><span>直达${t.arriveDate && t.arriveDate!==t.date ? ' · '+esc(t.arriveDate.slice(5))+'到达' : ''}</span></div><div class="arrival"><strong>${esc(t.arrive)}</strong><span>${esc(t.destination)}</span></div></div><div class="fare-row"><div><span class="fare-label">${esc(t.seat)}</span><span class="fare-note">${esc(t.date)} · 成人票</span></div><div class="fare">${esc(price)}<span class="fare-unit"> / 人</span></div></div></section>`;
  }
  function handoffURL(h) {
    if(!h || h.provider!=='amap' || h.state!=='ready' || !['car','walk','bus','ride'].includes(h.mode))return null;
    if(typeof h.destination!=='string' || !h.destination || h.destination.length>80 || typeof h.location!=='string' || !/^\d{1,3}\.\d+,\d{1,2}\.\d+$/.test(h.location))return null;
    const [lon,lat]=h.location.split(',').map(Number);if(lon>180||lat>90)return null;
    try {
      const u=new URL(h.url),q=u.searchParams;
      if(u.origin!=='https://uri.amap.com'||u.pathname!=='/navigation'||u.username||u.password||u.hash)return null;
      if([...q.keys()].sort().join(',')!=='callnative,mode,src,to'||q.get('to')!==h.location+','+h.destination||q.get('mode')!==h.mode||q.get('callnative')!=='1'||q.get('src')!=='Pi手机助手')return null;
      return u.href;
    }catch(_){return null;}
  }
  function flightTable(s) {
    const rows=Array.isArray(s.trips)?s.trips:Array.isArray(s.flights)?s.flights:[];
    if(!rows.length)return '';
    return `<div style="overflow-x:auto"><table class="detail-table"><thead><tr><th>班次 / 日期</th><th>出发 → 到达</th><th>站点</th><th>票价 / 余票</th></tr></thead><tbody>${rows.map(f=>`<tr><td>${esc(f.number||f.flight)}<br>${esc(f.date)}</td><td>${esc(f.departure)} → ${esc(f.arrival)}</td><td>${esc(f.origin||f.fromAirport)} → ${esc(f.destination||f.toAirport)}</td><td>${esc(f.price)}<br>${esc(f.availability||'')}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function productURL(product) {
    if(product.link_status !== 'ready')return null;
    try {
      const u=new URL(product.url);
      return u.protocol==='https:' && !u.username && !u.password ? u.href : null;
    } catch(_){return null;}
  }
  function productCard(product) {
    const url=productURL(product),platform=product.platform_label||'商品页';
    const metrics=[product.rating_percent!=null?'好评率 '+product.rating_percent+'%':null,
      product.review_count!=null?'评价 '+Number(product.review_count).toLocaleString('zh-CN')+' 条':null].filter(Boolean);
    const coupon=product.coupon_price!=null && product.coupon_price!==product.price
      ? `<div class="product-offer">${esc(product.offer_label||'优惠参考')} ¥${esc(product.coupon_price)}<span>${esc(product.offer_notice||'需满足商品页优惠条件')}</span></div>` : '';
    const terms=Array.isArray(product.offer_terms)&&product.offer_terms.length?`<details><summary>平台优惠说明（不代表可叠加）</summary>${product.offer_terms.map(t=>`<p class="document-copy">${esc(t)}</p>`).join('')}</details>`:'';
    return `<article class="product-card"><div class="product-platform">${esc(platform)}${product.shop?' · '+esc(product.shop):''}</div><h4>${esc(product.title)}</h4><div class="product-price">${product.price==null?'页面价格未提供':`¥${esc(product.price)}<span>页面报价</span>`}</div>${coupon}${terms}<p class="document-copy">${esc(product.reason)}</p>${metrics.length?`<p class="product-metrics">${metrics.map(esc).join(' · ')}</p>`:''}${url?`<a class="secondary-button product-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">到${esc(platform)}查看${icon('arrow')}</a>`:'<p class="product-link-pending">商品直达链接暂未取得</p>'}</article>`;
  }
  function bodyHTML(r) {
    if(r.research) {
      const s=r.research.summary||{};
      const sections=[...(s.findings||[]),...(s.recommendations||[])];
      return `<h3 class="document-title">${r.tool==='flight'?'航班查询':r.tool==='rail'?'高铁 / 火车票查询':'小红书攻略'}</h3>${['flight','rail'].includes(r.tool)?flightTable(s):''}<p class="document-copy">${esc(s.overview||r.reply)}</p>${sections.map(x=>`<p class="document-copy">${esc(x.text)}</p>`).join('')}<p class="data-warning">${esc((s.uncertainties||[]).join('；'))}</p><p class="result-source">${esc(s.evidenceNotice||'以实际读取的资料为准；平台验证需人工处理。')}</p>`;
    }
    if(r.commercial?.kind==='shopping') {
      const data=r.commercial,products=Array.isArray(data.products)?data.products:[];
      return `<h3 class="document-title">${esc(resultTitle(r))}</h3><p class="document-copy">${esc(r.reply)}</p><div class="product-list">${products.map(productCard).join('')}</div><p class="data-warning">${esc(data.scope)}</p>`;
    }
    if(['bookkeeping','secretary'].includes(r.commercial?.kind)) {
      const data=r.commercial,money=value=>{const n=BigInt(value),v=n<0n?-n:n;return (n<0n?'-':'')+String(v/100n)+'.'+String(v%100n).padStart(2,'0');},featureDate=value=>{const date=new Date(value);return Number.isFinite(date.getTime())?date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+'（北京时间）':value;};
      const details=(data.details||[]).map(d=>{
        const result=d.result||{},rows=result.rows||(result.transaction?[result.transaction]:[]),summary=result.summary;
        return `${summary?`<p class="query-context">收入 ¥${money(summary.income_fen)} · 净支出 ¥${money(summary.expense_fen)} · 未分类入账 ¥${money(summary.unclassified_inflow_fen)} · 结余 ¥${money(summary.net_fen)}</p>`:''}${rows.length?`<div class="document-copy">${rows.map(row=>`<p>${esc(row.date)} · ¥${esc(money(row.amount_fen))} · ${esc(row.notes||'流水')}</p>`).join('')}</div>`:''}`;
      }).join('');
      const items=(data.items||[]).map(item=>`<p class="document-copy"><strong>${esc(item.title)}</strong>${item.start_at?`<br>开始：${esc(featureDate(item.start_at))}`:''}${item.end_at?`<br>结束：${esc(featureDate(item.end_at))}`:''}${item.remind_at?`<br>提醒：${esc(featureDate(item.remind_at))}`:''}${item.notes?`<br>${esc(item.notes)}`:''}</p>`).join('');
      return `<h3 class="document-title">${esc(data.title)}</h3><p class="document-copy">${esc(r.reply)}</p>${details}${items}${data.kind==='secretary'?'<p class="data-warning">系统事项通过iCloud同步。是否已同步到手机及到点通知，请在iPhone提醒事项或日历中核对。</p>':''}`;
    }
    if(r.commercial && r.commercial.kind !== 'train') {
      const data=r.commercial,summary=data.summary||{};
      return `<h3 class="document-title">${esc(resultTitle(r))}</h3>${(summary.sections||[]).map(s=>`<h4>${esc(s.heading)}</h4>${(s.items||[]).map(i=>`<p class="document-copy">${esc(i.text)} ${[...new Set((i.evidence||[]).map(e=>e.source_id))].map(id=>'['+esc(id)+']').join('')}</p>`).join('')}`).join('')}<p class="data-warning">${esc(data.message||'')} ${esc((summary.uncertainties||[]).join('；'))}</p>`;
    }
    if(r.shopping) {
      const m=r.shopping,b=m.brief||{},items=Array.isArray(m.items)?m.items:[];
      return `<h3 class="document-title">${esc(b.productName||'购物任务')}</h3><p class="document-subtitle">${esc([b.quantity,b.budget].filter(Boolean).join(' · '))}</p><p class="document-copy">${esc(m.detailReply || r.reply)}</p>${items.map(x=>`<p class="document-copy"><strong>${esc(x.title)}</strong><br>${esc(x.store)} · ${esc(x.sku)}<br>${esc(x.cartState==='added'?'已加车并回读确认':'已在购物车')} · ${esc(x.quantity)}份</p>`).join('')}<p class="data-warning">这里只保留核对回执，商品请在同账号购物车比较。最终费用以结算页为准，由你自行下单支付。回看或重听不会再次加车。</p>`;
    }
    if(r.wechat) {const m=r.wechat;return `<h3 class="document-title">${esc(m.recipient ? '给'+m.recipient+'的微信' : '微信消息')}</h3>${m.content ? `<p class="user-utterance">${esc(m.content)}</p>` : ''}<p class="document-copy">${esc(r.reply)}</p><p class="data-warning">只有明确确认才发送。回看或重听不会发送消息，实际发送记录请在微信中核对。</p>`;}
    if(r.handoff) {
      const h=r.handoff,url=handoffURL(h),mode={car:'驾车',walk:'步行',bus:'公交',ride:'骑行'}[h.mode] || '出行';
      return `<h3 class="document-title">${esc(h.destination)}</h3><p class="document-subtitle">${esc(h.city)} · ${mode}</p><p class="document-copy">${esc(r.reply)}</p>${url?`<a class="secondary-button navigation-link" data-handoff href="${esc(url)}" rel="noreferrer">${icon('map')}打开高德${icon('arrow')}</a>`:'<p class="sheet-warning">地点入口暂时无效，请重新确认目的地。</p>'}<p class="data-warning">当前还未开始导航。起点由这台手机的高德获取，请在高德中核对目的地和路线。回看此卡不会自动打开App。</p>`;
    }
    if (r.rail?.trains?.length) {
      const trains = r.rail.trains;
      return `<h3 class="route-title">${esc(r.commercial?.request?.departure_station||trains[0].origin)}${icon('arrow')}${esc(r.commercial?.request?.arrival_station||trains[0].destination)}</h3><div class="query-context">${esc(r.rail.date)} · ${esc(r.commercial?.time_label||'全天')} · ${r.commercial?.total_matches!=null?'符合条件'+esc(r.commercial.total_matches)+'班，':''}先看${trains.length}班</div><div class="followup-note"><span class="note-line"></span><span>你说：${esc(r.text)}</span></div>${trainCard(trains[0],true)}${trains.length>1 ? `<details class="train-details"><summary>展开另外${trains.length-1}班，对比看看</summary>${trains.slice(1).map(t=>trainCard(t,false)).join('')}</details>` : ''}<p class="data-warning">聚合数据查询快照，票价余票以购票页为准。</p><a class="secondary-button" href="https://www.12306.cn/index/" target="_blank" rel="noopener noreferrer">打开12306官网首页</a>`;
    }
    if(r.commercial?.kind==='train')return `<h3 class="route-title">${esc(resultTitle(r))}</h3><p class="query-context">${esc(r.rail?.date)} · ${esc(r.commercial.time_label)}</p><p class="document-copy">${esc(r.reply)}</p>`;
    if (r.weather) {
      const w=r.weather, live=w.kind==='live';
      return `<h3 class="document-title">${esc(w.city)}的天气</h3><p class="document-subtitle">${esc(w.date)} · ${live?'天气实况':'天气预报'}</p><div class="detail-hero"><div><strong>${esc(live?w.temperature:w.high)}°</strong><p>${esc(live?w.condition:w.dayCondition)}</p></div>${icon('sun')}</div>${!live?`<table class="detail-table"><thead><tr><th>时段</th><th>天气</th><th>温度范围</th></tr></thead><tbody><tr><td>白天</td><td>${esc(w.dayCondition)}</td><td rowspan="2">${esc(w.low)}—${esc(w.high)}°C</td></tr><tr><td>夜间</td><td>${esc(w.nightCondition)}</td></tr></tbody></table>`:''}<p class="document-copy">${esc(r.reply)}</p><p class="data-warning">${live?'实况':'预报'}更新时间：${esc(w.reporttime)}。回看不会重新查询。</p>`;
    }
    if (r.route) return `<h3 class="document-title">这段路，心里有数。</h3><p class="document-copy">${esc(r.route.origin)} → ${esc(r.route.destination)}</p><div class="route-metrics"><div><strong>${esc(r.route.minutes)}</strong><span>预计分钟</span></div><div><strong>${esc(r.route.km)}</strong><span>公里</span></div></div><p class="document-copy">${esc(r.reply)}</p>`;
    return `<h3 class="document-title">${esc(resultTitle(r))}</h3><p class="document-copy">${esc(r.reply)}</p>${r.missing?.length ? `<div class="sheet-warning">尚未确认：${r.missing.map(esc).join('；')}</div>` : ''}`;
  }
  function renderCurrent() {
    if (!current&&!staged) return;
    const r=staged||current;
    const content=window.PiPresentation?window.PiPresentation.render(r,()=>bodyHTML(r),{productCard,trainCard,title:resultTitle}):bodyHTML(r);
    if(staged) {
      $('current-content').innerHTML=`<article class="result-card live-card"><div class="card-topline"><span class="category">${icon(icons[r.tool]||'history')}${esc(labels[r.tool]||'和言小办说')}</span><span class="sample-tag">结果已保存</span></div>${content}${sourceHTML(r)}<p class="presentation-awaiting">文字结果已保存，正在准备语音。</p></article>`;
      $('current-title').textContent='这次的结果';return;
    }
    $('current-content').innerHTML = `<article class="result-card live-card"><div class="card-topline"><span class="category">${icon(icons[r.tool] || 'history')}${esc(labels[r.tool] || '和言小办说')}</span><span class="sample-tag">${esc(currentStatus(r))}</span></div>${content}${sourceHTML(r)}<div class="card-actions"><button class="secondary-button" data-action="replay">${icon('sound')}重听结果</button><button class="text-button" data-action="followup">接着问${icon('arrow')}</button></div><div class="record-tools"><button class="text-button" data-action="export">导出 / 存到备忘录</button><button class="text-button muted-delete" data-action="delete">删除记录</button></div><button class="thread-button" data-action="thread">查看这段对话与此前结果${icon('chevron')}</button></article>`;
    $('current-content').querySelector('[data-action=replay]').onclick=()=>replay(r);
    $('current-content').querySelector('[data-action=followup]').onclick=()=>openInput(true);
    $('current-content').querySelector('[data-action=thread]').onclick=()=>openThread(r.id);
    $('current-content').querySelector('[data-action=export]').onclick=()=>openExport(r);
    $('current-content').querySelector('[data-action=delete]').onclick=()=>confirmDelete(r);
    $('current-content').querySelector('[data-handoff]')?.addEventListener('click',()=>stopAll());
    if(r.memory?.saved){
      const notice=document.createElement('button');notice.className='text-button memory-receipt';
      notice.textContent='这轮更新了个人记忆 · 查看';notice.onclick=showMemory;
      $('current-content').querySelector('.record-tools').before(notice);
    }
    $('current-title').textContent='这次的结果';
  }
  function taskCard(task) {
    const r=task.result;
    return `<button class="saved-card" data-task="${esc(task.id)}"><span class="saved-icon sage">${icon(icons[r.tool] || 'history')}</span><span class="saved-content"><span class="saved-type">${esc(labels[r.tool] || '对话')} · ${esc(statusName(r))}</span><strong>${esc(resultTitle(r))}</strong><span class="saved-description">${esc(timeLabel(r))}</span></span>${icon('chevron')}</button>`;
  }
  function bindTasks(container) { container.querySelectorAll('[data-task]').forEach(b=>b.onclick=()=>selectTask(b.dataset.task)); }
  function renderRecent() {
    $('recent-results').innerHTML=tasks.length ? tasks.slice(0,4).map(taskCard).join('') : '<p class="history-empty">还没有记录。第一次对话后，结果会留在这里。</p>';
    bindTasks($('recent-results'));
  }
  async function refreshHistory(initial=false,manual=false) {
    const ticket=++historyEpoch, oldCurrent=current;
    if(manual){$('connection-state').textContent='正在检查连接…';if($('load-message'))$('load-message').textContent='正在检查登录和服务连接，请稍候…';toast('正在检查连接…');}
    try {
      const data=await api({action:'history',before:0},undefined,10000);
      if (ticket!==historyEpoch) return;
      if (!Array.isArray(data.items)) throw new Error('历史结果暂时无法读取。');
      tasks=data.items; backgroundReceipts=Array.isArray(data.pending)?data.pending:[]; nextCursor=data.nextCursor; renderRecent(); connectionConfirmed=true; $('connection-state').textContent='已连接';
      if (initial && !oldCurrent && !current && !staged && tasks.length) { current=tasks[0].result;renderCurrent();layoutFor(current,true); }
      if ($('load-message')) $('load-message').textContent='打开语音，或者从下方输入一句话。';
      if(manual)toast('已连接，可以开始说话。');
      scheduleReceiptCheck();
      return true;
    } catch (error) {
      if(ticket!==historyEpoch)return false;
      connectionConfirmed=false; $('connection-state').textContent='连接待确认';
      stopAll();probe.status('error',error.message);
      if ($('load-message')) $('load-message').textContent=error.message;
      if(manual){const message=error.status===401?'当前 Safari 页面没有有效的言小办登录状态，请先打开连接页面完成连接。':error.message;if($('load-message'))$('load-message').textContent=message;toast(message);}
      if ($('load-message') && error.message===errors.device_connection_required) {
        const link=document.createElement('a');link.href='./';link.textContent=' 打开连接页面';$('load-message').append(link);
      }
      if ($('load-message')) {
        const retry=document.createElement('button');retry.type='button';retry.className='secondary-button';
        retry.textContent='重新检查连接';retry.onclick=()=>refreshHistory(false,true);$('load-message').append(retry);
      }
      if (!tasks.length) { $('recent-results').replaceChildren();const p=document.createElement('p');p.className='history-empty';p.textContent=error.message;$('recent-results').append(p); }
      return false;
    }
  }
  function receiptKey(result) {
    const task=result.shopping||result.research;
    return task?.taskId ? `${task.taskId}:${task.generation||1}:${task.state}` : result.wechat?.draftId||result.requestId;
  }
  function pendingReceipts() {
    const rows=[current,...tasks.map(t=>t.result),...backgroundReceipts].filter(Boolean);
    return [...new Map(rows.filter(r=>['queued','sending'].includes(r.wechat?.state)||['queued','running','cancelling'].includes((r.shopping||r.research)?.state)||((r.shopping||r.research)?.needsDecision&&!receiptSpoken.has(receiptKey(r)))).map(r=>[r.requestId,r])).values()];
  }
  function scheduleReceiptCheck(delay=2000) {
    if(receiptTimer||document.hidden||!connectionConfirmed)return;
    if(!pendingReceipts().length&&!receiptNotices.size)return;
    receiptTimer=setTimeout(checkReceipts,delay);
  }
  async function checkReceipts() {
    receiptTimer=null;
    if(receiptBusy||document.hidden||!connectionConfirmed)return;
    receiptBusy=true;
    try {
      for(const pending of pendingReceipts()) {
        const updated=await api({action:pending.research?'research_status':pending.shopping?'shopping_status':'wechat_status',id:pending.id,requestId:pending.requestId});
        if(updated.id!==pending.id||updated.requestId!==pending.requestId||hiddenConversations.has(pending.id))continue;
        const background=updated.shopping||updated.research;
        const ongoing=background?['queued','running','cancelling'].includes(background.state):['queued','sending'].includes(updated.wechat?.state);
        if(background && background.taskId!==(pending.shopping||pending.research)?.taskId)continue;
        if(!background&&!['sent','uncertain','cancelled','expired'].includes(updated.wechat?.state))continue;
        tasks=tasks.map(t=>t.result.requestId===updated.requestId?{...t,result:updated}:t);
        backgroundReceipts=backgroundReceipts.map(r=>r.requestId===updated.requestId?updated:r);
        renderRecent();
        if(current?.requestId===updated.requestId){current=updated;renderCurrent();}
        else if(ongoing)presentReceipt(updated);
        if(ongoing)continue;
        const key=receiptKey(updated);
        if(!receiptSpoken.has(key))receiptNotices.set(key,updated);
      }
      // Never interrupt the user's next utterance or another reply. Deliver when idle.
      if(receiptNotices.size&&!document.hidden&&!operation&&!activePhases.has(probe.phase)&&!probe.continuous) {
        const result=receiptNotices.values().next().value;
        presentReceipt(result);
        await replay(result);
      }
    } catch(error) { /* Read-only status errors retry; no message can be resent here. */ }
    finally {receiptBusy=false;scheduleReceiptCheck(3000);}
  }
  function presentReceipt(result) {
    // A later chat acknowledgement must not hide the task being announced.
    // Keep the newer conversation revision and any explicitly selected history.
    if (!current || current.id!==result.id || hiddenConversations.has(result.id))return;
    if (['回看结果','此前的结果'].includes($('current-title').textContent))return;
    if (current.requestId!==result.requestId && current.tool)return;
    current=result;renderCurrent();
  }
  function receive(result) {
    if(!result.presentation&&staged?.id===result.id&&staged.requestId===result.requestId)result.presentation=staged.presentation;
    staged=null;
    const task=result.shopping||result.research;
    if(task?.taskId)for(const [key,notice] of receiptNotices) {
      const old=notice.shopping||notice.research;
      if(old?.taskId===task.taskId&&(old.generation||1)<=(task.generation||1))receiptNotices.delete(key);
    }
    if(!keepTaskVisible(current,result))current=result;
    session={id:result.id,revision:result.revision};currentTurns=[];renderCurrent();
    $('save-status').textContent='已保存，刷新或回来后仍可查看。';
    refreshHistory();
    layoutFor(result,false);
  }
  function layoutFor(result,cachedOnly) {
    const ticket=epoch;
    window.PiPresentation?.start(result,layout=>{
      if(ticket!==epoch||hiddenConversations.has(result.id))return;
      const shown=staged||current;
      if(shown?.id!==result.id||shown.requestId!==result.requestId)return;
      const title=$('current-title').textContent,opened=$('current-content').querySelector('.presentation-original')?.open;
      shown.presentation=layout;renderCurrent();
      if(!staged)$('current-title').textContent=title;
      if(opened){const original=$('current-content').querySelector('.presentation-original');if(original)original.open=true;}
    },cachedOnly);
  }
  function progress(result) {
    if(keepTaskVisible(current,result))return;
    staged=result;renderCurrent();layoutFor(result,false);
  }
  const probe=new PiVoiceProbe.VoiceProbe(endpoint,s=>{
    voicePhase=s.phase;updateCurrentStatus();
    const retry=$('retry-recording');if(retry)retry.hidden=!probe.retryAvailable;
    document.body.dataset.voice=s.phase;
    $('voice-status').textContent=s.message;
    const active=activePhases.has(s.phase) || probe.continuous || Boolean(operation);
    const names={permission:'正在准备',recording:'正在听你说',processing:'正在理解',playing:'正在播报'};
    $('voice-label').textContent=active ? (names[s.phase] || '停止对话') : '开始说话';
    $('voice-hint').textContent=active?'点此停止':'我在这里';
    $('voice-button').setAttribute('aria-label',active?'停止收音或播报':'开始语音对话');
    $('voice-meter').hidden=s.phase!=='recording';$('level').value=0;
    if (s.phase==='error') {
      if(staged){current=staged;staged=null;renderCurrent();}
      $('save-status').textContent='已有结果仍保留；本次状态见下方提示。';
    }
  },signal=>{
    lastSignal=signal;$('level').value=Math.min(100,signal.rms*1500);
  },{welcomeUrl:'ready-v035.wav',onResult:receive,onProgress:progress});
  const retryRecording=document.createElement('button');retryRecording.id='retry-recording';
  retryRecording.className='secondary-button';retryRecording.textContent='重试这段录音';retryRecording.hidden=true;
  retryRecording.onclick=()=>probe.retryLast();$('voice-status').insertAdjacentElement('afterend',retryRecording);
  function stopAll() { ++epoch;operation?.abort();operation=null;probe.stop();window.PiPresentation?.cancel();if(staged){current=staged;staged=null;renderCurrent();} }
  function selectedSession() {
    if (session) return session;
    if (current) { const task=tasks.find(t=>t.id===current.id);return {id:current.id,revision:task?.revision || current.revision}; }
    return null;
  }
  function beginVoice(fresh=false) {
    if (!connectionConfirmed) {probe.status('error','尚未连接言小办，请先刷新，或使用原来的 Siri 入口打开。');return;}
    stopAll(); const context=fresh?null:selectedSession();
    probe.start(false,false,context);session=context;
  }
  $('voice-button').onclick=()=>{if(activePhases.has(probe.phase)||probe.continuous||operation)stopAll();else beginVoice();};
  $('finish-button').onclick=()=>probe.finish();
  async function selectTask(id) {
    stopAll();const ticket=epoch;
    try {
      const data=await api({action:'task',id});if(ticket!==epoch)return;
      currentTurns=data.turns;session={id:data.id,revision:data.revision};
      current=[...data.turns].reverse().find(r=>r.tool)||data.turns.at(-1);renderCurrent();layoutFor(current,true);
      $('detail-dialog').close();$('current-title').textContent='回看结果';
      document.querySelector('.current-section').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});
    } catch(error) {if(ticket===epoch)toast(error.message);}
  }
  function openDetail(title,eyebrow,html) {
    $('detail-title').textContent=title;$('detail-eyebrow').textContent=eyebrow;$('detail-body').innerHTML=html;
    if(!$('detail-dialog').open)$('detail-dialog').showModal();$('detail-dialog').scrollTop=0;
  }
  async function openThread(id) {
    stopAll();const ticket=epoch;
    openDetail('这段对话','正在读取','<p class="sheet-intro">读取已保存的回答…</p>');
    try {
      const data=await api({action:'task',id});if(ticket!==epoch)return;
      currentTurns=data.turns;session={id,revision:data.revision};
      openDetail('每次追问，都在这里',`${data.turns.length}轮对话 · 历史记录`,data.turns.map((r,i)=>`<section class="thread-turn"><p class="turn-label">第${i+1}轮 · ${esc(timeLabel(r))}</p><p class="user-utterance">${esc(r.text)}</p><p class="document-copy">${esc(r.reply)}</p>${sourceHTML(r)}<button class="text-button" data-version="${i}">查看这轮结果${icon('arrow')}</button></section>`).join(''));
      $('detail-body').querySelectorAll('[data-version]').forEach(b=>b.onclick=()=>{current=data.turns[Number(b.dataset.version)];renderCurrent();layoutFor(current,true);$('current-title').textContent='此前的结果';$('detail-dialog').close();});
    } catch(error) {if(ticket===epoch)$('detail-body').textContent=error.message;}
  }
  function historyList() {
    const q=historySearch.trim();const matches=tasks.filter(t=>(t.title+t.result.text+t.result.reply).includes(q));
    $('history-list').innerHTML=(matches.length?matches.map(taskCard).join(''):'<p class="history-empty">没有找到对应记录，试试其他关键词。</p>')+(nextCursor?'<button class="secondary-button" id="load-more">读取更早的记录</button>':'');
    bindTasks($('history-list'));
    if($('load-more'))$('load-more').onclick=async()=>{
      const button=$('load-more');button.disabled=true;
      try {const data=await api({action:'history',before:nextCursor});tasks=[...tasks,...data.items.filter(t=>!tasks.some(x=>x.id===t.id))];nextCursor=data.nextCursor;if($('history-list'))historyList();}
      catch(error){toast(error.message);button.disabled=false;}
    };
  }
  function showHistory() {
    stopAll();historySearch='';
    openDetail('留下的，都找得到。','你的历史结果',`<label class="history-search">${icon('search')}<input id="history-search-input" type="search" placeholder="搜索已读取的结果" aria-label="搜索历史结果"></label><div id="history-list"></div>`);
    historyList();$('history-search-input').oninput=e=>{historySearch=e.target.value;historyList();};
  }
  $('all-results').onclick=showHistory;$('history-button').onclick=showHistory;
  function openInput(followup=false) {
    stopAll();
    $('input-title').textContent=followup?'接着刚才的结果说。':'把想法告诉我。';
    $('input-context').textContent=selectedSession()?'会沿用这段对话的上下文；需要换话题，也可以直接说。':'写一句话就好，不需要固定格式。';
    $('message-input').value='';$('input-dialog').showModal();$('message-input').focus();
  }
  $('keyboard-button').onclick=()=>openInput();
  async function unlockSound() {
    const AC=window.AudioContext||window.webkitAudioContext;
    if(!AC)throw new Error('当前浏览器不能播放语音，结果仍可在页面查看。');
    if(!probe.ctx || probe.ctx.state==='closed')probe.ctx=new AC();
    await bounded(probe.ctx.resume(),5000,'声音通道启动超时；文字结果仍会保存。');
  }
  async function playResponse(result,ticket,original=result) {
    if(ticket!==epoch)return;
    if(result.state==='text_only') {probe.status('error',result.message||'回复已保存，但这次语音未能生成。');return;}
    if(result.state!=='ready'||result.mime!=='audio/wav'||typeof result.audio!=='string'||result.audio.length>8000000)throw new Error('语音格式异常，文字结果仍已保存。');
    const bytes=Uint8Array.from(atob(result.audio),c=>c.charCodeAt(0));
    const buffer=await bounded(probe.ctx.decodeAudioData(bytes.buffer),10000,'语音解码超时；可以稍后重听。');if(ticket!==epoch)return;
    probe.audio={buffer,text:result.reply};probe.continuous=false;
    if((original.shopping||original.research)?.needsDecision&&!document.hidden) {
      const revision=Math.max(original.revision||0,tasks.find(t=>t.id===original.id)?.revision||0,session?.id===original.id?session.revision:0);
      probe.conversationId=original.id;
      probe.conversationRevision=revision;probe.round=0;probe.continuous=true;
    }
    probe.status('ready','回复已保存，准备播报。');
    if(probe.ctx.state==='running') {
      probe.play(probe.rev);
      return await new Promise(resolve=>{
        const until=Date.now()+Math.ceil((buffer.duration||0)*1000)+3000;
        const timer=setInterval(check,100);
        function check() {
          const valid=ticket===epoch&&!document.hidden;
          if(!valid||probe.phase!=='playing'||Date.now()>until) {
            clearInterval(timer);resolve(valid&&['finished','permission','recording'].includes(probe.phase));
          }
        }
        check();
      });
    }
    probe.status('ready','结果已保存，请点“重听结果”开启声音。');return false;
  }
  async function replay(result) {
    stopAll();const ticket=epoch;operation=new AbortController();
    const unlocked=unlockSound();unlocked.catch(()=>{});
    probe.status('processing','正在准备这条结果的语音…');
    try {
      const data=await api({action:'replay',id:result.id,requestId:result.requestId},operation.signal);
      if(data.id!==result.id||data.requestId!==result.requestId)throw new Error('回复对应关系异常，已停止播放。');
      await unlocked;const played=await playResponse(data,ticket,result);
      const key=receiptKey(result);
      if(played&&receiptNotices.has(key)){receiptSpoken.add(key);receiptNotices.delete(key);}
    } catch(error){if(ticket===epoch)probe.status('error',error.name==='AbortError'?'语音等待超时，请重试。':error.message);}
    finally{if(ticket===epoch){operation=null;if(!activePhases.has(probe.phase))probe.status(probe.phase,$('voice-status').textContent);}}
  }
  $('message-form').onsubmit=async event=>{
    event.preventDefault();const text=$('message-input').value.replace(/[\r\n\t]+/g,' ').trim();if(!text)return;
    const context=selectedSession()||{id:crypto.randomUUID().replace(/-/g,''),revision:0};
    $('input-dialog').close();stopAll();const ticket=epoch;operation=new AbortController();
    const unlocked=unlockSound();unlocked.catch(()=>{});
    const requestId=crypto.randomUUID().replace(/-/g,'');
    probe.status('processing','正在理解这句话…');
    try {
      const result=await api({action:'text',...context,requestId,text},operation.signal);
      if(ticket!==epoch)return;
      if(result.id!==context.id||result.requestId!==requestId||result.revision!==context.revision+1)throw new Error('回复对应关系异常，已保留原结果。');
      receive(result);await unlocked;await playResponse(result,ticket);
    } catch(error){if(ticket===epoch){probe.status('error',error.name==='AbortError'?'这次等待超时；可稍后从历史记录查看。':error.message);refreshHistory();}}
    finally{if(ticket===epoch){operation=null;if(!activePhases.has(probe.phase))probe.status(probe.phase,$('voice-status').textContent);}}
  };

  function exportResult(r) {
    const lines=[`你说：${r.text || ''}`,`言小办：${r.reply || ''}`,
      `记录时间：${r.createdAt || '早期记录，未记录时间'}`,`状态：${statusName(r)}`];
    if(r.commercial?.kind==='shopping') {
      const data=r.commercial;
      lines.push('',data.title||'商品挑选');
      (data.products||[]).forEach((p,i)=>{
        lines.push(`${i+1}. ${p.title}`,p.shop?'店铺：'+p.shop:'',
          p.price==null?'页面报价未取得':`页面报价：¥${p.price}`,
          p.coupon_price!=null && p.coupon_price!==p.price?`${p.offer_label||'优惠参考'}：¥${p.coupon_price}，${p.offer_notice||'需满足商品页条件'}`:'',p.reason||'');
        if(Array.isArray(p.offer_terms))lines.push(...p.offer_terms);
        const url=productURL(p);if(url)lines.push('查看商品：'+url);
      });
      lines.push(data.scope||'');
    } else if(r.commercial && r.commercial.kind !== 'train') {
      const data=r.commercial,summary=data.summary||{};
      lines.push('',data.title||'',...(summary.sections||[]).flatMap(s=>[s.heading,...(s.items||[]).map(i=>i.text)]),
        ...(summary.uncertainties||[]));
    }
    if(r.shopping) {
      const m=r.shopping;
      (m.items||[]).forEach((x,i)=>lines.push(`${i+1}. ${x.title} · ${x.store} · ${x.sku} · ${x.quantity}份 · ${x.cartState==='added'?'已加车并回读确认':'已在购物车'}`));
      lines.push('以上为当次核对回执，最终费用以购物车结算为准。回看不会重新加车。');
    }
    if(r.research) {const s=r.research.summary||{};lines.push('',s.overview||'',...(s.trips||s.flights||[]).map(f=>`${f.date} ${f.number||f.flight} ${f.departure}—${f.arrival} ${f.origin||f.fromAirport}→${f.destination||f.toAirport} ${f.price} ${f.availability||''}`),...(s.findings||[]).map(x=>x.text),...(s.recommendations||[]).map(x=>x.text),...(s.uncertainties||[]));}
    if(r.rail?.trains?.length) {
      lines.push('',`铁路查询 · ${r.rail.date || ''}`);
      r.rail.trains.forEach((t,i)=>lines.push(`${i+1}. ${t.train} · ${t.origin} → ${t.destination}`,
        `   出发：${t.date || ''} ${t.depart || ''}；到达：${t.arriveDate || t.date || ''} ${t.arrive || ''}`,
        `   ${t.seat || '席别未取得'} · ${t.price == null ? '票价未取得' : '¥'+t.price+' / 人'} · ${t.availability || '余票未取得'}`));
      lines.push('票价、余票是当次查询快照，会随时间变化。');
    }
    if(r.weather) {
      const w=r.weather;
      lines.push('',`天气 · ${w.city} · ${w.date}`,
        w.kind==='live'?`实况：${w.condition}，${w.temperature}°C`:
          `预报：白天${w.dayCondition}，夜间${w.nightCondition}，${w.low}—${w.high}°C`,
        `天气更新时间：${w.reporttime || '未取得'}`);
    }
    if(r.handoff){lines.push('',`高德目的地：${r.handoff.destination}`,`方式：${{car:'驾车',walk:'步行',bus:'公交',ride:'骑行'}[r.handoff.mode] || '出行'}`,'状态：已准备入口，尚未开始导航。');const url=handoffURL(r.handoff);if(url)lines.push('打开高德：'+url);}
    if(r.route) lines.push('',`路线：${r.route.origin} → ${r.route.destination}`,
      `预计${r.route.minutes}分钟，${r.route.km}公里`, `路线查询时间：${r.route.queriedAt || r.createdAt || '未取得'}`);
    if(r.missing?.length)lines.push('',`尚未确认：${r.missing.join('；')}`);
    if(Array.isArray(r.sources) && r.sources.length) {
      lines.push('','资料来源：');
      r.sources.forEach(source=>{
        let url='';
        try{const u=new URL(source.url);if(['https:','http:'].includes(u.protocol)&&!u.username&&!u.password)url=u.href;}catch(_){}
        lines.push(`- ${source.title || source.provider || '资料来源'}${url?' '+url:''}${source.reporttime || source.queriedAt?' · '+(source.reporttime || source.queriedAt):''}`);
      });
    }
    return lines.join('\n');
  }
  async function openExport(result) {
    stopAll();const ticket=epoch;
    openDetail('把重要的留下来。','导出记录','<p class="sheet-intro">正在准备已保存的内容…</p>');
    try {
      const data=await api({action:'task',id:result.id});
      if(ticket!==epoch||!$('detail-dialog').open)return;
      const saved=data.turns.find(r=>r.requestId===result.requestId);
      if(!saved)throw new Error('这条结果暂时无法读取，请刷新后重试。');
      openDetail('把重要的留下来。','导出记录',`<label class="export-label" for="export-scope">导出范围</label><select id="export-scope"><option value="result">当前这一轮的结果</option><option value="conversation">整段对话（${data.turns.length}轮）</option></select><textarea id="export-preview" readonly aria-label="将要导出的内容"></textarea><div class="settings-actions"><button class="secondary-button" id="share-record">分享到备忘录…</button><button class="secondary-button" id="copy-record">复制内容</button><button class="secondary-button" id="download-record">下载文本</button></div><p class="sheet-intro" id="export-help">点分享后，在系统菜单中选择“备忘录”并保存。如果没有备忘录选项，可以复制后粘贴；导出不会删除云端原记录。</p>`);
      const title=resultTitle(saved);let text='';
      const update=()=>{
        const all=$('export-scope').value==='conversation';
        text=`言小办 · ${all?'对话记录':title}\n\n`+(all?data.turns.map((r,i)=>`第${i+1}轮\n${exportResult(r)}`).join('\n\n──────────\n\n'):exportResult(saved));
        $('export-preview').value=text;
      };
      update();$('export-scope').onchange=update;
      const share=$('share-record');
      if(typeof navigator.share!=='function') {share.disabled=true;$('export-help').textContent='当前浏览器不支持系统分享，请复制内容到备忘录，或下载文本。云端原记录仍会保留。';}
      share.onclick=async()=>{
        // Payload is prepared before this click, preserving iOS user activation.
        share.disabled=true;
        try {await navigator.share({title,text});toast('分享菜单已关闭，是否保存请在备忘录中确认。');}
        catch(error){toast(error.name==='AbortError'?'已取消分享，历史记录仍保留。':'暂时无法分享，可以复制或下载内容。');}
        finally{share.disabled=false;}
      };
      $('copy-record').onclick=async()=>{
        try{await navigator.clipboard.writeText(text);toast('已复制，可到备忘录粘贴。');}
        catch(_){$('export-preview').focus();$('export-preview').select();toast('请长按上方内容，选择全选并复制。');}
      };
      $('download-record').onclick=()=>{
        const url=URL.createObjectURL(new Blob(['\ufeff'+text],{type:'text/plain;charset=utf-8'}));
        const a=document.createElement('a');a.href=url;a.download='言小办-'+title.replace(/[\\/:*?"<>|\x00-\x1f]/g,'_').slice(0,50)+'.txt';
        document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
      };
    }catch(error){if(ticket===epoch&&$('detail-dialog').open)$('detail-body').textContent=error.message;}
  }
  function clearCurrent() {
    current=null;currentTurns=[];session=null;
    $('current-title').textContent='新的对话';
    $('current-content').innerHTML='<article class="result-card empty-home"><h3>想聊什么，直接说。</h3><p>其他结果仍在历史记录里，删除的对话可从“更多 → 最近删除”恢复。</p></article>';
  }
  function confirmDelete(result) {
    stopAll();
    openDetail('删除这段对话？','记录管理',`<p class="sheet-intro">将移走这段对话的全部提问、回答和查询结果。只是清理言小办里的记录，不会取消任何已经执行的操作。</p><p class="sheet-intro">可以在“更多 → 最近删除”恢复，目前不会自动清空。</p><div class="settings-actions"><button class="secondary-button danger-button" id="confirm-delete">移到最近删除</button><button class="secondary-button" id="cancel-delete">保留记录</button></div>`);
    $('cancel-delete').onclick=()=>$('detail-dialog').close();
    $('confirm-delete').onclick=async()=>{
      const button=$('confirm-delete');button.disabled=true;
      try {
        await api({action:'delete',id:result.id});
        hiddenConversations.add(result.id);
        backgroundReceipts=backgroundReceipts.filter(r=>r.id!==result.id);
        for(const [key,r] of receiptNotices)if(r.id===result.id)receiptNotices.delete(key);
        if(current?.id===result.id||session?.id===result.id){stopAll();clearCurrent();}
        tasks=tasks.filter(t=>t.id!==result.id);++historyEpoch;renderRecent();
        $('detail-dialog').close();toast('已移到最近删除，需要时可以恢复。');refreshHistory();
      }catch(error){toast(error.message);button.disabled=false;}
    };
  }
  async function showTrash() {
    stopAll();const ticket=epoch;let items=[],cursor=0;
    openDetail('最近删除','可恢复的整段对话','<p class="sheet-intro">正在读取…</p>');
    const load=async(reset=false)=>{
      const data=await api({action:'trash',before:reset?0:cursor});
      if(ticket!==epoch||!$('detail-dialog').open||$('detail-title').textContent!=='最近删除')return;
      items=reset?data.items:[...items,...data.items];cursor=data.nextCursor;
      $('detail-body').innerHTML='<p class="sheet-intro">这些对话已从首页和正常历史中移走，仍保留在云端。目前不会自动清空；恢复后可以继续查看、导出和追问。</p>'+(items.length?items.map(t=>`<section class="trash-record"><div><strong>${esc(t.title || resultTitle(t.result))}</strong><p>${t.revision}轮对话 · ${esc(timeLabel(t.result))}</p></div><button class="secondary-button" data-restore="${esc(t.id)}">恢复</button></section>`).join(''):'<p class="history-empty">最近删除是空的。</p>')+(cursor?'<button class="secondary-button" id="trash-more">读取更早的记录</button>':'');
      $('detail-body').querySelectorAll('[data-restore]').forEach(button=>button.onclick=async()=>{
        button.disabled=true;
        try{await api({action:'restore',id:button.dataset.restore});hiddenConversations.delete(button.dataset.restore);toast('已恢复到历史记录。');refreshHistory();await load(true);}
        catch(error){toast(error.message);button.disabled=false;}
      });
      if($('trash-more'))$('trash-more').onclick=async()=>{
        const button=$('trash-more');button.disabled=true;
        try{await load();}catch(error){toast(error.message);button.disabled=false;}
      };
    };
    try{await load(true);}catch(error){if(ticket===epoch&&$('detail-dialog').open)$('detail-body').textContent=error.message;}
  }

  async function showMemory() {
    stopAll();const ticket=epoch;
    openDetail('言小办记住的事','慢慢熟悉你，也由你做主。','<p class="sheet-intro">正在读取记忆…</p>');
    try {
      const data=await api({action:'memory_list'},undefined,10000);
      if(ticket!==epoch||!$('detail-dialog').open||$('detail-title').textContent!=='言小办记住的事')return;
      const changes=data.changes.map(change=>`<section class="memory-change"><p>${change.before?`原来：${esc(change.before)}<br>`:''}${change.after?`现在：${esc(change.after)}`:'已停止使用这条记忆'}</p>${change.canUndo?`<button class="text-button" data-memory-undo="${esc(change.id)}">撤销这次修改</button>`:'<span class="memory-meta">已撤销或此后又有修改</span>'}</section>`).join('');
      $('detail-body').innerHTML=`<p class="sheet-intro">言小办会把你明确说过、以后仍有用的信息单独记下，需要时才会用上。一次任务的预算、日期等仍属于那段对话。</p><button class="secondary-button" id="memory-tidy">检查重复与过时记忆</button><div class="memory-list">${data.items.length?data.items.map(item=>`<section class="memory-item"><p>${esc(item.text)}</p><details><summary>记忆来源</summary><p class="memory-meta">${item.origin==='manual'?'你在这里修改的内容':item.origin==='tidy'?'整理前的记忆':'你当时说'}：${esc(item.quote)}</p></details><div class="settings-actions"><button class="text-button" data-memory-edit="${esc(item.id)}">修改</button><button class="text-button muted-delete" data-memory-forget="${esc(item.id)}">不再记住</button></div></section>`).join(''):'<p class="history-empty">还没有单独保存的个人记忆。可以直接告诉言小办：“记住，我旅游喜欢慢节奏。”</p>'}</div><p class="sheet-intro">同一浏览器开始新对话后仍可使用，换设备或浏览器不会自动合并。自用与商用各自保存。这里的“不再记住”会停止采用该条，变更留有记录，方便撤销；原对话记录单独管理。</p><details class="memory-changes"><summary>最近的记忆变更${data.moreChanges?'（最近20条）':''}</summary>${changes||'<p class="sheet-intro">还没有变更。</p>'}</details>`;
      const mutate=async(body,button)=>{
        button.disabled=true;
        try{await api({...body,version:data.version});await showMemory();}
        catch(error){toast(error.message);button.disabled=false;}
      };
      $('detail-body').querySelectorAll('[data-memory-forget]').forEach(button=>button.onclick=()=>mutate({action:'memory_forget',id:button.dataset.memoryForget},button));
      $('detail-body').querySelectorAll('[data-memory-undo]').forEach(button=>button.onclick=()=>mutate({action:'memory_undo',id:button.dataset.memoryUndo},button));
      $('detail-body').querySelectorAll('[data-memory-edit]').forEach(button=>button.onclick=()=>{
        const item=data.items.find(x=>x.id===button.dataset.memoryEdit);
        openDetail('修改记忆','按你说的记。',`<form id="memory-edit-form"><label for="memory-edit-text">这条记忆应该是什么？</label><textarea class="memory-edit-text" id="memory-edit-text" maxlength="240" rows="4" required></textarea><div class="settings-actions"><button class="secondary-button" type="submit">保存修改</button><button class="text-button" id="memory-edit-cancel" type="button">返回</button></div></form>`);
        $('memory-edit-text').value=item.text;$('memory-edit-cancel').onclick=showMemory;
        $('memory-edit-form').onsubmit=event=>{event.preventDefault();mutate({action:'memory_edit',id:item.id,text:$('memory-edit-text').value},event.submitter);};
      });
      $('memory-tidy').onclick=async()=>{
        const button=$('memory-tidy');button.disabled=true;button.textContent='言小办正在检查…';
        try{
          const preview=await api({action:'memory_tidy'});
          if(ticket!==epoch||!$('detail-dialog').open)return;
          openDetail('记忆整理建议','先看看会怎样整理。',preview.skipped.length?'<p class="sheet-intro">这次建议尚未通过核验，记忆未改动。可以稍后再检查。</p>':preview.planned.length?preview.planned.map(p=>`<section class="memory-change"><p>原来：${p.before.map(esc).join('；')}</p><p>${p.op==='merge'?`整理后：${esc(p.content)}`:`建议停止使用：${esc(p.reason)}`}</p></section>`).join(''):'<p class="sheet-intro">言小办检查后认为暂时无需整理，原记忆保留。</p>');
          const actions=document.createElement('div');actions.className='settings-actions';
          if(preview.planned.length&&!preview.skipped.length){const applyButton=document.createElement('button');applyButton.className='secondary-button';applyButton.textContent='应用这次整理';applyButton.onclick=async()=>{applyButton.disabled=true;try{await api({action:'memory_apply',version:preview.version});await showMemory();toast('已整理，可以在变更记录中撤销。');}catch(error){toast(error.message);applyButton.disabled=false;}};actions.append(applyButton);}
          const back=document.createElement('button');back.className='text-button';back.textContent='返回记忆';back.onclick=showMemory;actions.append(back);$('detail-body').append(actions);
        }catch(error){toast(error.message);button.disabled=false;button.textContent='检查重复与过时记忆';}
      };
    }catch(error){if(ticket===epoch&&$('detail-dialog').open)$('detail-body').textContent=error.message;}
  }
  $('about-button').onclick=()=>{
    stopAll();
    openDetail('按你的节奏来。','言小办 · 更多',`<div class="settings-actions"><button class="secondary-button" id="new-conversation">开始新对话</button><button class="secondary-button" id="refresh-results">刷新历史结果</button><button class="secondary-button" id="recently-deleted">最近删除</button></div><p class="sheet-intro">历史结果保存在运行言小办的电脑，按已连接的设备隔离。目前不会自动覆盖或定期清理；每页读取20段，可以在同一段对话里继续交流。回看不会自动重新查询或播报。天气、票价、余票需要最新情况时，请明确说“更新一下”。</p><p class="sheet-intro">本预览提供网页搜索、旅行攻略、高铁与已连接平台选品；家庭记账和系统事项需先绑定并授权。价格与优惠以商品页及结算为准；航班查询需另行准备可选资料并配置自己的收费接口。</p><details class="diagnostic-details"><summary>收音与连接检查</summary><p id="diagnostic-text"></p><a href="./">前往言小办连接页 ↗</a><br><a href="./">打开旧版语音页面 ↗</a></details>`);
    $('diagnostic-text').textContent=lastSignal?`声音通道：${lastSignal.context}；峰值：${lastSignal.peak.toFixed(5)}；检测到声音：${lastSignal.heard?'是':'否'}`:'本次还没有开始收音。';
    $('new-conversation').onclick=()=>{session=null;current=null;currentTurns=[];$('detail-dialog').close();$('current-title').textContent='新的对话';$('current-content').innerHTML='<article class="result-card empty-home"><h3>想聊什么，直接说。</h3><p>已有结果仍保存在历史记录中。</p></article>';beginVoice(true);};
    $('refresh-results').onclick=()=>{refreshHistory();$('detail-dialog').close();};
    $('recently-deleted').onclick=showTrash;
    const memoryButton=document.createElement('button');memoryButton.className='secondary-button';
    memoryButton.id='personal-memory';memoryButton.textContent='言小办记住的事';memoryButton.onclick=showMemory;
    $('detail-body').querySelector('.settings-actions').append(memoryButton);
    const featuresButton=document.createElement('button');featuresButton.className='secondary-button';featuresButton.textContent='家庭记账与系统事项';featuresButton.onclick=()=>{if(window.PiFeatures){$('detail-dialog').close();window.PiFeatures.open();}};$('detail-body').querySelector('.settings-actions').append(featuresButton);
  };
  document.querySelectorAll('.close-dialog').forEach(b=>b.onclick=()=>b.closest('dialog').close());
  document.querySelectorAll('dialog').forEach(dialog=>{
    dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}});
  });
  document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(receiptTimer);receiptTimer=null;stopAll();}else refreshHistory();});
  window.addEventListener('pagehide',()=>{clearTimeout(receiptTimer);receiptTimer=null;stopAll();});
  window.addEventListener('pageshow',event=>{if(event.persisted)refreshHistory();});
  refreshHistory(true).then(connected=>{
    if(connected && connectionConfirmed && new URLSearchParams(location.search).get('entry')==='siri')probe.enter();
  });
})();
