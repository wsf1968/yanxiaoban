"""Private mobile entry; may use a separately configured HTTPS forwarding route."""
import argparse
import datetime as dt
import os
import json
import re
import time
import secrets

from app import ROOT, load_private_config, make_server
from session_store import save_sessions


def _login_link(private, now):
    """Keep a short-lived link reusable across browsers and normal service restarts."""
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(0o700)
    path = private / 'login-link.json'
    try:
        grant = json.loads(path.read_text())
    except FileNotFoundError:
        grant = {}
        # Retain the link already shown to the user when upgrading the old launcher.
        note = private / '手机配对说明.txt'
        if note.exists():
            match = re.search(r'^http://[^\n]+/#login=(\d{12})$', note.read_text(), re.M)
            if match:
                grant = {'code': match.group(1), 'expires_at': note.stat().st_mtime + 1800}
    if (not isinstance(grant, dict) or not isinstance(grant.get('code'), str)
            or not re.fullmatch(r'\d{12}', grant['code'])
            or type(grant.get('expires_at')) not in (int, float)
            or not now < grant['expires_at'] <= now + 1800):
        grant = {'code': ''.join(secrets.choice('0123456789') for _ in range(12)),
                 'expires_at': now + 1800}
    save_sessions(path, grant)
    return grant


import threading
_login_link_lock=threading.Lock()
def login_link(private, now):
    with _login_link_lock:
        return _login_link(private,now)


def main():
    parser = argparse.ArgumentParser(description='言小办同一Wi-Fi手机测试入口')
    parser.add_argument('--host', required=True, help='运行言小办的电脑的局域网IPv4地址')
    parser.add_argument('--port', type=int, default=18768)
    args = parser.parse_args()
    load_private_config()
    private = ROOT / '.private'
    grant = login_link(private, time.time())
    code = grant['code']
    voice = None
    if os.environ.get('PAI_SPEECH_TOKEN_FILE'):
        from commercial_voice import CommercialVoice
        voice = CommercialVoice(private / 'commercial-conversations.db', os.environ['PAI_SPEECH_TOKEN_FILE'])
    server, pool = make_server(args.port, bind_host=args.host, pairing_code=code,
                               lifetime=None, session_path=ROOT / '.private' / 'device-sessions.json',
                               pairing_expires_at=grant['expires_at'], voice=voice,
                               public_origin=os.environ.get('PAI_PUBLIC_ORIGIN'))
    address = (os.environ['PAI_PUBLIC_ORIGIN'] + os.environ.get('PAI_PUBLIC_PATH', '/')) if os.environ.get('PAI_PUBLIC_ORIGIN') else f'http://{args.host}:{args.port}/'
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    deadline = dt.datetime.fromtimestamp(grant['expires_at'], now.tzinfo)
    instructions = private / '手机配对说明.txt'
    text = f'''言小办商用版 · 手机登录

首次在 iPhone Safari 打开下面的登录链接，即可记住这部手机，无需手输配对码：
{address}#login={code}

以后直接打开：{address}
网页登录和快捷指令共用登录状态，服务重启保留，最长90天。
首次登录链接截止：{deadline:%H:%M:%S} 北京时间；截止前可分别登录 Safari 和 Chrome。
备用首次登录码：{code}
服务持续运行，不再2小时自动关闭；停止 mobile.py 或电脑休眠时访问会中断。

登录凭据只存在浏览器和本机私有目录（磁盘只保存凭据摘要）。
不要公开本文件、首次登录链接或私有配置。API key 不进入手机。
手机与电脑连接同一可信 Wi-Fi，无需路由器端口转发。
'''
    descriptor = os.open(instructions, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(text)
    instructions.chmod(0o600)
    print(f'手机测试入口已开启：{address}', flush=True)
    print('手机登录说明已写入私有文件，已登录设备重启后继续使用。', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pool.shutdown(wait=False, cancel_futures=True)


if __name__ == '__main__':
    main()
