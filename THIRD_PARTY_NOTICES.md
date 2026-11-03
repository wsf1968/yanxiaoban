# 第三方来源与资源边界

项目主代码采用GPL-3.0-only，详见LICENSE与NOTICE。本文件列明第三方来源；第三方原始条款、署名和其他要求照常保留。

| 组成 | 随包来源 |
|---|---|
| nanoMuse记忆实现 | `独立服务/shared_voice/vendor/nanomuse_memory/UPSTREAM.json` 与 `LICENSE`；原清单标注GPL-3.0-or-later，保留原始声明与移植记录 |
| Actual Budget及依赖 | `记账模块/package-lock.json`、`THIRD_PARTY_LICENSES/`，保留不同依赖各自许可 |
| jsonschema及依赖 | `秘书模块/THIRD_PARTY_LICENSES/` |
| Qwen3-TTS与MLX | `产品化/commercial-tts-source.json`、`产品化/许可来源/`，包含固定模型版本及原有许可文本 |
| SenseVoice/FunASR与Silero | `产品化/speech-asr-sources.json`、`产品化/许可来源/`，模型协议与代码许可证需分别处理 |
| Python与Node运行环境 | `产品化/runtime-sources.json`；构建时获取，本源码包不携带二进制 |

项目采用GPL-3.0-only，但不能把上述所有组成统一标为GPL、MIT或Apache。 项目主代码的许可证不替代第三方现有要求。大型模型与运行库不随源码包发布；下载或再分发时仍需遵守其各自条款。

本版图标已替换为项目自制几何矢量图，并随附生成源码；通用招呼音已核对为Qwen3预设音色合成、非声音克隆。来源和适用条款见 `ASSET_PROVENANCE.json` 与 `docs/ASSETS.md`。

聚合完整地点表无已确认的开放再分发授权，已从源码和默认构建中移除；仅保留可选本地下载、校验及转换代码。少量离线测试地点事实来自MIT许可的Timeon1/TMC_Dataset，原许可随测试保留。

本轮完成的是源码公开范围核对。未来公开安装包还需核对实际携带的全部依赖和模型，补齐相应声明、许可及完整对应源码。
