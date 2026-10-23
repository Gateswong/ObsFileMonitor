# OBS 录制信息叠加（OBS Recording Overlay）

用 OBS 录制视频时，在屏幕角落以**带阴影的半透明文字**（可选半透明背景）实时显示以下信息，窗口置顶、鼠标可穿透：

| 项目 | 数据来源 | 刷新频率 |
| --- | --- | --- |
| 录制状态（录制中 / 暂停 / 未录制 / 未连接） | OBS WebSocket `GetRecordStatus` | 1 秒（可配置） |
| 已录制时长 | OBS WebSocket `GetRecordStatus.outputDuration` | 1 秒 |
| 本次录制文件的大小 | 扫描 OBS 录制目录，读取本次录制文件的实际大小 | 15–30 秒（可配置） |
| 编码跳帧（编码过载） | OBS WebSocket `GetStats.outputSkippedFrames` | 1 秒 |
| 渲染跳帧（渲染延迟） | OBS WebSocket `GetStats.renderSkippedFrames` | 1 秒 |

**录制后自动重命名**：停止录制约 30 秒后，把录制文件重命名为

```
<原文件名> <应用标题> [<配置文件名>].<扩展名>
例：2026-10-21 20-00-00 Don't Starve Together [录制 4K60 单声道].mp4
```

