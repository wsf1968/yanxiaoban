"""Small bounded JSON client for the existing PI provider; no tool execution."""
import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class ModelFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ModelClient:
    def __init__(self, config):
        base=config['base_url'].rstrip('/')
        url=urlsplit(base)
        deepseek = url.hostname=='api.deepseek.com' and url.path in ('','/v1')
        bigmodel = url.hostname=='open.bigmodel.cn' and url.path=='/api/paas/v4'
        if url.scheme!='https' or not (deepseek or bigmodel) or url.query or url.fragment or url.username or url.port not in (None,443):
            raise ValueError('Unapproved model endpoint')
        self.url=base+'/chat/completions'
        self.key=config['api_key']
        self.model=config['model']
        self.experiment_thinking=config.get('experiment_thinking','disabled')
        if self.experiment_thinking not in ('disabled','low'):
            raise ValueError('Invalid experiment thinking mode')
        self.bigmodel = bigmodel
        if bigmodel and self.model != 'glm-5.3-flash':
            raise ValueError('Unapproved BigModel model')

    def ask(self, system, payload, max_tokens=1800, *, timeout=45):
        raw=json.dumps(payload,ensure_ascii=False)
        if len(raw)>32000:raise ValueError('Model input budget exceeded')
        params = {'thinking':{'type':'disabled'}, 'max_tokens':min(max_tokens,2600)}
        if self.experiment_thinking == 'low':
            params = {'thinking':{'type':'enabled'}, 'reasoning_effort':'low', 'max_tokens':6144}
        if self.bigmodel:
            params = {'thinking':{'type':'enabled','clear_thinking':False}, 'reasoning_effort':'low',
                      'temperature':1, 'top_p':0.95, 'max_tokens':min(8192,max(4096,max_tokens+2048))}
        request=urllib.request.Request(self.url,data=json.dumps({
            'model':self.model,'messages':[{'role':'system','content':system+'\n只返回 JSON。'},{'role':'user','content':raw}],
            'response_format':{'type':'json_object'}, **params
        },ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=timeout) as response:
                data=json.loads(response.read(1000000))
            choice=data['choices'][0]
            if choice.get('finish_reason')!='stop':raise ValueError('Incomplete model output')
            value=json.loads(choice['message']['content'])
            if not isinstance(value,dict):raise ValueError('Invalid model object')
            return value,{'model':data.get('model',self.model),'tokens':data.get('usage',{}).get('total_tokens',0)}
        except (TimeoutError, socket.timeout):
            raise ModelFailure('model_timeout') from None
        except urllib.error.HTTPError as error:
            code = 'model_busy' if error.code == 429 else 'model_http_error'
            try:
                detail = json.loads(error.read(4096))
                if self.bigmodel and str(detail.get('error', {}).get('code')) == '1301':
                    code = 'model_content_rejected'
            except (ValueError, TypeError, AttributeError, OSError):
                pass
            finally:
                error.close()
            raise ModelFailure(code) from None
        except urllib.error.URLError:
            raise ModelFailure('model_connection_failed') from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelFailure('model_invalid_response') from None
        except Exception:
            raise ModelFailure('model_failed') from None
