"""Build an account-free, reproducible internal macOS installer artifact."""
from pathlib import Path
import argparse,base64,datetime,hashlib,json,os,plistlib,shutil,subprocess,sys

ROOT=Path(__file__).resolve().parents[1];BUILD=ROOT/'.build'

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()

def copy_code(src,dst):
    dst.mkdir(parents=True,exist_ok=True)
    for p in src.glob('*.py'):
        if p.name not in ('jd_setup.py','taobao_setup.py','linux_start.py') and not p.name.startswith(('accept_','verify_','replay_','fix_')):shutil.copy2(p,dst/p.name)

def native_component(destination):
    contents=destination/'Contents';binary=contents/'MacOS/NativeSecretary';binary.parent.mkdir(parents=True)
    info={'CFBundleIdentifier':'org.yanxiaoban.preview.commercial.native','CFBundleName':'言小办系统事项','CFBundleDisplayName':'言小办系统事项','CFBundleExecutable':'NativeSecretary','CFBundleVersion':'1','CFBundlePackageType':'APPL','LSUIElement':True,
          'NSRemindersFullAccessUsageDescription':'将你交给助手的待办保存到苹果提醒事项，设置系统到点通知；仅操作助手登记的条目。',
          'NSCalendarsFullAccessUsageDescription':'将你交给助手的日程保存到苹果日历，并修改或取消助手登记的日程。'}
    (contents/'Info.plist').write_bytes(plistlib.dumps(info))
    subprocess.run(['xcrun','swiftc','-swift-version','5','-module-cache-path',str(BUILD/'swift-cache'),str(ROOT/'独立服务/native_macos/NativeSecretary.swift'),'-o',str(binary),'-framework','Cocoa','-framework','EventKit'],check=True)
    subprocess.run(['/usr/bin/codesign','--force','--sign','-','--identifier',info['CFBundleIdentifier'],str(destination)],check=True)

