"""Packaged voice: relocatable Qwen B default, explicit user settings preserved.

The existing development/self-use speech configuration is intentionally untouched.
ASR/VAD and waveform handling reuse the accepted implementation.
"""
from pathlib import Path
import json,secrets,os
from private_json import write_private

def load_speech(models,private):
    from qwen_speech import provider_settings
    # Fresh 0.4.1 releases carry B and their own interpreter. Keep explicit user
    # settings authoritative; old packages without these assets retain rollback.
    candidate=Path(models)/'Qwen3-TTS-1.7B-CustomVoice-8bit'
    environment=dict(os.environ)
    explicit=(Path(private)/'tts-provider.json').exists() or any(
        name in environment for name in ('PAI_TTS_BACKEND','PAI_QWEN_PYTHON','PAI_QWEN_MODEL'))
    if candidate.is_dir() and not explicit:
        release=Path(models).resolve().parents[1]
        environment.update(PAI_TTS_BACKEND='qwen3',
            PAI_QWEN_PYTHON=str(release/'runtime/python/bin/python3'),
            PAI_QWEN_MODEL=str(candidate.resolve()))
    backend_name,settings=provider_settings(private,default_backend='kokoro',environ=environment)
    if backend_name not in ('kokoro','qwen3'):raise ValueError('unsupported_product_tts_backend')
    if backend_name=='qwen3':
        from qwen_speech import load_qwen_speech
        return load_qwen_speech(models,private,environ=settings)
    from commercial_voice import CommercialVoice # Resolve shared module imports, no runtime creation.
    from speech_engine import SherpaBackend,SpeechEngine,RATE
    from speech_profile import ProfileSpeech
    import numpy as np
    import sherpa_onnx as so
    if so.__version__.split('.')[0] != '2':raise ValueError('Product voice requires the GPL-free 2.x engine')

    class ProductBackend(SherpaBackend):
        def __init__(self):
            self.np,self.so,self.root=np,so,Path(models)
            asr=self.root/'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
            self.asr=so.OfflineRecognizer.from_sense_voice(model=str(asr/'model.int8.onnx'),tokens=str(asr/'tokens.txt'),num_threads=2,language='zh',use_itn=False)
            root=self.root/'kokoro-int8-multi-lang-v1_1'
            config=so.OfflineTtsConfig(model=so.OfflineTtsModelConfig(kokoro=so.OfflineTtsKokoroModelConfig(
                model=str(root/'model.int8.onnx'),voices=str(root/'voices.bin'),tokens=str(root/'tokens.txt'),
                lexicon=str(root/'lexicon-zh.txt')+','+str(root/'lexicon-us-en.txt'),dict_dir=str(root/'dict')),
                num_threads=2,provider='cpu'),max_num_sentences=1,
                rule_fsts=','.join(str(root/name) for name in ('phone-zh.fst','date-zh.fst','number-zh.fst')))
            # No espeak-ng data or external phonemizer. 2.x uses its built-in lexicon frontend.
            self.tts=so.OfflineTts(config)
            self.vad_config=so.VadModelConfig();self.vad_config.silero_vad.model=str(self.root/'silero_vad_v5.onnx')
            self.vad_config.silero_vad.threshold=.5;self.vad_config.silero_vad.min_silence_duration=.8;self.vad_config.silero_vad.min_speech_duration=.25
            self.vad_config.sample_rate=RATE;self.vad_config.num_threads=1
            self.vad=so.VoiceActivityDetector(self.vad_config,buffer_size_in_seconds=40)
        def synthesize(self,text):
            config=so.GenerationConfig();config.sid=3
            audio=self.tts.generate(text,config);return audio.samples,audio.sample_rate

    backend=ProductBackend();engine=SpeechEngine(backend)
    def shared(path,body,kind,token):
        if path!='transcribe' or kind!='audio/wav':raise ValueError('Unsupported speech request')
        return engine.transcribe(body)
    profile_path=Path(private)/'speech-profile.json'
    profile=json.loads(profile_path.read_text()) if profile_path.exists() else {'model_root':str(Path(models).resolve()),'speed':.96,'silence_scale':.30}
    profile['model_root']=str(Path(models).resolve());write_private(profile_path,profile)
    def generation():
        value=so.GenerationConfig();value.sid=3;return value
    speech=ProfileSpeech(profile,shared,factory=lambda:(backend.tts,generation))
    speech.warmup()
    speech.backend_name=backend_name
    token=Path(private)/'local-speech-token'
    if not token.exists():
        from local_https import private_write
        private_write(token,secrets.token_urlsafe(32).encode())
    return speech,token
