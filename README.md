# Trae & Buddy 签到助手

> ## ⚠️ 项目声明（请先读）
>
> 本项目**基于开源项目 [cxqc168-wq / Trae-workbuddyAssistant](https://github.com/cxqc168-wq/Trae-workbuddyAssistant/) 修改而来**。
> 界面布局、交互形态与部分产品思路参考了该项目（原项目为 Tauri + React/Rust 技术栈），
> 在此基础上由本人**重写实现**并做了大量功能扩展 —— 本仓库是一套独立的 Python 实现，
> 不是原项目的分支拷贝，两者代码不通用。
>
> 原项目的著作权归原作者所有，在此郑重致谢。如果你喜欢本项目，也请一并为原项目点个 Star。

把 **TraeWork / TraeCN / WorkBuddy** 三个平台的每日签到，装进一个桌面 GUI；
同一套签到核心也能作为命令行脚本 + Windows 计划任务在后台静默执行。

| | |
| --- | --- |
| **GUI 版** | `AutoCheckinGUI/` —— 约 1.8 万行 Python，界面用标准库 tkinter，不依赖第三方 UI 框架 |
| **签到核心** | `核心脚本/` —— GUI 与命令行**共用同一份**签到逻辑，只此一份拷贝 |
| **运行平台** | Windows 10 / 11（用到注册表、计划任务、`Shell_NotifyIcon` 等 Windows 能力） |

GUI 与命令行**签到逻辑只此一份**：界面通过 `app/services/runner.py` 直接调用核心模块，
两边共用同一份 `config.json` / `logs/`，不会各签各的。

---

## 相比原项目的新增与创新

### 1. 架构 —— 整套重写为纯 Python，且不再需要「装证书 + 起代理」

| 原项目 | 本项目 |
| --- | --- |
| Tauri（Rust + React 前端），构建需要 Node + Rust 工具链 | **纯 Python + 标准库 tkinter**，`pip install -r requirements.txt` 就能源码运行，也能打成单个 exe 分发 |
| 前后端分离，靠 `invoke(command)` 契约通信 | 单进程桌面应用，`python main.py` 直接跑，无 IPC 层 |
| 需要把本地 CA 证书装进「受信任的根证书颁发机构」，再起代理进程抓取 JWT | **不装证书、不做中间人代理**：直接读本机既有的登录态 —— IDE 运行日志、浏览器持久化会话、桌面端 auth 文件 |

### 2. 平台覆盖 —— 三平台一体化

- **TraeWork**：Playwright 持久化浏览器会话签到，登录一次长期复用（每个账号独立 profile）
- **TraeCN**：从 Trae 系客户端日志里提取 JWT，直连 `checkin_credits/status|claim` 判定，不依赖页面渲染
- **WorkBuddy**：读桌面端 auth 文件 + 本地令牌快照，支持多账号累积
- 三个平台在同一个界面里切换，账号模型、签到流程、记录格式统一

### 3. 账号自动发现 —— 不用手填 uid 和令牌

- 扫描本机 **Trae CN IDE / TRAE SOLO CN 桌面端**日志里的昵称与 uid，自动识别「本机登录的是谁」
- 扫描 `profiles/` 下的 Chromium 会话目录判断登录态，**一键纳入**（也可全部纳入）
- WorkBuddy 一键「捕获当前账号」，切换账号后再捕获即可逐步累积
- 发现结果用「已识别 / 已纳入 / 可纳入 / 未登录」区分，不让人猜

### 4. 签到健康看板 —— 不只画一条积分曲线

- 今日完成度、近 7 日成功率、实际领取积分、下一次自动签到时间、近期开工流水
- 状态语义细分 `ok / skip / busy(待复核) / expired / fail`：把「服务端限流待复核」
  与「真失败」分开，避免积分其实已到账却报「失败」

### 5. 自动化执行链 —— 签到 × 补跑 × 弹窗联动

- 计划任务三类触发器：**每日定时 + 开机自启（延迟补跑）+ 网络恢复瞬间补跑**
- 幂等门控：已签完的那一轮秒退，不开浏览器、不发请求
- **手动优先且只等不抢**：手动点击时等自动化让出浏览器锁（最多 120 秒，每 5 秒报进度），
  避免并发写坏同一个 `user-data-dir`；计划任务与命令行行为完全不受影响
- 补跑弹窗按关键节点收敛（首轮 / 全部签完 / 窗口最后一轮仍失败），避免一天弹十几次

### 6. 凭据自动刷新

- 后台按间隔自动重新捕获，也支持**源文件一变就立刻刷新**（重开 IDE、切换账号后界面马上准确）
- 三平台凭据寿命差两个数量级（约 8 小时 / 5 天 / 60 天），界面直接标注「有效 / 临期」

### 7. 本地大模型接入 —— 云端账号池之外还能接本地模型

- 同一个 OpenAI 兼容端口按**模型名自动路由**：
  `local/ollama/<模型>` → Ollama、`local/llamacpp/<模型>` → llama.cpp、其余名字 → 云端账号池轮转
- Ollama 的 NDJSON 与 llama.cpp 的 SSE **两种原生协议都翻译成标准 OpenAI 格式**，支持流式
- 只连已运行的后端、不代管进程；回环请求强制绕开代理环境变量（避免 `HTTP 502 upstream` 之类假故障）

### 8. WorkBuddy 的抗丢失设计

- 令牌归档 `logs/wb_tokens_archive.json` **只增不减**，界面提供「找回历史账号」，快照被写坏也能恢复
- 写入路径加**严格读护栏**（读不出来就不写），杜绝「一次瞬时读失败 → 已保存账号全被清空」
- 新版把昵称/令牌加密（AES-GCM）后，本项目**不去硬解**，而是从同目录的历史明文快照按 uid 反查，
  都找不到时如实退回 uid 前缀，绝不把加密串当令牌发出去

### 9. 设备标识重置

- 覆盖 6 层：machineid / `storage.json` telemetry / aha.device / TinyStorage / 注册表 MachineGuid / webview 追踪数据
- **先扫描、后执行**，每个被改动的文件自动备份到 `%APPDATA%\TraeBuddyDeviceReset\backups\<时间戳>\`

### 10. 体验与工程化

- 无边框窗口（真正 0×0 非客户区）+ 原生边缘缩放，纯 ctypes 系统托盘（**零第三方依赖**）、亮/暗双主题、液态玻璃结果卡片
- 数据目录统一抽象 `runtime.data_root()`：源码 / exe / 便携三态一致，可用 `TRAEBUDDY_HOME` 覆盖
- 100+ 项自检与回归脚本，**关键断言都带正负对照**（防止「全绿」其实断言从未生效）
- 性能：页面渲染 18 995 ms → 约 1 515 ms，切换板块 4 553 ms → 565 ms（页面缓存 + 掐掉同步阻塞）

---

## 功能

| 模块 | 能力 |
| --- | --- |
| 三平台签到 | TraeWork（Playwright 持久化浏览器会话）、TraeCN（读 IDE 日志里的 JWT 直连接口）、WorkBuddy（本机 auth / 令牌快照） |
| 账号自动发现 | 扫描本机客户端日志与浏览器会话，自动识别已登录账号，一键纳入；WorkBuddy 支持多账号累积与历史找回 |
| 一键签到 | 账号勾选、跳过/强制、实时进度与逐账号结果、实时日志；手动优先（不抢浏览器锁，只等它让出） |
| 积分看板 | 今日完成度、近 7 日成功率、实际领取积分、下次自动签到、近期流水 |
| 计划任务 | `AutoDailyCheckin`：每日定时 + 开机自启补跑 + 网络恢复补跑，幂等门控（已签完秒退） |
| API 服务 | 本地 OpenAI 兼容服务：账号池轮转 / 冷却 / 清理 + 13 个云端模型入口 + Ollama / llama.cpp 本地模型（自动路由、支持流式） |
| 系统日志 | 平台筛选、自动刷新、历史流水、日志文件查看 |
| 凭据自动刷新 | 后台按间隔或「源文件变化」重新捕获，界面上的「有效 / 即将过期」保持准确 |
| 设备标识重置 | 6 层设备标识扫描与重置（每步先备份，可只读预览） |
| 其它 | 无边框自定义标题栏（原生缩放/贴边）、系统托盘（纯 ctypes）、亮/暗双主题、签到记录 CSV 可额外导出到自定位置 |

详细的实现说明（含大量踩坑记录、性能优化数据、回归用例清单）见
[`AutoCheckinGUI/README.md`](AutoCheckinGUI/README.md)。

---

## 目录结构

```
.
├─ AutoCheckinGUI/             GUI 源码、打包脚本与自检脚本
│  ├─ main.py                  源码运行入口（python main.py）
│  ├─ exe_launcher.py          打包入口：GUI / --run-checkin / --toast / --selftest
│  ├─ build_exe.spec           PyInstaller 配置（目录版 + 单文件版）
│  ├─ make_installer.py        安装包生成器（纯 Python，不需要 Inno Setup）
│  ├─ installer.iss            Inno Setup 脚本（可选）
│  ├─ 启动GUI.bat / .vbs        源码启动器（带控制台 / 无控制台）
│  ├─ app/
│  │  ├─ application.py        主窗口：装配、路由、主题、账号缓存、线程调度
│  │  ├─ config.py             数据目录定位 + 配置/状态/历史读写（与核心脚本共用）
│  │  ├─ theme.py              亮/暗色板与字体探测
│  │  ├─ ui/                   widgets / sidebar / titlebar / topbar / toast / 作者卡片 / 配置导航
│  │  ├─ pages/                概览 / 一键签到 / 账号管理 / 积分看板 / API 服务 / 日志 / 设置
│  │  └─ services/             runner · credits · scheduler · api_server · local_backends
│  │                          · credential_refresh · device_reset · client_launcher · tray …
│  ├─ assets/                  图标、版本信息、作者收款码
│  └─ tests/                   自检与回归脚本（见下）
└─ 核心脚本/                     签到核心：GUI 与命令行共用
   ├─ checkin.py               入口 + 配置 + 计划任务 + 状态/记录 + 网络等待
   ├─ tw_checkin.py            TraeWork 签到（Playwright 持久化会话）
   ├─ tc_checkin.py            TraeCN 签到（IDE 日志取 JWT → 直连接口）
   ├─ wb_checkin.py            WorkBuddy 签到（桌面端 auth / 令牌快照）
   ├─ runtime.py               运行时抽象：脚本模式 ↔ exe 模式的根目录定位与自调用
   ├─ toast_gui.py / toast.html  结果弹窗（原生 Toast 兜底见 notify.ps1）
   ├─ net_util.py              网络等待
   ├─ config.example.json      配置样例（首次运行会自动生成 config.json）
   └─ 双击签到.bat / capture账号.bat   命令行版一键入口
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
python -m playwright install chromium     # TraeWork 走浏览器会话，需要 Chromium 内核
```

### 2. 运行

| 方式 | 操作 |
| --- | --- |
| 源码 · 带控制台 | 双击 `AutoCheckinGUI\启动GUI.bat`（缺依赖会自动 `pip install`） |
| 源码 · 无控制台 | 双击 `AutoCheckinGUI\启动GUI.vbs` |
| 源码 · 命令行 | `python AutoCheckinGUI\main.py` |
| 纯命令行签到 | `python 核心脚本\checkin.py run` |

首次运行会在**数据目录**里自动生成 `config.json`、`logs/`、`profiles/traework/`，
开箱即用；进「账号管理」点「扫描本机会话」即可把本机已登录的账号纳进来。

### 3. 打包成 exe

```bash
python -m PyInstaller --clean --noconfirm AutoCheckinGUI/build_exe.spec
python AutoCheckinGUI/make_installer.py
```

产物：

| 文件 | 说明 |
| --- | --- |
| `AutoCheckinGUI/dist/TraeBuddy签到助手/` | 目录版（启动快，推荐） |
| `AutoCheckinGUI/dist/TraeBuddy签到助手-单文件.exe` | 单文件版（便于拷贝分发） |
| `AutoCheckinGUI/Output/TraeBuddy签到助手_Setup_x.y.z.exe` | 安装版（向导 + 快捷方式 + 卸载项） |

打包后可以直接对 exe 做完整性自检，不用装 Python：

```bash
"AutoCheckinGUI/dist/TraeBuddy签到助手/TraeBuddy签到助手.exe" --selftest
```

---

## 命令行用法

```bash
python 核心脚本\checkin.py run                     # 立即签到一轮
python 核心脚本\checkin.py status                  # 查看状态
python 核心脚本\checkin.py login                   # TraeWork 首次登录引导
python 核心脚本\checkin.py capture                 # 保存 WorkBuddy 当前登录账号令牌
python 核心脚本\checkin.py install                 # 注册计划任务 AutoDailyCheckin
python 核心脚本\checkin.py uninstall               # 移除计划任务
python 核心脚本\checkin.py settime 09:00 21:00     # 设定每日执行时间
python 核心脚本\checkin.py setretry 30 360         # 设定自动补跑（间隔 / 窗口，分钟）
```

计划任务包含三类触发器：每日定时、开机自启（延迟补跑）、网络恢复补跑；
配合幂等门控，已签完的那一轮会直接静默退出，不会重复请求接口。

---

## 数据目录

`config.json` / `logs/` / `profiles/` / 令牌快照都写在**数据目录**里，
不写进源码目录 —— 所以程序可以随便换位置、换机器，也可以整体备份搬走。

查找优先级（`runtime.data_root()`，GUI 与命令行用**同一套**判定）：

1. 环境变量 `TRAEBUDDY_HOME` 指定的目录
2. 与 GUI 同级的 `核心脚本/`（源码模式）
3. exe 旁边（便携版）
4. `~/.traebuddy`（exe 放在只读位置时的兜底）

想看当前用的是哪个目录：**系统设置 → 关于**里的「数据目录」一行，
或直接跑 `程序.exe --selftest`。

| 文件 | 用途 |
| --- | --- |
| `config.json` | 平台开关、账号列表、每日时间、运行选项 |
| `logs/state.json` | 今日签到状态（GUI 与计划任务共用的判重依据） |
| `logs/history.jsonl` | 签到流水 |
| `logs/签到记录.csv` | 导出的签到记录（UTF-8-SIG，Excel 直接打开） |
| `logs/checkin_YYYY-MM-DD.log` | 当日运行日志 |
| `tokens_workbuddy.json` | WorkBuddy 已保存令牌快照 |
| `logs/tokens_traecn.json` | TraeCN 令牌缓存 |
| `profiles/traework/<名称>/` | TraeWork 独立浏览器会话（登录态就存在这里） |

> ⚠️ 这些文件等于**你的登录态**。它们在 `.gitignore` 里已全部排除，
> 请勿手动提交或分享给他人。

---

## 自检脚本

```bash
python AutoCheckinGUI/tests/audit_pages.py          # 遍历页面 × 亮/暗主题，报告渲染异常
python AutoCheckinGUI/tests/diag_pages.py           # 绕开容错直接构造页面，拿完整 traceback
python AutoCheckinGUI/tests/verify_frameless.py     # 断言非客户区 0×0、最大化/还原几何
python AutoCheckinGUI/tests/verify_app_window.py    # 启动真实 App，校验外观 + 托盘
python AutoCheckinGUI/tests/perf_ui.py              # 各页面渲染耗时
python AutoCheckinGUI/tests/test_local_backends.py  # 本地后端协议单测（假 Ollama / llama.cpp）
python AutoCheckinGUI/tests/test_api_local_e2e.py   # 真起 HTTP 服务 + 真流式端到端
```

发版前建议在冻结 exe 上再跑一次窗口/图标探针：
`probe_exe_window.py`、`probe_exe_icon.py`、`probe_exe_version.py`。

---

## 已知行为

* **需要先在本机登录** —— 三个平台都复用你**本机客户端/浏览器已有的登录态**，
  本程序不代你登录，也不保存账号密码。
* **TraeWork 使用独立浏览器会话** —— Chromium 的 `user-data-dir` 是独占的，
  每个账号 profile 有 `.checkin.lock`；自动化那一轮跑着时手动点击会**等它让出**
  （最多 120 秒，日志每 5 秒报进度），只等不抢，避免并发写坏 profile。
* **WorkBuddy 昵称/令牌在新版被加密** —— 本程序不去硬解，而是从同目录的历史明文快照
  按 uid 反查；都找不到时如实退回 uid 前缀，不会把加密内容当令牌发出去。
* **TraeCN 依赖 IDE 日志** —— 打开一次 IDE 并登录后令牌会自动恢复。
* **API 服务不转发上游模型** —— 服务框架、账号池轮转、鉴权与本地模型（Ollama / llama.cpp）
  均已实现；云端模型的真实转发需要按各平台私有协议签名，留了接入点
  （`api_server._default_forward()`），调用时明确返回 `501`，不会伪装成模型故障。
* **设备标识重置前请完全关闭客户端** —— 文件被占用时可能写失败，界面会逐层报告成功/失败。

---

## 免责声明

本项目是**本机自动化辅助工具**，只操作你自己机器上、你自己已登录的账号。
使用前请自行确认目标平台的服务条款是否允许自动化签到；因使用本工具产生的
账号风险、数据丢失或其他后果由使用者自负。作者不对任何间接损失负责。

## 致谢

* 界面与交互参考：[Trae-workbuddyAssistant](https://github.com/cxqc168-wq/Trae-workbuddyAssistant)
* 本地模型接入：Ollama、llama.cpp

---

## 联系作者

| | |
| --- | --- |
| 邮箱 | **2571543714@qq.com** |
| 反馈 | 欢迎提 Issue / PR；也欢迎在 Issue 里附上 `程序.exe --selftest` 的输出，排障最快 |

## 赞赏支持

如果这个项目帮到了你，可以请作者喝杯咖啡 ☕ 感谢支持：

<img src="assets/reward_qr.jpg" width="260" alt="作者赞赏码">

> 程序内「作者」入口里也放了同一张赞赏码（`AutoCheckinGUI/assets/reward_qr.png`）。

## 许可

本仓库尚未附带开源许可证文件。若要公开分发，请先选定一个许可证
（例如 MIT）并补一个 `LICENSE`；在此之前默认保留所有权利。
