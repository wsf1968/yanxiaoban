"""Voice bridge only; search/planning remains in the existing Research executor."""
import re
from shopping_client import local_call

SYSTEM='''\n小红书攻略使用xiaohongshu工具，明确委托查询时action=prepare，query={"tool":"xiaohongshu","args":{"instruction":"结合上下文的完整攻略需求，最多500字"}}。缺少关键地点或主题时先问。不要说尚未接入，也不要从记忆编造攻略；实际状态以工具回执为准。查询进展用resume、停止用cancel，query=null。只读，不发布、点赞、评论或发私信。检索任务在后台执行，先告知正在查询，完整结果返回后再说查到了；不能把排队当作完成。遇到平台登录或验证由用户处理。'''

SYSTEM += '\n航班/机票查询使用flight工具，action=prepare，query={"tool":"flight","args":{"instruction":"包含出发城市、到达城市、明确日期和用户条件的完整查询需求"}}。由GLM通过12306官方App自主查询，后台执行，开始时只说正在查询。缺少城市或日期先问；相对日期按当前北京时间转换。只查询，不订票、下单或付款。费用是否含税以实际页面为准；报价不代表余票保证。追问已返回航班可用上下文，重新查价则发起完整新查询。状态查询resume、取消cancel。'
SYSTEM += '\n高铁、动车、火车票查询统一使用12306 App的rail工具，action=prepare，query={"tool":"rail","args":{"instruction":"结合上下文写出出发地、目的地、明确日期、车次/时段/座席等完整需求"}}。rail与flight共用12306手机操作通道；rail为首页火车票、flight为飞机票，导航步骤由执行模型自行判断。只查询，不预订。用户说第二班、一等座时依据上文具体车次承接，需查新价格或余票时把班次日期座席写全，不能丢掉上下文改查全部。没有具体余票数量就保持页面的有票/无票/候补表述。'

SYSTEM += '\n时间汇报的继续授权：用户明确要求接着执行已暂停任务，action=continue、query=null；查看进度仍用resume，不能因问进度就续跑。用户拒绝继续/停止用cancel。没答复、只解释、含糊答复、询问是否值得继续均不能执行continue。任务为time_review/needsDecision时，继续保持原条件，不重新prepare创建任务；修改需求先澄清或停止旧任务。只说“继续/接着做/不用继续了”等短句时，根据payload.shoppingTaskStatus和payload.pendingResearchTask中needsDecision为true的真实任务选择capability；有歧义先问。'

def validate_research(query):
    if not isinstance(query,dict) or set(query)!={'tool','args'} or query['tool'] not in ('xiaohongshu','flight','rail'):raise ValueError('invalid_research_query')
    args=query['args']
    if not isinstance(args,dict) or set(args)!={'instruction'}:raise ValueError('invalid_research_args')
    text=args['instruction']
    if not isinstance(text,str) or not 1<=len(text.strip())<=500 or any(ord(c)<32 for c in text):raise ValueError('invalid_research_args')
    return query


class ResearchClient:
    def __init__(self,call=None):self.call=call or (lambda cookie,body:local_call(cookie,body,kind='research'))
    def execute(self,cookie,conversation,request_id,action,query=None,task_id='',service='xiaohongshu'):
        body={'action':action,'conversation':conversation,'requestId':request_id}
        if action=='start':
            body['instruction']=validate_research(query)['args']['instruction'];body['service']=query['tool']
        else:
            body['taskId']=task_id
            if not task_id:body['service']=service
        try:
            r=self.call(cookie,body)
            if not isinstance(r,dict) or r.get('state') not in {'queued','running','cancelling','cancelled','waiting','partial','complete','failed','interrupted'}:raise ValueError('invalid_research_response')
            if not isinstance(r.get('reply'),str) or not 1<=len(r['reply'])<=300:raise ValueError('invalid_research_response')
            if r['state']!='failed' and not re.fullmatch('[a-f0-9]{32}',str(r.get('taskId',''))):raise ValueError('invalid_research_task')
            if r.get('completed') is True and r['state']!='complete':raise ValueError('invalid_research_completion')
            if not isinstance(r.get('sources',[]),list) or not isinstance(r.get('summary',{}),dict):raise ValueError('invalid_research_response')
            return r
        except (OSError,ValueError):
            return {'state':'uncertain','reply':'暂时没有取得可靠的查询任务状态，请稍后查看；不会自动重复提交。','completed':False,'sources':[],'summary':{}}