应用标题取自录制期间**显示时间最长的场景**中第一个“游戏采集”所捕获窗口的标题；没有游戏采集时用“窗口采集”；
再没有则用 `Display Capture`（显示器采集）；都没有则省略。详见 [录制后重命名](#录制后重命名)。

另带一个本地 Web 配置页面（<http://127.0.0.1:8765/>），用于填写 WebSocket 连接信息、录制目录、叠加窗口外观等。

> 叠加窗口默认对屏幕捕获隐藏（`WDA_EXCLUDEFROMCAPTURE`），即使 OBS 使用“显示器采集”也**不会**被录进视频，
> 而你自己在屏幕上能正常看到它。详见 [故障排查](docs/TROUBLESHOOTING.md#叠加窗口被录进了视频)。

## 运行要求

- Windows 10 / 11（鼠标穿透、DPI 适配依赖 Win32 API）
- Python 3.10+（需包含 tkinter，python.org 官方安装包默认包含）
- OBS Studio 28+（内置 obs-websocket 5.x）

## 快速开始

### 1. 在 OBS 中启用 WebSocket 服务器

OBS 菜单 **工具 → WebSocket 服务器设置**：

1. 勾选 **开启 WebSocket 服务器**，端口保持默认 `4455`；
2. 建议保留 **开启身份验证**，点击 **显示连接信息** 复制密码。

### 2. 启动本工具

双击 `start.bat`（或在 PowerShell 中运行 `.\start.ps1`）。首次运行会自动创建虚拟环境、安装依赖，并在浏览器中打开配置页面；
`requirements.txt` 变化后会自动重新安装依赖。传给脚本的参数会原样转给程序，例如 `start.bat --open-config`。

也可以手动启动：

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
.venv\Scripts\python.exe -m obs_overlay
```

命令行参数：

| 参数 | 说明 |
| --- | --- |
| `--open-config` | 启动后在浏览器中打开配置页面 |
| `--config PATH` | 使用指定的配置文件（默认：项目根目录 `config.json`） |
| `--debug` | 输出调试日志 |

### 3. 在配置页面填写密码

打开 <http://127.0.0.1:8765/>，在 **OBS WebSocket → 密码** 中粘贴密码，点击 **测试连接**，成功后点击 **保存配置**。
页面顶部的“运行状态”会显示 `✅ OBS 3x.x.x / ws 5.x.x`，叠加窗口的状态行变为“未录制”。

### 4. 开始录制

在 OBS 中开始录制，叠加窗口会显示“● 录制中”以及时长、大小、跳帧数。

### 退出

在配置页面点击 **退出程序**，或在控制台窗口按 `Ctrl+C`。

## 叠加窗口说明

```
状态        ● 录制中
时长        00:12:34
大小        1.82 GB
编码跳帧    3 (0.01%)
渲染跳帧    0 (0.00%)
```

- 跳帧数是**本次录制开始以来**的值（OBS 本身给出的是 OBS 启动以来的累计值，本工具在录制开始时记录基线并相减）。
  括号中是跳帧占同期总帧数的百分比。跳帧数 > 0 时显示为警告色。
- 如果启动本工具时录制已在进行，跳帧数后会带 `*`，表示只统计了工具启动之后的部分。
- 选择“扫描录制目录”作为大小来源时，大小每 15–30 秒（`scan_interval_sec`）更新一次。
- 录制停止后，叠加窗口保留最后一次录制的时长、大小、跳帧数，直到下一次录制开始。

## 录制后重命名

| 部分 | 来源 |
| --- | --- |
| 原文件名、扩展名 | OBS 生成的录制文件，保持不变 |
| 应用标题 | 录制期间显示时间最长的场景（暂停期间不计时）中，从来源列表**最上方**往下找第一个已启用的：<br>1. 游戏采集 → 其所捕获窗口的标题（OBS 中显示为 `[dontstarve_steam_x64.exe]: Don't Starve Together` → `Don't Starve Together`）<br>2. 窗口采集 → 同上<br>3. 显示器采集 → `Display Capture`<br>4. 都没有 → 省略 |
| 配置文件名 | 录制时使用的 OBS 配置文件（Profile），录制期间 OBS 不允许切换 |

- 文件名中不允许的字符（`\ / : * ? " < > |`）会替换成对应的全角字符，多余空格会合并，不会出现双空格。
- 分段录制（自动分割文件）的每个分段、以及 OBS“自动重新封装为 MP4”生成的副本都会一起重命名。
- 目标文件名已存在时追加 ` (2)`，**不会覆盖任何文件**；文件仍被占用时每 10 秒重试一次。
- 配置页面“运行状态”中的“最近重命名”显示计划与结果；可在“录制后重命名”中关闭此功能或修改延迟。
- 如果在延迟期间退出本程序，该次录制不会被重命名。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | 每个配置项的含义、取值范围、默认值 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构、线程模型、数据流、用到的 OBS WebSocket 请求与字段、重命名流程、设计取舍 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 分步实现指南、如何运行测试、如何扩展新的显示项 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 常见问题排查 |
| [CHANGELOG.md](CHANGELOG.md) | 版本更新记录 |

## 项目结构

```
ObsFileMonitor/
├─ obs_overlay/
│  ├─ __main__.py      程序入口，启动各线程
│  ├─ config.py        配置默认值、校验、读写、变更通知
│  ├─ state.py         线程间共享的运行状态
│  ├─ obs_client.py    OBS WebSocket 轮询、跳帧基线计算
│  ├─ file_monitor.py  录制目录扫描
│  ├─ renamer.py       录制后重命名（应用标题判定、延迟重命名线程）
│  ├─ overlay.py       tkinter 叠加窗口（含 Win32 鼠标穿透）
│  ├─ formatting.py    数值格式化
│  ├─ web_ui.py        本地配置页面后端（http.server）
│  └─ web/index.html   配置页面前端
├─ tests/             单元测试（test_core.py、test_renamer.py）
├─ docs/               文档
├─ requirements.txt
├─ start.ps1           启动脚本（创建虚拟环境、安装依赖、启动程序）
└─ start.bat           双击启动入口，只调用 start.ps1
```

## 安全说明

- `config.json` 中以**明文**保存 OBS WebSocket 密码，已加入 `.gitignore`，请勿分享。
- 配置页面只监听 `127.0.0.1`，并校验 Host 头、只接受 JSON 请求，其他网页无法读取或修改配置。
- 配置页面的接口不会把密码返回给浏览器。

## 许可证

[MIT](LICENSE) © 2026 Gateswong
