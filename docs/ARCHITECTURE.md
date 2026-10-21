# 架构设计

## 总览

```
                    ┌──────────────────────────────┐
                    │          ConfigStore          │  config.json
                    │  读写 / 校验 / 变更通知        │◄──────────────┐
                    └──────┬─────────────┬─────────┘               │ POST /api/config
             订阅变更 │             │ 订阅变更                       │
        ┌────────────▼──┐   ┌──────▼──────────┐         ┌──────────┴─────────┐
OBS ◄──►│  ObsPoller    │   │ DirectoryScanner │         │     WebServer       │◄── 浏览器
WebSocket│ (线程)        │   │ (线程)            │         │ (线程, http.server) │
        └──────┬────────┘   └──────┬──────────┘         └──────────┬─────────┘
               │ 写                 │ 写                  读 GET /api/status
               ▼                    ▼                              │
            ┌────────────────────────────────────────────────────┴┐
            │                     SharedState                       │
            │        (Status dataclass + 锁, 读取方拿快照)           │
            └───────────────────────────┬──────────────────────────┘
                                        │ 每 500ms 读快照
                                ┌───────▼────────┐
                                │    Overlay      │  主线程 (tkinter)
                                └────────────────┘
```

## 线程模型

| 线程 | 模块 | 职责 |
| --- | --- | --- |
| 主线程 | `overlay.py` | tkinter 叠加窗口。Tk 不是线程安全的，所有 Tk 调用都在这里进行。 |
| `ObsPoller` | `obs_client.py` | 连接 OBS，按 `poll_interval_sec` 轮询，断线后每 3 秒重连。 |
| `DirectoryScanner` | `file_monitor.py` | 每秒检查一次是否需要扫描；仅在 `directory` 模式且正在录制时工作。 |
| `WebServer` | `web_ui.py` | `ThreadingHTTPServer`，每个请求一个线程。 |
| `RenameWorker` | `renamer.py` | 持有按到期时间排序的重命名任务队列，到期后执行；文件被占用时重新入队。 |

线程之间只通过三样东西交互：

1. **`ConfigStore`**：带锁；`get()` 返回深拷贝。`update()` 保存后同步调用所有订阅者的回调
   `listener(old, new)`。回调运行在保存配置的线程（即 Web 请求线程）中，所以回调里只设置
   `threading.Event` 标志位，真正的处理由各自线程完成。
2. **`SharedState`**：带锁；`update(**fields)` 写，`snapshot()` 读出一份 dict。
3. **`quit_event`**：Web 页面“退出程序”或 Ctrl+C 设置它，叠加窗口的定时器检测到后销毁窗口，
   `main()` 随后停止其他线程。其他线程都是 daemon 线程，不会阻止进程退出。

## 数据来源：OBS WebSocket v5

