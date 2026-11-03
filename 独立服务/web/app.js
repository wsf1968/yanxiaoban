const $ = id => document.getElementById(id);
function element(tag, text, className) {
  const node = document.createElement(tag); if (text) node.textContent = text;
  if (className) node.className = className; return node;
}
async function api(path, body) {
  const response = await fetch(path, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {});
  const data = await response.json(); if (!response.ok) throw new Error(data.message || '请求未完成'); return data;
}
let configuration = {};
let cardCounter = 0;
let initialized = false;

function readVoice(fragment) {
  const params = new URLSearchParams(fragment.replace(/^#/, ''));
  if (!params.has('voice')) return null;
  const query = (params.get('voice') || '').trim();
  return {query, valid: query.length >= 2 && query.length <= 75};
}

function consumeVoice() {
  if (!initialized) return;
  const voice = readVoice(location.hash);
  if (!voice) return;
  // Remove the handoff text before any further navigation or reload.
  history.replaceState(null, '', location.pathname + location.search);
  $('mode').value = 'search';
  $('query').value = voice.query;
  updateMode();
  if (!voice.valid) {
    $('connection').textContent = '语音问题请控制在2至75字，修改后可重新提交。';
    return;
  }
  if (!configuration.ready) {
    $('connection').textContent = '语音已收到，搜索接口尚未配置，请先在电脑检查。';
    return;
  }
  if ($('request').requestSubmit) $('request').requestSubmit();
  else $('request').dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}));
}
window.addEventListener('hashchange', consumeVoice);
function updateMode() {
  $('search-fields').hidden = $('mode').value !== 'search';
  $('travel-fields').hidden = $('mode').value !== 'travel';
  $('train-fields').hidden = $('mode').value !== 'train';
  $('submit').disabled = !($('mode').value === 'train' ? configuration.train_ready : configuration.ready);
}
$('mode').addEventListener('change', updateMode);
const beijingDay = new Date(Date.now()+8*3600000).toISOString().slice(0,10);
$('train-date').min=beijingDay;
$('train-date').max=new Date(Date.parse(beijingDay+'T00:00:00Z')+14*86400000).toISOString().slice(0,10);
$('train-date').value=new Date(Date.parse(beijingDay+'T00:00:00Z')+86400000).toISOString().slice(0,10);
function render(container, result) {
  container.append(element('p', result.notice, 'notice'));
  if (result.trains) { renderTrains(container,result); return; }
  if (result.message) container.append(element('p', result.message, result.state === 'partial' ? 'error' : ''));
  for (const section of result.summary?.sections || []) {
    container.append(element('h3',section.heading)); const list = element('ul');
    for (const item of section.items) {
      const li=element('li',item.text);
      for (const id of [...new Set(item.evidence.map(e=>e.source_id))]) {
        const a=element('a',` [${id}]`); a.href=`#source-${container.id}-${id}`; li.append(a);
      }
      const details=element('details'); details.append(element('summary','查看对应的摘要片段'));
      for(const ref of item.evidence) details.append(element('p',`[${ref.source_id}] ${ref.quote}`));
      li.append(details); list.append(li);
    } container.append(list);
  }
  if(result.summary?.uncertainties?.length) {
    container.append(element('h3','仍需确认')); const ul=element('ul');
    result.summary.uncertainties.forEach(x=>ul.append(element('li',x))); container.append(ul);
  }
  if(result.sources.length) container.append(element('h3','来源 · 点击查看原页面'));
  for(const source of result.sources) {
    const p=element('p');p.id=`source-${container.id}-${source.id}`;
    const a=element('a',`[${source.id}] ${source.title}`);a.href=source.url;a.target='_blank';a.rel='noopener noreferrer';p.append(a);
    p.append(element('br'));p.append(element('small',`${source.kind === 'official' ? '官方域名搜索结果 · ' : ''}索引提供日期：${source.date || '未知'}（不代表正文已更新）`));container.append(p);
  }
}
function renderTrains(container,result) {
  container.append(element('p',`${result.provider} · 查询时间：${result.queried_at.replace('T',' ')}${result.cached ? ' · 使用一分钟内缓存' : ''}`, 'hint'));
  if(result.message) container.append(element('p',result.message));
  container.append(element('p',`接口返回 ${result.trains.length} 趟，按出发时间排序。`));
  const list=element('div');
  result.trains.forEach((train,index)=>{
    const card=element('section',null,'train-card');
    card.append(element('h3',`${train.train_no} · ${train.departure_time} → ${train.arrival_time}`));
    card.append(element('p',`${train.departure_station} → ${train.arrival_station} · 历时 ${train.duration}`));
    const seats=element('ul');
    train.seats.forEach(seat=>seats.append(element('li',`${seat.seat_name} · ${seat.price === null ? '价格未提供' : '¥'+seat.price} · 余票：${seat.availability}`)));
    card.append(seats);
    if(!train.booking_available) card.append(element('p','接口标记该班次当前不可预订','hint'));
    if(index < 5) list.append(card);
    else {
      let more=list.querySelector('details');
      if(!more){more=element('details');more.append(element('summary','展开其余车次'));list.append(more);}
      more.append(card);
    }
  });
  container.append(list);
  const link=element('a','前往12306官网确认 / 购票');link.href=result.purchase_url;link.target='_blank';link.rel='noopener noreferrer';container.append(link);
  container.append(element('p','打开12306首页，需要自行填写查询条件；未提供车次直达购票链接。','hint'));
}
$('request').addEventListener('submit', async event => {
  event.preventDefault(); $('submit').disabled=true;
  const mode=$('mode').value;
  const request=mode==='search'?{mode,query:$('query').value}:mode==='travel'?{mode,destination:$('destination').value,days:Number($('days').value),date:$('date').value}:{mode,departure_station:$('departure-station').value,arrival_station:$('arrival-station').value,date:$('train-date').value,filter:$('train-filter').value};
  const card=element('article',null,'result');card.id=`task-${Date.now()}-${++cardCounter}`;
  card.append(element('h2',mode==='search'?request.query:mode==='travel'?`${request.destination} · ${request.days}天攻略`:`${request.departure_station} → ${request.arrival_station} · ${request.date}`));
  const status=element('p','准备查询…');card.append(status);$('conversation').prepend(card);
  try {
    const created=await api('/api/tasks',request);const deadline=Date.now()+180000;
    while(Date.now()<deadline) {
      const task=await api('/api/tasks/'+encodeURIComponent(created.id));status.textContent=task.progress || '正在处理';
      if(task.result){status.textContent=task.state==='complete'?(mode==='train'?'查询完成，以购票页为准':'整理完成，信息仍需核实'):task.state==='empty'?'没有匹配结果':'已返回部分资料';render(card,task.result);return;}
      if(task.state==='failed')throw new Error(task.message);
      await new Promise(resolve=>setTimeout(resolve,1200));
    } throw new Error('等待超时，任务可能仍在处理；暂时不要重复提交。');
  } catch(error){status.textContent=error.message;status.className='error';}
  finally {updateMode();}
});
api('/api/status').then(x=>{configuration=x;initialized=true;$('connection').textContent=`搜索/攻略：${x.ready?'已配置':'待配置'} · 高铁：${x.train_ready?'已配置':'待配置'} · ${x.access === 'lan' ? '局域网 · 已登录' : x.access === 'lan-test' ? '局域网临时测试' : '独立环境'}`;updateMode();consumeVoice();}).catch(()=>{$('connection').textContent='服务未连接，请重新打开首页；已登录设备无需重新授权';$('submit').disabled=true;});
