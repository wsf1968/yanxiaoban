"""Structured tools for an already-authorized local model agent and human fallback."""
import argparse,json
from pathlib import Path
from maintenance import Maintenance,TOOLS,safe_error

def main():
    p=argparse.ArgumentParser();p.add_argument('--home',default=str(Path.home()/'Library/Application Support/言小办开源预览'));p.add_argument('--bundle',required=True);p.add_argument('--action',choices=list(TOOLS));p.add_argument('--catalog',action='store_true');a=p.parse_args()
    if a.catalog:print(json.dumps({'tools':TOOLS,'arbitrary_shell':False,'credentials_in_output':False},ensure_ascii=False));return
    try:answer={'ok':True,'result':Maintenance(a.home,a.bundle).execute(a.action or 'inspect')}
    except Exception as error:answer={'ok':False,'error':safe_error(error),'manual_available':True}
    print(json.dumps(answer,ensure_ascii=False))

if __name__=='__main__':main()
