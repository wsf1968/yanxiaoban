'use strict';
// Local IPC adapter. The host supplies an authenticated actor; this is not an HTTP API.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const Database = require('better-sqlite3');
const api = require('@actual-app/api');
process.umask(0o077);

class BridgeError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function fail(code) { throw new BridgeError(code); }
function exact(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      required.some(k => !Object.hasOwn(value, k)) ||
      Object.keys(value).some(k => ![...required, ...optional].includes(k))) fail('invalid_fields');
}
function text(value, max = 500) {
  if (typeof value !== 'string' || !value.trim() || value.length > max || /[\x00-\x1f]/.test(value)) fail('invalid_text');
  return value;
}
function date(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) ||
      !Number.isFinite(Date.parse(value)) || new Date(value).toISOString().slice(0, 10) !== value) fail('invalid_date');
  return value;
}
function amount(value) {
  if (!Number.isSafeInteger(value) || value === 0 || Math.abs(value) > 9e12) fail('invalid_amount_fen');
  return value;
}
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])]));
  return value;
}
const READS = new Set(['catalog', 'query']);

class ActualBridge {
  constructor(root) {
    this.root = path.resolve(root);
    fs.mkdirSync(this.root, { recursive: true, mode: 0o700 });
    fs.chmodSync(this.root, 0o700);
    this.dataDir = path.join(this.root, 'actual');
    fs.mkdirSync(this.dataDir, { mode: 0o700, recursive: true });
    this.db = new Database(path.join(this.root, 'access-and-operations.sqlite'));
    fs.chmodSync(path.join(this.root, 'access-and-operations.sqlite'), 0o600);
    this.db.pragma('journal_mode = WAL');
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS books(id TEXT PRIMARY KEY,budget_id TEXT NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS members(book_id TEXT,actor TEXT,role TEXT NOT NULL,PRIMARY KEY(book_id,actor));
      CREATE TABLE IF NOT EXISTS operations(book_id TEXT,actor TEXT,request_id TEXT,digest TEXT,status TEXT,result TEXT,
        PRIMARY KEY(book_id,actor,request_id));
      CREATE TABLE IF NOT EXISTS attribution(book_id TEXT,transaction_id TEXT,author TEXT,last_editor TEXT,
        PRIMARY KEY(book_id,transaction_id));
    `);
    this.initialized = false;
    this.loaded = null;
    this.busy = false;
  }
  async init() {
    if (!this.initialized) {
      await api.init({ dataDir: this.dataDir });
      this.initialized = true;
    }
  }
  async close() {
    if (this.initialized) await api.shutdown();
    this.initialized = false;
    this.db.close();
  }
  async exclusive(callback) {
    if (this.busy) fail('bookkeeping_busy_retry_same_request');
    this.busy = true;
    try { return await callback(); } finally { this.busy = false; }
  }
  // Trusted installation/setup API only. Never put provisioning/member control in the model tool catalog.
  async provision({ id, name, kind, owner, accountName }) {
    return this.exclusive(async () => {
      for (const s of [id, name, owner, accountName]) text(s, 120);
      if (!['personal', 'family'].includes(kind)) fail('invalid_book_kind');
      if (this.db.prepare('SELECT 1 FROM books WHERE id=?').get(id)) fail('book_exists');
      await this.init();
      const prior = new Set((await api.getBudgets()).map(b => b.id));
      await api.runImport(name, async () => { await api.createAccount({ name: accountName }); });
      const created = (await api.getBudgets()).filter(b => !prior.has(b.id));
      if (created.length !== 1) fail('book_provision_uncertain');
      const budget = created[0];
      await api.setPreference('defaultCurrencyCode','CNY');
      if ((await api.getPreferences()).defaultCurrencyCode !== 'CNY') fail('currency_readback_mismatch');
      this.db.transaction(() => {
        this.db.prepare('INSERT INTO books VALUES(?,?,?,?)').run(id, budget.id, name, kind);
        this.db.prepare('INSERT INTO members VALUES(?,?,?)').run(id, owner, 'owner');
      })();
      this.loaded = budget.id;
      return { id, name, kind, currency: 'CNY' };
    });
  }
  // Host verifies user intent and identity before calling. The scope is the whole shared book.
  setMember(owner, bookId, actor, role) {
    text(actor, 120);
    const book = this.access(owner, bookId, false);
    if (book.role !== 'owner' || book.kind !== 'family' || actor === owner) fail('member_change_denied');
    if (role === null) this.db.prepare('DELETE FROM members WHERE book_id=? AND actor=?').run(bookId, actor);
    else {
      if (!['editor', 'viewer'].includes(role)) fail('invalid_role');
      this.db.prepare('INSERT INTO members VALUES(?,?,?) ON CONFLICT(book_id,actor) DO UPDATE SET role=excluded.role').run(bookId, actor, role);
    }
  }
  access(actor, bookId, write) {
    text(actor, 120); text(bookId, 120);
    const book = this.db.prepare('SELECT b.*,m.role FROM books b JOIN members m ON m.book_id=b.id WHERE b.id=? AND m.actor=?').get(bookId, actor);
    if (!book) fail('book_access_denied');
    if (write && !['owner', 'editor'].includes(book.role)) fail('book_read_only');
    return book;
  }
  listBooks(actor) {
    text(actor, 120);
    return this.db.prepare('SELECT b.id,b.name,b.kind,m.role FROM books b JOIN members m ON m.book_id=b.id WHERE m.actor=? ORDER BY b.id').all(actor)
      .map(b => ({ ...b, currency: 'CNY' }));
  }
  async select(book) {
    await this.init();
    if (this.loaded !== book.budget_id) {
      await api.loadBudget(book.budget_id);
      this.loaded = book.budget_id;
    }
  }
  async catalog(bookId) {
    return { accounts: await api.getAccounts(), categories: await api.getCategories(), groups: await api.getCategoryGroups(),
      members: this.db.prepare('SELECT actor,role FROM members WHERE book_id=? ORDER BY actor').all(bookId), currency: 'CNY' };
  }
  async account(id) {
    if (!(await api.getAccounts()).some(a => a.id === id && !a.closed)) fail('account_not_in_book');
  }
  async category(id) {
    if (id !== null && !(await api.getCategories()).some(c => c.id === id)) fail('category_not_in_book');
  }
  async findTransaction(id) {
    for (const a of await api.getAccounts()) {
      const rows = await api.getTransactions(a.id, '0001-01-01', '9999-12-31');
      const row = rows.find(t => t.id === id);
      if (row) return row;
    }
    fail('transaction_not_in_book');
  }
  publicTransaction(bookId, row) {
    const a = this.db.prepare('SELECT author,last_editor FROM attribution WHERE book_id=? AND transaction_id=?').get(bookId, row.id);
    return { id: row.id, account_id: row.account, date: row.date, amount_fen: row.amount, category_id: row.category,
      notes: row.notes || '', payee_id: row.payee, transfer_id: row.transfer_id || null,
      recorded_by: a?.author || null, last_editor: a?.last_editor || null };
  }
  async query(bookId, args) {
    exact(args, ['start_date', 'end_date', 'offset', 'limit'], ['account_id', 'category_id', 'recorded_by']);
    date(args.start_date); date(args.end_date);
    if (args.start_date > args.end_date || !Number.isSafeInteger(args.offset) || args.offset < 0 ||
      !Number.isSafeInteger(args.limit) || args.limit < 1 || args.limit > 100) fail('invalid_query_range');
    if (args.account_id) await this.account(args.account_id);
    if (Object.hasOwn(args, 'category_id')) await this.category(args.category_id);
    let rows = [];
    for (const a of await api.getAccounts()) {
      if (!args.account_id || args.account_id === a.id) rows.push(...await api.getTransactions(a.id, args.start_date, args.end_date));
    }
    rows = rows.map(r => this.publicTransaction(bookId, r))
      .filter(r => (!Object.hasOwn(args, 'category_id') || r.category_id === args.category_id) &&
        (!args.recorded_by || r.recorded_by === args.recorded_by))
      .sort((a,b) => a.date.localeCompare(b.date) || a.id.localeCompare(b.id));
    let income = 0, expense = 0, transfer = 0, unclassifiedInflow = 0;
    const nativeCategories = new Map((await api.getCategories()).map(c => [c.id,c]));
    const categories = Object.create(null), authors = Object.create(null);
    for (const row of rows) {
      if (row.transfer_id) { transfer += 1; continue; }
      const category = nativeCategories.get(row.category_id);
      if (category?.is_income) income += row.amount_fen;
      else if (category || row.amount_fen < 0) expense -= row.amount_fen;
      else unclassifiedInflow += row.amount_fen;
      const key = row.category_id || 'uncategorized';
      categories[key] = (categories[key] || 0) + row.amount_fen;
      const author = row.recorded_by || 'unknown'; authors[author] = (authors[author] || 0) + row.amount_fen;
    }
    if (![income, expense, unclassifiedInflow, ...Object.values(categories), ...Object.values(authors)].every(Number.isSafeInteger)) fail('aggregation_overflow');
    return { rows: rows.slice(args.offset, args.offset + args.limit), total: rows.length,
      has_more: args.offset + args.limit < rows.length,
      summary: { income_fen: income, expense_fen: expense, unclassified_inflow_fen: unclassifiedInflow,
        net_fen: income - expense + unclassifiedInflow,
        transfer_rows_excluded: transfer, by_category_net_fen: categories, by_author_net_fen: authors },
      currency: 'CNY', query: args };
  }
  validateOperation(operation, args) {
    if (operation === 'create_account') { exact(args, ['name']); text(args.name, 120); }
    else if (operation === 'create_category') {
      exact(args, ['name', 'group_id']); text(args.name, 120); text(args.group_id, 120);
    } else if (operation === 'create_group') {
      exact(args, ['name', 'is_income']); text(args.name, 120); if (typeof args.is_income !== 'boolean') fail('invalid_group');
      if (args.is_income) fail('income_group_creation_not_supported_use_existing');
    } else if (operation === 'add') {
      exact(args, ['account_id', 'date', 'amount_fen', 'category_id', 'notes']);
      text(args.account_id,120); date(args.date); amount(args.amount_fen);
      if (args.category_id !== null) text(args.category_id,120);
      if (typeof args.notes !== 'string' || args.notes.length > 500 || /[\x00-\x1f]/.test(args.notes)) fail('invalid_notes');
    } else if (operation === 'update') {
      exact(args, ['transaction_id', 'fields']); text(args.transaction_id, 120);
      exact(args.fields, [], ['date', 'amount_fen', 'category_id', 'notes']);
      if (!Object.keys(args.fields).length) fail('empty_update');
      if (Object.hasOwn(args.fields, 'date')) date(args.fields.date);
      if (Object.hasOwn(args.fields, 'amount_fen')) amount(args.fields.amount_fen);
      if (Object.hasOwn(args.fields, 'category_id') && args.fields.category_id !== null) text(args.fields.category_id,120);
      if (Object.hasOwn(args.fields, 'notes') && (typeof args.fields.notes !== 'string' || args.fields.notes.length > 500)) fail('invalid_notes');
    } else if (operation === 'delete') { exact(args, ['transaction_id']); text(args.transaction_id,120); }
    else if (operation === 'transfer') {
      exact(args, ['from_account_id','to_account_id','date','amount_fen','notes']);
      text(args.from_account_id,120);text(args.to_account_id,120);date(args.date);amount(args.amount_fen);
      if (args.amount_fen < 0 || args.from_account_id === args.to_account_id) fail('invalid_transfer');
      if (typeof args.notes !== 'string' || args.notes.length > 500) fail('invalid_notes');
    } else fail('unsupported_operation');
  }
  async apply(bookId, actor, operation, args) {
    if (operation === 'create_account') {
      const id = await api.createAccount({ name: args.name });
      const account = (await api.getAccounts()).find(a => a.id === id);
      if (!account || account.name !== args.name) fail('write_readback_mismatch');
      return { status: 'saved', account };
    }
    if (operation === 'create_group') {
      const id = await api.createCategoryGroup(args);
      const group = (await api.getCategoryGroups()).find(g => g.id === id);
      if (!group || group.name !== args.name || !!group.is_income !== args.is_income) fail('write_readback_mismatch');
      return { status: 'saved', group };
    }
    if (operation === 'create_category') {
      const group = (await api.getCategoryGroups()).find(g => g.id === args.group_id);
      if (!group) fail('group_not_in_book');
      const id = await api.createCategory({ ...args, is_income: !!group.is_income });
      const category = (await api.getCategories()).find(c => c.id === id);
      if (!category || category.name !== args.name || category.group_id !== args.group_id || !!category.is_income !== !!group.is_income) fail('write_readback_mismatch');
      return { status: 'saved', category };
    }
    if (operation === 'add') {
      await this.account(args.account_id); await this.category(args.category_id);
      const id = crypto.randomUUID();
      await api.addTransactions(args.account_id, [{ id, date: args.date, amount: args.amount_fen,
        category: args.category_id, notes: args.notes }], { runTransfers: false, learnCategories: false });
      this.db.prepare('INSERT INTO attribution VALUES(?,?,?,?)').run(bookId, id, actor, actor);
      const row = await this.findTransaction(id);
      if (row.amount !== args.amount_fen || row.date !== args.date || (row.category || null) !== args.category_id || (row.notes || '') !== args.notes) fail('write_readback_mismatch');
      return { status: 'saved', transaction: this.publicTransaction(bookId, row) };
    }
    if (operation === 'transfer') {
      await this.account(args.from_account_id); await this.account(args.to_account_id);
      const payee = (await api.getPayees()).find(p => p.transfer_acct === args.to_account_id);
      if (!payee) fail('transfer_payee_missing');
      const id = crypto.randomUUID();
      await api.addTransactions(args.from_account_id, [{ id, date: args.date, amount: -args.amount_fen, payee: payee.id, notes: args.notes }], { runTransfers: true, learnCategories: false });
      const row = await this.findTransaction(id);
      if (!row.transfer_id) fail('transfer_readback_mismatch');
      const other = await this.findTransaction(row.transfer_id);
      if (row.account !== args.from_account_id || other.account !== args.to_account_id || row.amount !== -args.amount_fen || other.amount !== args.amount_fen) fail('transfer_readback_mismatch');
      for (const id of [row.id, other.id]) this.db.prepare('INSERT INTO attribution VALUES(?,?,?,?)').run(bookId,id,actor,actor);
      return { status: 'saved', transfers: [this.publicTransaction(bookId,row),this.publicTransaction(bookId,other)] };
    }
    const before = await this.findTransaction(args.transaction_id);
    if (before.transfer_id || before.is_parent || before.parent_id) fail('complex_transaction_edit_not_enabled');
    if (operation === 'update') {
      const fields = { ...args.fields };
      if (Object.hasOwn(fields,'category_id')) { await this.category(fields.category_id); fields.category = fields.category_id; delete fields.category_id; }
      if (Object.hasOwn(fields,'amount_fen')) { fields.amount = fields.amount_fen; delete fields.amount_fen; }
      await api.updateTransaction(before.id, fields);
      const after = await this.findTransaction(before.id);
      if (Object.entries(fields).some(([k,v]) => (after[k] ?? null) !== v)) fail('write_readback_mismatch');
      this.db.prepare('INSERT INTO attribution VALUES(?,?,?,?) ON CONFLICT(book_id,transaction_id) DO UPDATE SET last_editor=excluded.last_editor').run(bookId,before.id,actor,actor);
      return { status:'updated', before: this.publicTransaction(bookId,before), transaction: this.publicTransaction(bookId,after) };
    }
    await api.deleteTransaction(before.id);
    try { await this.findTransaction(before.id); } catch (e) {
      if (e.code === 'transaction_not_in_book') return { status:'deleted', before: this.publicTransaction(bookId,before) };
      throw e;
    }
    fail('delete_readback_mismatch');
  }
  async execute(actor, bookId, requestId, operation, args) {
    return this.exclusive(async () => {
      const write = !READS.has(operation);
      const book = this.access(actor, bookId, write);
      text(requestId,160);
      if (!write && operation === 'catalog') exact(args, []);
      if (write) this.validateOperation(operation,args);
      await this.select(book);
      if (operation === 'catalog') return this.catalog(bookId);
      if (operation === 'query') return this.query(bookId,args);
      const digest = crypto.createHash('sha256').update(JSON.stringify(canonical({operation,args}))).digest('hex');
      const prior = this.db.prepare('SELECT * FROM operations WHERE book_id=? AND actor=? AND request_id=?').get(bookId,actor,requestId);
      if (prior) {
        if (prior.digest !== digest) fail('request_id_conflict');
        if (prior.status === 'rejected') fail(JSON.parse(prior.result).code);
        if (prior.status !== 'complete') fail('write_outcome_unknown_do_not_retry');
        return { ...JSON.parse(prior.result), replayed: true };
      }
      if (this.db.prepare("SELECT 1 FROM operations WHERE book_id=? AND status='pending'").get(bookId)) fail('book_has_unverified_write');
      // Save a native Actual export before every mutation; never back up a live SQLite file by raw copy.
      const backupDir = path.join(this.root,'backups');fs.mkdirSync(backupDir,{recursive:true,mode:0o700});
      fs.writeFileSync(path.join(backupDir,crypto.randomUUID()+'.zip'),await api.exportBudget(),{mode:0o600});
      this.db.prepare('INSERT INTO operations VALUES(?,?,?,?,?,NULL)').run(bookId,actor,requestId,digest,'pending');
      try {
        const result = await this.apply(bookId,actor,operation,args);
        this.db.prepare("UPDATE operations SET status='complete',result=? WHERE book_id=? AND actor=? AND request_id=?").run(JSON.stringify(result),bookId,actor,requestId);
        return { ...result, replayed:false };
      } catch (error) {
        // Scope/protocol failures before a provider write are known not-applied.
        if (['account_not_in_book','category_not_in_book','group_not_in_book','transaction_not_in_book','complex_transaction_edit_not_enabled','transfer_payee_missing'].includes(error.code)) {
          this.db.prepare("UPDATE operations SET status='rejected',result=? WHERE book_id=? AND actor=? AND request_id=?").run(JSON.stringify({code:error.code}),bookId,actor,requestId);
        }
        throw error;
      }
    });
  }
}
module.exports = { ActualBridge, BridgeError };
