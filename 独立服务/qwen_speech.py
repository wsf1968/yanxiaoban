"""Commercial-only local Qwen TTS. One owned worker; no fallback or downloads.

The existing Bridge owns number/rail formatting. ASR and VAD still use the
accepted shared engine; neither imports MLX into the HTTP/model process.
"""
import atexit
import base64
import io
import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import uuid
import wave

B_INSTRUCTION = '用自然、亲切、清晰的普通话说话，像日常交谈一样，语速适中，按语义自然停顿。'
MAX_MESSAGE = 8_000_000


class QwenSpeechError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def provider_settings(private, *, default_backend='kokoro', environ=None):
    """Persisted commercial choice, with explicit environment overrides.

    No network/runtime settings or historical speech profile is changed.
    Missing configuration retains the entrypoint's existing backend for rollback.
    Malformed or incomplete Qwen configuration fails explicitly.
    """
    env = dict(os.environ if environ is None else environ)
    path = Path(private)/'tts-provider.json'
    config = {}
    if path.exists():
        try:
            if path.stat().st_size > 8192:
                raise ValueError()
            config = json.loads(path.read_text())
            if not isinstance(config, dict) or set(config)-{'backend','qwen_python','qwen_model'}:
                raise ValueError()
            if config.get('backend') not in ('qwen3','kokoro','matcha'):
                raise ValueError()
            if any(not isinstance(config[k], str) or not config[k] for k in config):
                raise ValueError()
        except (OSError, ValueError, UnicodeError):
            raise QwenSpeechError('invalid_tts_provider_config') from None
    backend = env.get('PAI_TTS_BACKEND', config.get('backend', default_backend))
    if backend not in ('qwen3','kokoro','matcha'):
        raise ValueError('unsupported_product_tts_backend')
    for field, name in [('qwen_python','PAI_QWEN_PYTHON'),('qwen_model','PAI_QWEN_MODEL')]:
        if name not in env and field in config:
            env[name] = config[field]
    env['PAI_TTS_BACKEND'] = backend
    if backend == 'qwen3' and (not env.get('PAI_QWEN_PYTHON') or not env.get('PAI_QWEN_MODEL')):
        raise QwenSpeechError('qwen_configuration_missing')
    return backend, env


def speech_text(body, content_type):
    if content_type != 'application/json':
        raise ValueError('requires_json')
    data = json.loads(body)
    if not isinstance(data, dict) or set(data) != {'text'}:
        raise ValueError('invalid_speech_request')
    text = data['text']
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 350:
        raise ValueError('invalid_speech_text')
    text = re.sub(r'[\r\n\t]+', ' ', text).strip()
    if not text or any(ord(c) < 32 for c in text):
        raise ValueError('invalid_speech_text')
    return text


