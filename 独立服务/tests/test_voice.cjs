const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

function fixture(hash, ready=true) {
  const nodes = new Map(), calls = [], events = {};
  const make = () => ({value:'',listeners:{},children:[],
    append(...children){this.children.push(...children);},prepend(child){this.children.unshift(child);},
    addEventListener(name,fn){this.listeners[name]=fn;},
    requestSubmit(){this.listeners.submit({preventDefault(){}});}});
  const get = id => {if(!nodes.has(id)) nodes.set(id,make());return nodes.get(id);};
  const location={hash,pathname:'/',search:''};
  const context=vm.createContext({URLSearchParams,Date,Set,Event,
    document:{getElementById:get,createElement:make},
    window:{addEventListener:(name,fn)=>events[name]=fn},location,
    history:{replaceState:()=>location.hash=''},
    fetch:async (url,options)=>{
      calls.push({url,options});
      const data=url==='/api/status'?{ready,train_ready:true}:url==='/api/tasks'?{id:'fixture-task'}:
        {state:'empty',result:{state:'empty',sources:[],notice:'本地模拟测试',message:'测试完成'}};
      return {ok:true,json:async()=>data};
    },setTimeout});
  vm.runInContext(source,context);
  return {nodes,calls,location,events};
}
const settle = () => new Promise(resolve=>setImmediate(resolve));

test('Chinese and special characters hand off once, fragment removed, reload cannot resubmit',async()=>{
  const query='成都博物馆 & 展览 + 公交？';
  const f=fixture('#voice='+encodeURIComponent(query));await settle();
  const submissions=f.calls.filter(c=>c.url==='/api/tasks');
  assert.equal(submissions.length,1);
  assert.deepEqual(JSON.parse(submissions[0].options.body),{mode:'search',query});
  assert.equal(f.location.hash,'');
  f.events.hashchange();await settle();
  assert.equal(f.calls.filter(c=>c.url==='/api/tasks').length,1);
  const reload=fixture(f.location.hash);await settle();
  assert.equal(reload.calls.filter(c=>c.url==='/api/tasks').length,0);
});

test('unrelated citation anchors, missing key, empty or oversized speech never submit',async()=>{
  for(const hash of ['#source-task-1','#voice=','#voice='+encodeURIComponent('长'.repeat(76))]) {
    const f=fixture(hash);await settle();
    assert.equal(f.calls.filter(c=>c.url==='/api/tasks').length,0);
  }
  const noKey=fixture('#voice='+encodeURIComponent('普通问题'),false);await settle();
  assert.equal(noKey.calls.filter(c=>c.url==='/api/tasks').length,0);
});

test('same-tab next Siri handoff works without changing destination or interpreting markup',async()=>{
  const f=fixture('');await settle();
  f.location.hash='#voice='+encodeURIComponent('<script>alert(1)</script>');
  f.events.hashchange();await settle();
  assert.equal(f.nodes.get('query').value,'<script>alert(1)</script>');
  assert.equal(f.location.pathname,'/');
  assert.equal(f.calls.filter(c=>c.url==='/api/tasks').length,1);
  assert.equal(f.nodes.get('conversation').children[0].children[0].textContent,'<script>alert(1)</script>');
});

function loginFixture(hash, alreadyLoggedIn=false, search=""){
  const nodes=new Map(),calls=[];
  const get=id=>{if(!nodes.has(id))nodes.set(id,{value:'',addEventListener(){}});return nodes.get(id);};
  const location={hash,pathname:'/',search,replace:value=>calls.push({redirect:value})};
  const context=vm.createContext({URLSearchParams,document:{getElementById:get},location,
    history:{replaceState(_state,_title,url){location.hash=url.includes('#')?'#'+url.split('#')[1]:'';}},
    fetch:async(url,options)=>{
      calls.push({url,options});
      return {ok:url==='api/pair'||alreadyLoggedIn,json:async()=>({})};
    }});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/pair.js'),'utf8'),context);
  return {calls,location,nodes};
}

test('first login link logs in without typing and preserves a pending Siri question',async()=>{
  const f=loginFixture('#login=123456789012&voice='+encodeURIComponent('成都有哪些博物馆'),false,'?entry=siri');
  await settle();
  const logins=f.calls.filter(c=>c.url==='api/pair');
  assert.equal(logins.length,1);
  assert.equal(JSON.parse(logins[0].options.body).code,'123456789012');
  const redirect=f.calls.find(c=>c.redirect).redirect;
  assert.equal(new URLSearchParams(redirect.split('#')[1]).get('voice'),'成都有哪些博物馆');
  assert.equal(redirect.includes('login='),false);
  assert.ok(redirect.startsWith('./?entry=siri#'));
});

test('existing browser login resumes cross-app navigation without another pairing request',async()=>{
  const f=loginFixture('#voice='+encodeURIComponent('普通搜索问题'),true);
  await settle();
  assert.equal(f.calls.filter(c=>c.url==='api/pair').length,0);
  assert.equal(f.calls.filter(c=>c.redirect).length,1);
  assert.ok(f.calls.find(c=>c.redirect).redirect.startsWith('./#voice='));
});
