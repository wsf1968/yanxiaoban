"""DS owns all bookkeeping decisions; the connector preserves explicit tool args."""
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time


class BookkeepingFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class LocalActual:
    """The trusted host constructs this with an authenticated member identity.

    Neither actor nor the private root is accepted from a model output/browser body.
    No HTTP endpoint or new device authentication scheme is installed here.
    """
    def __init__(self, root, actor, node=None):
        self.root = Path(root).resolve()
        self.actor = actor
        self.node = node or shutil.which('node')
        if not self.node:
            raise BookkeepingFailure('isolated_node_runtime_missing')
        self.runner = Path(__file__).resolve().parents[1] / 'runner.cjs'

    def _call(self, body):
        body = dict(body, actor=self.actor)
        try:
            result = subprocess.run([self.node, str(self.runner), str(self.root)],
                input=json.dumps(body, ensure_ascii=False), text=True, capture_output=True, timeout=30)
            value = json.loads(result.stdout)
            if not isinstance(value, dict) or type(value.get('ok')) is not bool:
                raise ValueError('invalid provider reply')
            return value
        except subprocess.TimeoutExpired:
            return {'ok':False, 'error':'write_outcome_unknown_do_not_retry'}
        except (OSError, ValueError, TypeError):
            return {'ok':False, 'error':'provider_reply_unavailable_do_not_retry'}

    def books(self):
        result = self._call({'command':'list_books'})
        if not result['ok']:
            raise BookkeepingFailure(result.get('error','provider_failure'))
        return result['result']

    def execute(self, book_id, request_id, operation, args):
        return self._call({'command':'execute','book_id':book_id,'request_id':request_id,
                           'operation':operation,'args':args})


REVIEW = '''你是言小办记账交付复核者。只返回JSON {"verdict":"pass或revise","issues":["具体问题"]}。
核对本轮原始用户要求、完整历史、真实工具回执与最终候选回复：金额、日期、账本、家庭共享范围、收支性质与完成状态必须有据。
查询不能冒充写入；有未完成操作时不能宣称全部完成；未知回执不能当成功。候选声称的已保存/修改/删除必须能对应相应真实成功回执。
请求记账但没有工具回执时不能只用将来时敷衍完成；信息缺失而澄清是正确行为。用户只问、明确不要记账，或对象不明确时不得擅自记账。
summary的金额单位是分，消费/收入不含转账，recorded_by仅指录入者。只评实际矛盾和丢失的关键要求，不因未逐字复读所有条件或表达偏好误报。
回复最多100字，不应声称手机已验收或已付款。程序不会代改模型输出，请给出需DS自行修改的明确问题。'''

PREPARE_REVIEW = '''你是言小办记账写入前复核者。只返回JSON {"verdict":"pass或revise","issues":["具体问题"]}。
这是逐工具调用复核，不是整轮最终交付复核。candidate_tool是下一步的一个原子动作；后面还会继续调用工具。必须按该operation自己的协议核对，不能要求每一步都包含完整交易或一次完成用户整项任务。
实际联调失败：用户要求“晚饭80元、分类餐饮”，真实账本只有收入组。你连续6次把create_group判为“没有记录80元流水、缺少交易日期和金额”，使正确记账耗时88.497秒。这是复核误报，必须纠正。创建支出组只需name和is_income=false；创建分类只需真实group_id和name；它们是后续正确记账的必要准备，不需要交易金额/日期/账户，也不表示已完成记账。不得因缺少不属于该operation的字段拒绝。
写入前核对原始用户要求、相关完整历史、已授权books、真实catalog/query和本轮已完成工具回执，与candidate_tool逐项对照。
金额单位分、正负号、日期、家庭或个人账本、明确否定、真实账户和流水ID必须正确。不能把账户间转账当收支。
退款的真实失败必须纠正：用户说“晚饭退了20元，另记一笔餐饮退款冲减原支出，不作为收入”，决策模型本来给出正确add amount_fen=+2000，你却反向要求改成-2000，随后又误判通过，导致80元消费加20元退款被算成100元消费。Actual中退款冲减支出必须是正数入账且使用原支出分类；20元为+2000分，不能用-2000分再次增加支出。“负数支出”这句话不能代替真实工具正负号核验。先看candidate_tool.args.amount_fen真实符号与分类，不要只相信准备话术说“冲减”。收入也要核对真实is_income分类，不能只看分类中文名字。
只查询、未指定多个账本中的目标、未说要保存、目标记录不明确、金额或时间缺失而无法确定时不能通过写入。
同一明确需求若已有成功的同一笔回执，不得为再次确认、分类或口播重复新增；应查询/修改原流水或据回执交付。用户明确要求分别记两笔相同金额时按真实意图判断，不能仅以相同金额当重复。
创建用于本轮明确记账的新分类组/分类可允许，不凭固定分类表替用户决定。日期可依赖真实now理解今天或昨天；备注可准确概括原话。
只因事实矛盾、授权缺失、条件丢失或重复执行而revise，不因未逐字复读条件、合理分类或回复风格而误报。通过时issues为空。'''


