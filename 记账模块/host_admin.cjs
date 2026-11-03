'use strict';
// Trusted local setup API, separate from the model's tool runner.
const fs=require('node:fs'), path=require('node:path'), lockfile=require('proper-lockfile');
const {ActualBridge}=require('./actual_bridge.cjs');
for(const name of ['log','info','warn','debug'])console[name]=()=>{};
(async()=>{const root=process.argv[2],request=JSON.parse(fs.readFileSync(0,'utf8'));
 if(!path.isAbsolute(root))throw Error('private_root_required');fs.mkdirSync(root,{recursive:true,mode:0o700});
 const release=await lockfile.lock(root,{stale:300000,update:10000,retries:0});const bridge=new ActualBridge(root);
 try{let result;
 if(request.command==='provision'){
  const prior=bridge.listBooks(request.owner).find(b=>b.id===request.id);
  if(prior){if(prior.name!==request.name||prior.kind!==request.kind)throw Error('setup_conflict');result=prior;}
  else result=await bridge.provision(request);
 }else if(request.command==='set_member'){bridge.setMember(request.owner,request.book_id,request.actor,request.role);result={saved:true};}
 else throw Error('unsupported_setup_command');process.stdout.write(JSON.stringify({ok:true,result}));
 }finally{await bridge.close();await release();}
})().catch(e=>{process.stdout.write(JSON.stringify({ok:false,error:e.code||e.message||'setup_failed'}));process.exitCode=1;});
