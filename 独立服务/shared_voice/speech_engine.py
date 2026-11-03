"""Isolated audio diagnostic. No business tools, GLM, or persistent audio storage."""
import base64
import io
import math
import struct
import threading
import time
import wave
from pathlib import Path

RATE = 16000
MAX_SECONDS = 30
MAX_BODY = RATE * MAX_SECONDS * 2 + 4096


class AudioError(ValueError):
    pass


def read_pcm(raw):
    if not isinstance(raw, bytes) or not 44 <= len(raw) <= MAX_BODY:
        raise AudioError('invalid_audio_size')
    try:
        with wave.open(io.BytesIO(raw), 'rb') as source:
            frames = source.getnframes()
            if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, RATE, 'NONE'):
                raise AudioError('requires_mono_pcm16_16000')
            if not RATE // 10 <= frames <= RATE * MAX_SECONDS:
                raise AudioError('invalid_audio_duration')
            pcm = source.readframes(frames)
            if len(pcm) != frames * 2:
                raise AudioError('truncated_audio')
            return pcm, frames / RATE
    except (wave.Error, EOFError, struct.error) as exc:
        raise AudioError('invalid_wav') from exc


def write_pcm(samples, rate):
    if not 8000 <= rate <= 48000:
        raise AudioError('invalid_output_rate')
    values = []
    for sample in samples:
        if not math.isfinite(float(sample)):
            raise AudioError('invalid_output_samples')
        values.append(max(-32768, min(32767, int(float(sample) * 32767))))
    if not 1 <= len(values) <= rate * 60:
        raise AudioError('invalid_output_duration')
    out = io.BytesIO()
    with wave.open(out, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(struct.pack('<' + 'h' * len(values), *values))
    return out.getvalue()


class SpeechEngine:
    def __init__(self, backend):
        self.backend = backend
        self.slot = threading.Lock()

    def transcribe(self, raw):
        pcm, seconds = read_pcm(raw)
        if not self.slot.acquire(blocking=False):
            raise AudioError('speech_busy')
        try:
            text = self.backend.transcribe(pcm).strip()
            return {'state': 'transcribed' if text else 'no_speech', 'text': text,
                    'taskExecuted': False, 'inputSeconds': seconds}
        finally:
            self.slot.release()

    def synthesize(self, text):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 350 or any(ord(c) < 32 for c in text):
            raise AudioError('invalid_text')
        if not self.slot.acquire(blocking=False):
            raise AudioError('speech_busy')
        try:
            samples, rate = self.backend.synthesize(text.strip())
            return {'audio': base64.b64encode(write_pcm(samples, rate)).decode('ascii'), 'mime': 'audio/wav'}
        finally:
            self.slot.release()

    def roundtrip(self, raw):
        pcm, seconds = read_pcm(raw)
        if not self.slot.acquire(blocking=False):
            raise AudioError('speech_busy')
        started = time.monotonic()
        try:
            text = self.backend.transcribe(pcm).strip()
            if not text:
                return {'state': 'no_speech', 'text': '', 'audio': None,
                        'message': '没有识别到清楚的人声，请再说一次。', 'taskExecuted': False}
            # This is an audible transcript check, never a business completion notice.
            spoken = '识别结果是：' + text[:120]
            if len(text) > 120:
                spoken += '。较长内容请查看文字。'
            samples, rate = self.backend.synthesize(spoken)
            encoded = base64.b64encode(write_pcm(samples, rate)).decode('ascii')
            return {'state': 'ready', 'text': text, 'spoken': spoken,
                    'audio': encoded, 'mime': 'audio/wav', 'taskExecuted': False,
                    'inputSeconds': seconds, 'computeSeconds': time.monotonic() - started}
        finally:
            self.slot.release()


class SherpaBackend:
    def __init__(self, model_root):
        import numpy as np
        import sherpa_onnx as so
        self.np, self.so = np, so
        root = Path(model_root)
        self.root = root
        asr = root / 'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
        self.asr = so.OfflineRecognizer.from_sense_voice(
            model=str(asr / 'model.int8.onnx'), tokens=str(asr / 'tokens.txt'),
            num_threads=2, language='zh', use_itn=False)
        tts = root / 'matcha-icefall-zh-baker'
        self.tts = so.OfflineTts(so.OfflineTtsConfig(model=so.OfflineTtsModelConfig(
            matcha=so.OfflineTtsMatchaModelConfig(
                acoustic_model=str(tts / 'model-steps-3.onnx'),
                vocoder=str(root / 'vocos-22khz-univ.onnx'),
                lexicon=str(tts / 'lexicon.txt'), tokens=str(tts / 'tokens.txt')),
            num_threads=2, provider='cpu'),
            rule_fsts=','.join(str(tts / f) for f in ('phone.fst', 'date.fst', 'number.fst')),
            max_num_sentences=1))
        self.vad_config = so.VadModelConfig()
        self.vad_config.silero_vad.model = str(root / 'silero_vad_v5.onnx')
        self.vad_config.silero_vad.threshold = .5
        self.vad_config.silero_vad.min_silence_duration = .8
        self.vad_config.silero_vad.min_speech_duration = .25
        self.vad_config.sample_rate = RATE
        self.vad_config.num_threads = 1
        self.vad = so.VoiceActivityDetector(self.vad_config, buffer_size_in_seconds=40)

    def transcribe(self, pcm):
        np = self.np
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        self.vad.reset()
        padded = np.concatenate([np.zeros(3200, dtype=np.float32), samples,
                                 np.zeros(RATE, dtype=np.float32)])
        for start in range(0, len(padded), 512):
            block = padded[start:start + 512]
            self.vad.accept_waveform(np.pad(block, (0, 512 - len(block))))
        self.vad.flush()
        if self.vad.empty():
            return ''
        # VAD gates the request; preserve full waveform so negations aren't trimmed.
        stream = self.asr.create_stream()
        stream.accept_waveform(RATE, samples)
        self.asr.decode_stream(stream)
        return stream.result.text

    def synthesize(self, text):
        audio = self.tts.generate(text, self.so.GenerationConfig())
        return audio.samples, audio.sample_rate
