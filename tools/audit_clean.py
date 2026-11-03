"""Conservative source export checks. Reports locations only, never matched values."""
from pathlib import Path
import argparse
import ast
import hashlib
import json
import re

ROOT=Path(__file__).resolve().parents[1]
FORBIDDEN_DIRS={'.git','.private','.build','.venv','node_modules','__pycache__'}
FORBIDDEN_SUFFIXES={'.pyc','.db','.sqlite','.sqlite3','.pem','.key','.p12','.pfx','.mobileconfig','.log','.onnx','.safetensors'}
BINARY_ALLOWLIST={'独立服务/web_shared/yanxiaoban-touch.png','独立服务/web_shared/yanxiaoban-favicon.png',
                  '独立服务/web_shared/ready-v035.wav'}
PATTERNS={
    'api_key_pattern': re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'),
    'private_key_block': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'),
    'github_token': re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}'),
    'cloud_access_key': re.compile(r'\bAKIA[A-Z0-9]{16}'),
    'developer_home': re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
}


def inspect(root=ROOT):
    root=Path(root)
    findings=[];count=0;total=0
    for p in sorted(root.rglob('*')):
        relative=p.relative_to(root);name=relative.as_posix()
        if p.is_symlink():
            findings.append({'path':name,'rule':'symlink_not_allowed'});continue
        if not p.is_file():continue
        count+=1;total+=p.stat().st_size
        if set(relative.parts)&FORBIDDEN_DIRS or p.name=='.env' or p.suffix in FORBIDDEN_SUFFIXES:
            findings.append({'path':name,'rule':'private_or_generated_file'})
        raw=p.read_bytes()
        if name == '独立服务/data/flight-locations.json' or p.suffix == '.xlsx':
            findings.append({'path':name,'rule':'provider_dataset_not_for_source_distribution'})
        try:text=raw.decode('utf-8')
        except UnicodeError:
            if name not in BINARY_ALLOWLIST:
                findings.append({'path':name,'rule':'unreviewed_binary'})
            continue
        if p.suffix=='.py':
            try:ast.parse(text,filename=name)
            except SyntaxError as error:findings.append({'path':name,'line':error.lineno,'rule':'python_syntax'})
        if p.suffix=='.json':
            try:json.loads(text)
            except ValueError:findings.append({'path':name,'rule':'json_syntax'})
        for rule,pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                findings.append({'path':name,'line':text[:match.start()].count('\n')+1,'rule':rule})
    audio=root/'独立服务/web_shared/ready-v035.wav'
    metadata=root/'产品化/greeting-source.json'
    if audio.exists() and metadata.exists():
        value=json.loads(metadata.read_text())
        if value.get('text')!='我在，请说。' or value.get('voice_cloned') is not False or hashlib.sha256(audio.read_bytes()).hexdigest()!=value.get('sha256'):
            findings.append({'path':str(audio.relative_to(root)),'rule':'generic_greeting_mismatch'})
    provenance=root/'ASSET_PROVENANCE.json'
    if provenance.is_file():
        for entry in json.loads(provenance.read_text())['resources']:
            path=root/entry['path']
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:
                findings.append({'path':entry['path'],'rule':'reviewed_resource_changed'})
    return {'passed':not findings,'files':count,'bytes':total,'findings':findings,
            'scope':'static hygiene only; not a complete security or license audit'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=ROOT);args=parser.parse_args()
    result=inspect(args.root);print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(not result['passed'])
