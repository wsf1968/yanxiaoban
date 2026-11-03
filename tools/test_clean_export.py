from pathlib import Path
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
import audit_clean

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'独立服务/shared_voice'))
spec=importlib.util.spec_from_file_location('clean_prepare_assets',ROOT/'产品化/prepare_assets.py')
prepare=importlib.util.module_from_spec(spec);spec.loader.exec_module(prepare)


class CleanExportTests(unittest.TestCase):
    def test_source_passes_audit(self):
        result=audit_clean.inspect(ROOT)
        self.assertEqual(result['findings'],[])

    def test_audit_detects_private_file_and_key_without_echoing_value(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'.env';secret='sk-'+'x'*32;p.write_text('KEY='+secret)
            result=audit_clean.inspect(folder)
            self.assertFalse(result['passed'])
            self.assertNotIn(secret,json.dumps(result))
            self.assertTrue(any(f['rule']=='api_key_pattern' for f in result['findings']))

    def test_unused_personal_transports_cannot_connect(self):
        import shopping_client,wechat_client
        with patch('urllib.request.urlopen',side_effect=AssertionError('No personal service may be contacted')):
            for call in [shopping_client.local_call,wechat_client.local_call]:
                with self.assertRaisesRegex(ValueError,'legacy_personal_bridge'):
                    call('fixture-device',{})

    def test_default_voice_cannot_connect_to_an_existing_speech_service(self):
        sys.path.insert(0,str(ROOT/'独立服务'))
        from commercial_voice import CommercialVoice
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder);token=p/'token';token.write_text('synthetic-voice-token')
            voice=CommercialVoice(p/'history.db',token)
            with patch('urllib.request.urlopen',side_effect=AssertionError('No fallback network')):
                with self.assertRaisesRegex(RuntimeError,'local_speech_provider_required'):
                    voice.bridge.call('synthesize',b'{}','application/json','fixture')

    def test_asset_path_traversal_rejected(self):
        for name in ['../secret','/absolute/file','a/../../file']:
            with self.assertRaises(ValueError):prepare.safe_relative(name)

    def test_cached_asset_requires_correct_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'asset';p.write_bytes(b'known public sample')
            expected=hashlib.sha256(p.read_bytes()).hexdigest()
            with patch('urllib.request.urlopen',side_effect=AssertionError('No network needed')):
                prepare.download('https://example.invalid/asset',p,expected)
                p.write_bytes(b'altered')
                with self.assertRaises(AssertionError):
                    prepare.download('https://example.invalid/asset',p,expected)

    def test_personal_entry_migration_removed_and_install_home_is_separate(self):
        entry=(ROOT/'独立服务/macos_local.py').read_text()
        self.assertNotIn('existing-entry.json',entry)
        self.assertNotIn('existing-commercial',entry)
        launcher=(ROOT/'产品化/Launcher.swift').read_text()
        self.assertIn('Application Support/言小办开源预览',launcher)
        self.assertNotIn('Application Support/小派助手"',launcher)

    def test_tts_manifest_has_only_public_files(self):
        manifest=prepare.model_entries()
        self.assertTrue(manifest['files'])
        self.assertFalse(any(e['path'].startswith('.cache/') for e in manifest['files']))
        for entry in manifest['files']:
            prepare.safe_relative(entry['path'])
            self.assertRegex(entry['sha256'],r'^[0-9a-f]{64}$')



class BuildLayoutTests(unittest.TestCase):
    def test_packaging_layout_with_fake_runtimes_never_copies_private_config(self):
        import shutil
        import importlib.util
        spec=importlib.util.spec_from_file_location('preview_build',ROOT/'产品化/build_product.py')
        build=importlib.util.module_from_spec(spec);spec.loader.exec_module(build)
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source';shutil.copytree(ROOT,source)
            cache=source/'.build';cache.mkdir()
            (cache/'prepared.json').write_text('{}')
            (cache/'flight-data').mkdir()
            shutil.copy2(source/'独立服务/tests/flight-locations.fixture.json',cache/'flight-data/flight-locations.json')
            data_manifest = json.loads((source/'产品化/flight-data-sources.json').read_text())
            data_manifest['converted_sha256'] = hashlib.sha256((cache/'flight-data/flight-locations.json').read_bytes()).hexdigest()
            (source/'产品化/flight-data-sources.json').write_text(json.dumps(data_manifest))
            python=cache/'python-runtime-qwen-0.4.1/python/bin/python3'
            python.parent.mkdir(parents=True);python.write_text('#!/bin/sh\nexit 0\n')
            node=cache/'node-runtime/node-v22.23.1-darwin-arm64/bin/node'
            node.parent.mkdir(parents=True);node.write_text('#!/bin/sh\nexit 0\n')
            models=cache/'speech/models';models.mkdir(parents=True)
            (models/'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17').mkdir()
            (models/'silero_vad_v5.onnx').write_bytes(b'synthetic test asset')
            tts=models/'Qwen3-TTS-1.7B-CustomVoice-8bit';tts.mkdir()
            (tts/'config.json').write_text('{}')
            manifest={'files':[{'path':'config.json','sha256':hashlib.sha256(b'{}').hexdigest()}]}
            (source/'产品化/commercial-tts-source.json').write_text(json.dumps(manifest))
            (source/'独立服务/.env').write_text('DO_NOT_COPY=fixture-private-marker')
            (source/'独立服务/data').mkdir(exist_ok=True)
            (source/'独立服务/data/device-sessions.json').write_text('{"private":"fixture"}')
            def fake_run(args,**kwargs):
                if args[0]=='xcrun':
                    destination=Path(args[args.index('-o')+1]);destination.write_bytes(b'fake executable')
            with patch.object(build,'ROOT',source),patch.object(build,'BUILD',cache),patch.object(build.subprocess,'run',fake_run):
                build.build('0.4.1-preview.1',Path(folder)/'output',include_flight_data=True)
            payload=Path(folder)/'output/言小办安装助手.app/Contents/Resources/payload'
            release=json.loads((payload/'release.json').read_text())
            paths={entry['path'] for entry in release['files']}
            self.assertIn('独立服务/data/flight-locations.json',paths)
            self.assertIn('独立服务/jd_channels.json',paths)
            self.assertNotIn('独立服务/.env',paths)
            self.assertNotIn('独立服务/data/device-sessions.json',paths)
            self.assertFalse(any('release-signing-key' in path for path in paths))
            self.assertFalse(release['phone_tested'])
            self.assertTrue((payload/'使用说明/先读我.txt').is_file())
            self.assertTrue((payload/'assets/greeting-source.json').is_file())
            with patch.object(build,'ROOT',source),patch.object(build,'BUILD',cache),patch.object(build.subprocess,'run',fake_run):
                build.build('0.4.1-preview.1',Path(folder)/'default-output')
            default_payload=Path(folder)/'default-output/言小办安装助手.app/Contents/Resources/payload'
            self.assertFalse((default_payload/'独立服务/data/flight-locations.json').exists())
            self.assertFalse(json.loads((default_payload/'release.json').read_text())['optional_juhe_reference_data_included'])

if __name__=='__main__':unittest.main()
