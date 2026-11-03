(() => {
  'use strict';
  const endpoint=new URL('api/features',document.baseURI).pathname;
  const dialog=document.createElement('dialog');dialog.className='sheet';dialog.setAttribute('aria-label','家庭记账与系统事项');document.body.append(dialog);
  const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const errors={setup_link_expired:'授权码已过期，请在Mac连接页重新扫码。',household_already_created:'家庭账已启用，请由管理员生成加入链接。',device_already_bound:'这部手机已经绑定成员。',invitation_expired:'邀请已使用或已过期，请让管理员重新生成。',member_admin_required:'需要家庭账管理员操作。',setup_in_progress:'另一部手机正在启用，请稍候。',setup_outcome_unknown_do_not_repeat:'启用结果需要核实，请勿反复新建。',feature_operation_unverified:'操作尚未核实，请稍后查看状态。',invalid_name:'请填写1至60字的称呼或账本名称。'};
  async function api(body){const response=await fetch(endpoint,{credentials:'same-origin',...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});const data=await response.json();if(!response.ok)throw Error(errors[data.error]||'尚未完成，请检查连接后再试。');return data;}
  function show(content){dialog.innerHTML=`<div class="sheet-handle" aria-hidden="true"></div><header class="sheet-header"><h2>家庭记账与系统事项</h2><button class="secondary-button" data-close>关闭</button></header><div class="sheet-body feature-panel">${content}<p role="status" data-status></p></div>`;dialog.querySelector('[data-close]').onclick=()=>dialog.close();if(!dialog.open)dialog.showModal();}
  function status(text){dialog.querySelector('[data-status]').textContent=text;}
  function input(name,text,value=''){return `<label>${esc(text)}<input name="${esc(name)}" maxlength="60" value="${esc(value)}" required></label>`;}
  async function open(){
    show('<p>正在读取连接状态…</p>');
    try{const data=await api();if(!data.bound){show('<p>首次启用请在Mac上打开“手机连接说明”，用你自己的iPhone扫描“启用家庭记账与系统事项”二维码。授权后，日常继续用原来的Siri口令。</p>');return;}
      const role={owner:'管理员',editor:'可编辑家庭账',viewer:'仅查看家庭账',revoked:'家庭访问已撤销'}[data.role]||data.role;
      const connected=data.native?.reminders_authorized&&data.native?.events_authorized&&(data.native?.sources_ready??data.native?.icloud_sources?.length===1);
      show(`<p>${esc(data.name)} · ${esc(role)}</p><p>${data.family_member?'家庭账：'+esc(data.household)+'。':'家庭访问已撤销，原个人账保留。'}个人账仅你自己可见；共享权限只作用于家庭账。</p><p>${data.native_account_owner?(connected?'苹果系统事项已连接。保存后通过iCloud同步到同账户iPhone；首次仍需实测到点通知。':'苹果系统事项尚未连接，请在Mac完成系统授权。'):'这位成员尚未连接自己的苹果系统事项；不会把家人的提醒写到管理员账户。'}</p>${data.role==='owner'?`<h3>邀请家人</h3><p>家人先连接言小办，再打开邀请链接。链接只能使用一次，24小时有效。</p><label>家庭账权限<select id="family-role"><option value="editor">可记账和修改</option><option value="viewer">只查看</option><option value="my_device">绑定我自己的另一部设备</option></select></label><button class="secondary-button" id="family-invite">生成加入链接</button><div id="family-link"></div><h3>家庭成员</h3><div id="family-members"></div>`:''}`);
      if(data.role==='owner'){
        for(const member of data.members){const row=document.createElement('p');row.textContent=member.name+' · '+({owner:'管理员',editor:'可编辑',viewer:'只查看',revoked:'已撤销'}[member.role]||member.role)+(member.active?'':' · 已撤销');if(member.role!=='owner'&&member.active){const button=document.createElement('button');button.className='text-button';button.textContent='撤销家庭访问';button.onclick=async()=>{button.disabled=true;try{await api({action:'revoke',member:member.id});await open();}catch(e){status(e.message);button.disabled=false;}};row.append(button);}dialog.querySelector('#family-members').append(row);}
        dialog.querySelector('#family-role').onchange=()=>{dialog.querySelector('#family-link').replaceChildren();status('');};
        dialog.querySelector('#family-invite').onclick=async()=>{try{const invite=await api({action:'invite',role:dialog.querySelector('#family-role').value});status(invite.role==='my_device'?'此链接会授予你的管理员身份及个人账访问，请只用于你自己的设备。':'请把此一次性链接发给要加入的家人。');const url=new URL('./',location.href);url.search='';url.hash='feature_join='+invite.token;const container=dialog.querySelector('#family-link');container.replaceChildren();const field=document.createElement('input');field.readOnly=true;field.value=url.href;field.className='feature-invite-link';const copy=document.createElement('button');copy.className='secondary-button';copy.textContent='复制邀请链接';copy.onclick=async()=>{try{await navigator.clipboard.writeText(url.href);status('邀请链接已复制，请由你发给对应家人。');}catch(_){field.focus();field.select();status('请复制上面的邀请链接。');}};container.append(field,copy);}catch(e){status(e.message);}};
      }
    }catch(e){show('<p>暂时无法读取功能状态。</p>');status(e.message);}
  }
  function claim(token,bootstrap){
    show(`<p>${bootstrap?'你将在这部已连接的手机上成为家庭账管理员。将创建独立的家庭账和个人账；系统事项使用这台Mac同账户的iCloud“言小办商用提醒”和“言小办商用日程”。':'你将按邀请授予的权限加入家庭账，同时拥有独立个人账。'}</p><form id="feature-claim">${input('name','你的称呼')}${bootstrap?input('family_name','家庭账名称','家庭账')+input('personal_name','个人账名称','个人账'):''}<button class="secondary-button" type="submit">${bootstrap?'确认启用':'确认加入'}</button></form>`);
    dialog.querySelector('form').onsubmit=async event=>{event.preventDefault();const form=event.target,button=form.querySelector('button');button.disabled=true;status('正在准备账本，请稍候。');const values=Object.fromEntries(new FormData(form));try{await api({action:bootstrap?'bootstrap':'join',token,...values});await open();}catch(e){status(e.message);button.disabled=false;}};
  }
  window.PiFeatures={open};
  const fragment=new URLSearchParams(location.hash.slice(1));const bootstrap=fragment.get('feature_setup'),join=fragment.get('feature_join');
  if(bootstrap||join){fragment.delete('feature_setup');fragment.delete('feature_join');fragment.delete('login');history.replaceState(null,'',location.pathname+location.search+(fragment.toString()?'#'+fragment:''));claim(bootstrap||join,Boolean(bootstrap));}
})();
