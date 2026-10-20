# 安装与安卓虚拟机指南

小鲸鱼目前是开发预览源码。建议先跑通聊天核心，再接 QQ，最后适配安卓操作；安装 Python 核心不等于已经有一台能自动使用所有 App 的 AI 手机。本仓库不附通用 APK、模型权重、QQ 客户端或他人的账号配置。

## 选择运行方式

| 方式 | 能做什么 | 还需要什么 |
| --- | --- | --- |
| 电脑 / Linux 服务器 | 运行聊天页面、角色、记忆、日记及 QQ 接线层 | Python；QQ 另行安装 NapCat 并登录 |
| 自己安卓手机的独立虚拟机 | 给 AI 独立的安卓空间，在里面安装 Termux 和 App | 虚拟机兼容性、正常的 Termux Python、设备配对 |
| 服务器里的安卓 | 目标是全天独立运行的手机空间 | 尚未形成通用安装方案；先验证内核、虚拟化、图形和 App 兼容 |

只运行 API 聊天核心不需要 GPU。专属声线、唱歌、安卓图形应用有另外的资源要求，不能按文字聊天配置推算全部能力。

## 一、先在电脑跑通聊天核心

安装 [Python](https://www.python.org/downloads/) 3.10 或以上，以及 [Git](https://git-scm.com/downloads)。Windows 安装 Python 时勾选加入 PATH；Linux 可使用发行版提供的受支持 Python。

```bash
git clone https://github.com/liusinong08-del/LittleWhale-QQ-AI-Companion.git
cd LittleWhale-QQ-AI-Companion
python --version
python check.py
python app.py --home ./.littlewhale --port 8767 --no-browser
```

系统只有 `python3` 时，将上面的 `python` 换成 `python3`。核心使用 Python 标准库，不需要执行 `pip install` 下载一整套框架。

保持终端运行，打开它输出的 `http://127.0.0.1:8767/#...` 页面。这是管理与测试页面，QQ 是另一个聊天入口。链接中包含访问令牌，不要公开截图或把它提交到 GitHub。

在“生活设置”中：

1. 填写自己模型服务的 HTTPS 地址、实际可用的模型名称和 API 密钥。
2. 保存设置，点击“检查模型连接”。该检查会调用模型，可能计费。
3. 修改角色卡、称呼及思考等级，先在“聊一会儿”测试回复。
4. 确认后再开启主动行为；每日调用上限填 `0` 表示程序不设该项上限，不代表 API 免费。

数据保存在指定的 `.littlewhale` 目录。保留整个数据目录，不用仓库空配置覆盖它。页面可打开但模型不回复时，先看模型检查结果，不要直接重登 QQ。

## 二、手机里安装独立安卓虚拟机

开发中使用过 VMOS+ 的安卓独立环境。它不是本仓库的一部分；镜像、免费功能、授权和后台限制可能变化，请从官方渠道核对，不能把某台设备的成功等同于所有手机都支持。

- [VMOS+ 官方网站](https://plus.vmos.cn/zh/)
- [VMOS 文档](https://docs.vmos.app/zh/)
- [Termux 官方安装说明](https://github.com/termux/termux-app#installation)

实际操作顺序：

1. 在主手机安装虚拟机，创建独立安卓系统，先进入其桌面。
2. 在虚拟机浏览器里确认普通 HTTPS 网页能够打开；开启自动日期和时间，测试下载。只显示 Wi-Fi 图标不代表互联网可用。
3. 将 Termux 装进**虚拟机内部**；QQ、微信等打算给 AI 使用的 App 也装在里面。主手机与虚拟机不是同一个存储和运行环境。
4. Termux 与 Termux:Boot、Termux:API 等插件应从同一官方支持来源安装，避免签名不一致。不要把他人的备份、私有运行库或账号配置当安装包使用。
5. 先验证 Termux 中能正常执行 Python、SQLite 和 HTTPS，再准备 App 自动化权限或 ADB 配对。

### Termux 包管理正常时

下面仅适用于包管理器和软件源正常、没有既有个人部署的新环境：

```bash
pkg update
pkg install python git
python --version
git clone https://github.com/liusinong08-del/LittleWhale-QQ-AI-Companion.git
cd LittleWhale-QQ-AI-Companion
python check.py
python app.py --home "$HOME/.littlewhale" --port 8767 --no-browser
```

在虚拟机里的浏览器打开终端输出的本机页面。Python 低于 3.10、包管理报错、HTTPS 证书失败时，停在这里排查；不要用修改系统文件、关闭证书验证或随意替换运行库的方式继续。

开发设备曾遇到老 Android / 虚拟机包管理问题，并使用专门构建的私有运行库适配。仓库中的 `guest-*`、`pack_runtime.py`、`pack_napcat_runtime.py` 是相关适配源码，**不是可以在任意 VM 上直接运行的一键安装器**。例如 `guest-start.sh` 要求先有匹配的 `.runtime-path`，第一次克隆仓库后不能直接拿它启动。

### 后台运行需要单独验证

- 根据手机与虚拟机官方设置，允许必要的后台运行、启动和电池使用；保持虚拟机与 Termux 进程存活。
- 在装有相应 Termux 工具的环境，可尝试 `termux-wake-lock` 减少休眠影响，但它不保证系统不杀进程。
- 分别测试锁屏、切换 Wi-Fi/流量、断网恢复、虚拟机被杀与整机重启。页面关掉和进程停止是两回事。
- 数据备份与重启恢复没有通过前，不宣称“全天无人值守”。Android 官方系统及厂商后台机制仍会影响运行，Termux 官方也记录了相关限制。

## 三、接 QQ，以及微信当前状态

QQ 使用 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 提供的 OneBot 接口。选择适合自己系统与 CPU 架构的官方部署方式，再在小鲸鱼“QQ 与微信”中配置网关与授权会话。手机 Termux、普通 Linux 与 Windows 的运行库不能互换。

核对这些条件：

1. 自己准备的 QQ 账号能由本人完成扫码及身份验证；密码和验证码不写进仓库。
2. 网关与核心可以互相访问，HTTP 服务、事件上报地址及鉴权 token 一致。OneBot / WebUI 只对本机开放。
3. 只授权自己的测试私聊，验证接收一条消息、回复一条消息，再检查连续对话与断网恢复。
4. 同一个账号不要同时启动手机端与云端两套接管实例，以免会话互踢。迁移先备份数据、停旧端，再启新端。

微信走安卓侧消息 / GUI 路线，当前还没有通用且已完成验收的自动回复安装流程。不要把 QQ 接通写成微信也已接通；也不要把自动化环境中的登录转圈直接判断为验证码错误。

## 四、构建小鲸鱼的安卓 App

仓库提供聊天入口、消息助手、日记和记忆应用的 Java / Manifest 源码；不是把网页截图做成 APK。当前需要 SDK platform 35、build-tools 35.0.0、JDK，以及自己的配对资料。用 `ANDROID_SDK_ROOT` 或 `ANDROID_HOME` 指向 SDK。

```bash
python android/build.py --device /path/to/your-device.json
python android/build.py --device /path/to/your-device.json --app
python android/build.py --device /path/to/your-device.json --notebook diary
python android/build.py --device /path/to/your-device.json --notebook memory
```

`/path/to/your-device.json` 是需自行准备的配对文件，至少包含目标虚拟机的 `android_id` 和 `sdk`。不要照抄他人的标识；Android 新版本还存在应用作用域 ID 差异，构建成功不等于配对完成。

`phone.py` 还会检查回环 ADB 地址和虚拟机指纹等身份，避免误操作主手机。设备检查脚本有开发环境路径假设，先修改为自己的环境，再明确选择目标；不能默认操作第一台连接的手机。签名文件、配对 JSON、生成 APK 及个人数据均不应提交到仓库。

## 五、服务器与云端安卓

纯聊天核心可在 Linux 上按第一节启动，通过 SSH 隧道访问页面，例如在自己的电脑执行：

```bash
ssh -L 8767:127.0.0.1:8767 your-user@your-server
```

这只建立本地端口转发，不会把服务公开到互联网。使用服务器上生成的页面令牌，保留鉴权。`cloud/` 的 systemd 模板有数据与迁移标记条件，需配置自己的用户、路径、QQ 依赖和备份；复制文件不能替代迁移。

在服务器里另运行安卓，还需检查虚拟化透传/KVM、或安卓容器需要的 binder 等内核能力，以及图形、内存和 ARM App 兼容性。**不是任何便宜云主机都能用；目前项目没有通过通用云安卓安装验收。** 先做可行性测试，再决定是否升级配置，不能把普通 CPU 主机当作语音/唱歌 GPU。

## 安装后验收清单

- [ ] 本机页面能打开，API 检查通过，能连续文字聊天。
- [ ] 修改角色卡后新回复采用设置，重启后记忆和日记仍存在。
- [ ] QQ 指定私聊能收发；未授权联系人不自动回复。
- [ ] 图片和表情包能接收与回发，失败时明确说明。
- [ ] 暂停、发送限流、断网恢复和重启不会重复投递旧消息。
- [ ] 手机 VM 锁屏/切网/低内存测试分别记录，未通过项不视作已完成。
- [ ] 原始语音本地识别、真实 QQ 语音条、唱歌与微信分别验收，不能靠开关存在判断已可用。