协议文档：<https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md>
客户端库：[obsws-python](https://github.com/aatikturk/obsws-python)（只用它的 `ReqClient.send(..., raw=True)`，
直接拿协议原始字段，便于对照官方文档）。

| 请求 | 何时调用 | 使用的字段 |
| --- | --- | --- |
| `GetVersion` | 连接成功后一次 | `obsVersion`, `obsWebSocketVersion` |
| `GetRecordDirectory` | 连接后、每次录制开始时 | `recordDirectory` |
| `GetRecordStatus` | 每次轮询 | `outputActive`, `outputPaused`, `outputDuration`(ms), `outputTimecode`, `outputBytes` |
| `GetStats` | 每次轮询 | `outputSkippedFrames`, `outputTotalFrames`, `renderSkippedFrames`, `renderTotalFrames` |
| `GetProfileList` | 每次录制开始时 | `currentProfileName` |
| `GetCurrentProgramScene` | 录制期间每次轮询 | `currentProgramSceneName`（旧版本为 `sceneName`） |
| `GetSceneItemList` | 录制停止时 | `sceneItems[]`：`sourceName`, `inputKind`, `sceneItemIndex`, `sceneItemEnabled`, `isGroup` |
| `GetGroupSceneItemList` | 录制停止时，场景中有分组时 | 同上 |
| `GetInputSettings` | 录制停止时，对游戏采集/窗口采集 | `inputSettings.window` |

### 为什么用轮询而不是事件订阅？

obs-websocket 支持事件推送（如 `RecordStateChanged`），但录制时长、字节数、跳帧数都**没有**对应的推送事件，
无论如何都要轮询。只用轮询可以让连接逻辑保持单线程、简单可靠；1 秒一次的两个小请求对 OBS 几乎没有开销。

### 两种跳帧的含义

| 显示名 | OBS 字段 | OBS 界面中的对应 | 原因 |
| --- | --- | --- | --- |
| 编码跳帧 | `outputSkippedFrames` | 状态栏/统计窗口“因编码延迟丢帧” | 编码器跟不上（CPU/GPU 编码负载过高、预设太慢） |
| 渲染跳帧 | `renderSkippedFrames` | 统计窗口“因渲染延迟丢帧” | GPU 渲染场景跟不上（游戏占满 GPU、滤镜过多） |

这两个计数都是 **OBS 启动以来的累计值**，并且包含推流等其他输出的影响。
`SkipTracker`（`obs_client.py`）负责把它换算成“本次录制以来”的值：

1. 观察到 `outputActive` 从 false 变 true 时，记录当前计数为**基线**；
2. 录制期间显示 `当前值 − 基线`；
3. 录制停止后保留最后的结果，直到下次录制开始；
4. 首次观察到录制时 `outputDuration > 5 秒` → 说明录制在本工具启动前就开始了，基线不准确，标记 `skipped_partial`（界面显示 `*`）；
5. 计数比基线还小（OBS 重启）→ 重建基线；
6. 断线重连后重建基线（断线期间的数据未知）。

> 没有使用 `GetOutputStatus` 中录制输出的 `outputSkippedFrames`：对文件输出而言它统计的是“网络丢帧”，始终为 0，
> 而且录制输出的名字（`simple_file_output` / `adv_file_output`）随 OBS 输出模式变化。

## 数据来源：录制目录扫描

`file_monitor.py` 中的 `find_latest_recording()`：

1. 列出录制目录（不递归）中扩展名匹配的文件；
2. 只保留**创建时间** ≥ 录制开始时间 − 10 秒 的文件（录制开始时间 = 观察到录制开始的时刻 − `outputDuration`）；
3. 取创建时间最新的一个。

使用创建时间而不是修改时间，是因为 NTFS 上正在被写入的文件，其目录项中的修改时间可能长时间不更新。

`read_size()` 先**打开文件再 `fstat`** 读取大小，而不是直接 `os.stat()`：Windows 上 `os.stat()` 可能返回目录项中缓存的大小，
对正在写入的文件会明显滞后。OBS 以允许共享读取的方式打开录制文件，只读打开不会影响录制。

扫描时机：每 `scan_interval_sec` 秒；检测到新录制开始时立即扫描；没找到文件时 2 秒后重试；配置保存后立即扫描。

## 录制后重命名

```
ObsPoller（每次轮询）                                  RenameWorker
  录制开始 ─ GetProfileList → 记录配置文件
           SceneTracker.start()
  录制中   ─ GetCurrentProgramScene
           两次轮询的间隔计入当前场景（暂停时不计）
  录制停止 ─ SceneTracker.longest() → 最长场景
           GetSceneItemList / GetInputSettings
           → pick_app_title() → 应用标题
           submit(RenameJob, due = 停止时间 + delay_sec) ──►  到期 → run_job()
                                                              文件被占用 → 10 秒后重试（最多 30 次）
                                                              结果写入 SharedState.last_rename
```

应用标题在**录制停止的那一刻**就确定（此时 OBS 一定是连着的），延迟只用于等待文件被释放。

### 应用标题判定（`pick_app_title`）

1. 把场景来源按 OBS 来源列表从上到下排序：`sceneItemIndex` 越大越靠上；跳过已隐藏（`sceneItemEnabled=false`）的来源；
   分组（`isGroup`）原地展开；嵌套的场景不展开。
2. 第一个有窗口标题的 `game_capture` → 标题；
3. 否则第一个有窗口标题的 `window_capture` → 标题；
4. 否则若有 `monitor_capture` → `Display Capture`；
5. 否则省略。

`game_capture` / `window_capture` 的 `window` 设置格式为 `标题:窗口类名:进程名`，字段内的 `:` 编码为 `#3A`、`#` 编码为 `#22`。
例如 `Don't Starve Together:opengles2.0:dontstarve_steam_x64.exe`，OBS 界面中显示为
`[dontstarve_steam_x64.exe]: Don't Starve Together`。`parse_window_title()` 取第一个字段、解码并去掉首尾空白
（有的游戏标题带尾随空格，例如 `MortalShell2  `）。“捕获任意全屏应用程序”模式的游戏采集没有 `window`，会被跳过。

### 找到要改名的文件（`run_job`）

- 本次录制的文件 = 录制目录中扩展名匹配、**创建时间**在 `[开始 − 10 秒, 停止 + 5 秒]` 内的文件。
  这样能覆盖分段录制的所有分段，又不会误伤延迟期间开始的下一次录制。
- 第一次执行时记下这些文件名（不含扩展名）；之后每次执行都处理“同名、任意录制扩展名”的文件，
  从而把录制停止后才生成的 remux 副本（如 `.mkv` → `.mp4`）一起改名。
- 已改名的文件名已变化，不会被重复处理；文件名已以要追加的内容结尾的也会跳过。
- 用 `os.rename`（Windows 上目标存在时失败，不会覆盖），目标已存在时用 `unique_path()` 追加 ` (2)`。
- `PermissionError`（OBS 仍占用、remux 正在读取）→ 其余文件照常处理，本任务 10 秒后重试，最多 30 次。

### 边界情况

| 情况 | 行为 |
| --- | --- |
| 本程序在录制中途启动 | 场景时长只统计启动之后；配置文件名正常 |
| 录制期间 OBS 断开 | 看不到“录制停止”，不重命名（重连时若仍在录制，按新录制重新统计） |
| 延迟期间退出本程序 | 任务丢失，文件保持原名（退出时日志会提示） |
| 找不到文件 | “最近重命名”显示原因，不重试 |

## 叠加窗口

`overlay.py` 使用 tkinter，由**两个**无边框窗口叠在一起组成，这样文字和背景可以分别设置不透明度：

```
  ┌──────────────────────┐  文字窗口 (Tk root)
  │ Canvas: 阴影 + 文字    │  -transparentcolor = 色键 → 只剩文字像素
  │ 其余像素 = 色键颜色    │  -alpha = text_opacity
  └──────────────────────┘
  ┌──────────────────────┐  背景窗口 (Toplevel)，在文字窗口正下方
  │      纯色 bg_color     │  -alpha = bg_opacity；为 0 时隐藏
  └──────────────────────┘
```

| 效果 | 实现 |
| --- | --- |
| 无边框 | `overrideredirect(True)` |
| 置顶 | `attributes("-topmost", True)`，每次刷新都重新设置，再 `lift()` 文字窗口保证它在背景之上 |
| 无背景、只显示文字 | `attributes("-transparentcolor", 色键)`：等于色键的像素完全透明（Tk 内部使用 `SetLayeredWindowAttributes(LWA_COLORKEY)`） |
| 半透明 | `attributes("-alpha", ...)`，文字窗口与背景窗口各自设置 |
| 文字阴影 | Canvas 先用阴影色在偏移位置画若干次同样的文字，再画正文 |
| 鼠标穿透 | Win32 扩展样式 `WS_EX_TRANSPARENT` |
| 不出现在任务栏/Alt+Tab | `WS_EX_TOOLWINDOW` |
| 不抢焦点 | `WS_EX_NOACTIVATE` |
| 不被录进视频 | `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`：DXGI 桌面复制和 Windows Graphics Capture 都会跳过该窗口 |
| 高 DPI 清晰 | 创建 Tk 前调用 `SetProcessDpiAwareness(2)` |

### 色键与抗锯齿毛边

色键抠图只把**完全等于**色键的像素变透明。文字边缘的抗锯齿像素是文字色与背景色（色键）的混合，
不会被抠掉；如果色键选一个醒目的颜色（例如品红），文字周围就会有一圈彩色毛边。
`color_key_for()` 让色键只比阴影色差 1（蓝色分量 ±1），于是毛边颜色与阴影几乎相同，看起来就是阴影的一部分。

### 窗口位置

Tk 自己记录的窗口几何信息（`geometry()` 的返回值）可能与窗口的真实位置不一致：

- 窗口第一次真正显示（映射）之前设置的位置会丢失，但 `geometry()` 仍返回设置值；
- Tk 认为窗口“已经在目标位置”时，再次调用 `wm geometry` 不会真正移动窗口——
  表现为修改位置/边距后只有背景窗口移动，文字窗口留在原处，变宽后还会超出屏幕。

因此 `_place()` 每次刷新时：

1. 计算目标位置，并限制在屏幕范围内（文字再长也不会超出屏幕）；
2. 用 Win32 `GetWindowRect` 读取真实位置，一致则什么都不做；
3. 不一致时先调用 Tk 的 `geometry()`，若真实位置仍不对，直接用 Win32 `SetWindowPos` 移动。

### 窗口样式

注意 `winfo_id()` 返回的是 Tk 内部的子窗口句柄，扩展样式要设置在它的父窗口（`GetParent`）上。
Tk 在窗口映射、修改 `-alpha`/`-topmost` 时会重写扩展样式，所以 `_set_click_through()` 在每次刷新（500ms）时都会检查，
样式被改写才重新设置。显示亲和性（`WDA_EXCLUDEFROMCAPTURE`）同样在每次刷新时检查。

## Web 配置页面

纯标准库 `http.server` + 单个静态 HTML 页面（无前端构建步骤）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 配置页面 |
| GET | `/api/config` | 当前配置；`obs.password` 固定为 `null`，另加 `obs.password_set` |
| POST | `/api/config` | 保存配置。`obs.password` 为 `null` 表示保持不变。校验失败返回 400 `{"error": "..."}` |
| GET | `/api/status` | `SharedState` 快照（字段见 `state.py` 的 `Status`） |
| POST | `/api/test` | 请求体为 `obs` 配置段，返回 `{"ok": bool, "message": str}` |
| POST | `/api/quit` | 退出程序 |

安全措施：

- 只监听 `127.0.0.1`；
- 校验 `Host` 头只能是 `127.0.0.1:<port>` 或 `localhost:<port>`，防止 DNS 重绑定攻击读取配置；
- POST 必须是 `Content-Type: application/json`：其他网站的跨站请求若带这个类型会触发 CORS 预检，本服务不响应预检，因此无法跨站修改配置；
- 密码不回传给浏览器。

## 设计取舍记录

| 决定 | 理由 | 放弃的方案 |
| --- | --- | --- |
| Python + tkinter | 环境已有 Python；tkinter 随 Python 自带，零额外依赖；做一个小信息窗口足够 | Electron（体积大）、C# WPF（需要 .NET 工具链、学习成本） |
| 只依赖 `obsws-python` | WebSocket v5 的鉴权握手自己写也不难，但用成熟库更省心 | 手写 WebSocket 客户端 |
| 配置页面用 `http.server` | 只有几个接口，不值得引入 Flask/FastAPI | Flask |
| 大小默认扫描录制目录 | 实测 `outputBytes` 在一些环境中是本次 OBS 会话所有录制的累计值，而需求要的是本次录制文件的实际大小 | 默认用 `outputBytes`（0.1.0 版本的做法，现作为可选模式保留） |
| 录制停止时立即确定应用标题，延迟只等文件 | 延迟期间 OBS 可能被关闭或场景被修改 | 到期后再查询 OBS |
| 按创建时间区间找文件，而不是只用目录扫描得到的“最新文件” | 能覆盖分段录制和 remux 副本，且与 `size_source` 无关 | 只重命名 `file_path` |
| 两个窗口实现背景 | Tk 的 `-alpha` 作用于整个窗口，文字和背景无法分别设置透明度 | 单窗口 + 统一透明度；`UpdateLayeredWindow` 逐像素 alpha（需要自己渲染位图，复杂度高） |
| 轮询而非事件 | 需要的数据都没有推送事件 | 事件 + 轮询混合 |
| 跳帧做基线差值 | OBS 给的是全局累计值，直接显示会包含录制之前的跳帧 | 直接显示累计值 |
