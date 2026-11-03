"""Connect audited feature modules to the existing DS-selected conversation tools."""
import hashlib,json,os,shutil,subprocess,sys,threading,time,uuid
import fcntl
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from private_json import write_private
from feature_accounts import FeatureAccounts,FeatureError

PROJECT=Path(__file__).resolve().parents[1]
for folder in ['秘书模块','记账模块']:
    if str(PROJECT/folder) not in sys.path:sys.path.insert(0,str(PROJECT/folder))
from xiaopai_bookkeeping.engine import LocalActual,Bookkeeping
from xiaopai_secretary import Secretary,Journal,Scope
from xiaopai_secretary.journal import BoundaryError
from xiaopai_secretary.model import deepseek_model

DIRECTORY='''
新增工具bookkeeping与secretary，只有enabledExecutors含对应项时可调用。由你结合完整对话决定，不按关键词机械分流。
bookkeeping处理个人/家庭收支录入、查询统计、修改与撤销；secretary处理日程、待办、计划和苹果系统到点提醒的创建、查看、修改和取消。
调用任一工具时capability取该工具名，action=prepare，query={"tool":"对应工具名","args":{}}；取消已保存的提醒也由secretary读取原话决定删除动作，不能仅返回通用cancel口播。
args必须为空对象。连接层将本轮原始用户话、完整本会话历史、真实身份与原请求ID直接交模块；不要改写原话或代填金额、日期、账本。
记账由用户明确选择家庭账或个人账；多个账本且无明确选择时澄清，不猜默认账本。用户明确要求记住偏好而非日程/待办时继续使用原个人记忆，不擅自安排提醒。
这些模块只有真实写入回执才可交付完成。未绑定成员时会提供连接指引，不假称已开账或已提醒。家人的编辑权限必须由账本管理员在设置中邀请，模型不能授权。
秘书使用当前Mac的苹果系统事项连接，同账户iCloud同步手机；尚未验证手机通知时不能宣称已在手机响铃，不承诺电脑离线前尚未同步的事项仍会通知。
联调真实失败：用户明确说“明天上午九点到十点复习，先别保存”，主回复却追问“怎么分配这半小时”，把一小时变成半小时。请自行纠正：开始、结束及它们对应的时长必须一致，不把前一条任务的时间带入本轮；要补问复习内容时也须保留已有的一小时，不发明30分钟。秘书可做未保存的安排草案，不必为了不保存而退出该工具。
'''

class NativeAdapter:
    capabilities={'create_event','create_reminder','update_item','delete_item','booking_metadata','readback','operation_marker','undated_reminder','no_alert'}
    def __init__(self,private):self.private=Path(private);self.app=self.private/'native/言小办系统事项.app';self.lock=threading.Lock()
    def call(self,command,**fields):
        with self.lock:
            folder=self.private/'features/native-requests';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            request=folder/(uuid.uuid4().hex+'.json');result=request.with_suffix('.result.json')
            write_private(request,dict(command=command,**fields))
            lock_path=folder/'execution.lock'
            lock_file=lock_path.open('a');lock_path.chmod(0o600)
            fcntl.flock(lock_file.fileno(),fcntl.LOCK_EX)
            try:
                process=subprocess.run(['/usr/bin/open','-g','-n','-W',str(self.app),'--args',str(request)],capture_output=True,text=True,timeout=45)
                if process.returncode or not result.exists():return {'ok':False,'error':'native_component_unavailable'}
                value=json.loads(result.read_text())
                return value if isinstance(value,dict) else {'ok':False,'error':'invalid_native_result'}
            except (OSError,ValueError,subprocess.TimeoutExpired):return {'ok':False,'error':'native_outcome_unknown'}
            finally:
                fcntl.flock(lock_file.fileno(),fcntl.LOCK_UN);lock_file.close()
    def status(self):return self.call('status')

