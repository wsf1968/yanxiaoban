'use strict';
const fs = require('node:fs');
const lockfile = require('proper-lockfile');
const { ActualBridge } = require('./actual_bridge.cjs');
// Actual emits diagnostics: keep stdout a JSON IPC channel and cap stderr upstream.
for (const method of ['log','info','warn','debug']) console[method] = (...args) => process.stderr.write(args.map(a => typeof a === 'string' ? a : JSON.stringify(a)).join(' ')+'\n');
async function main() {
  const root = process.argv[2];
  if (!root || !require('node:path').isAbsolute(root)) throw new Error('absolute_private_root_required');
  const raw = fs.readFileSync(0,'utf8');
  if (raw.length > 100000) throw new Error('request_too_large');
  const request = JSON.parse(raw);
  fs.mkdirSync(root,{recursive:true,mode:0o700});
  const release = await lockfile.lock(root,{stale:300000,update:10000,retries:0});
  const bridge = new ActualBridge(root);
  try {
    let result;
    if (request.command === 'list_books') result = bridge.listBooks(request.actor);
    else if (request.command === 'execute') result = await bridge.execute(request.actor,request.book_id,request.request_id,request.operation,request.args);
    else throw new Error('unsupported_command');
    process.stdout.write(JSON.stringify({ok:true,result})+'\n');
  } finally { try { await bridge.close(); } finally { await release(); } }
}
main().catch(error => { process.stdout.write(JSON.stringify({ok:false,error:error.code || 'provider_failure'})+'\n');process.exitCode=1; });
