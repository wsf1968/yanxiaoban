// Synthetic accounts only. Not an installation command for the live assistant.
const {ActualBridge}=require('../actual_bridge.cjs');
for(const method of ['log','info','warn','debug'])console[method]=(...args)=>process.stderr.write(args.map(String).join(' ')+'\n');
(async()=>{
  const bridge=new ActualBridge(process.argv[2]);
  try{
    await bridge.provision({id:'family-fixture',name:'测试家庭账',kind:'family',owner:'fixture-alice',accountName:'家庭日常流水'});
    bridge.setMember('fixture-alice','family-fixture','fixture-bob','editor');
    await bridge.provision({id:'personal-fixture',name:'测试个人账',kind:'personal',owner:'fixture-alice',accountName:'个人日常流水'});
    process.stdout.write(JSON.stringify({fixture_only:true,books:bridge.listBooks('fixture-alice')})+'\n');
  }finally{await bridge.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