def displayed_sources(results):
    sources=[];presentation={'items':[],'ordered_source_ids':[]}
    for result in results:
        turn=result.get('requestId','')
        for original in result.get('sources',[]):
            if isinstance(original,dict) and original.get('id'):
                sources.append({**original,'id':turn+'-source-'+str(original['id']),'original_source_id':original['id'],'shown_in_turn':turn})
        rows=result.get('rail',{}).get('trains',[]);kind='train'
        if result.get('commercial',{}).get('kind')=='shopping':
            rows=result['commercial'].get('products',[]);kind='product'
        if rows:
            items=[]
            for index,row in enumerate(rows,1):
                source_id=turn+'-'+kind+'-'+str(index)
                sources.append({'id':source_id,'kind':'displayed_'+kind,'shown_in_turn':turn,
                    'data':row,'date':result.get('rail',{}).get('date'),'request':result.get('commercial',{}).get('request',{})})
                items.append({'index':index,'source_id':source_id,'label':str(row.get('train') or row.get('title') or '')})
            presentation={'items':items,'ordered_source_ids':[x['source_id'] for x in items]}
    return sources,presentation

class AssistantFeatures:
    def __init__(self,private,model,node=None,native=None,secretary_model=None):
        self.private=Path(private);self.model=model;self.node=node or shutil.which('node')
        if not self.node:raise RuntimeError('记账运行环境未就绪')
        self.accounts=FeatureAccounts(self.private/'features',PROJECT/'记账模块',self.node)
        self.native=native or NativeAdapter(self.private)
        self.journal=Journal(self.private/'features/secretary.sqlite')
        self.secretary=Secretary(secretary_model or deepseek_model(os.environ['DEEPSEEK_API_KEY']),environment='你是言小办秘书。当前是真实商用Mac环境，使用苹果系统日历和提醒事项，不是模拟数据。只有可信原生定点读回后才可说保存、修改或删除；iCloud保存不等于手机通知已到达。只依据本轮原话、实际history和sources决策。实际原生能力在native_capabilities中；不支持结束时间为空的日程，缺少必要结束时间时由你澄清或提供草案，不能擅自补时长。当前只接普通一次性提醒与无日期待办，未接重复或位置提醒；用户要求每天、每周、循环或到达某处等条件时，说明尚不支持并澄清，不降为一次性、不丢条件。联调发现最终口播读出了“已通过原生读回确认”等内部过程。请自行改正，只交付保存、修改、取消的实际结果及必要时间，不朗读原生、读回、回执、协议等实现术语；程序不会替你裁剪。reply最多180字。')
    def status(self,owner):
        result=self.accounts.status(owner)
        result['native']=self.native.status() if result.get('native_account_owner') else {'available':False,'reason':'native_account_binding_required'}
        return result
    def handle(self,owner,body):return self.accounts.handle(owner,body)
    def bootstrap_url(self,base):
        token=self.accounts.bootstrap_token()
        return base+'#feature_setup='+token if token else None
    def failure(self,kind,context,error,facts=None):
        policy="""你是言小办，向普通用户交付真实的连接或执行问题。只返回JSON {"reply":"最多180字的自然口语"}。
不能编造完成，不能重新执行。member_binding_required表示手机尚未启用功能，身份校验在下发前拒绝，任何记账或系统写入都未开始，确实未记入。只需说明这笔还没记上，先用Mac连接页的一次性功能授权二维码在自己的手机确认启用即可；不要要求再管理一遍家庭，邀请家人才需要更多设置。
this_member_has_no_native_icloud_binding也表示没有下发系统操作。
联调真实错误：未绑定时你曾同时说“没记上”和“不是完全没写入”，必须纠正，不能把unknown话术套用到已证实not_started。
只有实际结果unknown或写入不明时才说明尚不能确认是否保存，先核实同一条事项，不能重复创建。真实失败反馈：你曾建议“若没有记录，再重新添加一次”，这是错误的；本机无记录、页面没显示、用户说没看到，都不能证明原生事项没有保存，禁止建议据此重建。已有成功回执要如实保留。
不要朗读身份校验、下发、原生、读回、回执、unknown、协议等内部过程或错误码。直接说用户要知道的结果与下一步。
"""
        source={'utterance':context['utterance'],'history':context['history'],'feature':kind,'error':error,'actual':facts}
        trace=[];feedback=[];value=None
        for _ in range(2):
            value,usage=self.model.ask(policy,{'input':source,'feedback':feedback},max_tokens=600,timeout=20)
            trace.append({'stage':'delivery','raw_output':value,'usage':usage})
            if not isinstance(value,dict) or set(value)!={'reply'} or not isinstance(value['reply'],str) or not 1<=len(value['reply'])<=180:
                feedback.append({'error':'返回且仅返回reply，1至180字，不截断。'});continue
            audit,usage=self.model.ask(policy+'\n本次只做交付复核，不返回reply。只返回JSON {"verdict":"pass或revise","issues":["具体问题"]}。核对候选是否与真实状态一致：not_started不能说结果不明；unknown不能说肯定没写入，也不能建议看不到就重建；已有成功部分不能抹掉。排除内部术语，勿凭空添加操作步骤。',{'input':source,'candidate':value},max_tokens=500,timeout=20)
            trace.append({'stage':'audit','raw_output':audit,'usage':usage})
            if audit=={'verdict':'pass','issues':[]}:break
            feedback.append({'candidate':value,'review':audit})
        else:value=None
        write_private(self.private/'features/error-deliveries'/(uuid.uuid4().hex+'.json'),{'input':source,'trace':trace,'final':value})
        if value is None:raise ValueError('feature_delivery_not_verified')
        return {'status':'blocked','reply':value['reply'],'sources':[],'performed':False,'commercial':{'kind':kind,'title':'家庭记账' if kind=='bookkeeping' else '言小办秘书','verified':False,'error':error}}
    def execute(self,kind,context):
        member=self.accounts.member(context['owner'])
        if not member:return self.failure(kind,context,'member_binding_required',{'execution':'not_started','writes':0})
        try:
            if kind=='bookkeeping':return self.bookkeeping(context,member)
            if not self.accounts.status(context['owner'])['native_account_owner']:return self.failure(kind,context,'this_member_has_no_native_icloud_binding')
            return self.secretary_turn(context,member)
        except (FeatureError,BoundaryError) as error:return self.failure(kind,context,str(error))
    def bookkeeping(self,context,member):
        backend=LocalActual(self.accounts.book_root,member['actor'],self.node)
        engine=Bookkeeping(self.model,backend,environment='当前是真实商用家庭/个人账本，使用真实Actual Budget。只有真实工具读回成功才可确认保存、修改或撤销；不能把测试或后台核验当手机验收。个人账与家庭账的共享范围以books真实权限为准。')
        answer=engine.turn(context['utterance'],context['history'],context['identity']+'-'+context['request_id'],context['now'])
        if not answer['complete']:return self.failure('bookkeeping',context,answer['error'],{'tools':answer.get('tools',[])})
        successful=[t for t in answer['tools'] if t['receipt'].get('ok')]
        details=[{'operation':t['tool']['operation'],'book_id':t['tool']['book_id'],'result':t['receipt']['result']} for t in successful if t['tool']['operation'] not in ('catalog','create_group','create_category','create_account')]
        return {'status':'complete' if details else 'partial','reply':answer['reply'],'sources':[],'performed':bool(details),
                'commercial':{'kind':'bookkeeping','title':'家庭记账','verified':bool(details),'details':details}}
    def secretary_turn(self,context,member):
        native_status=self.native.status()
        if not native_status.get('ok') or not native_status.get('reminders_authorized') or not native_status.get('events_authorized') or not native_status.get('sources_ready',len(native_status.get('icloud_sources',[]))==1):return self.failure('secretary',context,'apple_system_connection_unavailable',native_status)
        scope=Scope(member['actor'],member['actor'],True)
        key=hashlib.sha256((context['owner']+context['identity']+context['request_id']).encode()).hexdigest()
        folder=self.private/'features/secretary-turns';folder.mkdir(parents=True,exist_ok=True,mode=0o700);path=folder/(key+'.json')
        payload={'request_id':context['request_id'],'user':context['utterance'],'history':context['history'],'now':context['now'],'timezone':'Asia/Shanghai','sources':[],'existing_items':self.journal.existing_items(scope),'presentation':{'items':[],'ordered_source_ids':[]},'native_capabilities':sorted(self.native.capabilities)}
        # Bind IDs to actual returned UI rows in their displayed order.
        payload['sources'],payload['presentation']=displayed_sources(context.get('results',[]))
        prior=json.loads(path.read_text()) if path.exists() else None
        if prior and prior.get('result'):return prior['result']
        if prior and prior['payload']!=payload:
            # Existing items can change only through this operation; keep the frozen input.
            if prior['payload']['user']!=payload['user'] or prior['payload']['history']!=payload['history']:raise FeatureError('secretary_request_conflict')
            payload=prior['payload']
        trace=prior.get('trace',[]) if prior else []
        decision=prior.get('decision') if prior else self.secretary.prepare(payload,trace)
        if decision is None:return self.failure('secretary',context,'secretary_model_not_approved')
        state=prior or {'payload':payload,'decision':decision,'trace':trace,'dispatches':{},'receipts':[]};write_private(path,state)
        if decision['decision']!='prepare':
            result={'status':'partial','reply':decision['reply'],'sources':[],'performed':False,'commercial':{'kind':'secretary','title':'言小办秘书','verified':False,'items':[]}}
        else:
            jobs=self.journal.stage(scope,payload['request_id'],payload,decision)
            for job in jobs:
                current=self.journal.get(scope,job['id'])
                if current['state']=='succeeded':continue
                if current['state'] in ('not_applied','cancelled'):return self.failure('secretary',context,'native_operation_not_applied',{'execution':'not_started','operation':self.journal.model_receipt(scope,job['id'])})
                envelope=state['dispatches'].get(job['id'])
                if envelope is None:
                    envelope=self.journal.claim(scope,job['id'],self.native.capabilities);state['dispatches'][job['id']]=envelope;write_private(path,state)
                    command='execute'
                else:command='reconcile'
                outcome=self.native.call(command,scope=member['actor'],envelope=envelope)
                if not outcome.get('ok'):
                    if outcome.get('not_started'):
                        receipt={'protocol_version':1,'operation_id':envelope['operation_id'],'action_sha256':envelope['action_sha256'],'outcome':'not_applied','native_id':None,'readback':{'operation_id':envelope['operation_id'],'execution_state':'not_started','exists':None,'fields':None,'booking':envelope['booking']}}
                        self.journal.acknowledge(scope,job['id'],envelope['receipt_token'],receipt)
                    else:self.journal.mark_uncertain(scope,job['id'])
                    write_private(path,state)
                    return self.failure('secretary',context,outcome.get('error','native_outcome_unknown'),{'execution':'not_applied' if outcome.get('not_started') else 'unknown','operation':self.journal.model_receipt(scope,job['id'])})
                self.journal.acknowledge(scope,job['id'],envelope['receipt_token'],outcome['receipt'])
                state['receipts'].append(self.journal.model_receipt(scope,job['id']));write_private(path,state)
            receipts=[self.journal.model_receipt(scope,job['id']) for job in jobs]
            final_payload={**payload,'prior_model_output':decision,'operation':receipts[-1],'verified_operations':receipts,'existing_items':self.journal.existing_items(scope)}
            final=self.secretary.prepare(final_payload,trace);state['trace']=trace;write_private(path,state)
            if final is None or final['actions']:return self.failure('secretary',context,'completion_reply_not_approved',{'verified_operations':receipts})
            changed_ids={r['item_id'] for r in receipts}
            changed_items=[item for item in self.journal.existing_items(scope) if item['id'] in changed_ids]
            result={'status':'complete','reply':final['reply'],'sources':[],'performed':True,'commercial':{'kind':'secretary','title':'言小办秘书','verified':True,'items':changed_items,'phone_notification_verified':False}}
        state['result']=result;write_private(path,state);return result

class FeatureQueries:
    def __init__(self,base,features):self.base=base;self.features=features;self.tools=base.tools+['bookkeeping','secretary'];self.commerce=base.commerce
    @property
    def decider(self):return self.base.decider
    @decider.setter
    def decider(self,value):self.base.decider=value
    @property
    def rules(self):return self.base.rules+DIRECTORY
    def timeout_for(self,tool):return 165 if tool in ('bookkeeping','secretary') else self.base.timeout_for(tool)
    def execute(self,query,deadline,context):
        if query['tool'] in ('bookkeeping','secretary'):return self.features.execute(query['tool'],context)
        return self.base.execute(query,deadline,context)
