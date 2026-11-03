"""Prepare pinned public build inputs inside this checkout; never read live user data.

No model/API credentials are needed. --check performs no downloads or installation.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.build'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def download(url, path, expected, size=None):
    if urllib.parse.urlsplit(url).scheme != 'https' or len(expected) != 64:
        raise ValueError('HTTPS and a pinned SHA-256 are required')
    path = Path(path)
    if path.is_file() and digest(path) == expected and (size is None or path.stat().st_size == size):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.part')
    print('Downloading '+path.name, flush=True)
    request = urllib.request.Request(url, headers={'User-Agent': 'yanxiaoban-source-build'})
    with urllib.request.urlopen(request, timeout=60) as source, temporary.open('wb') as target:
        shutil.copyfileobj(source, target, 1024 * 1024)
    if digest(temporary) != expected or (size is not None and temporary.stat().st_size != size):
        raise ValueError('Downloaded file does not match pinned metadata: ' + path.name)
    os.replace(temporary, path)


def safe_relative(name):
    p = Path(name)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        raise ValueError('Unsafe relative asset path')
    return p


def unpack(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        source.extractall(destination, filter='data')


def model_entries():
    return json.loads((ROOT/'产品化/commercial-tts-source.json').read_text())


def check():
    missing = []
    required = [CACHE/'python-runtime-qwen-0.4.1/python/bin/python3',
                CACHE/'node-runtime/node-v22.23.1-darwin-arm64/bin/node',
                CACHE/'speech/models/silero_vad_v5.onnx',
                CACHE/'speech/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/model.int8.onnx',
                ROOT/'记账模块/node_modules/@actual-app/api/package.json', CACHE/'prepared.json']
    missing += [str(p.relative_to(ROOT)) for p in required if not p.is_file()]
    tts = model_entries()
    for entry in tts['files']:
        p = CACHE/'speech/models'/tts['name']/safe_relative(entry['path'])
        if not p.is_file() or digest(p) != entry['sha256']:
            missing.append(str(p.relative_to(ROOT)))
    print(json.dumps({'build_inputs_ready': not missing, 'missing_or_mismatched':missing,
                      'optional_juhe_flight_data_ready': (CACHE/'flight-data/flight-locations.json').is_file(),
                      'downloads_performed':False}, ensure_ascii=False, indent=2))
    return not missing


def prepare(with_juhe_flight_data=False):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('This preview build targets Apple Silicon macOS only')
    if int(platform.mac_ver()[0].split('.')[0]) < 26:
        raise RuntimeError('The pinned MLX runtime requires macOS 26 or newer')
    CACHE.mkdir(parents=True, exist_ok=True)
    if with_juhe_flight_data:
        from flight_data import prepare as prepare_flight_data
        prepare_flight_data(ROOT, CACHE, download)
    for entry in json.loads((ROOT/'产品化/runtime-sources.json').read_text()):
        archive = CACHE/'downloads'/safe_relative(entry['name'])
        download(entry['url'], archive, entry['sha256'], entry.get('size'))
        destination = CACHE/('python-runtime-qwen-0.4.1' if entry['name'].startswith('cpython') else 'node-runtime')
        if not destination.exists():
            unpack(archive, destination)
    python = CACHE/'python-runtime-qwen-0.4.1/python/bin/python3'
    node_root = CACHE/'node-runtime/node-v22.23.1-darwin-arm64'
    env = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')}
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
               PATH=str(node_root/'bin')+os.pathsep+os.environ.get('PATH',''))
    subprocess.run([str(python), '-I', '-m', 'pip', '--isolated', 'install',
                    '--disable-pip-version-check', '--no-cache-dir', '--only-binary=:all:',
                    '--index-url', 'https://pypi.org/simple', '-r',
                    str(ROOT/'产品化/runtime-requirements.lock')], env=env, check=True)
    npm_config=CACHE/'empty.npmrc';npm_config.write_text('')
    subprocess.run([str(node_root/'bin/node'), str(node_root/'lib/node_modules/npm/bin/npm-cli.js'),
                    'ci', '--prefix', str(ROOT/'记账模块'), '--no-audit', '--no-fund',
                    '--cache', str(CACHE/'npm-cache'), '--userconfig', str(npm_config),
                    '--globalconfig', str(CACHE/'global-empty.npmrc'), '--registry=https://registry.npmjs.org'],
                   env=env, check=True)
    models = CACHE/'speech/models'
    for entry in json.loads((ROOT/'产品化/speech-asr-sources.json').read_text()):
        archive = CACHE/'downloads'/safe_relative(entry['name'])
        expected = entry.get('digest') or 'sha256:'+entry['downloaded_sha256']
        download(entry['browser_download_url'], archive, expected.removeprefix('sha256:'), entry['size'])
        if entry['name'].endswith('.tar.bz2'):
            unpack(archive, models)
        else:
            models.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, models/entry['name'])
    tts = model_entries()
    base = 'https://huggingface.co/'+tts['repo']+'/resolve/'+tts['revision']+'/'
    for entry in tts['files']:
        relative = safe_relative(entry['path'])
        download(base+urllib.parse.quote(relative.as_posix(), safe='/'), models/tts['name']/relative,
                 entry['sha256'], entry['bytes'])
    subprocess.run([str(python), '-I', '-B', '-c',
                    'import cryptography,jsonschema,sherpa_onnx,numpy,soundfile,mlx.core,mlx_audio'],
                   env=env, check=True)
    (CACHE/'prepared.json').write_text(json.dumps({'schema':1,'tts_revision':tts['revision'],
        'requirements_sha256':digest(ROOT/'产品化/runtime-requirements.lock'),
        'public_sources_only':True,'isolated_download_cache':True}, indent=2)+'\n')
    if not check():
        raise RuntimeError('Build inputs are incomplete')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--with-juhe-flight-data', action='store_true',
                        help='Download provider reference sheets for local use; no open redistribution grant')
    parser.add_argument('--flight-data-only', action='store_true',
                        help='Only download/verify the optional Juhe reference sheets; no paid API call')
    args = parser.parse_args()
    if args.check:
        raise SystemExit(0 if check() else 1)
    if args.flight_data_only:
        from flight_data import prepare as prepare_flight_data
        prepare_flight_data(ROOT, CACHE, download)
    else:
        prepare(args.with_juhe_flight_data)
