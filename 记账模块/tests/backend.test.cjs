'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {ActualBridge}=require('../actual_bridge.cjs');

test('real Actual engine: household access, exact amounts, durable writes and transfer accounting',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'xiaopai-household-tests-'));
  let bridge=new ActualBridge(root),counter=0;
  const call=(actor,book,op,args,request)=>bridge.execute(actor,book,request||'request-'+(++counter),op,args);
  const query=(actor='alice',book='family')=>call(actor,book,'query',{start_date:'2026-10-01',end_date:'2026-10-31',offset:0,limit:100});
  let account,personal,category,first;
  try {
    await t.test('provisions independent personal and family books with the native API',async()=>{
      await bridge.provision({id:'family',name:'家庭账',kind:'family',owner:'alice',accountName:'家庭流水'});
      await bridge.provision({id:'personal',name:'个人账',kind:'personal',owner:'alice',accountName:'个人流水'});
      bridge.setMember('alice','family','bob','editor');bridge.setMember('alice','family','reader','viewer');
      account=(await call('alice','family','catalog',{})).accounts[0].id;
      personal=(await call('alice','personal','catalog',{})).accounts[0].id;
      assert.deepEqual(bridge.listBooks('bob').map(b=>b.id),['family']);
      assert.deepEqual(bridge.listBooks('outsider'),[]);
    });
    await t.test('unauthorized users cannot read shared books or private books',async()=>{
      await assert.rejects(query('outsider'),{code:'book_access_denied'});
      await assert.rejects(query('bob','personal'),{code:'book_access_denied'});
      assert.throws(()=>bridge.setMember('bob','family','outsider','editor'),{code:'member_change_denied'});
      assert.throws(()=>bridge.setMember('alice','personal','bob','editor'),{code:'member_change_denied'});
    });
    await t.test('uses model-supplied category names, without built-in category rules',async()=>{
      const group=await call('alice','family','create_group',{name:'家庭生活',is_income:false});
      const receipt=await call('bob','family','create_category',{name:'餐饮',group_id:group.group.id});
      category=receipt.category.id;assert.equal(receipt.category.name,'餐饮');
    });
    await t.test('two family members write the same ledger with exact fen and attribution',async()=>{
      first=await call('alice','family','add',{account_id:account,date:'2026-10-26',amount_fen:-3550,category_id:category,notes:'午饭'},'lunch');
      await call('bob','family','add',{account_id:account,date:'2026-10-26',amount_fen:-1001,category_id:category,notes:'早餐'});
      const rows=await query('bob');assert.equal(rows.total,2);assert.equal(rows.summary.expense_fen,4551);
      assert.deepEqual(new Set(rows.rows.map(r=>r.recorded_by)),new Set(['alice','bob']));
    });
    await t.test('transport retry does not add a second transaction and conflicts are refused',async()=>{
      const args={account_id:account,date:'2026-10-26',amount_fen:-3550,category_id:category,notes:'午饭'};
      const replay=await call('alice','family','add',args,'lunch');assert.equal(replay.replayed,true);
      assert.equal(replay.transaction.id,first.transaction.id);assert.equal((await query()).total,2);
      await assert.rejects(call('alice','family','add',{...args,amount_fen:-4000},'lunch'),{code:'request_id_conflict'});
    });
    await t.test('viewers can read and cannot change entries',async()=>{
      assert.equal((await query('reader')).total,2);
      await assert.rejects(call('reader','family','delete',{transaction_id:first.transaction.id}),{code:'book_read_only'});
    });
    await t.test('rejects fractions, impossible dates and fields not supported by the connector',async()=>{
      for(const amount_fen of [-0.1,NaN,Infinity,0,'-35.50'])await assert.rejects(call('alice','family','add',{account_id:account,date:'2026-10-26',amount_fen,category_id:null,notes:''}),{code:'invalid_amount_fen'});
      await assert.rejects(call('alice','family','add',{account_id:account,date:'2026-02-30',amount_fen:-100,category_id:null,notes:''}),{code:'invalid_date'});
      await assert.rejects(call('alice','family','delete',{transaction_id:first.transaction.id,sql:'DROP TABLE'}),{code:'invalid_fields'});
    });
    await t.test('cross-book account identifiers cannot leak into a family write',async()=>{
      await assert.rejects(call('bob','family','add',{account_id:personal,date:'2026-10-26',amount_fen:-100,category_id:null,notes:''}),{code:'account_not_in_book'});
      assert.equal((await query()).total,2);
    });
    await t.test('refunds offset their expense category, income is classified, and unknown inflows stay separate',async()=>{
      await assert.rejects(call('alice','family','create_group',{name:'收入',is_income:true}),{code:'income_group_creation_not_supported_use_existing'});
      const incomeGroup=(await call('alice','family','catalog',{})).groups.find(g=>g.is_income);
      const salaryCategory=await call('alice','family','create_category',{name:'工资',group_id:incomeGroup.id});
      const salary=await call('alice','family','add',{account_id:account,date:'2026-10-25',amount_fen:100000,category_id:salaryCategory.category.id,notes:'测试工资'});
      const refund=await call('alice','family','add',{account_id:account,date:'2026-10-25',amount_fen:500,category_id:category,notes:'餐饮退款'});
      const unknown=await call('alice','family','add',{account_id:account,date:'2026-10-25',amount_fen:123,category_id:null,notes:'来源待分类'});
      const result=await query();assert.equal(result.summary.income_fen,100000);assert.equal(result.summary.expense_fen,4051);
      assert.equal(result.summary.unclassified_inflow_fen,123);assert.equal(result.summary.net_fen,96072);
      for(const item of [salary,refund,unknown])await call('alice','family','delete',{transaction_id:item.transaction.id});
    });
    await t.test('updates read back the same transaction and retain its original author',async()=>{
      const result=await call('bob','family','update',{transaction_id:first.transaction.id,fields:{amount_fen:-3250}});
      assert.equal(result.transaction.id,first.transaction.id);assert.equal(result.transaction.amount_fen,-3250);
      assert.equal(result.transaction.recorded_by,'alice');assert.equal(result.transaction.last_editor,'bob');
      assert.equal((await query()).summary.expense_fen,4251);
    });
    await t.test('deletes only the selected entry and retains a pre-write native backup',async()=>{
      const result=await call('alice','family','delete',{transaction_id:first.transaction.id});
      assert.equal(result.status,'deleted');assert.equal((await query()).total,1);
      const files=fs.readdirSync(path.join(root,'backups'));assert.ok(files.length>=6);
      assert.ok(files.every(n=>n.endsWith('.zip')));
    });
    await t.test('native account transfers create two sides and are excluded from income/expense',async()=>{
      const second=await call('alice','family','create_account',{name:'家庭备用金'});
      const transfer=await call('bob','family','transfer',{from_account_id:account,to_account_id:second.account.id,date:'2026-10-26',amount_fen:50000,notes:'账户间转账'});
      assert.equal(transfer.transfers.length,2);
      const result=await query();assert.equal(result.total,3);assert.equal(result.summary.expense_fen,1001);
      assert.equal(result.summary.income_fen,0);assert.equal(result.summary.transfer_rows_excluded,2);
      await assert.rejects(call('alice','family','delete',{transaction_id:transfer.transfers[0].id}),{code:'complex_transaction_edit_not_enabled'});
    });
    await t.test('reopening the process retains data, sharing and operation deduplication',async()=>{
      await bridge.close();bridge=new ActualBridge(root);
      assert.equal((await query('bob')).total,3);
      const replay=await call('alice','family','add',{account_id:account,date:'2026-10-26',amount_fen:-3550,category_id:category,notes:'午饭'},'lunch');
      assert.equal(replay.replayed,true);assert.equal((await query()).total,3);
    });
    await t.test('revoking a member takes effect on the next request',async()=>{
      bridge.setMember('alice','family','bob',null);
      await assert.rejects(query('bob'),{code:'book_access_denied'});
    });
    await t.test('a crash with an unknown write blocks further writes while allowing inspection',async()=>{
      bridge.db.prepare('INSERT INTO operations VALUES(?,?,?,?,?,NULL)').run('family','alice','crash-fixture','fixture','pending');
      await assert.rejects(call('alice','family','add',{account_id:account,date:'2026-10-26',amount_fen:-100,category_id:null,notes:'must not write'}),{code:'book_has_unverified_write'});
      assert.equal((await query()).total,3);
    });
  } finally { await bridge.close(); }
});
