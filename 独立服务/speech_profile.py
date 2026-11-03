"""Commercial-only TTS parameters using existing local model assets.

ASR remains on the shared engine. No model downloads, playback resampling,
text rewriting, cloud TTS or changes to self-use voice settings.
"""
import base64
import json
import re
import threading
from pathlib import Path


class ProfileSpeech:
    def __init__(self, profile, shared_call, *, factory=None):
        if not isinstance(profile, dict) or set(profile) != {'model_root', 'speed', 'silence_scale'}:
            raise ValueError('invalid_speech_profile')
        self.root = Path(profile['model_root'])
        if not self.root.is_absolute():
            raise ValueError('requires_absolute_model_root')
        for name, minimum, maximum in [('speed', .85, 1.15), ('silence_scale', .1, .5)]:
            value = profile[name]
            if type(value) not in (int, float) or not minimum <= value <= maximum:
                raise ValueError('invalid_speech_profile')
        self.speed, self.silence = profile['speed'], profile['silence_scale']
        self.shared = shared_call
        self.factory = factory or self.load_tts
        self.tts = None
        self.lock = threading.Lock()

    def load_tts(self):
        import sherpa_onnx as so
        root = self.root / 'matcha-icefall-zh-baker'
        required = [root / n for n in ['model-steps-3.onnx', 'lexicon.txt', 'tokens.txt',
                                       'phone.fst', 'date.fst', 'number.fst']]
        required.append(self.root / 'vocos-22khz-univ.onnx')
        if not all(p.is_file() for p in required):
            raise ValueError('speech_model_assets_missing')
        # Same acoustic model, vocoder, pronunciation rules and CPU budget as
        # the mature local engine, loading TTS only (not another ASR model).
        engine = so.OfflineTts(so.OfflineTtsConfig(
            model=so.OfflineTtsModelConfig(matcha=so.OfflineTtsMatchaModelConfig(
                acoustic_model=str(root / 'model-steps-3.onnx'),
                vocoder=str(self.root / 'vocos-22khz-univ.onnx'),
                lexicon=str(root / 'lexicon.txt'), tokens=str(root / 'tokens.txt')),
                num_threads=2, provider='cpu'),
            rule_fsts=','.join(str(root / n) for n in ['phone.fst', 'date.fst', 'number.fst']),
            max_num_sentences=1))
        return engine, so.GenerationConfig

    def warmup(self):
        with self.lock:
            if self.tts is None:
                self.tts, self.generation_config = self.factory()

    def __call__(self, path, body, content_type, token):
        if path != 'synthesize':
            return self.shared(path, body, content_type, token)
        if content_type != 'application/json':
            raise ValueError('requires_json')
        data = json.loads(body)
        if not isinstance(data, dict) or set(data) != {'text'}:
            raise ValueError('invalid_speech_request')
        text = data['text']
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 350:
            raise ValueError('invalid_speech_text')
        # DS can use paragraph breaks in otherwise valid spoken text. The
        # local engine expects a single line: adapt layout whitespace only,
        # leaving the model's words, order, numbers and punctuation intact.
        # Other control characters remain invalid; never truncate content.
        text = re.sub(r'[\r\n\t]+', ' ', text).strip()
        if not text or any(ord(c) < 32 for c in text):
            raise ValueError('invalid_speech_text')
        self.warmup()
        with self.lock:
            config = self.generation_config()
            config.speed = self.speed
            config.silence_scale = self.silence
            audio = self.tts.generate(text.strip(), config)
            from speech_engine import write_pcm
            raw = write_pcm(audio.samples, audio.sample_rate)
            return {'audio': base64.b64encode(raw).decode('ascii'), 'mime': 'audio/wav'}


def configured_speech(profile_path, shared_call):
    path = Path(profile_path)
    if not path.is_file():
        return shared_call
    speech = ProfileSpeech(json.loads(path.read_text()), shared_call)
    # Fail at startup rather than serving a partially configured voice profile.
    speech.warmup()
    return speech
