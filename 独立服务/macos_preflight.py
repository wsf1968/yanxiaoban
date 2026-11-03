"""Read-only macOS readiness probe. No server, downloads, key output or system changes."""
import argparse
import importlib
import json
from pathlib import Path
import platform
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent


def inspect_environment():
    result = {'platform': platform.system(), 'macos_version': platform.mac_ver()[0],
              'architecture': platform.machine(), 'python': platform.python_version(),
              'supported_platform_probe': platform.system() == 'Darwin',
              'libraries': {}, 'private_config_present': (ROOT / '.env').is_file(),
              'speech_profile_present': (ROOT / '.private/speech-profile.json').is_file(),
              'local_speech_connection_verified': False, 'local_https_iphone_verified': False,
              'mac_iphone_acceptance': 'pending', 'read_only': True}
    for name in ('sherpa_onnx', 'numpy', 'soundfile', 'onnxruntime'):
        try:
            importlib.import_module(name)
            result['libraries'][name] = {'imported': True}
        except Exception as error:
            result['libraries'][name] = {'imported': False, 'error_type': type(error).__name__}
    node = shutil.which('node')
    result['node_present'] = bool(node)
    if node:
        try:
            result['node_version'] = subprocess.run([node, '--version'], capture_output=True,
                                                     text=True, timeout=3, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            result['node_version'] = None
    # A profile's presence is not evidence that ASR/TTS or the iPhone actually works.
    result['next_verification'] = ['Mac本机语音服务及模型资源', '同一Wi-Fi本地HTTPS及固定入口',
                                   'iPhone登录收音播报与连续对话', '系统提醒和家庭记账接入']
    return result


def main():
    parser = argparse.ArgumentParser(description='言小办Mac版环境只读检查')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    result = inspect_environment()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('言小办 Mac 版环境检查')
        print('系统：macOS ' + result['macos_version'] + '；架构：' + result['architecture'])
        print('语音运行库：' + ('已具备' if all(x['imported'] for x in result['libraries'].values()) else '尚未齐备'))
        print('仍需核验：Mac本机语音连接、手机安全入口、收音与播报。')
        print('本次只检查，没有启动服务、下载文件或修改系统与个人配置。')
    return 0 if result['supported_platform_probe'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
