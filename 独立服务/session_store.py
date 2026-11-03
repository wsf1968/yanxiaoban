"""Persist device authorization hashes without writing browser tokens to disk."""
import json
import math
import os
from pathlib import Path
import tempfile


def load_sessions(path, now):
    path = Path(path)
    try:
        entries = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(entries, dict):
        raise ValueError('登录记录格式不正确。')
    valid = {}
    for key, expiry in entries.items():
        if (isinstance(key, str) and len(key) == 64
                and all(c in '0123456789abcdef' for c in key)
                and type(expiry) in (int, float) and math.isfinite(expiry) and expiry > now):
            valid[key] = expiry
    return valid


def save_sessions(path, entries):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.sessions-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(entries, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
