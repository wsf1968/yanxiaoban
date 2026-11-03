"""Transactional execution boundary. No semantic selection and no native writes.

Only authenticated host code constructs Scope. Never construct it from a JSON body.
A receipt is evidence supplied by a trusted native adapter, not a browser callback.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import uuid

from .engine import protocol_errors


class BoundaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Scope:
    owner: str
    device: str
    # Supplied by the host's current device authorization check on EACH call.
    authorized: bool = False


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Journal:
    """One local DB per commercial installation, never a production default path.

    Adapter capability IDs must come from a tested local adapter manifest, not DS.
    This API is transport-independent; it does not authorize remote callers itself.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._transaction() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS requests (
              owner TEXT, device TEXT, request_id TEXT, fingerprint TEXT,
              PRIMARY KEY(owner,device,request_id));
            CREATE TABLE IF NOT EXISTS operations (
              id TEXT PRIMARY KEY, owner TEXT, device TEXT, request_id TEXT,
              position INTEGER, model_id TEXT, action TEXT, booking TEXT,
              state TEXT, nonce_hash TEXT, receipt TEXT,
              UNIQUE(owner,device,request_id,position));
            CREATE TABLE IF NOT EXISTS items (
              owner TEXT, device TEXT, id TEXT, kind TEXT, fields TEXT,
              booking TEXT, deleted INTEGER DEFAULT 0,
              PRIMARY KEY(owner,device,id));
            CREATE TABLE IF NOT EXISTS transitions (
              sequence INTEGER PRIMARY KEY, operation_id TEXT, state TEXT,
              recorded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            if db.in_transaction: db.commit()
        except Exception:
            if db.in_transaction: db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _check(scope):
        if not isinstance(scope, Scope) or not scope.authorized or not scope.owner or not scope.device:
            raise BoundaryError('unauthorized_device')

    def _row(self, db, scope, operation_id):
        self._check(scope)
        row = db.execute('SELECT * FROM operations WHERE id=? AND owner=? AND device=?',
                         (operation_id, scope.owner, scope.device)).fetchone()
        if row is None: raise BoundaryError('unknown_operation')
        return row

    @staticmethod
    def _state(db, operation_id, state):
        db.execute('UPDATE operations SET state=? WHERE id=?', (state, operation_id))
        db.execute('INSERT INTO transitions(operation_id,state) VALUES (?,?)', (operation_id, state))

    @staticmethod
    def _snapshot(row):
        return {key: row[key] for key in ('id','request_id','position','model_id','state')}

    def stage(self, scope, request_id, payload, decision):
        """Store the approved raw decision atomically, keeping DS IDs as provenance.

        request_id must be allocated by the host for the actual user turn and reused
        on retries. A new model output cannot overwrite an existing request.
        """
        self._check(scope)
        if not isinstance(request_id, str) or not request_id or payload.get('request_id') != request_id:
            raise BoundaryError('request_binding_mismatch')
        errors = protocol_errors(decision, payload)
        if errors: raise BoundaryError('invalid_decision: ' + '; '.join(errors))
        fingerprint = digest({'input': payload, 'decision': decision})
        with self._transaction() as db:
            old = db.execute('SELECT fingerprint FROM requests WHERE owner=? AND device=? AND request_id=?',
                             (scope.owner,scope.device,request_id)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint: raise BoundaryError('request_conflict')
            else:
                db.execute('INSERT INTO requests VALUES (?,?,?,?)', (scope.owner,scope.device,request_id,fingerprint))
                for index, action in enumerate(decision['actions']):
                    operation_id = uuid.uuid4().hex
                    db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                               (operation_id,scope.owner,scope.device,request_id,index,action['operation_id'],
                                encode(action),encode(decision['booking']),'prepared',None,None))
                    self._state(db,operation_id,'prepared')
            return [self._snapshot(r) for r in db.execute(
                'SELECT * FROM operations WHERE owner=? AND device=? AND request_id=? ORDER BY position',
                (scope.owner,scope.device,request_id))]

    @staticmethod
    def _requirements(action):
        required = {action['tool'], 'booking_metadata', 'readback', 'operation_marker'}
        params = action.get('changes', action)
        if 'end_at' in params and params['end_at'] is None: required.add('nullable_event_end')
        if action['tool'] == 'create_reminder' and action['remind_at'] is None: required.add('undated_reminder')
        if 'remind_at' in params and params['remind_at'] is None: required.add('no_alert')
        return required

    def claim(self, scope, operation_id, capabilities):
        """One delivery only. Reopening/restarting never reissues a dispatch token.

        An unresolved operation blocks subsequent dispatch for this device. The
        adapter must persist its own operation marker before native execution.
        """
        self._check(scope)
        with self._transaction() as db:
            row = self._row(db,scope,operation_id)
            if row['state'] != 'prepared': raise BoundaryError('already_dispatched_or_terminal')
            unresolved = db.execute("SELECT id FROM operations WHERE owner=? AND device=? AND state IN ('dispatched','unknown')",
                                    (scope.owner,scope.device)).fetchone()
            if unresolved: raise BoundaryError('reconcile_unresolved_operation_first')
            earlier = db.execute("SELECT id FROM operations WHERE owner=? AND device=? AND request_id=? AND position<? AND state!='succeeded'",
                                 (scope.owner,scope.device,row['request_id'],row['position'])).fetchone()
            if earlier: raise BoundaryError('prior_operation_not_succeeded')
            action = json.loads(row['action'])
            missing = self._requirements(action) - set(capabilities)
            if missing: raise BoundaryError('unsupported_adapter_capability: '+','.join(sorted(missing)))
            if 'target_id' in action:
                item = db.execute('SELECT * FROM items WHERE owner=? AND device=? AND id=? AND deleted=0',
                                  (scope.owner,scope.device,action['target_id'])).fetchone()
                if item is None: raise BoundaryError('target_not_owned_by_device')
                if action['tool']=='update_item':
                    allowed = {'title','remind_at','notes'} if item['kind']=='reminder' else {'title','start_at','end_at','remind_at','location','notes'}
                    if set(action['changes'])-allowed: raise BoundaryError('unsupported_target_field')
                    if item['kind']=='reminder' and 'remind_at' in action['changes'] and action['changes']['remind_at'] is None and 'undated_reminder' not in capabilities:
                        raise BoundaryError('unsupported_adapter_capability: undated_reminder')
            nonce = secrets.token_urlsafe(32)
            db.execute('UPDATE operations SET nonce_hash=? WHERE id=?', (hashlib.sha256(nonce.encode()).hexdigest(),operation_id))
            self._state(db,operation_id,'dispatched')
            return {'protocol_version':1,'operation_id':operation_id,'receipt_token':nonce,
                    'action_sha256':digest(action),'action':action,'booking':json.loads(row['booking'])}

    def cancel_prepared(self, scope, operation_id):
        """Cancel locally only while the operation has never been delivered."""
        with self._transaction() as db:
            row = self._row(db,scope,operation_id)
            if row['state'] != 'prepared': raise BoundaryError('already_dispatched_or_terminal')
            self._state(db,operation_id,'cancelled')
            return self._snapshot(self._row(db,scope,operation_id))

    def mark_uncertain(self, scope, operation_id):
        """Timeout, x-success/cancel/error and disconnect are not native proof."""
        with self._transaction() as db:
            row = self._row(db,scope,operation_id)
            if row['state'] in ('dispatched','unknown'): self._state(db,operation_id,'unknown')
            return self._snapshot(self._row(db,scope,operation_id))

    def acknowledge(self, scope, operation_id, token, receipt):
        """Called ONLY after authenticated native targeted readback.

        Browser callback query strings are NEVER receipts. The host must bind the
        native sender to Scope. Token possession alone does not authenticate it.
        """
        with self._transaction() as db:
            row = self._row(db,scope,operation_id)
            if not isinstance(token,str) or not row['nonce_hash'] or not secrets.compare_digest(
                    row['nonce_hash'],hashlib.sha256(token.encode()).hexdigest()):
                raise BoundaryError('invalid_receipt_token')
            if row['receipt']:
                if row['receipt'] != encode(receipt): raise BoundaryError('receipt_conflict')
                return self._snapshot(row)
            if row['state'] not in ('dispatched','unknown'): raise BoundaryError('not_dispatched')
            action = json.loads(row['action'])
            expected = {'protocol_version','operation_id','action_sha256','outcome','native_id','readback'}
            if set(receipt) != expected or receipt['protocol_version'] != 1 or receipt['operation_id'] != operation_id or receipt['action_sha256'] != digest(action):
                raise BoundaryError('receipt_binding_mismatch')
            if receipt['outcome'] not in ('succeeded','unknown','not_applied'): raise BoundaryError('unsupported_receipt_outcome')
            if receipt['outcome']=='unknown':
                # No terminal receipt stored: a later targeted reconciliation may resolve it.
                self._state(db,operation_id,'unknown')
                return self._snapshot(self._row(db,scope,operation_id))
            if receipt['outcome']=='not_applied':
                # A native durable ledger must prove execution never started.
                # Cancellation/error callbacks alone must use mark_uncertain instead.
                expected_back={'operation_id':operation_id,'execution_state':'not_started',
                               'exists':None,'fields':None,'booking':json.loads(row['booking'])}
                if receipt['native_id'] is not None or receipt['readback'] != expected_back:
                    raise BoundaryError('missing_native_no_write_proof')
                db.execute('UPDATE operations SET receipt=? WHERE id=?',(encode(receipt),operation_id))
                self._state(db,operation_id,'not_applied')
                return self._snapshot(self._row(db,scope,operation_id))
            native_id = receipt['native_id']
            if not isinstance(native_id,str) or not native_id: raise BoundaryError('missing_native_id')
            booking = json.loads(row['booking'])
            back = receipt['readback']
            if not isinstance(back,dict) or set(back) != {'operation_id','exists','fields','booking'} or back['operation_id'] != operation_id or type(back['exists']) is not bool:
                raise BoundaryError('readback_binding_mismatch')
            owned = db.execute('SELECT * FROM items WHERE owner=? AND device=? AND id=?',
                               (scope.owner,scope.device,native_id)).fetchone()
            tool = action['tool']
            if tool.startswith('create_'):
                if owned: raise BoundaryError('native_id_already_owned')
                fields = {key:action[key] for key in (('title','start_at','end_at','remind_at','location','notes') if tool=='create_event' else ('title','remind_at','notes'))}
                kind = 'event' if tool=='create_event' else 'reminder'
            else:
                if native_id != action['target_id'] or owned is None or owned['deleted']:
                    raise BoundaryError('target_receipt_mismatch')
                fields = json.loads(owned['fields'])
                kind = owned['kind']
                if tool=='update_item': fields.update(action['changes'])
            deleted = tool == 'delete_item'
            if back['exists'] != (not deleted) or back['fields'] != (None if deleted else fields) or back['booking'] != booking:
                raise BoundaryError('native_readback_mismatch')
            db.execute('INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?)',
                       (scope.owner,scope.device,native_id,kind,encode(fields),encode(booking),int(deleted)))
            db.execute('UPDATE operations SET receipt=? WHERE id=?', (encode(receipt),operation_id))
            self._state(db,operation_id,'succeeded')
            return self._snapshot(self._row(db,scope,operation_id))

    def get(self, scope, operation_id):
        with self._transaction() as db: return self._snapshot(self._row(db,scope,operation_id))

    def existing_items(self, scope):
        self._check(scope)
        with self._transaction() as db:
            return [{'id':r['id'],'kind':r['kind'],**json.loads(r['fields']),'booking':json.loads(r['booking'])}
                    for r in db.execute('SELECT * FROM items WHERE owner=? AND device=? AND deleted=0', (scope.owner,scope.device))]

    def model_receipt(self, scope, operation_id):
        """Factual context for DS completion speech; no program-written business reply."""
        with self._transaction() as db:
            row = self._row(db,scope,operation_id)
            raw = json.loads(row['receipt']) if row['receipt'] else None
            return {'id':row['model_id'],'execution_id':row['id'],'tool':json.loads(row['action'])['tool'],
                    'status':({'create_event':'saved','create_reminder':'saved','update_item':'updated','delete_item':'deleted'}[json.loads(row['action'])['tool']] if row['state']=='succeeded' else ('error' if row['state']=='not_applied' else ('cancelled' if row['state']=='cancelled' else 'unknown'))),
                    'execution_state':row['state'],'item_id':raw['native_id'] if raw else None,
                    'verified_native_readback':row['state']=='succeeded'}
