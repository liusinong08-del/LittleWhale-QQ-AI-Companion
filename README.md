# 小鲸鱼 · LittleWhale

一个持续开发中的 AI 伙伴：有自己的角色、长期记忆和日记，可以在安卓独立环境中运行，并通过 QQ 与你交流。

这是 **开发预览源码**，不是安装即用的成品。QQ 图片和表情包收发、文字聊天曾在开发设备实测；不同设备与账号仍需单独验证。微信自动回复、稳定专属声音、唱歌和完整自主手机生活尚未全部完成。

## 当前能力与进度

| 能力 | 状态 |
| --- | --- |
| 角色卡、聊天页面、SQLite 记忆和日记 | 已实现核心流程 |
| QQ 私聊、队列去重、授权与发送限流 | 已集成 OneBot / NapCat，需自行部署和登录 |
| QQ 图片、表情包接收与回发 | 开发设备已验证；本地相册收藏不等于 QQ 原生收藏 |
| 本地语音识别 | 代码和电脑测试已准备，实际 QQ 语音转码仍待设备验收 |
| 专属声线与唱歌 | 开发中；不附模型权重或承诺实时生成 |
| 安卓消息助手、独立日记和记忆 App | 提供源码，构建需要自己的 SDK、配对信息与签名 |
| 云端安卓、微信、游戏和 App 自主操作 | 持续开发，兼容性与账号登录分别验证 |

## 先在电脑启动核心

需要 Python 3.10 或以上。Python 核心使用标准库；Android、QQ、语音是需要另外准备的可选运行组件。

```bash
python app.py --home ./.littlewhale --port 8767 --no-browser
```

打开终端显示的本机页面，在“生活设置”填写自己的模型 API 密钥和角色卡。**页面链接包含访问令牌，留在自己设备上，不要截图公开。** 未填写密钥时不会产生真实模型聊天；API 费用由自己的服务商账户承担。

启动核心不等于 QQ/微信已接通。QQ 需另装 NapCat、登录独立账号并配置回环 OneBot 与明确会话授权。当前 `app.py` 只绑定本机地址，云端使用 SSH 隧道访问，不要直接把管理端口、OneBot 或 QQ WebUI 暴露公网。

云端示例单元在 `cloud/`，其中迁移标记和数据条件用于防止空库误启动；复制配置不会自动安装 QQ、迁移数据或登录账号。

## 运行检查

```bash
python check.py
node check-panel.mjs
node check-humanize.mjs
python tools/check_public.py
```

Node.js 用于两项前端/第三方模块检查，不是 Python 核心的必装依赖。测试使用隔离数据和模拟服务；通过测试不等于真实平台投递已通过。

## 安卓构建

`android/` 包含原生消息助手、聊天入口、日记和记忆应用源码。当前构建使用 Android SDK platform 35、build-tools 35.0.0 和 JDK；通过 `ANDROID_SDK_ROOT` 或 `ANDROID_HOME` 指向自己的 SDK。

`python android/build.py --device <你的配对JSON路径>` 用于构建配对应用。配对资料、签名文件和 APK 不在仓库内；构建会调用现有 SDK/JDK，不会自动下载这些工具。设备检查脚本仍有开发环境路径假设，使用前应调整并核对目标，不能直接操作默认第一台设备。`guest-*` 与运行库打包脚本针对特定 Termux/Android 环境，是适配参考，不是通用一键安装器。

## 数据与隐私

- 角色、记忆、日记、API 密钥、QQ 会话、设备配对及运行日志不随源码发布。
- 仅为明确授权的联系人开启回复，保留暂停、限流、排队及不确定投递不重发。
- 原始用户语音默认本地识别。云端模板禁用 QQ 录音获取，避免迁移时悄悄改变音频处理范围。
- 升级或迁移前做一致数据库备份，不能用仓库空配置覆盖个人数据。

## 第三方与素材

实际随附的 Humanizer 来自 [ai-companion-kit](https://github.com/pinganbbOvO/ai-companion-kit)，固定来源、MIT 许可和哈希位于 `vendor/ai-companion/`。保留其原许可证。QQ 接入使用外部 [NapCat](https://github.com/NapNeko/NapCatQQ) 的 OneBot 接口，QQ 客户端、模型权重与第三方运行库不在发布包中。

图标为 AI 生成的鲸鱼娘风格插画，提示词保存在 `android/icons/prompts.json`。这是个人项目，与 DeepSeek、腾讯及其他提到的产品没有官方隶属关系。第三方组件及模型的使用条件以各自许可证为准；项目自有部分尚未指定统一开源许可证。

## 自愿赞助

如果这个项目对你有帮助，欢迎自愿支持开发维护与服务器费用。赞助完全自愿，不影响使用，也不是购买功能或完成时间承诺。

| 微信 | 支付宝 |
| --- | --- |
| <img src="assets/sponsor/wechat.jpg" alt="微信收款码" width="260"> | <img src="assets/sponsor/alipay.jpg" alt="支付宝收款码" width="260"> |
| [查看原图](assets/sponsor/wechat.jpg) | [查看原图](assets/sponsor/alipay.jpg) |

以上收款码由项目维护者提供。扫码时请核对收款信息，金额自定。
