# 从纯净源码构建

本说明供开发者使用。一键安装是最终用户交付目标，源码构建步骤不算该目标已验收。

## 构建条件

- Apple Silicon Mac、macOS 26或更新版本。
- Python 3.13或更新版本用于准备脚本（使用标准库的安全解包能力）。
- 苹果命令行开发工具，用于Swift编译和本地临时签名。
- 可访问公开的GitHub、Node.js、PyPI、npm和Hugging Face资源；下载模型与运行库需要网络流量和磁盘空间。
- 不需要开发者账号、模型API Key或任何个人历史来构建。

## 构建步骤

在源码根目录执行：

```sh
python3 产品化/prepare_assets.py
.build/python-runtime-qwen-0.4.1/python/bin/python3 -I -B 产品化/build_product.py --version 0.4.1-preview.5 --output dist/preview
```

第一步把运行库与语音模型下载到当前目录下的 `.build/`，按记录的SHA-256校验，并在 `记账模块/node_modules/` 安装锁定的npm依赖。它不读取现用商用配置、不启动言小办、不调用收费API，不修改源码中的资源清单。Python依赖固定版本，但尚未建立每个依赖文件的完整散列锁定。2026-11-03已在当前Mac新空目录中实际下载并完成构建；准备过程禁用pip共享缓存，并隔离npm缓存和配置。

第二步编译独立安装助手并生成本地更新签名。签名私钥仅在 `.build/release-signing-key.bin`；不要发布整个 `.build/`。临时签名不等于Developer ID签名或苹果公证，生成的包仍属于内部预览，不能冒充已公证发行版。

输出：`dist/preview/言小办安装助手.app`。打开后由向导引导配置。当前Mac的隔离目录构建、安装与本地语音已验证；另一台Mac和iPhone首次安装尚未验证。

只检查构建资源、完全不下载：

```sh
python3 产品化/prepare_assets.py --check
```

源码包不携带大型模型、运行库或依赖目录，因此首次检查返回资源缺失是预期行为。

## 离线测试

使用已装有 `jsonschema`、`cryptography`、`qrcode` 等测试依赖的Python；完成资源准备后也可使用 `.build/python-runtime-qwen-0.4.1/python/bin/python3`：

```sh
python3 -B tools/test_offline.py service
python3 -B tools/test_offline.py installer
python3 -B tools/test_offline.py secretary
python3 -B tools/test_offline.py bookkeeping
python3 -B tools/test_offline.py clean
node --test 独立服务/tests/*.cjs
npm --prefix 记账模块 test
python3 -B tools/audit_clean.py
```

Python测试入口拒绝非回环网络连接，不使用真实Key。秘书模块的87条历史模型轨迹未随源码公开，对应一项回放测试显式跳过；其他保留的测试使用人工构造的样例。源码纯净专项还使用假的运行库和编译输出检查安装包结构，这不等于完整编译或实际语音通过。

测试通过不等于模型效果、声音听感、手机权限或系统通知通过。不要用现有商用账号和历史来填补新副本的空白配置。

构建出的安装包现已随带项目LICENSE、NOTICE与第三方说明。公开分发安装包时仍需提供对应版本完整源码，并满足第三方依赖与模型的分发条件；见 docs/LICENSING.md。

## 可选聚合航班资料

默认构建不携带聚合地点表；没有这份资料时航班查询会明确提示，其他功能不受影响。用户按供应商规则自行获取，以下步骤不发起收费查询：

```sh
python3 产品化/prepare_assets.py --flight-data-only
.build/python-runtime-qwen-0.4.1/python/bin/python3 -I -B 产品化/build_product.py --version 0.4.1-preview.5 --output dist/with-juhe-local --with-juhe-flight-data
```

完整首次准备也可用 `python3 产品化/prepare_assets.py --with-juhe-flight-data`。下载和转换结果只保存在 `.build/`，已列入忽略与源码审计规则。含该表的安装包仅作本地使用；未获得供应商再分发授权前不要公开上传。当前固定散列若因供应商更新而不符，脚本会停止，不能关闭校验强行继续。

高铁与航班当前适配聚合，用户自行承担其账户费用；其他平台需要新增适配并验证，详见 [供应商说明](docs/PROVIDERS.md)。

`tools/audit_clean.py` 用于即将上传的纯净源码目录；已经下载依赖或构建的工作目录会因含有 `.build/`、`node_modules/` 等而被拒绝，这是预期保护。请对不含生成资源的源码副本做最终发布检查。