class Bookkeeping:
    def __init__(self, model, backend, *, environment=None):
        self.model, self.backend = model, backend
        self.contract = Path(__file__).with_name('contract.txt').read_text()
        if environment:
            old='这是隔离开发环境，账本中是标注为测试的数据；但工具使用真实Actual Budget引擎，只有实际读回成功才可说本次测试记录已保存。没有iPhone收音或手机通过证据，不能说手机已通过。'
            self.contract=self.contract.replace(old,environment)
        self.policy = self.contract+'\n'+Path(__file__).with_name('policy.txt').read_text()
        backend.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        backend.root.chmod(0o700)
        self.db_path = backend.root / 'conversation-operations.sqlite'
        with self.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS turns
                (actor TEXT,request_id TEXT,fingerprint TEXT,status TEXT,result TEXT,
                 PRIMARY KEY(actor,request_id))''')
        self.db_path.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:yield connection
        finally:connection.close()

    def turn(self, text, history, request_id, now):
        if not isinstance(text,str) or not text.strip() or not isinstance(history,list) or not isinstance(request_id,str) or not 1<=len(request_id)<=100:
            raise BookkeepingFailure('invalid_turn')
        from datetime import datetime
        if datetime.fromisoformat(now).utcoffset() is None:
            raise BookkeepingFailure('timezone_required')
        # The timestamp is supplied by the trusted host, never extracted with keywords.
        source = {'user':text,'history':history,'now':now,'timezone':'Asia/Shanghai'}
        fingerprint = hashlib.sha256(json.dumps({'user':text,'history':history},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            prior = connection.execute('SELECT * FROM turns WHERE actor=? AND request_id=?', (self.backend.actor,request_id)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise BookkeepingFailure('request_id_conflict')
                if prior['result'] is None:
                    raise BookkeepingFailure('turn_pending_do_not_repeat')
                return {**json.loads(prior['result']), 'replayed':True}
            connection.execute('INSERT INTO turns VALUES(?,?,?,?,NULL)',(self.backend.actor,request_id,fingerprint,'pending'))
        trace, tool_history, feedback = [], [], []
        started = time.monotonic()
        final = None
        try:
            source['books'] = self.backend.books()
            for step in range(12):
                if time.monotonic()-started>150:
                    raise BookkeepingFailure('model_processing_budget_reached')
                payload = dict(source, tool_history=tool_history, feedback=feedback)
                if len(json.dumps(payload,ensure_ascii=False)) > 30000:
                    raise BookkeepingFailure('model_context_budget_reached')
                output, usage = self.model.ask(self.policy, payload, max_tokens=1800, timeout=45, reasoning_effort='low')
                trace.append({'stage':'decision','raw_output':output,'usage':usage})
                if (not isinstance(output,dict) or set(output) != {'decision','reply','tool'}
                    or output['decision'] not in ('tool','reply','clarify') or not isinstance(output['reply'],str)
                    or not output['reply'].strip()):
                    feedback.append({'error':'请返回恰含decision、reply、tool的合法对象。'})
                    continue
                if output['decision']=='tool':
                    tool = output['tool']
                    if (not isinstance(tool,dict) or set(tool)!={'book_id','operation','args'}
                        or not isinstance(tool['book_id'],str) or not isinstance(tool['operation'],str) or not isinstance(tool['args'],dict)):
                        feedback.append({'error':'工具协议不完整，请自行修正。'})
                        continue
                    if tool['operation'] not in ('catalog','query'):
                        review, usage = self.model.ask(self.contract+'\n本次只执行写入前复核，按下述复核输出协议返回：\n'+PREPARE_REVIEW, {'input':source,'tool_history':tool_history,'candidate_tool':tool},
                            max_tokens=1000,timeout=45,reasoning_effort='low')
                        trace.append({'stage':'before_write_review','raw_output':review,'usage':usage})
                        if not isinstance(review,dict) or review!={'verdict':'pass','issues':[]}:
                            feedback.append({'candidate_tool':tool,'review':review,'instruction':'未写入，请依据原话与真实资料自行修正；已完成的操作不得重记。'})
                            continue
                    result = self.backend.execute(tool['book_id'], request_id+'-step-'+str(step), tool['operation'],tool['args'])
                    tool_history.append({'tool':tool,'receipt':result})
                    trace.append({'stage':'tool','request':tool,'receipt':result})
                    if not result['ok'] and result.get('error') in ('write_outcome_unknown_do_not_retry','book_has_unverified_write','provider_reply_unavailable_do_not_retry'):
                        raise BookkeepingFailure(result['error'])
                    continue
                if output['tool'] is not None or len(output['reply'])>100:
                    feedback.append({'error':'最终回复tool须为null，reply最多100字；请自行改写，程序不会裁剪。'})
                    continue
                review, usage = self.model.ask(self.contract+'\n本次只执行最终交付复核，按下述复核输出协议返回：\n'+REVIEW, {'input':source,'tool_history':tool_history,'candidate':output},
                                               max_tokens=1000,timeout=45,reasoning_effort='low')
                trace.append({'stage':'review','raw_output':review,'usage':usage})
                if isinstance(review,dict) and review=={'verdict':'pass','issues':[]}:
                    final={'complete':True,'decision':output['decision'],'reply':output['reply'], 'tools':tool_history,
                           'trace':trace,'seconds':round(time.monotonic()-started,3),'replayed':False}
                    break
                feedback.append({'candidate':output,'review':review,'instruction':'请依据真实回执自行修正，不能重新执行已成功的操作。'})
            if final is None:
                raise BookkeepingFailure('model_processing_budget_reached')
        except Exception as error:
            final={'complete':False,'error':getattr(error,'code','bookkeeping_failed'),'tools':tool_history,
                   'trace':trace,'seconds':round(time.monotonic()-started,3),'replayed':False}
        with self.connect() as connection:
            connection.execute('UPDATE turns SET status=?,result=? WHERE actor=? AND request_id=?',
                               ('complete' if final['complete'] else 'failed',json.dumps(final,ensure_ascii=False),self.backend.actor,request_id))
        return final
