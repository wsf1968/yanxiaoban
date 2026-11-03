"""Same-device shopping bridge. No retries, no checkout or payment operation."""
import json,re,urllib.request,urllib.error
TOOLS={'taobao_instant','meituan','jd'}
FIELDS={'productName','specification','budget','quantity','purpose','area'}
SYSTEM='''\n购物补充规则优先于旧执行器关闭说明：仅enabledExecutors中的淘宝闪购taobao_instant、美团meituan、京东jd可交给后台自主筛选加车，用户自行比较支付。当前先支持一款一份，多份或整单需求需说明尚未开放，不启动任务。用户明确委托选购才prepare；只是提到商品、说先别查或先别买不能执行。缺少商品、购买份数、预算上限、明确配送城市和小区时，结合上下文只追问真正缺失的信息。不得用其他设备的位置代替用户地点，不必索要门牌号。规格未限定写未限定规格，用途未说写未说明用途，不自行增加偏好。购买数量不是包装规格数量。只有一份时“40元以内”可明确整理为总预算40元；多份预算口径不明先问总价或每份。\n执行query={"tool":"taobao_instant或meituan或jd","args":{"productName":"商品原意","specification":"规格或未限定规格","budget":"总预算40元或单件30元或用户明确不限预算","quantity":"1份","purpose":"用途或未说明用途","area":"用户明确的配送城市和小区名称"}}，capability与tool相同，action=prepare。不得把查询或排队说成加车完成，不能承诺预算内或已经付款。任务取消用cancel（保留已经加车商品）；查看购物进展用resume，query=null。正在运行时修改条件要先说明需停止原任务；不要重复提交同样任务。不调用支付，不自动领券、不删除原有商品。'''

SYSTEM += '\n购物任务状态以payload.shoppingTaskStatus为实时依据，不能用本段聊天没有记录推断没有任务。该状态跨对话保留同一账号记录；deviceBusy=true表示设备还有任务占位，即使当前账号没有可见购物任务也不能说设备空闲。state为failed/partial/complete/cancelled且无uncertainWrites表示已停止，不代表仍在执行；waiting或uncertainWrites必须说明等待处理。state=uncertain表示查询失败，不能回答没有任务。用户询问任务是否未完成、进展或要停止购物任务时，选择购物capability及resume/cancel，由工具返回真实结果；不要只凭口头答复。新购物需求仍用prepare，不把查看旧任务当成完成新购物。'

SYSTEM += '\n时间汇报的继续授权：用户明确要求接着执行已暂停任务，action=continue、query=null；查看进度仍用resume，不能因问进度就续跑。用户拒绝继续/停止用cancel。没答复、只解释、含糊答复、询问是否值得继续均不能执行continue。任务为time_review/needsDecision时，继续保持原条件，不重新prepare创建任务；修改需求先澄清或停止旧任务。只说“继续/接着做/不用继续了”等短句时，根据payload.shoppingTaskStatus和payload.pendingResearchTask中needsDecision为true的真实任务选择capability；有歧义先问。'

SYSTEM += '\n购物追问是对话，不是每次都查任务状态。结合用户原话、history和实时shoppingTaskStatus判断：仅询问已知事实可chat解释；用户纠正结果、质疑是否满足原要求或要求补查时，若原任务已停止，使用prepare交给执行者核实，沿用仍有效的原需求。工具会同时传递本轮原话、上下文和上次核验事实。购物车是参考，不代表本次决定；继续选原商品还是换口味、换店由本轮意图决定，不明确才澄清。不得复用旧回执当作新处理结果。不因一句谢谢或好的重做。五分钟等待授权仍遵守continue规则，正在运行的任务不另起并行任务。'

