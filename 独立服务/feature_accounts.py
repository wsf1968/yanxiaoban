"""Authenticated devices bind explicitly to people; DS never grants household access."""
import hashlib,json,secrets,sqlite3,subprocess,threading,time,uuid
from contextlib import contextmanager
from pathlib import Path
from private_json import write_private

class FeatureError(ValueError):pass

def digest(value):return hashlib.sha256(value.encode()).hexdigest()

def label(value):
    if not isinstance(value,str) or not 1<=len(value.strip())<=60 or any(ord(x)<32 for x in value):raise FeatureError('invalid_name')
    return value.strip()

class FeatureAccounts:
    def __init__(self,private,module,node):
        self.private=Path(private);self.private.mkdir(parents=True,exist_ok=True,mode=0o700);self.private.chmod(0o700)
        self.book_root=self.private/'books';self.module=Path(module);self.node=node;self.lock=threading.RLock()
        self.path=self.private/'accounts.sqlite'
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS household(id INTEGER PRIMARY KEY,actor TEXT,family TEXT,name TEXT,state TEXT);
            CREATE TABLE IF NOT EXISTS members(actor TEXT PRIMARY KEY,name TEXT,role TEXT,personal TEXT,active INTEGER);
            CREATE TABLE IF NOT EXISTS devices(owner TEXT PRIMARY KEY,actor TEXT);
            CREATE TABLE IF NOT EXISTS invitations(hash TEXT PRIMARY KEY,actor TEXT,role TEXT,expires REAL,claimed_by TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);''')
        self.path.chmod(0o600)
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    def admin_call(self,request):
        result=subprocess.run([self.node,str(self.module/'host_admin.cjs'),str(self.book_root)],input=json.dumps(request,ensure_ascii=False),text=True,capture_output=True,timeout=45)
        try:value=json.loads(result.stdout)
        except ValueError:raise FeatureError('setup_outcome_unknown_do_not_repeat') from None
        if not value.get('ok'):raise FeatureError(value.get('error','setup_failed'))
        return value['result']
    def bootstrap_token(self):
        with self.lock:
            with self.connect() as db:
                if db.execute('SELECT 1 FROM household WHERE state="ready"').fetchone():return None
            path=self.private/'bootstrap.json'
            value=json.loads(path.read_text()) if path.exists() else {}
            if value.get('expires',0)<=time.time():
                value={'token':secrets.token_urlsafe(32),'expires':time.time()+1800};write_private(path,value)
            return value['token']
    def member(self,owner):
        with self.connect() as db:
            row=db.execute('SELECT m.* FROM members m JOIN devices d ON d.actor=m.actor WHERE d.owner=? AND m.active=1',(owner,)).fetchone()
            return dict(row) if row else None
    def status(self,owner):
        member=self.member(owner)
        if not member:return {'bound':False}
        with self.connect() as db:
            house=dict(db.execute('SELECT * FROM household WHERE state="ready"').fetchone())
            people=[dict(r) for r in db.execute("SELECT actor AS id,name,role,(role!='revoked') AS active FROM members")] if member['role']=='owner' else []
        return {'bound':True,'name':member['name'],'role':member['role'],'household':house['name'],'members':people,
                'native_account_owner':member['actor']==house['actor'],'family_member':member['role']!='revoked'}
    def bootstrap(self,owner,token,name,family_name,personal_name):
        name,family_name,personal_name=map(label,[name,family_name,personal_name])
        with self.lock:
            value=json.loads((self.private/'bootstrap.json').read_text())
            if not isinstance(token,str) or not secrets.compare_digest(token,value['token']) or value['expires']<=time.time():raise FeatureError('setup_link_expired')
            with self.connect() as db:
                house=db.execute('SELECT * FROM household').fetchone()
                if house and house['state']=='ready':
                    if self.member(owner):return self.status(owner)
                    raise FeatureError('household_already_created')
                if house:
                    old=db.execute('SELECT owner FROM devices WHERE actor=?',(house['actor'],)).fetchone()
                    if not old or old['owner']!=owner:raise FeatureError('setup_in_progress')
                    actor,family=house['actor'],house['family']
                    existing=db.execute('SELECT * FROM members WHERE actor=?',(actor,)).fetchone()
                    if existing['name']!=name or house['name']!=family_name:raise FeatureError('setup_conflict')
                else:
                    actor=uuid.uuid4().hex;family='family-'+uuid.uuid4().hex
                    db.execute('INSERT INTO household VALUES(1,?,?,?,"preparing")',(actor,family,family_name))
                    db.execute('INSERT INTO members VALUES(?,?,?, ?,1)',(actor,name,'owner','personal-'+actor))
                    db.execute('INSERT INTO devices VALUES(?,?)',(owner,actor))
            self.admin_call({'command':'provision','id':family,'name':family_name,'kind':'family','owner':actor,'accountName':'日常流水'})
            self.admin_call({'command':'provision','id':'personal-'+actor,'name':personal_name,'kind':'personal','owner':actor,'accountName':'日常流水'})
            with self.connect() as db:db.execute('UPDATE household SET state="ready" WHERE id=1')
            return self.status(owner)
    def invite(self,owner,role):
        member=self.member(owner)
        if not member or member['role']!='owner' or role not in ('editor','viewer','my_device'):raise FeatureError('member_admin_required')
        token=secrets.token_urlsafe(32)
        with self.connect() as db:db.execute('INSERT INTO invitations VALUES(?,?,?,?,NULL)',(digest(token),member['actor'] if role=='my_device' else None,role,time.time()+86400))
        return {'token':token,'role':role,'expires_seconds':86400}
    def join(self,owner,token,name):
        name=label(name)
        if not isinstance(token,str):raise FeatureError('invalid_invitation')
        with self.lock:
            if self.member(owner):raise FeatureError('device_already_bound')
            with self.connect() as db:
                inv=db.execute('SELECT * FROM invitations WHERE hash=?',(digest(token),)).fetchone()
                if not inv or inv['expires']<=time.time() or inv['claimed_by']:raise FeatureError('invitation_expired')
                house=db.execute('SELECT * FROM household WHERE state="ready"').fetchone()
                actor=inv['actor'] or uuid.uuid4().hex;role=inv['role']
                if not inv['actor']:db.execute('UPDATE invitations SET actor=? WHERE hash=?',(actor,digest(token)))
            if role!='my_device':
                self.admin_call({'command':'provision','id':'personal-'+actor,'name':name+'的个人账','kind':'personal','owner':actor,'accountName':'日常流水'})
                self.admin_call({'command':'set_member','owner':house['actor'],'book_id':house['family'],'actor':actor,'role':role})
            with self.connect() as db:
                if role!='my_device':db.execute('INSERT INTO members VALUES(?,?,?,?,1)',(actor,name,role,'personal-'+actor))
                db.execute('INSERT INTO devices VALUES(?,?)',(owner,actor));db.execute('UPDATE invitations SET claimed_by=? WHERE hash=?',(owner,digest(token)))
            return self.status(owner)
    def revoke(self,owner,actor):
        member=self.member(owner)
        if not member or member['role']!='owner' or actor==member['actor']:raise FeatureError('member_admin_required')
        with self.lock:
            with self.connect() as db:
                house=db.execute('SELECT * FROM household WHERE state="ready"').fetchone()
                target=db.execute('SELECT * FROM members WHERE actor=?',(actor,)).fetchone()
                if not target:raise FeatureError('member_not_found')
            self.admin_call({'command':'set_member','owner':house['actor'],'book_id':house['family'],'actor':actor,'role':None})
            with self.connect() as db:db.execute("UPDATE members SET role='revoked' WHERE actor=?",(actor,))
            return self.status(owner)
    def handle(self,owner,body):
        action=body.get('action')
        if action=='bootstrap' and set(body)=={'action','token','name','family_name','personal_name'}:return self.bootstrap(owner,body['token'],body['name'],body['family_name'],body['personal_name'])
        if action=='invite' and set(body)=={'action','role'}:return self.invite(owner,body['role'])
        if action=='join' and set(body)=={'action','token','name'}:return self.join(owner,body['token'],body['name'])
        if action=='revoke' and set(body)=={'action','member'}:return self.revoke(owner,body['member'])
        raise FeatureError('invalid_feature_request')