class QwenWorker:
    """Bounded JSON-lines IPC. Busy requests fail immediately, never queue turns."""
    def __init__(self, python, model, *, timeout=90, startup_timeout=60,
                 instruction=B_INSTRUCTION, memory_gb=6, worker_script=None):
        # Preserve the venv interpreter symlink: resolve() would lose its site-packages.
        self.python, self.model = Path(python), Path(model)
        if not self.python.is_absolute() or not self.python.is_file() or not os.access(self.python, os.X_OK):
            raise QwenSpeechError('qwen_python_missing')
        if not self.model.is_absolute() or not all((self.model / n).is_file() for n in ('config.json', 'model.safetensors')):
            raise QwenSpeechError('qwen_model_missing')
        if not (self.model / 'speech_tokenizer').is_dir():
            raise QwenSpeechError('qwen_tokenizer_missing')
        for value, lo, hi in ((timeout, .01, 120), (startup_timeout, .01, 120), (memory_gb, 3, 16)):
            if type(value) not in (int, float) or not lo <= value <= hi:
                raise ValueError('invalid_qwen_limits')
        if not isinstance(instruction, str) or not 1 <= len(instruction) <= 300 or any(ord(c) < 32 for c in instruction):
            raise ValueError('invalid_qwen_instruction')
        self.timeout, self.startup_timeout = timeout, startup_timeout
        self.instruction, self.memory_gb = instruction, memory_gb
        self.script = Path(worker_script) if worker_script else Path(__file__).with_name('qwen_tts_worker.py')
        self.slot = threading.Lock()
        self.process = None
        self.events = None
        self.last_metrics = None
        atexit.register(self.close)

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        if not env.get('PAI_QWEN_PYTHON') or not env.get('PAI_QWEN_MODEL'):
            raise QwenSpeechError('qwen_configuration_missing')
        return cls(env['PAI_QWEN_PYTHON'], env['PAI_QWEN_MODEL'],
                   timeout=float(env.get('PAI_QWEN_TIMEOUT_SECONDS', '90')),
                   startup_timeout=float(env.get('PAI_QWEN_STARTUP_SECONDS', '60')),
                   memory_gb=float(env.get('PAI_QWEN_MEMORY_GB', '6')),
                   instruction=env.get('PAI_QWEN_INSTRUCTION', B_INSTRUCTION))

    def _read(self, process, events):
        try:
            while True:
                line = process.stdout.readline(MAX_MESSAGE + 1)
                if not line:
                    events.put({'error': 'qwen_worker_exited'})
                    return
                if len(line) > MAX_MESSAGE or not line.endswith(b'\n'):
                    events.put({'error': 'qwen_invalid_response'})
                    return
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    value = {'error': 'qwen_invalid_response'}
                events.put(value)
        except (OSError, ValueError):
            events.put({'error': 'qwen_worker_exited'})

    def _receive(self, timeout):
        try:
            value = self.events.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise QwenSpeechError('qwen_timeout') from None
        if not isinstance(value, dict) or 'error' in value:
            code = value.get('error') if isinstance(value, dict) else None
            allowed = {'qwen_worker_exited', 'qwen_invalid_response', 'qwen_load_failed',
                       'qwen_generation_failed', 'qwen_resource_limit', 'qwen_invalid_audio',
                       'qwen_token_limit', 'qwen_audio_duration'}
            self.close()
            raise QwenSpeechError(code if code in allowed else 'qwen_failed')
        return value

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                   TOKENIZERS_PARALLELISM='false', PYTHONDONTWRITEBYTECODE='1')
        self.events = queue.Queue()
        try:
            self.process = subprocess.Popen(
                [str(self.python), '-u', '-B', str(self.script), '--model', str(self.model),
                 '--memory-gb', str(self.memory_gb)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        except OSError:
            raise QwenSpeechError('qwen_worker_start_failed') from None
        threading.Thread(target=self._read, args=(self.process, self.events), daemon=True).start()
        ready = self._receive(self.startup_timeout)
        if ready.get('state') != 'ready' or ready.get('speaker') != 'Vivian':
            self.close()
            raise QwenSpeechError('qwen_invalid_response')

    def warmup(self):
        if not self.slot.acquire(blocking=False):
            raise QwenSpeechError('qwen_busy')
        try:
            self._start()
        finally:
            self.slot.release()

    def synthesize(self, text):
        if not self.slot.acquire(blocking=False):
            raise QwenSpeechError('qwen_busy')
        try:
            self._start()
            request_id = uuid.uuid4().hex
            request = {'id': request_id, 'text': text, 'instruction': self.instruction}
            try:
                self.process.stdin.write((json.dumps(request, ensure_ascii=False) + '\n').encode())
                self.process.stdin.flush()
            except (OSError, ValueError):
                self.close()
                raise QwenSpeechError('qwen_worker_exited') from None
            value = self._receive(self.timeout)
            if value.get('id') != request_id or value.get('mime') != 'audio/wav':
                self.close()
                raise QwenSpeechError('qwen_invalid_response')
            try:
                raw = base64.b64decode(value['audio'], validate=True)
                with wave.open(io.BytesIO(raw)) as audio:
                    rate, frames = audio.getframerate(), audio.getnframes()
                    if (audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (1, 2, 'NONE') or not 8000 <= rate <= 48000 or not 1 <= frames <= rate * 120:
                        raise ValueError()
                    if len(audio.readframes(frames)) != frames * 2:
                        raise ValueError()
            except (KeyError, ValueError, TypeError, wave.Error, EOFError):
                self.close()
                raise QwenSpeechError('qwen_invalid_audio') from None
            self.last_metrics = value.get('metrics')
            return {'audio': value['audio'], 'mime': 'audio/wav'}
        finally:
            self.slot.release()

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for pipe in (process.stdin, process.stdout):
            if pipe:
                pipe.close()


class QwenSpeech:
    """Same call interface as ProfileSpeech; ASR bypasses the TTS worker."""
    def __init__(self, shared_call, worker):
        self.shared, self.worker = shared_call, worker

    def warmup(self):
        self.worker.warmup()

    def __call__(self, path, body, content_type, token):
        if path != 'synthesize':
            return self.shared(path, body, content_type, token)
        text = speech_text(body, content_type)
        try:
            return self.worker.synthesize(text)
        except QwenSpeechError as error:
            logging.getLogger(__name__).error('commercial_qwen_tts_failed code=%s', error.code)
            raise

    def close(self):
        self.worker.close()


def load_qwen_speech(models, private, *, environ=None):
    """Opt-in hook for both commercial entrypoints, retaining the same ASR/VAD.

    Uses either existing sherpa version for ASR only; never constructs its
    Matcha/Kokoro TTS. MLX is confined to the separate configured interpreter.
    """
    _, settings = provider_settings(private, environ=environ)
    worker = QwenWorker.from_env(settings)
    from commercial_voice import CommercialVoice  # Existing shared import setup.
    from speech_engine import SherpaBackend, SpeechEngine, RATE
    import numpy as np
    import sherpa_onnx as so

    class AsrOnly(SherpaBackend):
        def __init__(self):
            self.np, self.so, self.root = np, so, Path(models)
            root = self.root / 'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
            self.asr = so.OfflineRecognizer.from_sense_voice(
                model=str(root/'model.int8.onnx'), tokens=str(root/'tokens.txt'),
                num_threads=2, language='zh', use_itn=False)
            self.vad_config = so.VadModelConfig()
            self.vad_config.silero_vad.model = str(self.root/'silero_vad_v5.onnx')
            self.vad_config.silero_vad.threshold = .5
            self.vad_config.silero_vad.min_silence_duration = .8
            self.vad_config.silero_vad.min_speech_duration = .25
            self.vad_config.sample_rate = RATE
            self.vad_config.num_threads = 1
            self.vad = so.VoiceActivityDetector(self.vad_config, buffer_size_in_seconds=40)

    engine = SpeechEngine(AsrOnly())
    def shared(path, body, kind, token):
        if path != 'transcribe' or kind != 'audio/wav':
            raise ValueError('Unsupported local speech request')
        return engine.transcribe(body)
    speech = QwenSpeech(shared, worker)
    speech.backend_name = 'qwen3'
    speech.warmup()
    token = Path(private)/'local-speech-token'
    if not token.exists():
        import secrets
        from local_https import private_write
        private_write(token, secrets.token_urlsafe(32).encode())
    return speech, token
