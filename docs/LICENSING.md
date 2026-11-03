# 项目许可证与适用范围

项目自有代码采用 GNU General Public License version 3，SPDX标识 GPL-3.0-only。

**2026-11-03，项目负责人授权决定后，项目主许可证确定为GPL-3.0-only。顶层LICENSE为GNU官方未经改写的GPLv3全文，NOTICE与LICENSE_SCOPE.json说明适用范围。** 第三方原有许可及署名保持不变。

## 选择依据

- 记忆模块直接引入nanoMuse移植代码；其来源记录标注GPL-3.0-or-later。选择其允许的第3版与项目GPLv3方案相符，原文件的“或后续版本”授权继续保留。
- 实际下载的记账依赖HyperFormula 3.5.0标注GPL-3.0-only，并提供GPLv3与专有许可两条路线。因此为本项目主代码明确选择第3版，不笼统宣称整个组合可以自动换用任意未来GPL版本。
- 其他组件有MIT、ISC、BSD、Apache-2.0、BlueOak等不同原始许可。项目顶层许可不能删除它们的版权、署名、NOTICE或其他适用要求。

这是基于现有组成确定的主许可证，不是全部第三方资源已完成法律审核的保证。现有记账清单有部分包缺少独立许可文本，正式分发包含这些包的二进制前需要补齐来源声明与适用文本；源码仓库未包含这些依赖代码时也应保留准确依赖说明。

## 权利与义务摘要

允许个人或商业使用、修改、分享及收费分发，也允许收费安装、维护和开发；不另加“禁止商用”“只许本人使用”“商用必须向我们缴费”等条件。

分发受GPL约束的程序及修改版本，应按许可提供相应源码、保留适用声明，并满足所采用分发方式的要求。只在自己电脑上修改使用，不因此必须上传GitHub。GPL不会自动使所有服务收入归原维护方，也不自动赋予其分成权。

我们自己的分发同样需要遵守已引入的第三方GPL义务，不能仅凭项目身份替整个包含第三方代码的版本出售闭源豁免。

服务、隐私、品牌说明分别解释服务合同、数据处理和真实身份，不改写GPL正文，不额外限制GPL授予的软件权利。付费服务的明确交付承诺独立履行。

## 程序、模型和素材分别说明

SenseVoice/FunASR模型另有非标准模型协议，其限制不能由项目GPLv3覆盖。Qwen模型、MLX及其他依赖也各有原始许可。当前GitHub准备包不携带模型权重，不能因此跳过对下载、组合使用或未来二进制分发条件的核对。

随源码图标已改为项目自制并提供矢量与生成代码；招呼音的模型、预设音色、生成记录和散列已核对。完整聚合地点表不随源码或默认构建再分发。资源条款和证据见 [ASSETS.md](ASSETS.md) 与 `ASSET_PROVENANCE.json`。

## 已落实及分发要求

1. 顶层LICENSE已放置未经改写的GPLv3全文；NOTICE与LICENSE_SCOPE.json标注项目自有代码GPL-3.0-only。
2. 保留第三方文件原始许可及署名，不把nanoMuse原有GPL-3.0-or-later改成only，不覆盖模型条款。
3. 更新README许可入口与第三方清单；服务说明不塞进GPL正文。
4. 核对拟公开文件和相应来源；源码预览发布与完整二进制分发分别检查。

## 参考

- GNU GPLv3正文：https://www.gnu.org/licenses/gpl-3.0.html
- GNU许可兼容说明：https://www.gnu.org/licenses/license-compatibility.en.html
- Apache基金会GPL兼容说明：https://www.apache.org/licenses/GPL-compatibility
- HyperFormula官方许可说明：https://handsontable.com/docs/hyperformula/guide/licensing
- 本仓库nanoMuse来源：../独立服务/shared_voice/vendor/nanomuse_memory/UPSTREAM.json
- 本仓库HyperFormula条款：../记账模块/THIRD_PARTY_LICENSES/hyperformula/LICENSE.txt

## 源码与二进制发布分别处理

本源码包许可已确定，不代表已经公开发布。今后分发安装包时，除随包保留许可证和声明，还须按GPL所选分发方式提供对应版本的完整相应源码（包括构建脚本和Swift源码），并处理所带依赖的相应义务；仅附上GPL全文不等于完成源码提供义务。

服务文件不是额外软件使用许可，不要求免费使用者购买服务，也不限制他们向第三方寻求有偿帮助。项目自有文档按GPL-3.0-only提供，文档中引述的第三方许可和资料保持其原有权利。
