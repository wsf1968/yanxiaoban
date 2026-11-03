"""Loopback-only commercial sandbox. No changes to production routes or keys."""
import concurrent.futures
import hmac
import hashlib
import html
import ipaddress
import json
import os
import mimetypes
from pathlib import Path
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from core import DeepSeek, ServiceError, validate_request
from trains import TrainClient
from session_store import load_sessions, save_sessions

ROOT = Path(__file__).resolve().parent


def load_private_config():
    path = ROOT / '.env'
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            name, sep, value = line.partition('=')
            if sep and name.strip() in ('DEEPSEEK_API_KEY', 'JUHE_TRAIN_KEY', 'PAI_SPEECH_TOKEN_FILE', 'PAI_PUBLIC_ORIGIN', 'PAI_PUBLIC_PATH'):
                value = value.strip().strip('\"\'')
                if value and not os.environ.get(name.strip()):
                    os.environ[name.strip()] = value


def make_server(port=18767, client=None, train_client=None, *, bind_host='127.0.0.1',
                pairing_code=None, clock=time.time, lifetime=7200, session_path=None, pairing_expires_at=None, voice=None, public_origin=None, pairing_provider=None):
    cookie_name = getattr(voice, 'install_profile', {}).get('cookie_name', 'pai_session')
    if not __import__('re').fullmatch(r'pai_session(?:_[a-f0-9]{12})?', cookie_name):raise ValueError('Invalid installation cookie name')
    mobile = bind_host != '127.0.0.1' or pairing_code is not None
    if mobile:
        address = ipaddress.ip_address(bind_host)
        if (address.version != 4 or not address.is_private or address.is_unspecified
                or not isinstance(pairing_code, str) or not 12 <= len(pairing_code) <= 64):
            raise ValueError('手机测试仅允许绑定指定局域网IPv4，并要求一次性配对码。')
    token = secrets.token_urlsafe(32)
    client = client or DeepSeek(os.environ.get('DEEPSEEK_API_KEY', ''))
    train_client = train_client or TrainClient(os.environ.get('JUHE_TRAIN_KEY', ''))
    tasks, lock = {}, threading.Lock()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    capacity = threading.BoundedSemaphore(2)
    origin = f'http://{bind_host}:{port}'
    origins = {f'{bind_host}:{port}': origin}
    if public_origin:
        public = urlsplit(public_origin)
        if public.scheme != 'https' or not public.hostname or public.path or public.query or public.fragment or public.username:
            raise ValueError('Public entry must be an HTTPS origin')
        origins[public.netloc] = public_origin
    shared_web = ROOT / 'web_shared'
    shared_assets = {p.name for p in shared_web.iterdir() if p.is_file()} if voice else set()
    started = clock()
    pairing_deadline = started + 1800 if pairing_expires_at is None else pairing_expires_at
    persistent = mobile and session_path is not None
    session_age = 90 * 86400 if persistent else lifetime
    sessions = load_sessions(session_path, clock()) if persistent else {}
    pair_attempts = []
    pair_used = False

    def execute(identity, payload):
        def update(message):
            with lock:
                tasks[identity].update(state='running', progress=message)
        try:
            provider = train_client if payload['mode'] == 'train' else client
            result = provider.run(payload, update)
            with lock:
                tasks[identity].update(state=result['state'], result=result, progress='已完成')
        except Exception as error:
            message = str(error) if isinstance(error, ServiceError) else '任务未完成，请稍后重试。'
            with lock:
                tasks[identity].update(state='failed', message=message)
        finally:
            capacity.release()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Do not log queries, credentials or URL parameters.

        def send(self, status, value, kind='application/json; charset=utf-8', cookie=False):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "media-src 'self' blob:; default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie:
                cookie_value = token if cookie is True else cookie
                same_site = 'Lax' if persistent else 'Strict'
                secure = '; Secure' if public_origin and self.headers.get('Host') == urlsplit(public_origin).netloc else ''
                self.send_header('Set-Cookie', f'{cookie_name}={cookie_value}; HttpOnly; SameSite={same_site}; Path=/; Max-Age={session_age}{secure}')
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def valid_host(self):
            return self.headers.get('Host') in origins

        def stream_events(self, produce):
            # Opt-in transport only. Original JSON clients retain their exact API.
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
            self.send_header('Cache-Control', 'no-store, no-transform')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            connected = True
            def emit(value):
                nonlocal connected
                if connected:
                    try:
                        self.wfile.write(json.dumps(value, ensure_ascii=False).encode() + b'\n')
                        self.wfile.flush()
                    except (OSError, ValueError):
                        connected = False
                # Disconnecting a display must never abort an in-flight write.
            try:
                produce(emit)
            except Exception as error:
                from commercial_voice import Conflict, ConversationFailure
                from personal_memory import MemoryConflict, MemoryCapacity
                code = ('conversation_conflict' if isinstance(error, Conflict) else
                        'memory_changed' if isinstance(error, MemoryConflict) else
                        'memory_capacity' if isinstance(error, MemoryCapacity) else
                        error.code if isinstance(error, ConversationFailure) else
                        'result_not_found' if isinstance(error, LookupError) else
                        'invalid_home_request' if isinstance(error, (ValueError, TypeError, UnicodeError)) else
                        'speech_unavailable')
                emit({'event': 'error', 'error': code})

        def session(self):
            try:
                cookies = SimpleCookie(self.headers.get('Cookie', ''))
                value = cookies[cookie_name].value if cookie_name in cookies else ''
                if not mobile:
                    return token if hmac.compare_digest(value, token) else None
                with lock:
                    identity = hashlib.sha256(value.encode()).hexdigest() if persistent else value
                    return identity if sessions.get(identity, 0) > clock() else None
            except Exception:
                return None

        def authenticated(self):
            return self.session() is not None

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {'message': '请使用本次测试指定的地址。'})
            if mobile and lifetime is not None and clock() - started >= lifetime:
                return self.send(410, {'message': '手机测试已到期，请在运行言小办的电脑重新启动入口。'})
            path = urlsplit(self.path).path
            static = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/pair.js': ('pair.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if mobile and not self.authenticated():
                guest_files = {'/': ('pair.html', 'text/html; charset=utf-8'),
                               '/pair.js': ('pair.js', 'text/javascript; charset=utf-8'),
                               '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if path in guest_files:
                    name, kind = guest_files[path]
                    return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
                return self.send(401, {'message': '请先通过电脑提供的链接登录。'})
            if voice and (path == '/' or path.lstrip('/') in shared_assets):
                name = 'index.html' if path == '/' else path.lstrip('/')
                kind = mimetypes.guess_type(name)[0] or 'application/octet-stream'
                profile = getattr(voice, 'install_profile', None)
                if profile and name == 'index.html':
                    page = (shared_web / name).read_text()
                    alias = html.escape(profile['assistant_name'])
                    greeting = html.escape(profile.get('user_name') or alias)
                    page = page.replace('言小办', alias).replace('商用测试 · '+alias, alias).replace('你好，我在', greeting+'，我在')
                    return self.send(200, page.encode(), kind, cookie=not mobile)
                welcome = getattr(voice, 'welcome_path', None)
                if name == 'ready-v035.wav' and welcome and Path(welcome).is_file():
                    return self.send(200, Path(welcome).read_bytes(), kind)
                return self.send(200, (shared_web / name).read_bytes(), kind, cookie=path == '/' and not mobile)
            if path in static:
                name, kind = static[path]
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind, cookie=path == '/' and not mobile)
            if not self.authenticated():
                return self.send(401, {'message': '请从本机首页打开。'})
            if path == '/api/features':
                features=getattr(voice,'features',None)
                if features is None:return self.send(404,{'error':'features_not_connected'})
                current=SimpleCookie(self.headers.get('Cookie',''))[cookie_name].value
                return self.send(200,features.status(hashlib.sha256(current.encode()).hexdigest()))
            if path == '/api/status':
                commerce = getattr(getattr(voice, 'runtime', None), 'commerce', None)
                profile = getattr(voice, 'install_profile', None)
                if profile:
                    tools=profile['capabilities']
                    return self.send(200, {'ready':True,'train_ready':'rail' in tools,'model':profile['model'],
                        'capabilities':tools,'shopping_platforms':list(commerce.adapters) if commerce else [],
                        'access':'lan' if mobile else 'local','phone_tested':False})
                return self.send(200, {'ready': bool(client.key), 'train_ready': bool(train_client.key),
                                       'capabilities': ['search', 'travel', 'train'] + (['shopping'] if commerce else []) + (['bookkeeping','secretary'] if getattr(voice,'features',None) else []),
                                       'model': 'deepseek-flash', 'fetch': False, 'shopping': ('jd-curated' if list(commerce.adapters) == ['jd'] else 'connected') if commerce else 'pending',
                                       'shopping_platforms': list(commerce.adapters) if commerce else [],
                                       'access': ('lan' if persistent else 'lan-test') if mobile else 'local',
                                       'expires_at': started + lifetime if mobile and lifetime is not None else None},
                                 cookie=SimpleCookie(self.headers.get('Cookie', ''))[cookie_name].value if persistent else False)
            if path.startswith('/api/tasks/'):
                with lock:
                    task = dict(tasks.get(path.rsplit('/', 1)[-1], {}))
                if task.get('_owner') != self.session():
                    task = {}
                task.pop('_owner', None)
                return self.send(200 if task else 404, task or {'message': '任务不存在或已过期。'})
            return self.send(404, {'message': '未找到。'})

        def do_POST(self):
            nonlocal pair_used
            if not self.valid_host() or self.headers.get('Origin') != origins.get(self.headers.get('Host')):
                return self.send(403, {'message': '请在本机页面内发起任务。'})
            if mobile and lifetime is not None and clock() - started >= lifetime:
                return self.send(410, {'message': '手机测试已到期，请在运行言小办的电脑重新启动入口。'})
            if mobile and urlsplit(self.path).path == '/api/pair':
                with lock:
                    pair_attempts[:] = [t for t in pair_attempts if clock() - t < 60]
                    if len(pair_attempts) >= 10:
                        return self.send(429, {'message': '配对尝试过多，请稍后再试。'})
                    pair_attempts.append(clock())
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 1 <= length <= 256:
                        raise ValueError
                    body = json.loads(self.rfile.read(length))
                    candidate = body.get('code') if isinstance(body, dict) else None
                    if not isinstance(candidate, str):
                        raise ValueError
                except (ValueError, UnicodeError):
                    return self.send(400, {'message': '配对信息格式不正确。'})
                with lock:
                    active_code,active_deadline=pairing_code,pairing_deadline
                    if pairing_provider:
                        current=pairing_provider()
                        active_code,active_deadline=current['code'],current['expires_at']
                        if not isinstance(active_code,str) or not 12<=len(active_code)<=64 or not isinstance(active_deadline,(int,float)):
                            return self.send(503,{'message':'手机连接授权暂未准备好。'})
                    if (pair_used and not persistent) or clock() >= active_deadline:
                        return self.send(410, {'message': '登录链接已到期；已登录浏览器请直接打开首页，新浏览器请从电脑获取新链接。'})
                    if not hmac.compare_digest(candidate, active_code):
                        return self.send(401, {'message': '配对码不正确，请核对电脑上的12位数字。'})
                    session = secrets.token_urlsafe(32)
                    identity = hashlib.sha256(session.encode()).hexdigest() if persistent else session
                    expiry = clock() + session_age if persistent else started + lifetime
                    updated = {k: v for k, v in sessions.items() if v > clock()}
                    updated[identity] = expiry
                    if persistent:
                        try:
                            save_sessions(session_path, updated)
                        except OSError:
                            return self.send(503, {'message': '登录暂未保存，请稍后重试。'})
                    sessions.clear()
                    sessions.update(updated)
                    pair_used = True
                return self.send(200, {'message': '配对成功'}, cookie=session)
            if not self.authenticated():
                return self.send(403, {'message': '请先登录，再发起查询。'})
            if urlsplit(self.path).path == '/api/features':
                features=getattr(voice,'features',None)
                if features is None:return self.send(404,{'error':'features_not_connected'})
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 1<=length<=4096 or self.headers.get('Transfer-Encoding'):raise ValueError('invalid_feature_request')
                    body=json.loads(self.rfile.read(length))
                    if not isinstance(body,dict):raise ValueError('invalid_feature_request')
                    current=SimpleCookie(self.headers.get('Cookie',''))[cookie_name].value
                    return self.send(200,features.handle(hashlib.sha256(current.encode()).hexdigest(),body))
                except ValueError as error:
                    return self.send(400,{'error':str(error)})
                except Exception:
                    return self.send(503,{'error':'feature_operation_unverified'})
            if voice and urlsplit(self.path).path == '/api/presentation':
                try:
                    from home_api import identity
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 1 <= length <= 256 or self.headers.get('Transfer-Encoding'):
                        return self.send(400, {'error': 'invalid_presentation_request'})
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        return self.send(415, {'error': 'invalid_content_type'})
                    body = json.loads(self.rfile.read(length))
                    if (not isinstance(body, dict) or set(body) not in ({'id', 'requestId'}, {'id', 'requestId', 'cached'})
                            or ('cached' in body and body['cached'] is not True)):
                        raise ValueError('invalid_presentation_request')
                    task_id, request_id = identity(body['id']), identity(body['requestId'])
                    current = SimpleCookie(self.headers.get('Cookie', ''))[cookie_name].value
                    owner = hashlib.sha256(current.encode()).hexdigest()
                    voice.presenter.load(owner, task_id, request_id)
                except LookupError:
                    return self.send(404, {'error': 'result_not_found'})
                except (ValueError, TypeError, UnicodeError):
                    return self.send(400, {'error': 'invalid_presentation_request'})
                return self.stream_events(lambda emit: voice.presenter.stream(owner, task_id, request_id, emit, body.get('cached', False)))
            if voice and urlsplit(self.path).path == '/api/roundtrip':
                from commercial_voice import Conflict, ConversationFailure
                from personal_memory import MemoryCapacity, MemoryConflict
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 1 <= length <= 1_000_000 or self.headers.get('Transfer-Encoding'):
                        return self.send(413, {'error': 'invalid_request_size'})
                    kind = self.headers.get('Content-Type', '').split(';')[0]
                    if kind not in ('audio/wav', 'application/json'):
                        return self.send(415, {'error': 'invalid_content_type'})
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        return self.send(400, {'error': 'truncated_request'})
                    current_cookie = SimpleCookie(self.headers.get('Cookie', ''))[cookie_name].value
                    if (self.headers.get('Accept') == 'application/x-ndjson'
                            and hasattr(voice, 'handle_progress')):
                        return self.stream_events(lambda emit: voice.handle_progress(
                            'pi_cc_device=' + current_cookie, raw, self.headers,
                            kind == 'audio/wav', emit))
                    answer = voice.handle('pi_cc_device=' + current_cookie, raw, self.headers, audio=kind == 'audio/wav')
                    return self.send(200, answer)
                except MemoryConflict:
                    return self.send(409, {'error': 'memory_changed'})
                except MemoryCapacity:
                    return self.send(400, {'error': 'memory_capacity'})
                except Conflict:
                    return self.send(409, {'error': 'conversation_conflict'})
                except LookupError:
                    return self.send(404, {'error': 'result_not_found'})
                except ConversationFailure as error:
                    return self.send(503, {'error': error.code})
                except (ValueError, TypeError, UnicodeError):
                    return self.send(400, {'error': 'invalid_home_request'})
                except Exception:
                    return self.send(503, {'error': 'speech_unavailable'})
            if urlsplit(self.path).path != '/api/tasks':
                return self.send(404, {'message': '未找到。'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 1 <= length <= 4096:
                    return self.send(413, {'message': '问题过长。'})
                payload = validate_request(json.loads(self.rfile.read(length)))
            except (ValueError, UnicodeError, ServiceError) as error:
                return self.send(400, {'message': str(error) if isinstance(error, ServiceError) else '请求格式不正确。'})
            provider = train_client if payload['mode'] == 'train' else client
            if not provider.key:
                name = 'JUHE_TRAIN_KEY' if payload['mode'] == 'train' else 'DEEPSEEK_API_KEY'
                return self.send(503, {'message': f'请在本机私有配置中填写 {name}。'})
            if not capacity.acquire(blocking=False):
                return self.send(429, {'message': '已有两个任务在处理，请稍后再试。'})
            identity = secrets.token_urlsafe(18)
            now = time.time()
            owner = self.session()
            with lock:
                expired = [k for k, v in tasks.items() if now - v['created'] > 3600
                           and v['state'] not in ('running', 'queued')]
                for k in expired:
                    tasks.pop(k)
                if len(tasks) >= 100:
                    capacity.release()
                    return self.send(429, {'message': '本次预览任务已满，请重启预览清空记录。'})
                tasks[identity] = {'id': identity, 'state': 'queued', 'created': now,
                                   'progress': '准备查询', '_owner': owner}
            pool.submit(execute, identity, payload)
            return self.send(202, {'id': identity})

    server = ThreadingHTTPServer((bind_host, port), Handler)
    server.daemon_threads = True
    return server, pool


if __name__ == '__main__':
    load_private_config()
    server, pool = make_server()
    print('言小办商用本机验证：http://127.0.0.1:18767/ （Ctrl+C停止）', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pool.shutdown(wait=False, cancel_futures=True)
