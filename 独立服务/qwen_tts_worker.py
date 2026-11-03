"""Private stdin/stdout worker for commercial Qwen TTS. Never an HTTP listener."""
import argparse
import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import time
import wave


def send(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + '\n')
    sys.stdout.flush()


MAX_TOKENS = 2048
MAX_SECONDS = 120


class TtsFailure(RuntimeError):
    pass


def generate_wav(model, text, instruction, mx, np, memory_gb):
    """Internal streaming, full-WAV public result; never return a partial utterance."""
    mx.random.seed(20261027)
    mx.reset_peak_memory()
    parts, rate, count, samples = [], None, 0, 0
    started = time.monotonic()
    first_chunk = None
    for result in model.generate_custom_voice(text=text, speaker='Vivian', language='Chinese',
            instruct=instruction, temperature=.7, max_tokens=MAX_TOKENS, verbose=False,
            stream=True, streaming_interval=1.0):
        if first_chunk is None:
            first_chunk = time.monotonic() - started
        a = np.asarray(result.audio, dtype=np.float32).reshape(-1)
        if rate is not None and rate != result.sample_rate:
            raise TtsFailure('qwen_invalid_audio')
        rate = result.sample_rate
        count += result.token_count
        samples += len(a)
        if count >= MAX_TOKENS:
            raise TtsFailure('qwen_token_limit')
        if not 8000 <= rate <= 48000 or samples > rate * MAX_SECONDS:
            raise TtsFailure('qwen_audio_duration')
        if mx.get_peak_memory() > memory_gb * 1e9:
            raise TtsFailure('qwen_resource_limit')
        parts.append(a)
    if not parts:
        raise TtsFailure('qwen_invalid_audio')
    audio = np.concatenate(parts)
    if not np.all(np.isfinite(audio)) or not len(audio):
        raise TtsFailure('qwen_invalid_audio')
    out = io.BytesIO()
    with wave.open(out, 'wb') as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(rate)
        target.writeframes((np.clip(audio, -1, 1) * 32767).astype('<i2').tobytes())
    return out.getvalue(), {'seconds': round(len(audio)/rate, 3),
            'generation_seconds': round(time.monotonic()-started, 3),
            'first_chunk_seconds': round(first_chunk, 3),
            'peak_memory_gb': round(mx.get_peak_memory()/1e9, 3),
            'speaker': 'Vivian', 'token_count': count, 'token_limit': MAX_TOKENS,
            'stream': True, 'streaming_interval': 1.0, 'seed': 20261027}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--memory-gb', type=float, default=6)
    args = parser.parse_args()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            import mlx.core as mx
            import numpy as np
            from mlx_audio.tts.utils import load_model
            mx.set_memory_limit(int(args.memory_gb * 1e9))
            mx.set_cache_limit(256_000_000)
            model = load_model(str(Path(args.model)))
        send({'state': 'ready', 'speaker': 'Vivian', 'engine': 'qwen3', 'temperature': .7})
    except Exception:
        send({'error': 'qwen_load_failed'})
        return 2
    for line in sys.stdin.buffer:
        if len(line) > 8192:
            send({'error': 'qwen_invalid_request'})
            return 2
        try:
            request = json.loads(line)
            text, instruction = request['text'], request['instruction']
            if not isinstance(text, str) or not 1 <= len(text) <= 350 or any(ord(c) < 32 for c in text):
                raise ValueError()
            if not isinstance(instruction, str) or not 1 <= len(instruction) <= 300:
                raise ValueError()
            with contextlib.redirect_stdout(sys.stderr):
                raw, metrics = generate_wav(model, text, instruction, mx, np, args.memory_gb)
            send({'id': request['id'], 'mime': 'audio/wav',
                  'audio': base64.b64encode(raw).decode('ascii'), 'metrics': metrics})
        except TtsFailure as error:
            send({'error': str(error)})
            return 2
        except Exception as error:
            resource = 'memory' in str(error).lower() or 'alloc' in str(error).lower()
            send({'error': 'qwen_resource_limit' if resource else 'qwen_generation_failed'})
            return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
