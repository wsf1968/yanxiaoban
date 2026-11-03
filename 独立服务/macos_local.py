"""Isolated Mac + iPhone development runtime; reuses the mature commercial UI and DS."""
import argparse
import base64
from datetime import datetime
import io
import ipaddress
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import socket
import subprocess
import threading
import time
import wave

from app import ROOT, load_private_config, make_server
from local_https import LANServer, TLSServer, prepare_certificates, private_write, proxy_handler, setup_handler
from mobile import login_link
from private_json import write_private

PRIVATE = ROOT/'.private/macos'
# Generic stock greeting for the clean commercial preview.
WELCOME_TEXT = '我在，请说。'


def prepare_welcome(voice,speech,token,private):
    """Use the selected Qwen voice for the existing greeting; leave shared assets intact."""
    if getattr(speech,'backend_name',None)!='qwen3':return None
    body=json.dumps({'text':WELCOME_TEXT},ensure_ascii=False).encode()
    result=speech('synthesize',body,'application/json',Path(token).read_text())
    if result.get('mime')!='audio/wav':raise ValueError('Invalid welcome audio type')
    raw=base64.b64decode(result['audio'],validate=True)
    with wave.open(io.BytesIO(raw)) as audio:
        frames=audio.getnframes()
        if ((audio.getnchannels(),audio.getsampwidth(),audio.getcomptype())!=(1,2,'NONE')
                or not 8000<=audio.getframerate()<=48000
                or not 1<=frames<=audio.getframerate()*30
                or len(audio.readframes(frames))!=frames*2):
            raise ValueError('Invalid welcome WAV')
    destination=Path(private)/'ready-qwen3.wav'
    private_write(destination,raw)
    voice.welcome_path=destination
    return destination


def feature_setup_url(features,private,base):
    address=features.bootstrap_url(base)
    return address+'&login='+login_link(private,time.time())['code'] if address else None


def native_hostname():
    name = subprocess.run(['/usr/sbin/scutil','--get','LocalHostName'],capture_output=True,text=True,check=True,timeout=5).stdout.strip()
    return name.lower()+'.local'


def lan_address():
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.connect(('192.0.2.1',9))
        address=sock.getsockname()[0]
    ip=ipaddress.ip_address(address)
    if not ip.is_private or ip.is_loopback: raise ValueError('未找到可供手机连接的局域网地址。')
    return address


def configuration(private=PRIVATE):
    private.mkdir(parents=True,exist_ok=True,mode=0o700);private.chmod(0o700)
    path=private/'runtime.json'
    actual=native_hostname()
    if path.exists():
        config=json.loads(path.read_text())
        if config['hostname']!=actual: raise ValueError('电脑局域网名称已变化；未擅自替换手机入口，请先核对原连接。')
        return config
    config={'hostname':actual,'https_port':18780,'setup_port':18779,'backend_port':18781}
    write_private(path,config)
    return config


def local_speech(models,private):
    from qwen_speech import provider_settings
    backend_name,settings=provider_settings(private,default_backend='matcha')
    if backend_name=='qwen3':
        from qwen_speech import load_qwen_speech
        return load_qwen_speech(models,private,environ=settings)
    from commercial_voice import CommercialVoice  # Makes the existing shared modules importable.
    from speech_engine import SherpaBackend,SpeechEngine
    from speech_profile import ProfileSpeech
    backend=SherpaBackend(models)
    engine=SpeechEngine(backend)
    path=private/'speech-profile.json'
    if path.exists(): profile=json.loads(path.read_text())
    else:
        profile={'model_root':str(models),'speed':0.96,'silence_scale':0.30}
        write_private(path,profile)
    def shared(route,body,kind,token):
        if route!='transcribe' or kind!='audio/wav':raise ValueError('Unsupported local speech request')
        return engine.transcribe(body)
    # Reuse the already-loaded TTS object; no duplicate acoustic model is loaded.
    speech=ProfileSpeech(profile,shared,factory=lambda:(backend.tts,backend.so.GenerationConfig))
    speech.warmup()
    token=private/'local-speech-token'
    if not token.exists():private_write(token,secrets.token_urlsafe(32).encode())
    return speech,token


def main():
    parser=argparse.ArgumentParser(description='言小办源码环境检查；安装请参照仓库 BUILDING.md')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    if not args.check:
        parser.error('请先按 BUILDING.md 构建独立安装包，再通过安装向导启动。')
    from macos_preflight import inspect_environment
    print(json.dumps(inspect_environment(),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
