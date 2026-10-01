# Trae & Buddy 签到助手（GUI 版）

把原项目的命令行签到脚本，装进一个复刻自
[Trae-workbuddyAssistant](https://github.com/cxqc168-wq/Trae-workbuddyAssistant)
的桌面界面里。**签到逻辑完全复用原脚本**，GUI 只是外壳。

> **本仓库里的目录对应关系**：本文提到的「项目根 / 签到模块所在目录」，
> 在本仓库中是**上一级的 `核心脚本/`**（`checkin.py` / `tw_checkin.py` /
> `tc_checkin.py` / `wb_checkin.py` / `runtime.py` / `toast*.{py,html}` 都在那儿），
> 数据文件（`config.json` / `logs` / `profiles` / `tokens_*.json`）同样落在它里面。
> 仓库总览与快速开始见[根目录 README](../README.md)。

---

## 快速开始

### 方式一：安装版（推荐给普通用户）

| 文件 | 体积 | 说明 |
| --- | --- | --- |
| `Output\TraeBuddy签到助手_Setup_2.4.6.exe` | ~67 MB | **安装程序**：向导式安装、可选桌面快捷方式与开机自启、自动写卸载项 |

安装后从开始菜单或桌面启动即可，无需安装 Python 环境。
卸载走「设置 → 应用」或安装目录下的 `uninstall.exe`；**签到记录与配置会保留**
（`logs\`、`config.json`、`profiles\`、`tokens_workbuddy.json`）。

安装向导做的事：

1. 把内嵌的 `payload.zip` 解压到安装目录（默认 `%LOCALAPPDATA%\Programs\TraeBuddy`）
2. 生成开始菜单快捷方式「Trae & Buddy 签到助手」与「卸载 Trae & Buddy 签到助手」
3. 按勾选情况生成桌面快捷方式、写入开机自启（HKCU `Run`）
4. 复制自身为 `uninstall.exe`，并在 HKCU 写卸载项（设置→应用里可见）
5. 卸载时额外删除 `AutoDailyCheckin` 计划任务

### 方式二：免安装 exe（推荐给便携使用）

| 文件 | 体积 | 说明 |
| --- | --- | --- |
| `dist\TraeBuddy签到助手\TraeBuddy签到助手.exe` | ~150 MB | 目录版，**启动快**（推荐） |
| `dist\TraeBuddy签到助手-单文件.exe` | ~57 MB | 单文件版，便于拷贝分发，首次启动稍慢 |

把整个目录（或单文件）拷到任意位置即可运行。

**数据目录怎么找**（整理后的桌面版）：

1. 环境变量 `TRAEBUDDY_HOME` 指定的目录（用户显式指定时优先）
2. **`桌面版\核心脚本`**（桌面版固定的数据与核心代码目录）
3. 便携 exe 目录旁边已有数据
4. 便携 exe 目录旁边（首次运行时初始化）
5. `~\.traebuddy`（exe 安装到只读位置时的兜底）

桌面版与 `脚本版` 的配置、日志、profiles 和令牌相互独立；不要混用两边的数据目录。

> 第 2 条很关键：早期版本只看「exe 旁边有没有 `checkin.py`」，而打包后该文件
> 在 `_internal` 里，条件永远为假，于是数据目录落到 exe 所在的空文件夹——
> 界面表现为「0 个账号 / 暂无账号」，跟你在项目里跑的配置完全脱节。
> 现在会主动向上找到项目根并复用它的 `config.json` 与 `logs`。
>
> 首次运行（找不到任何已有数据）会自动初始化：写出默认 `config.json`、
> 建好 `logs\` 与 `profiles\traework\`，让程序开箱可用。
>
> 想知道当前用的是哪个目录？**系统设置 → 关于**里有「数据目录」一行，
> 旁边还有「打开数据目录」按钮；命令行 `程序.exe --selftest` 也会打印。

### 方式三：源码运行（开发/调试）

| 方式 | 操作 | 说明 |
| --- | --- | --- |
| 无控制台 | 双击 `启动GUI.vbs` | 和双击 exe 体验一致 |
| 带控制台 | 双击 `启动GUI.bat` | 依赖缺失会自动安装、报错可见 |
| 命令行 | `python main.py` | 手动指定解释器时使用 |

首次运行会自动检测 `requests` / `playwright`；缺失时 `.bat` 会尝试
`pip install -r requirements.txt`。

---

## 打包成 exe

```bash
# 1. 打包主程序（产出目录版 + 单文件版）
python -m PyInstaller --clean --noconfirm build_exe.spec

# 2. 生成安装程序（纯 Python，无需 Inno Setup）
python make_installer.py
```

`build_exe.spec` 的要点：

* 把项目根的签到模块（`checkin` / `tw_checkin` / `tc_checkin` /
  `wb_checkin` / `net_util` / `toast_gui` / `runtime`）作为
  `hiddenimports` 打进包——exe 模式下它们不在同目录，静态分析看不到。
* `requests` 装在 user site-packages 时，需把它加进 `pathex`。
* `playwright` 只做**运行时检测**（浏览器内核体积巨大且需单独安装），
  不打进包；缺内核时界面会给出明确提示。
* **数据文件不打进包**：`config.json` / `logs` / `profiles` / `tokens_*.json`
  一律在数据目录读写（查找规则见上文「方式二」），保证可备份、可迁移。
* `app/config.py` 的 `_locate_root()` 是数据目录定位的唯一入口，
  `ensure_data_dir()` 负责首次运行初始化，两者都有测试覆盖
  （`tests/test_accounts_discovery.py` 第 7 组）。

已安装 Inno Setup 6 的话，也可以改用 `installer.iss`：
```bash
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

---

## 界面结构

**左侧栏** — 顶部胶囊分段控制器切换三个平台模块，下方是对应导航，
底部是全局页（**API 服务** / 系统日志 / 系统设置）与**作者入口**。

| 模块 | 视图 | 说明 |
| --- | --- | --- |
| TraeWork / TraeCN / WorkBuddy | 概览 | 配置导航（未完成初始化时显示）+ 4 张统计卡 + 计划任务状态 + 账号签到状态 + 最近记录 |
| | 一键签到 | 账号勾选、跳过/强制、实时进度条与逐账号结果、实时日志 |
| | 账号管理 | **自动扫描本机已登录的账号**（三平台统一，见下节），一键纳入/捕获；也保留手动新增与移除 |
| | 积分看板 | **签到健康 + 近期收益 + 账号可用性**：今日完成度、近 7 日成功率、实际领取积分、下一次自动签到、近期开工流水；额度接口作为逐账号补充，不再因接口未开放留空白 |
| 全局 | API 服务 | 本地 OpenAI 兼容服务：启停、账号池轮转、冷却清理、密钥掩码、13 个云端模型 + Ollama / llama.cpp 本地模型（自动路由、支持流式） |
| | 系统日志 | 平台筛选、5 秒自动刷新、历史流水、日志文件查看 |
| | 系统设置 | 平台开关、每日签到时间、**自动化执行链（签到 × 补跑 × 弹窗联动）**、运行选项、**账号凭据自动刷新**、计划任务安装/卸载、**设备标识重置**、数据维护、主题切换 |

**顶栏** — 实时状态徽标（账号数 / 今日已签 / 令牌临期 / 计划任务 / 网络）、
主题切换、一键签到。

---

## 账号自动发现（不用手填）

三个平台的账号管理页都已统一为「**先自动发现本机登录态，再一键纳入**」，
进入页面即静默扫描一次，也可点主按钮重新扫描。

| 平台 | 扫描来源 | 纳入方式 |
| --- | --- | --- |
| **TraeWork** | ① 本机 Trae 系客户端（Trae CN IDE / **TraeWork CN 桌面端**）日志里的 `ScreenName` + `UserID`；② `profiles/traework/<会话名>/`（Chromium 会话目录，`Default/Cookies` 非空即视为已登录）+ `nickname.txt` | ① 客户端账号标「已识别」，签到走 TraeCN 通道，**无需纳入**；② 网页会话点「纳入」登记进 `config.json`，登录态是现成的，也可「全部纳入」 |
| **WorkBuddy** | 桌面端 `auth` 文件（当前登录）+ `tokens_workbuddy.json`（已保存快照） | 点「捕获当前账号」保存令牌快照；切换桌面端账号后可再捕获，逐步累积 |
| **TraeCN** | Trae 系客户端（Trae CN IDE / **TraeWork CN 桌面端**）运行日志里的 JWT（`collect_accounts`） | 自动出现在列表，无需操作；页面只做展示与「查看令牌」 |

> **TraeWork CN 桌面端（TRAE SOLO CN）说明**
>
> TraeWork CN 桌面软件与 Trae CN IDE 用的是**同一套 `api.trae.cn` 账号体系**，
> 也调用同一个签到接口（`/trae/api/v2/ug/checkin_credits/status|claim`），
> 只是它日志里的 token 被脱敏成 `******`，拿不到可签到的令牌。
> 因此策略是：**在 TraeWork 页把它识别出来并显示「已识别」**，
> 让你确认本机软件登录的是哪个账号；真正的签到由 TraeCN 通道完成
> （`tc_checkin` 会扫描全部 Trae 系客户端日志目录取令牌）。
> 这样「TraeWork 板块看不到 TraeWork CN 里的账号」的问题就不存在了。

发现区会明确区分状态，不会让人猜：

- **● 已识别** — 本机 Trae 客户端登录的账号（只读展示，签到走 TraeCN 通道，无需纳入）
- **● 已纳入** — 已在该平台账号列表里
- **● 可纳入** — 登录态存在但尚未登记（TraeWork 网页会话会给出「纳入」按钮）
- **○ 未登录** — 会话目录存在但没有登录痕迹，提示先去登录

`display` 名称优先取真实昵称（TraeWork 读 `nickname.txt`、TraeCN/WorkBuddy 走接口
反查并缓存），失败才回退目录名，所以列表里看到的是「Klein」而不是「trae1」。

> 手动「＋ 手动新增」仍然保留，但只是补充手段 —— 仅在需要预留一个空会话时用。
> 正常情况下点「扫描本机会话」就足够了。

**为什么之前会觉得「手动添加没反应」**：账号其实已经写进了 `config.json`，
但 UI 读的是内存缓存快照，而刷新请求在「已有一轮刷新进行中」时被**静默丢弃**了
（页面加载触发的那轮扫描可能要跑 3~15 秒，正好覆盖用户的操作窗口）。
现在迟到的刷新请求会被记为**待补刷新**，本轮结束后立刻再跑一遍，
所以任何增删操作都保证能反映到界面上。
**右上角** — 自定义标题栏：最小化 / 最大化 / 边缘拖拽缩放 / 关闭（关闭前二次确认）。

### 无边框窗口是怎么做的（避免"两层外框"）

`overrideredirect(True)` 单独用就能拿到 **非客户区 0×0**（真正的无边框）。
坑在于：**只要把 `WS_THICKFRAME` 加回来让窗口可缩放，Windows 就会重新生成
一圈 14×14 的调整边框**，于是又变成"两层"。实测三种状态：

| 状态 | STYLE | EXSTYLE | 非客户区 |
| --- | --- | --- | --- |
| Tk 默认 | `0x16CF0008` | `0x00000100` | **16×39** |
| 仅 `overrideredirect` | `0x00000000` | `0x00000000` | **0×0** |
| 再加 `WS_THICKFRAME` | `0x00040000` | `0x00040100` | **14×14** |

所以本项目**不还原 `WS_THICKFRAME`**，边缘缩放全部由 `WM_NCHITTEST` 回调
返回 `HTLEFT/HTRIGHT/HTTOP/…` 让系统进入原生缩放循环。同时：

* `WS_EX_APPWINDOW` 置位、`WS_EX_TOOLWINDOW` 清除 → 任务栏能显示图标
* `WM_SETICON` + Tk `iconphoto` 双重设置 → 任务栏/Alt-Tab 都有图标
* `WM_GETMINMAXINFO` 按 `SPI_GETWORKAREA` 收口 → 最大化不盖住任务栏
* `WM_NCCALCSIZE` 返回 0 → 客户区等于整窗，杜绝残留边框

### 系统托盘

`app/services/tray.py` 用纯 ctypes 调 `Shell_NotifyIcon`，起一个后台线程跑
自己的消息循环（隐藏窗口类 `TraeBuddyTrayWnd`），**不依赖第三方库**。
右键菜单：显示主窗口 / 立即签到 / 退出；单击图标唤回主窗口。

### 响应速度

三个热点已消除（实测 7 个页面总渲染 **18 995 ms → 约 1 514 ms**）：

| 热点 | 原因 | 处理 |
| --- | --- | --- |
| `scheduler.task_info()` | 每次调用**起一个 PowerShell 进程（≈1.1s）**，一次切页被调 14 次 | 加 5 秒 TTL 缓存 + 启动后台预热；渲染路径一律改读 `cached_task_info()`（永不阻塞） |
| `setup_guide._deps_ok()` | `__import__("playwright")` 每次 ≈400 ms | 进程内缓存 + 启动时后台预热 |
| `api_service._accounts()` | 重扫 TraeCN IDE 日志并请求接口 | 优先复用主程序后台扫描好的账号缓存 |

第二轮（2026-09-22）：**页面缓存 + 掐掉两处同步阻塞**，
「点击切换板块」实测 **4 553 ms → 565 ms（8×）**。

| 热点 | 原因 | 处理 |
| --- | --- | --- |
| 切页**每次都销毁重建页面** | 单页 226~1939 ms，第二次进同一页一样慢 | `App._show` 加 `_page_cache`（key=(模块,视图)）：命中就只跑 `on_show()`。换主题时整体失效 |
| `settings` 凭据状态 | `CR.entries()` 同步跑 `tc_checkin._scan_ide_logs()`（≈0.4 s） | 改后台线程取，先占位/沿用上次文本再回填（1 389 → 73 ms） |
| `api_service` AI 状态 | `ai_status()` 同步探测本地后端（urllib，超时 2 s） | 只读缓存 + 后台探测；用户点「刷新状态」才强制重探（838 → 97 ms） |
| `logs` / `dashboard` / `accounts` | 内容没变也重建上百个控件 | 用 `(mtime_ns, size, 筛选, 主题)` 当签名，一致就跳过（1 050 → **1.9 ms**） |
| `topbar.set_badges()` | 每次切页都重建 4~5 个徽标 | 签名一致直接返回 |

回归：`tests/test_page_cache.py`（14 项，含 4 条正对照）· `tests/perf_ui.py`（建页 vs 缓存命中分开量）。

---

## API 服务（本地 OpenAI 兼容）

一个端口同时对外提供**账号池**与**本地模型**两类能力，用**模型名**分流：

| 端点 | 说明 |
| --- | --- |
| `GET /health` | 健康检查（端口、池内账号数、运行时长，并附 `local` 各后端状态） |
| `GET /v1/models` | 模型列表（OpenAI 格式）：云端 13 个 + 已启用后端的本地模型 |
| `GET /v1/local/status` | 本地后端明细（地址 / 是否启用 / 模型数 / 最近一次探测结果） |
| `POST /v1/chat/completions` | 聊天补全，**按模型名自动路由**，`stream: true` 走 SSE |

### 路由规则

| 模型名写法 | 去向 |
| --- | --- |
| `local/ollama/<模型名>` | Ollama（默认 `http://127.0.0.1:11434`） |
| `local/llamacpp/<模型名>` | llama.cpp `llama-server`（默认 `http://127.0.0.1:8080`） |
| `local/<模型名>` | 只启用了一个后端时用它；启用多个则要求写全前缀 |
| 其它任何名字 | 账号池轮转（原逻辑不变） |

客户端只需换模型名即可在云端与本地之间切换，无需改地址：

```bash
curl http://127.0.0.1:7864/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"local/ollama/moondream:latest",
       "messages":[{"role":"user","content":"你好"}]}'
```

### 本地后端说明

| 后端 | 模型列表 | 对话接口 | 流式协议 |
| --- | --- | --- | --- |
| Ollama | `GET /api/tags` | `POST /api/chat` | NDJSON 逐行 JSON，末帧 `done:true` |
| llama.cpp | `GET /v1/models`（老的构建退 `GET /props`） | `POST /v1/chat/completions` | 标准 SSE `data: {...}` |

两种原生协议都由服务端**翻译成标准 OpenAI 格式**输出，所以客户端只认 OpenAI 一套即可。
采样参数也做了映射：Ollama 把 `temperature` / `top_p` 塞进顶层 `options`，
`max_tokens` → `num_predict`；llama.cpp 直接用 OpenAI 的字段名。
token 计数从 Ollama 的 `prompt_eval_count` / `eval_count` 映射到
`prompt_tokens` / `completion_tokens`。

**只连已运行的服务，不代管进程**：Ollama 请自行 `ollama serve`，
llama.cpp 请自行 `llama-server -m <模型> --port 8080`。
本服务不会启动或停止它们。未启用 / 连不上时会返回可读错误
（`local_backend_error`），不会伪装成模型故障。

> **代理绕行（重要）**：本机若设置了 `HTTP_PROXY` / `HTTPS_PROXY` 而
> `NO_PROXY` 为空，`urllib` 会把 `127.0.0.1:11434` 这类回环请求也丢给代理，
> 表现为莫名其妙的 `HTTP 502 upstream connect failed`。因此
> `local_backends.py` 内部统一使用 `ProxyHandler({})` 构造的 opener，
> **完全忽略代理环境变量**。若你迁移到别的机器后本地后端连不上，先查这一项。

**探测（探测按钮 / 打开页面时自动）**会重试：Ollama 刚启动时端口已监听但 HTTP
层未就绪，首次请求容易超时；而对「连接被拒绝」则**不重试**（那是确定结论，
重试只会白等）。所以冷启动能被等到，服务没开也能秒回。

能力范围：**服务框架 + 账号池调度（选号轮转 / 错误计数 / 冷却 / 清理）+
本地模型接入（Ollama / llama.cpp，含流式）+ 鉴权 + 状态统计**均已实现。
**云端模型**的真实转发需要按各平台私有协议签名，本项目以签到为主，
故留出接入点 `_default_forward()`，调用时会明确返回 `501 not_implemented`，
不会伪装成模型故障。

本地模型的地址与开关保存在 `logs/gui_settings.json`（**不写共享的
`config.json`**，避免影响命令行版与计划任务）。

---

## 设备标识重置

对齐参考项目的两个动作，均**先扫描、后执行**，每个改动文件自动备份到
`%APPDATA%\TraeBuddyDeviceReset\backups\<时间戳>\`：

| 动作 | 覆盖范围 |
| --- | --- |
| **执行 6 层重置** | ① machineid ② storage.json telemetry ③ storage.json aha.device ④ TinyStorage / Local Storage ⑤ 注册表 MachineGuid（可选，需管理员）⑥ webview / Network 追踪数据 |
| **重置设备码** | 轮换 machineid + telemetry，并清除 storage.json 登录凭据键（强制登出） |

> ⚠️ 执行前请**完全关闭 Trae 客户端**；重置后需重新登录账号。
> 注册表项默认**不勾选**，勾选后需要管理员权限，失败也不影响其余各层。

---

## 与原项目的关系

GUI **不重写任何签到逻辑**，全部通过 `app/services/runner.py` 调用原模块：

| 原模块 | 被复用的部分 |
| --- | --- |
| `checkin.py` | `load_config` / `load_state` / `save_state` / `_append_records` / `_gate_skip` / `cmd_install` / `cmd_uninstall` / `cmd_settime` |
| `tw_checkin.py` | `_run_account`（Playwright 持久化会话）、`_display_name` |
| `tc_checkin.py` | `collect_accounts`、`_checkin_one` |
| `wb_checkin.py` | `_collect_accounts`、`_checkin_one`、`_expiry_warning` |
| `runtime.py`（新增） | `app_dir` / `self_command` / `is_frozen` —— 让同一套代码在脚本模式与 exe 模式下都能正确定位根目录、重启自身 |

数据文件共用同一份，所以 **GUI 与计划任务不会打架**：

```
config.json                 平台开关 / 账号 / 每日时间 / 运行选项
logs/state.json             今日签到状态（GUI 与计划任务共用判重依据）
logs/history.jsonl          签到流水
logs/签到记录.csv           导出记录（utf-8-sig）
logs/checkin_YYYY-MM-DD.log 运行日志
tokens_workbuddy.json       WorkBuddy 已保存令牌
logs/tokens_traecn.json     TraeCN IDE 令牌缓存
profiles/traework/<profile> TraeWork 独立浏览器会话
```

### 自定义签到记录的存放位置

默认 `logs/签到记录.csv` 写在数据目录里。如果你希望另外在**桌面、网盘同步盘、共享盘**
也留一份（方便随时打开或随云盘同步），可以在 **系统设置 → 签到记录导出位置** 里：

- 开启「启用自定义导出位置」；
- 点「选文件夹」选一个目录（会在该目录下生成 `签到记录.csv`），或点「指定文件名」
  直接给一个完整的 `.csv` 路径（文件名任意）；
- 点「保存设置」后，每次签到都会**自动同步一份**到这个位置。

> ⚠️ 这是一个**额外副本**：原来数据目录里的那份始终保留，各页面/命令行读的都是它。
> 这样即使目标盘被拔掉、网盘没同步、路径没权限，也只影响这一份副本，
> 不会让主记录「凭空消失」。想一次性拿过去看，也可以点「立即导出一份」。

计划任务名仍是 `AutoDailyCheckin`，包含三类触发器：

1. **每日定时** —— 按 `daily_times`（默认 09:00 / 21:00）
2. **开机自启** —— 登录后延迟 60s 执行，错过的时点自动补跑
3. **网络恢复补跑** —— 订阅 `NetworkProfile` 事件，联网瞬间自动补签

配合 `_gate_skip()` 的幂等门控：已全部签完则静默退出，不会重复请求。

---

## 目录结构

```
AutoCheckinGUI/
├─ main.py                    源码入口（DPI、UTF-8、依赖自检、异常落盘）
├─ exe_launcher.py            打包入口（GUI / --run-checkin / --toast 三合一分发）
├─ runtime.py                 运行时抽象（脚本模式 ↔ exe 模式的根目录与自调用）
├─ build_exe.spec             PyInstaller 打包配置
├─ make_installer.py          安装程序生成器（纯 Python）
├─ installer.iss              Inno Setup 脚本（可选，需已安装 Inno Setup）
├─ assets/                    图标与版本信息
├─ 启动GUI.vbs / 启动GUI.bat  源码启动器
├─ app/
│  ├─ application.py          主窗口：装配、路由、主题、账号缓存、线程调度
│  ├─ config.py               与原项目的数据桥（路径解析 + 账号缓存）
│  ├─ theme.py                亮/暗双主题色板 + 字体探测
│  ├─ ui/                     widgets / sidebar / titlebar / topbar / toast
│  │                          + author_card（作者卡片）/ setup_guide（配置导航）
│  │                          + resources（图标资源定位，兼容冻结环境）
│  ├─ pages/                  dashboard / checkin / accounts / credits
│  │                          + api_service / logs / settings
│  └─ services/               runner（签到）· credits（额度）
│                              · scheduler（计划任务，带 TTL 缓存）
│                              · api_server（本地 API）
│                              · device_reset（设备标识重置）
│                              · tray（系统托盘，纯 ctypes）
└─ tests/                     自检脚本（见下）
```

---

## 自检脚本

```bash
python tests/audit_pages.py          # 遍历页面 × 亮/暗主题，报告渲染异常
python tests/diag_pages.py           # 绕开容错直接构造页面，拿完整 traceback
python tests/shoot.py                # 截图到 tests/_shots/（已处理 DPI 缩放）
python tests/verify_frameless.py     # 断言非客户区 0×0、最大化/还原几何、回调异常数
python tests/verify_app_window.py    # 启动真实 App，校验外观 + 托盘 + 最大化内容可见性
python tests/probe_exe_window.py     # 在冻结 exe 上校验同样的项目（推荐发版前跑）
python tests/probe_exe_icon.py       # ★ 校验 exe 窗口**真的有图标**（WM_GETICON 非零）
python tests/probe_exe.py            # 启动 exe 并截图（需已解锁桌面）
python tests/perf_ui.py              # 各页面渲染耗时 / 缩放回弹 / 滚轮帧耗时
python tests/diag_window_style.py    # 打印 STYLE/EXSTYLE 位含义与非客户区尺寸
python tests/test_local_backends.py  # 本地后端协议单测（跑假 Ollama/llama.cpp，24 项）
python tests/test_api_local_e2e.py   # 真起 HTTP 服务 + 真流式端到端（26 项）
```

打包后的完整性自检（**不用装 Python，直接对 exe 跑**）：

```bash
"dist/TraeBuddy签到助手/TraeBuddy签到助手.exe" --selftest   # 打印 10 个关键模块是否可导入
```

用户反馈「某个功能点了没反应」时，先让他跑 `--selftest`，
一眼就能区分「模块没打进去」还是「运行期出错」。

`audit_pages.py` 会同时捕获两类问题：Tkinter 回调内的异常，
以及被页面容错逻辑渲染成「页面加载失败」错误块的情况。
期望输出 **33 项全 OK、0 项 FAIL、0 项 WARN**（约 14 秒）。

`probe_exe_window.py` 在发版前跑一次，期望看到：

```
可见带标题窗口数 = 1
非客户区 0x0
CAPTION=False THICKFRAME=False APPWINDOW=True TOOLWINDOW=False
最大化后 1536x816（屏幕 1536x864）   ← 高度小于屏幕，说明避开了任务栏
RESULT: OK
```

`probe_exe_icon.py` 同样发版前跑一次（**图标只在 exe 上会坏**：
源码运行按源码目录总能找到 `assets/app.ico`，冻结后要找的是
`<exe旁>/_internal/assets/app.ico`；拿不到时 `set_window_icon()` 是**静默 return**，
表现为任务栏/窗口空白图标，不影响任何功能测试）：

```
  OK   dist\TraeBuddy签到助手\TraeBuddy签到助手.exe   pid=... ICON_SMALL/BIG/SMALL2 = ['0x...', ...]
  OK   dist\TraeBuddy签到助手-单文件.exe              pid=... ICON_SMALL/BIG/SMALL2 = ['0x...', ...]
RESULT: OK  2 OK / 0 FAIL
```

⚠️ 找窗口时**不能按 `Popen` 的 pid 匹配**：onefile 的引导进程会再拉一个子进程，
窗口属于子进程（只比父进程 pid 会得到「没找到窗口」的假失败）。
本探针按「窗口类 `TkTopLevel` + 应用标题」枚举，再用
`QueryFullProcessImageNameW` 反查镜像路径确认。

---

## 已知行为

* **手动签到是「优先执行」的** —— 各板块「开始签到」与顶栏/托盘
  「一键签到（全部平台）」都按**手动优先**处理：
  · TraeWork 用浏览器，而 Chromium 的 `user-data-dir` 是**独占**的，
    所以每个账号 profile 有 `.checkin.lock`。**自动化那一轮正在跑**时，
    手动点击原先会**立刻**被判成 `busy`（跳过）→ 一个都没真签，
    只能干等下一轮自动补跑（这就是「滞后性」）。
    现在手动入口会**等对方让出浏览器**（最多 `runner.MANUAL_LOCK_WAIT_S` = 120 秒，
    日志里每 5 秒报一次进度），等到就立刻签到。
    ★ **只等不抢** —— 并发用同一个 `user-data-dir` 会丢登录态甚至损坏 profile。
  · **计划任务 / CLI 不受影响**：不传等待参数 → `wait_lock_s = 0`，
    行为与改动前完全一致。
  · 跑完会明确写「手动优先执行已完成…**无需等待自动补跑**」；
    若仍有待复核/失败项，会提示「可再点一次立即重试，自动补跑仍会兜底」。
  · 点击时若已有任务在跑，会给明确提示（不再**静默无反应**）。
  回归：`tests/test_manual_priority.py`（30 项，含多条正/负对照）。
* **WorkBuddy 账号有自动备份，可一键找回** —— 每个**捕获过**的账号都会自动
  并进 `logs/wb_tokens_archive.json`（**只增不减**，同一 uid 保留令牌更优的一份，
  并记 `first_seen` / `last_seen`）。账号管理页（WorkBuddy）有
  「**⟲ 找回历史账号（N）**」按钮：列出备份里**快照中已缺失**的账号，
  勾选后一键合并回快照。
  为什么需要：`capture_current()` 的写入是「读旧 → 加一条 → 整体覆盖」，
  一次瞬时读失败就会把**其它已保存账号全部清空**（实测发生过：两个账号只剩一个）。
  现在写入路径已加严格读护栏（读不出来就**不写**），归档再兜一层 ——
  误删、清空、换设备都能找回。
  回归：`tests/test_wb_archive_recover.py`（25 项，含正/负对照）。
* **WorkBuddy 昵称/令牌在新版里是加密的，本软件会自动换明文来源** ——
  新版把 `nickname` / `phone` / `accessToken` 都存成
  `{"$wbEncrypted": 1, "envelope": "..."}`（AES-GCM + 服务端密钥，**本地解不开**）。
  本软件**不去硬解**，而是从同一个 auth 目录里的**历史快照**
  （`workbuddy-desktop.<时间戳>.info`，旧格式是**明文**）按 uid 反查：
  昵称/手机号取明文，令牌取**最新的明文 JWT**（实测仍在有效期内）。
  都找不到时退回 uid 前缀并如实说明，**不会把加密内容当令牌发出去**
  （那会得到 401 并被误报成「令牌过期」）。
  回归：`tests/test_wb_encrypted_name.py`（45 项）。
* **API 服务不转发上游模型** —— 服务框架、账号池轮转与鉴权均已就绪，
  但真实模型调用需按平台私有协议签名；调用 `/v1/chat/completions` 会明确
  返回 `501`，不会伪装成模型故障。接入点见 `api_server._default_forward()`。
* **TraeWork 积分不支持自动查询** —— 该平台额度只在浏览器会话内可见，
  看板会如实标注「需在浏览器会话中查看，暂不支持自动查询」，
  不会伪装成查询失败。
* **WorkBuddy 令牌过期** —— 走 `copilot.tencent.com` 接口，令牌失效时
  提示「令牌过期，请重新 capture」，到「账号管理」重新抓取即可。
* **TraeCN 依赖 IDE 日志** —— 令牌从 `%APPDATA%\Trae CN\logs` 提取，
  打开一次 IDE 登录后会自动恢复。
* **设备标识重置需先关客户端** —— 文件被 Trae 占用时可能写入失败，
  界面会逐层报告成功/失败，失败不阻断其余层。
* **作者信息** —— 界面内「作者」入口展示 **Klein**（联系方式 `2571543714@qq.com`），
  署名与联系方式以 XOR 混淆形式存放于 `app/ui/author_card.py`，启动时做完整性自校验。
  「奖励作者」区展示微信收款码：`assets/reward_qr.png`（卡片显示尺寸，整张海报
  等比缩放、完整不裁剪），点击图片用系统看图程序打开原图
  `assets/reward_qr_full.png` 便于放大扫码；弹窗按「可用桌面区」（排除任务栏）
  自适应高度与定位，小屏也不会被任务栏遮住。回归：`tests/probe_author_card.py`。
* **打包版首次启动稍慢** —— 单文件版需解压运行时；追求启动速度请用目录版。
* **重新打包前请先关掉正在运行的 exe** —— 旧实例会占用 `dist\` 里的
  `_tkinter.pyd` / `*.dll`，导致目录删不掉。检查：
  `tasklist | findstr TraeBuddy`，有则 `taskkill /F /PID <pid>`。