def build(version,output,include_flight_data=False):
    import re
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', version):raise ValueError('Invalid version')
    if not (BUILD/'prepared.json').is_file():raise ValueError('Run 产品化/prepare_assets.py first')
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    inputs=[p for folder in ('产品化','独立服务','记账模块','秘书模块') for p in (ROOT/folder).rglob('*') if p.is_file() and p.suffix in ('.py','.js','.css','.html','.swift','.json','.txt','.lock','.toml','.cjs','.wav','.png') and not any(x in p.parts for x in ('.private','.build','__pycache__','node_modules','tests','dist'))]
    fingerprints={str(p.relative_to(ROOT)):digest(p) for p in inputs}
    output=Path(output);app=output/'言小办安装助手.app'
    if app.exists():raise ValueError('Build destination already exists')
    payload=app/'Contents/Resources/payload';payload.mkdir(parents=True)
    service=payload/'独立服务';copy_code(ROOT/'独立服务',service)
    # Read-only JD capability catalogue is required at execution time; it is
    # public protocol metadata, not the developer's saved account catalogue.
    shutil.copy2(ROOT/'独立服务/jd_channels.json',service/'jd_channels.json')
    (service/'data').mkdir()
    if include_flight_data:
        expected = json.loads((ROOT/'产品化/flight-data-sources.json').read_text())['converted_sha256']
        if digest(BUILD/'flight-data/flight-locations.json') != expected:
            raise ValueError('Local flight reference data does not match pinned metadata')
        shutil.copy2(BUILD/'flight-data/flight-locations.json',service/'data/flight-locations.json')
    shutil.copytree(ROOT/'独立服务/shared_voice',service/'shared_voice',ignore=shutil.ignore_patterns('__pycache__','*.pyc','.private','tests'))
    for folder in ['web','web_shared']:
        shutil.copytree(ROOT/'独立服务'/folder,service/folder,ignore=shutil.ignore_patterns('.DS_Store'))
    greeting=ROOT/'独立服务/web_shared/ready-v035.wav'
    if not greeting.is_file():raise ValueError('Qwen generic greeting must be prepared first')
    shutil.copy2(greeting,service/'web_shared/ready-v035.wav')
    for folder in ['记账模块','秘书模块']:
        target=payload/folder;target.mkdir()
        for p in (ROOT/folder).iterdir():
            if p.name.startswith('.') or p.name in ('__pycache__','tests','test-results'):continue
            if p.is_file() and p.suffix in ('.py','.cjs','.json','.txt','.md','.toml'):shutil.copy2(p,target/p.name)
            elif p.is_dir() and (p.name.startswith('xiaopai_') or p.name in ('node_modules','THIRD_PARTY_LICENSES')):
                shutil.copytree(p,target/p.name,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.cache'))
    installer=payload/'installer';installer.mkdir()
    for name in ['maintenance.py','installer_model.py','installer_server.py','maintain_cli.py']:shutil.copy2(ROOT/'产品化'/name,installer/name)
    shutil.copytree(ROOT/'产品化/web',installer/'web')
    runtime=payload/'runtime';runtime.mkdir()
    shutil.copytree(BUILD/'python-runtime-qwen-0.4.1/python',runtime/'python',symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    # Build-time pip/CLI shebangs must not retain this developer's source path.
    for entry in (runtime/'python/bin').iterdir():
        if entry.is_file() and not entry.is_symlink():
            data=entry.read_bytes()
            if data.startswith(b'#!') and b'/Users/' in data[:1024]:
                lines=data.splitlines(keepends=True)
                body=b''.join(lines[3:] if lines[0].strip()==b'#!/bin/sh' and lines[1].startswith(b"'''exec'") else lines[1:])
                wrapper=b'''#!/bin/sh
''' + b"'''exec' \"$(dirname \"$0\")/python3\" \"$0\" \"$@\"\n' '''\n"
                entry.write_bytes(wrapper+body)
    node=next((BUILD/'node-runtime').glob('node-*'))
    shutil.copytree(node,runtime/'node',symlinks=True)
    assets=payload/'assets';assets.mkdir()
    models=assets/'models';models.mkdir()
    old_models=BUILD/'speech/models'
    shutil.copytree(old_models/'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17',models/'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17')
    shutil.copy2(old_models/'silero_vad_v5.onnx',models/'silero_vad_v5.onnx')
    candidate=BUILD/'speech/models/Qwen3-TTS-1.7B-CustomVoice-8bit'
    shutil.copytree(candidate,models/candidate.name,ignore=shutil.ignore_patterns('espeak-ng-data','*.wav','.DS_Store'))
    tts_source=json.loads((ROOT/'产品化/commercial-tts-source.json').read_text())
    for entry in tts_source['files']:
        target=models/candidate.name/entry['path']
        if digest(target)!=entry['sha256']:raise ValueError('Pinned Qwen model hash mismatch')
    native_component(payload/'native/言小办系统事项.app')
    shutil.copy2(ROOT/'产品化/runtime-sources.json',assets/'runtime-sources.json')
    shutil.copy2(ROOT/'产品化/runtime-requirements.lock',assets/'runtime-requirements.lock')
    sources=json.loads((ROOT/'产品化/speech-asr-sources.json').read_text())
    sources=[s for s in sources if s['name']=='silero_vad_v5.onnx' or s['name'].startswith('sherpa-onnx-sense-voice-')]
    sources.append(json.loads((ROOT/'产品化/commercial-tts-source.json').read_text()))
    (assets/'speech-model-sources.json').write_text(json.dumps(sources,ensure_ascii=False,indent=2))
    shutil.copy2(ROOT/'产品化/commercial-tts-source.json',assets/'commercial-tts-source.json')
    for license_name in ('LICENSE','NOTICE','LICENSE_SCOPE.json','THIRD_PARTY_NOTICES.md','ASSET_PROVENANCE.json'):
        shutil.copy2(ROOT/license_name,assets/license_name)
    shutil.copytree(ROOT/'assets',assets/'project-artwork')
    shutil.copy2(ROOT/'docs/ASSETS.md',assets/'RESOURCE_TERMS.md')
    shutil.copy2(ROOT/'产品化/flight-data-sources.json',assets/'flight-data-sources.json')
    notices=assets/'THIRD_PARTY_NOTICES';notices.mkdir()
    for p in (ROOT/'产品化/许可来源').glob('*'):
        if p.is_file():shutil.copy2(p,notices/p.name)
    shutil.copy2(ROOT/'产品化/greeting-source.json',assets/'greeting-source.json')
    shutil.copytree(ROOT/'产品化/说明',payload/'使用说明')
    (assets/'语音资源说明.txt').write_text('本安装包默认采用Qwen3-TTS 1.7B CustomVoice MLX 8bit的Vivian预设音色B（原模型及转换Apache-2.0），mlx-audio 0.4.8与MLX推理代码为MIT许可。模型固定版本和各文件散列随包提供；默认解释器及模型由包内相对位置发现，无需填写开发机路径。固定应答为同一B声音的通用“我在，请说。”。ASR保留SenseVoiceSmall及FunASR模型协议1.1，VAD沿用Silero公开模型；未打包旧Matcha-Baker或Kokoro权重。B声音已获原用户听感认可，仍不能替代此新包在干净Mac与新安装iPhone的验收。正式公开分发前仍需完成签名公证和交付验收。\n')
    (assets/'交付边界.txt').write_text('macOS＋iPhone内部验收包。本版本随包MLX 0.32.0轮子要求macOS 26.0及以上、Apple Silicon arm64，实际已测系统以源码TECHNICAL_VERIFICATION.json为准；较早系统需另行适配。模型和接口使用用户自己的账号；个人数据与程序版本分离。未包含用户Key、证书私钥、手机授权、历史、记忆或账本。可选机票查询使用用户自有聚合818账号和Key，调用付费、费用直付供应商，由用户选择配置启用；默认关闭，未启用不可宣称可用。范围仅航班查询与参考票价，税费、舱位和可购性未核验，不承诺最低价或下单。本轮未发起付费机票调用，历史和模拟验证不算实时手机查询通过。正式对外发布前还需发布签名公证、干净用户环境与iPhone完整验收，以及随包模型来源和许可的最终确认。\n')
    secret=BUILD/'release-signing-key.bin'
    if not secret.exists():
        key=Ed25519PrivateKey.generate();secret.write_bytes(key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption()));secret.chmod(0o600)
    else:key=Ed25519PrivateKey.from_private_bytes(secret.read_bytes())
    public=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex()
    (payload/'release-public-key.txt').write_text(public+'\n')
    entries=[]
    for p in sorted(payload.rglob('*')):
        if p.is_symlink():entries.append({'path':str(p.relative_to(payload)),'kind':'symlink','target':os.readlink(p)})
        elif p.is_file():entries.append({'path':str(p.relative_to(payload)),'kind':'file','sha256':digest(p),'bytes':p.stat().st_size})
    manifest={'schema':'commercial-v1','platform':'macos-arm64','version':version,'files':entries,'phone_tested':False,'distribution':'gpl3_source_preview; external_acceptance_pending','source_fingerprints':fingerprints}
    manifest['optional_juhe_reference_data_included'] = include_flight_data
    manifest['signature']=base64.b64encode(key.sign(json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())).decode()
    (payload/'release.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    binary=app/'Contents/MacOS/Launcher';binary.parent.mkdir()
    subprocess.run(['xcrun','swiftc','-swift-version','5','-module-cache-path',str(BUILD/'swift-cache'),str(ROOT/'产品化/Launcher.swift'),'-o',str(binary),'-framework','Cocoa'],check=True)
    info={'CFBundleIdentifier':'org.yanxiaoban.preview.installer','CFBundleName':'言小办安装助手','CFBundleDisplayName':'言小办安装助手','CFBundleExecutable':'Launcher','CFBundlePackageType':'APPL','CFBundleVersion':version,'LSUIElement':True,'LSMinimumSystemVersion':'26.0'}
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(app)],check=True)
    if any(digest(ROOT/p)!=v for p,v in fingerprints.items()):raise RuntimeError('Source changed during build; artifact not accepted')
    report={'app':str(app),'version':version,'payload_files':len(entries),'payload_bytes':sum(e.get('bytes',0) for e in entries),'personal_secrets_included':False,'signing':'ad_hoc_internal','notarized':False,'phone_tested':False}
    (output/'构建结果.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--version',required=True);p.add_argument('--output',required=True)
    p.add_argument('--with-juhe-flight-data',action='store_true',help='Include locally prepared provider reference data; not for public redistribution')
    a=p.parse_args();build(a.version,a.output,a.with_juhe_flight_data)
