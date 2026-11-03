"""Bounded, signed read-only Taobao Union transport. Business arguments are caller/DS-owned.
Reference: https://developer.alibaba.com/docs/api.htm?apiId=64759
No shopping account operations, no provider fallback, no query/candidate rewriting.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import urllib.request
import urllib.error
import urllib.parse
from zoneinfo import ZoneInfo

ENDPOINT='https://eco.taobao.com/router/rest'
METHODS={'taobao.tbk.dg.material.optional.upgrade','taobao.tbk.item.info.upgrade.get',
         'taobao.tbk.dg.general.link.convert'}
SYSTEM_FIELDS={'method','app_key','app_secret','sign','sign_method','session','timestamp','v','format','target_app_key','partner_id','simplify'}

class TaobaoFailure(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__(code)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise TaobaoFailure('taobao_redirect_blocked')

def parameter(value):
    if isinstance(value,bool):return 'true' if value else 'false'
    if isinstance(value,(dict,list)):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    if isinstance(value,(str,int,float)):
        if isinstance(value,float) and (value!=value or abs(value)==float('inf')):raise ValueError('Non-finite value')
        return str(value)
    raise ValueError('Invalid argument type')

def signed_params(config,method,args,*,now=None):
    if method not in METHODS or not isinstance(args,dict) or set(args)&SYSTEM_FIELDS:
        raise ValueError('Unsupported method or reserved parameter')
    if any(not isinstance(k,str) or not re.fullmatch(r'[a-z][a-z0-9_]*',k) for k in args):
        raise ValueError('Invalid argument name')
    stamp=(now or datetime.now(ZoneInfo('Asia/Shanghai'))).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    params={k:parameter(v) for k,v in args.items()}
    params.update(method=method,app_key=config['app_key'],sign_method='md5',timestamp=stamp,format='json',v='2.0')
    plain=config['app_secret']+''.join(k+params[k] for k in sorted(params))+config['app_secret']
    params['sign']=hashlib.md5(plain.encode('utf-8')).hexdigest().upper()
    return params

class TaobaoClient:
    def __init__(self,config,*,open_call=None):
        self.config=dict(config)
        if not all(isinstance(config.get(k),str) and config[k] for k in ('app_key','app_secret')):
            raise ValueError('Missing credentials')
        if not re.fullmatch(r'mm_[1-9][0-9]*_[1-9][0-9]*_[1-9][0-9]*',config.get('pid','')):
            raise ValueError('Missing complete PID')
        self.adzone_id=int(config['pid'].rsplit('_',1)[1])
        self.open_call=open_call or urllib.request.build_opener(NoRedirect()).open

    @classmethod
    def from_private(cls,path):return cls(json.loads(Path(path).read_text()))

    def call(self,method,args,*,timeout=20):
        params=signed_params(self.config,method,args)
        data=urllib.parse.urlencode(params).encode('utf-8')
        if len(data)>32000:raise ValueError('Request budget exceeded')
        request=urllib.request.Request(ENDPOINT,data=data,headers={'Content-Type':'application/x-www-form-urlencoded;charset=utf-8'},method='POST')
        try:
            with self.open_call(request,timeout=min(25,max(1,timeout))) as response:
                raw=response.read(2_000_001)
            if len(raw)>2_000_000:raise TaobaoFailure('taobao_response_too_large')
            output=json.loads(raw)
            if not isinstance(output,dict):raise TaobaoFailure('taobao_invalid_response')
            return output  # Preserve real supplier errors as errors, never turn them into empty results.
        except TaobaoFailure:raise
        except urllib.error.HTTPError as error:
            error.close();raise TaobaoFailure('taobao_http_error') from None
        except (TimeoutError,urllib.error.URLError):raise TaobaoFailure('taobao_connection_failed') from None
        except (ValueError,UnicodeError):raise TaobaoFailure('taobao_invalid_response') from None
