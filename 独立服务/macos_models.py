"""Prepare the existing speech backend's public model assets in the Mac private area."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def prepare(manifest, private=ROOT/'.private/macos'):
    os.umask(0o077)
    private = Path(private)
    cache, models = private/'downloads', private/'models'
    for path in (private, cache, models):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries = json.loads(Path(manifest).read_text())
    def obtain(entry):
        name = entry['name']
        if Path(name).name != name:
            raise ValueError('Invalid model filename')
        url = entry['browser_download_url']
        if not url.startswith('https://github.com/k2-fsa/sherpa-onnx/releases/download/'):
            raise ValueError('Unapproved model source')
        destination = cache/name
        expected = entry.get('digest')
        if destination.is_file() and destination.stat().st_size == entry['size']:
            digest = file_hash(destination)
            if expected and expected != 'sha256:'+digest:
                raise ValueError('Existing model archive checksum mismatch')
        else:
            temporary = destination.with_name(name+'.part')
            with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'xiaopai-macos-development'}),timeout=45) as source, temporary.open('wb') as target:
                shutil.copyfileobj(source,target,1024*1024)
            if temporary.stat().st_size != entry['size']:
                raise ValueError('Model download size mismatch: '+name)
            digest = file_hash(temporary)
            if expected and expected != 'sha256:'+digest:
                raise ValueError('Model download checksum mismatch: '+name)
            os.replace(temporary,destination)
        if name.endswith('.tar.bz2'):
            with tarfile.open(destination) as archive:
                for member in archive.getmembers():
                    if not (member.isfile() or member.isdir()) or Path(member.name).is_absolute() or '..' in Path(member.name).parts:
                        raise ValueError('Unsafe model archive member')
                archive.extractall(models,filter='data')
        else:
            shutil.copyfile(destination,models/name)
        print(json.dumps({'model_asset':name,'ready':True,'bytes':entry['size']}),flush=True)
        return {**entry,'downloaded_sha256':digest,'official_digest_available':bool(expected)}
    results=[]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures=[executor.submit(obtain,entry) for entry in entries]
        for future in as_completed(futures):results.append(future.result())
    (private/'model-sources.json').write_text(json.dumps(results,indent=2)+'\n')
    return models


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('manifest');args=parser.parse_args()
    print('Speech assets ready: '+str(prepare(args.manifest)))
