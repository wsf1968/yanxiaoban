"""Model connection for maintenance, with no credentials in prompts or reports."""
import json,urllib.request

PROVIDERS={
    'deepseek':{'label':'DeepSeek','base_url':'https://api.deepseek.com','model':'deepseek-flash'},
    'glm':{'label':'智谱 GLM（逐项适配）','base_url':'https://open.bigmodel.cn/api/paas/v4','model':'glm-5.3-flash'},
}

def ask(config,system,payload,timeout=25):
    provider=PROVIDERS[config['provider']]
    if not config.get('api_key'):raise ValueError('model_key_required')
    raw=json.dumps(payload,ensure_ascii=False)
    if len(raw)>20000:raise ValueError('diagnostic_budget_exceeded')
    params={'thinking':{'type':'disabled'},'max_tokens':1200}
    if config['provider']=='glm':params={'thinking':{'type':'enabled','clear_thinking':False},'reasoning_effort':'low','max_tokens':4096,'temperature':1,'top_p':.95}
    body={'model':config.get('model') or provider['model'],'response_format':{'type':'json_object'},
          'messages':[{'role':'system','content':system},{'role':'user','content':raw}],**params}
    req=urllib.request.Request(provider['base_url']+'/chat/completions',data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+config['api_key']})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:response=json.loads(r.read(1000000))
        choice=response['choices'][0]
        if choice.get('finish_reason')!='stop':raise ValueError('model_incomplete')
        value=json.loads(choice['message']['content'])
        if not isinstance(value,dict):raise ValueError('model_invalid_reply')
        return value
    except ValueError:raise
    except Exception:raise ValueError('model_connection_failed') from None
