"""Same-device cookie forwarding to the existing local WeChat service; no retries."""
import json
import unicodedata
import urllib.error
import urllib.request


def spoken_command(text):
    value = ''.join(c for c in unicodedata.normalize('NFKC', text)
                    if not c.isspace() and unicodedata.category(c)[0] != 'P' and unicodedata.category(c) != 'Cf')
    return value in ('确认发送','确定发送','确认发出','确定发出','发送','发出')


def validate_message(query):
    if not isinstance(query, dict) or set(query) != {'tool','args'} or query['tool'] != 'wechat':
        raise ValueError('invalid_wechat_query')
    args = query['args']
    if not isinstance(args, dict) or set(args) != {'recipient','text'}:
        raise ValueError('invalid_wechat_args')
    for key, limit in [('recipient',200),('text',500)]:
        value=args[key]
        if not isinstance(value,str) or not 1<=len(value.strip())<=limit or any(ord(c)<32 for c in value):
            raise ValueError('invalid_wechat_args')
    return query


def local_call(cookie, body):
    raise ValueError("legacy_personal_bridge_not_available_in_commercial_preview")


class WechatClient:
    def __init__(self,call=local_call):self.call=call

    def execute(self,cookie,conversation,request_id,action,utterance,args=None,draft=None):
        body={'action':action,'conversation':conversation,'requestId':request_id}
        if action=='prepare':body.update(args or {})
        elif action=='select':body.update(text=utterance)
        elif action=='receipt':body.update(draftId=(draft or {}).get('draftId',''))
        elif action=='confirm':
            body.update(text=utterance,draftId=(draft or {}).get('draftId',''),draftHash=(draft or {}).get('draftHash',''))
        try:
            result=self.call(cookie,body)
            states={'draft','queued','sending','sent','uncertain','cancelled','expired','listening','blocked','missing','choosing'}
            if not isinstance(result,dict) or result.get('state') not in states or not isinstance(result.get('reply'),str) or not 1<=len(result['reply'])<=1200:
                raise ValueError('invalid_wechat_response')
            for key in ('recipient','content'):
                if key in result and (not isinstance(result[key],str) or len(result[key])>1000):raise ValueError('invalid_wechat_response')
            # Only a real terminal receipt may become a completed action.
            return {k:result[k] for k in ('state','reply','recipient','content','expiresAt','draftId','draftHash','choices','complete') if k in result}
        except (ValueError,OSError,urllib.error.URLError):
            return {'state':'uncertain','reply':'微信连接暂未返回可靠结果，请查看微信聊天记录。本次不会自动重发；恢复连接后可以说“查看微信发送结果”。'}

def choice_number(text):
    import re
    if not isinstance(text,str):return None
    match=re.fullmatch(r'(?:选|选择|就选|我要|要)?(?:第)?([1-9一二三四五六七八九十两]|10)(?:个|位|项|号)?',text.strip().rstrip('。！!，,'))
    if not match:return None
    value=match.group(1)
    return int(value)-1 if value.isdigit() else '一二三四五六七八九十'.index('二' if value=='两' else value)