SYSTEM += '\n金额证据：verification.feesComplete或priceCertain不是true、total为空时，不能声称符合预算、不能只报单价或约价后说在预算内。终止任务的费用不会自动继续核实，不得承诺等费用结果而未实际提交任务。用户只说谢谢时简短回应，不主动复述价格或承诺。购物车里的同款先核实全部要求；确实符合且用户尚未选择沿用时，询问沿用还是换一款。超预算则说明不符并按原委托继续选。waitingReason=user_input表示执行者正在询问，用户答复后用prepare传递完整上下文继续处理；这与time_review的continue授权不同。'

REPLY_SYSTEM = '''你是言小办，正在根据刚取得的购物工具事实回应用户本轮的话。
history和utterance是对话；result是实际工具回执，不是让你照读的台词。直接回应本轮疑问或纠正，通常一到两句，不重复旧回执来回避问题。items.cartState=existing是原来已有，不得说本次已加购；只有added才是本次新增。
排队只说已接到任务；partial不能说所有要求完成；verification.feesComplete或priceCertain不是true、total为空时不得声称预算内，不以商品单价/约价替代总费用。错误、不确定、待续跑须如实说明。没有真实新任务不得说已经开始执行或将自动核实。需要用户回答时传达实际question，不自行替用户选择。不得执行任何操作。
只返回JSON {"reply":"不超过180字的中文回答"}。'''

def validate_shopping(query):
 if not isinstance(query,dict) or set(query)!={'tool','args'} or query['tool'] not in TOOLS:raise ValueError('invalid_shopping_query')
 a=query['args']
 if not isinstance(a,dict) or set(a)!=FIELDS:raise ValueError('invalid_shopping_args')
 if any(not isinstance(v,str) or not 1<=len(v.strip())<=100 or any(ord(c)<32 for c in v) for v in a.values()):raise ValueError('invalid_shopping_args')
 return query

def local_call(cookie,body,kind='shopping'):
    raise ValueError("legacy_personal_bridge_not_available_in_commercial_preview")


class ShoppingClient:
 def __init__(self,call=local_call,tools=None,context=''):
  self.call=call
  self.tools=sorted(TOOLS if tools is None else set(tools).intersection(TOOLS))
  self.context=context
 def execute(self,cookie,conversation,request_id,action,query=None,task_id='',context=None):
  body={'action':action,'conversation':conversation,'requestId':request_id}
  if action=='start':
   body['args']={**query['args'],'service':query['tool']}
   if context is not None:body['context']=context
  else:body['taskId']=task_id
  try:
   r=self.call(cookie,body)
   if not isinstance(r,dict) or r.get('state') not in {'queued','running','cancelling','cancelled','waiting','partial','complete','failed','interrupted'} or not isinstance(r.get('reply'),str):raise ValueError('invalid_shopping_response')
   if not 1<=len(r['reply'])<=1000 or not isinstance(r.get('items',[]),list) or len(r.get('items',[]))>3:raise ValueError('invalid_shopping_response')
   if r.get('state')!='failed' and not re.fullmatch('[a-f0-9]{32}',str(r.get('taskId',''))):raise ValueError('invalid_shopping_task')
   if r.get('completed') is True and r['state']!='complete':raise ValueError('invalid_shopping_completion')
   return r
  except urllib.error.HTTPError as e:
   try:
    raw=e.read(4096);message=json.loads(raw).get('error','')
    if action in ('start','continue') and e.code in (400,409,503) and isinstance(message,str) and 0<len(message)<=300:
     return {'state':'failed','reply':('未能继续任务：' if action=='continue' else '这次购物需求未提交：')+message,'items':[],'completed':False}
   except (ValueError,OSError):pass
   finally:e.close()
   return {'state':'uncertain','reply':'购物连接暂时异常，尚未取得可靠结果；不会自动重新加车。','items':[],'completed':False}
  except (OSError,ValueError,urllib.error.URLError):
   return {'state':'uncertain','reply':'暂时没取得可靠的购物状态。请稍后查看进展，不要重复提交；我不会自动重新加车。','items':[],'completed':False}
