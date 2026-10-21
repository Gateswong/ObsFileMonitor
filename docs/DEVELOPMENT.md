# 开发指南

本文按实现顺序讲解这个工具是怎么一步步搭起来的，便于理解代码、自己修改或扩展。
各模块的设计细节见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 开发环境

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
.venv\Scripts\python.exe -m obs_overlay --debug --open-config
```

运行单元测试（不需要 OBS，也不会打开窗口）：

```bash
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 分步实现

### 第 1 步：确认数据从哪里来

需求中的三项数据，先在 obs-websocket 协议文档里逐一找到来源：

- **时长**：`GetRecordStatus.outputDuration`（毫秒）；
- **跳帧**：`GetStats` 的 `outputSkippedFrames`（编码）与 `renderSkippedFrames`（渲染），都是 OBS 启动以来的累计值；
- **大小**：协议中 `GetRecordStatus.outputBytes` 看起来就是已写入字节数，0.1.0 版本曾把它作为默认来源。
  但实测发现它在一些环境中是**本次 OBS 会话所有录制的累计值**，一开始录制就已经有好几 GB，
  不符合“本次录制文件大小”的需求。因此改回需求最初的方案——扫描录制目录读取文件实际大小，
  `outputBytes` 保留为可选模式（对比见 [CONFIGURATION.md](CONFIGURATION.md#两种大小来源怎么选)）。
  教训：协议字段的含义要用真实数据验证，不能只看名字。

可以先用几行代码在交互式环境中验证：

```python
import obsws_python as obs
c = obs.ReqClient(host="127.0.0.1", port=4455, password="你的密码", timeout=3)
print(c.send("GetRecordStatus", raw=True))
print(c.send("GetStats", raw=True))
```

### 第 2 步：配置（`config.py`）

- `DEFAULTS` 定义所有配置项和默认值；
- `validate()` 负责类型转换和范围检查，所有入口（读文件、Web 保存）都走它；
- `ConfigStore` 加锁、原子写文件（先写 `.tmp` 再替换）、保存后通知订阅者。

### 第 3 步：共享状态（`state.py`）

一个 `Status` dataclass 列出叠加窗口和 Web 页面需要的全部字段，`SharedState` 用锁保护它。
先定义好状态，后面的数据采集模块只管“写字段”，显示模块只管“读快照”，彼此解耦。

### 第 4 步：OBS 轮询（`obs_client.py`）

- `ObsPoller.run()` 是一个“连接 → 循环轮询 → 出错则断开并等待重试”的外层循环；
- 配置变更时设置 `_reconnect` 事件，内层循环退出并用新参数重连；
- `SkipTracker` 把累计跳帧换算成本次录制的值，是纯逻辑类，单独做了单元测试；
- `describe_error()` 把各类异常翻译成中文提示，显示在叠加窗口和配置页面。

### 第 5 步：目录扫描（`file_monitor.py`）

- `find_latest_recording()` 与 `read_size()` 是纯函数，便于测试；
- `DirectoryScanner` 线程负责“什么时候扫”：周期到了、新录制开始、配置变化。

### 第 6 步：叠加窗口（`overlay.py`）

先用 tkinter 做出普通窗口，再逐步加上：无边框 → 置顶 → 半透明 → Win32 鼠标穿透 → DPI 适配。
之后按需求改成“没有背景方块、只有带阴影的文字”：文字窗口用色键透明（`-transparentcolor`）去掉背景，
另开一个背景窗口实现可选的半透明底板，两者的不透明度可分别设置（原理见 [ARCHITECTURE.md](ARCHITECTURE.md#叠加窗口)）。
数据刷新使用 `root.after(500, ...)` 定时器，在主线程读 `SharedState` 快照。

实现中遇到的坑之一：窗口首次映射前用 `geometry()` 设置的位置会丢失，而 `geometry()` 仍返回设置值，
窗口因此停在 (0, 0)。改为用 Win32 `GetWindowRect` 获取真实位置来判断。
后来又发现，即使发现位置不对再调用 `geometry()`，Tk 也可能因为“认为已在目标位置”而不移动窗口
（修改位置/边距只对背景窗口生效）。最终方案：真实位置仍不对时直接调用 Win32 `SetWindowPos`。

实现中遇到的坑之二：Tk 会在窗口映射和设置 `-alpha`/`-topmost` 时重写扩展窗口样式，
导致一开始设置的 `WS_EX_TRANSPARENT` 被清掉、鼠标穿透失效。解决方法是在每次刷新时检查并恢复样式。

### 第 7 步：Web 配置页面（`web_ui.py` + `web/index.html`）

- 后端用 `http.server`，路由写在 `do_GET` / `do_POST` 中；
- 前端是一个纯 HTML + 原生 JS 页面：读取 `/api/config` 填表，提交时整份配置 POST 回去；
- 每 2 秒拉取 `/api/status` 显示运行状态，方便排查连接问题；
- 密码采用“留空保持不变”的约定，避免把密码回传到浏览器。

### 第 8 步：录制后重命名（`renamer.py`）

先用只读请求在真实 OBS 中确认数据格式：`GetSceneItemList` 的来源类型（`game_capture`、`window_capture`、
`monitor_capture`），以及 `GetInputSettings` 中 `window` 字段的 `标题:类名:进程名` 格式。然后：

- 纯函数 `parse_window_title` / `pick_app_title` / `build_new_stem` / `unique_path` 和 `SceneTracker` 先写好并单元测试；
- `ObsPoller` 在录制开始时记录配置文件，录制中累计场景时长，录制停止时确定应用标题并提交 `RenameJob`；
- `RenameWorker` 按到期时间执行任务，文件被占用则重新入队；
- `file_monitor.list_recordings()` 从原来的 `find_latest_recording()` 中提取出来，供两者共用。

### 第 9 步：入口（`__main__.py`）

创建 `ConfigStore`、`SharedState`，启动三个后台线程，最后在主线程运行叠加窗口。

## 测试

`tests/test_core.py` 与 `tests/test_renamer.py` 覆盖不依赖 OBS 和图形界面的逻辑：

| 测试类 | 覆盖内容 |
| --- | --- |
| `FormattingTest` | 字节、时长、跳帧百分比格式化 |
| `ConfigTest` | 默认值合法（默认无背景、目录扫描）、非法值被拒绝、扩展名规范化、保存/读取/变更通知 |
| `ColorKeyTest` | 透明色键与阴影色只差 1 |
| `PasswordHandlingTest` | 密码不回传、“留空保持不变”合并逻辑 |
| `SkipTrackerTest` | 基线差值、停止后保留、中途接入标记 `*`、OBS 重启重建基线 |
| `FileMonitorTest` | 选取最新文件、按录制开始时间过滤、读取正在写入文件的大小、目录不存在 |
| `ParseWindowTitleTest` | 真实 OBS 的 `window` 值、`#3A`/`#22` 转义、尾随空格、空值 |
| `PickAppTitleTest` | 游戏采集 → 窗口采集 → 显示器采集的优先级、来源顺序、隐藏来源、分组、无窗口的游戏采集 |
| `BuildNewStemTest` | 完整文件名、缺少部分时无双空格、非法字符替换 |
| `SceneTrackerTest` | 最长场景 |
| `RunJobTest` | 改名、只处理录制时间窗口内的文件、分段 + remux 副本、不覆盖、文件被占用时重试且不重复追加、找不到文件 |
| `RenameWorkerTest` | 到期任务被执行并回报结果 |

与 OBS 的联调需要手动进行，建议的检查清单：

1. OBS 未运行 → 叠加窗口显示“OBS 未连接”，配置页面显示原因；
2. 启动 OBS → 数秒内自动连上，显示“未录制”；
3. 密码错误 → 显示“身份验证失败”；
4. 开始录制 → 时长每秒增加；大小从接近 0 开始，每 15–30 秒更新一次，与资源管理器中本次录制文件的大小一致；
5. 暂停 / 继续 → 状态行切换为“⏸ 已暂停”；
6. 停止录制 → 保留最终数值；再次录制 → 跳帧数归零重新计数；
7. 依次切换四个角、修改边距和字号 → 文字窗口与背景窗口都移动到正确位置，且不超出屏幕；
8. 背景不透明度设为 0 → 只有带阴影的文字；设为 0.5 → 出现半透明底板，文字仍在底板之上；
9. 修改透明度、颜色并保存 → 叠加窗口立即变化；
10. 鼠标点击叠加窗口区域 → 点击落到下层窗口；
11. OBS 添加“显示器采集”源 → 预览画面中看不到叠加窗口，屏幕上能看到；
12. 在游戏场景录制约 1 分钟、中途切到其他场景几秒后停止 → 约 30 秒后文件变为 `<原名> <游戏窗口标题> [<配置文件>].<扩展名>`；
13. 主要在“显示器采集”场景录制 → 应用标题为 `Display Capture`；
14. 停止后 30 秒内开始下一次录制 → 只有上一个文件被重命名。

## 如何添加一个新的显示项

以“显示 OBS CPU 占用”为例（`GetStats.cpuUsage`）：

1. `state.py`：在 `Status` 中添加字段 `cpu_usage: float = 0.0`；
2. `obs_client.py`：在 `_poll()` 中 `changes["cpu_usage"] = stats["cpuUsage"]`；
3. `overlay.py`：在 `ROWS`、`LABELS` 中添加 `"cpu"`，在 `render_rows()` 中生成文本；
4. （可选）`web/index.html`：在 `refreshStatus()` 的 `rows` 中添加一行；
5. 更新 README 中的显示项表格。

## 代码约定

- 注释、日志、界面文字使用中文；标识符使用英文；
- 只在 `overlay.py`（主线程）中调用 Tk；
- 后台线程之间不直接调用对方的方法，只通过 `ConfigStore` / `SharedState` / `Event` 交互；
- 新增的纯逻辑尽量写成不依赖 OBS、文件系统之外资源的函数，并补充单元测试。
