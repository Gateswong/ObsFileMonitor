# 配置说明

配置保存在项目根目录的 `config.json`（可用 `--config` 参数或环境变量 `OBS_OVERLAY_CONFIG` 指定其他路径）。
首次启动时自动生成默认配置。推荐通过 Web 配置页面 <http://127.0.0.1:8765/> 修改；
也可以手动编辑 JSON 后重启程序。

**生效时机**：通过配置页面保存后立即生效（OBS 连接会自动重连、叠加窗口立即重绘），
唯一例外是 `web.port`，需要重启程序。

**校验**：配置页面保存时会校验所有字段，不合法时拒绝保存并提示原因。
手动编辑导致 `config.json` 不合法时，程序会以默认配置启动（原文件不会被覆盖，直到你在页面上保存）。

默认值和校验规则的权威来源是 [`obs_overlay/config.py`](../obs_overlay/config.py) 中的 `DEFAULTS` 和 `validate()`。

## `obs` — OBS WebSocket 连接

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `host` | 字符串 | `"127.0.0.1"` | OBS 所在电脑的地址。OBS 在本机时保持默认。 |
| `port` | 整数 1–65535 | `4455` | 与 OBS“WebSocket 服务器设置”中的端口一致。 |
| `password` | 字符串 | `""` | WebSocket 密码，明文保存。OBS 关闭了身份验证时留空。 |
| `poll_interval_sec` | 小数 0.2–10 | `1.0` | 多久向 OBS 请求一次录制状态和跳帧统计。 |

配置页面中的密码框：留空 = 保持原密码；勾选“清除已保存的密码”= 置为空。

## `recording` — 录制文件大小

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `size_source` | `"directory"` / `"websocket"` | `"directory"` | 文件大小的数据来源，见下文。 |
| `directory` | 字符串 | `""` | 录制目录。留空时自动使用 OBS 中设置的录制路径（`GetRecordDirectory`）。 |
| `scan_interval_sec` | 整数 15–30 | `20` | 目录扫描间隔（仅 `directory` 模式）。 |
| `extensions` | 字符串数组 | `.mkv .mp4 .mov .flv .ts .m3u8 .hybrid_mp4` | 视为录制文件的扩展名，不区分大小写。 |

### 两种大小来源怎么选？

| | `directory`（默认，推荐） | `websocket` |
| --- | --- | --- |
| 原理 | 找到录制目录中本次录制创建的最新文件，读取其实际大小 | OBS 报告的 `GetRecordStatus.outputBytes` |
| 显示的是 | **本次录制文件**在磁盘上的大小 | 实测在一些环境中是**本次 OBS 会话所有录制的累计字节数**，一开始录制就已经是好几 GB |
| 刷新 | 每 `scan_interval_sec` 秒（录制开始时立即扫描一次） | 跟随 `poll_interval_sec`（默认 1 秒） |
| 需要配置目录 | 否（默认跟随 OBS），也可手动指定 | 否 |
| 分段录制（自动分割文件） | 只显示当前分段文件的大小 | 累计值 |
| 适用场景 | 绝大多数情况 | 只关心 OBS 本次会话写入了多少数据 |

## `overlay` — 叠加窗口

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | 布尔 | `true` | 是否显示叠加窗口。 |
| `show_when_idle` | 布尔 | `true` | 未录制时是否也显示。设为 `false` 则只在录制时出现。 |
| `click_through` | 布尔 | `true` | 鼠标穿透：点击会落到下面的窗口上。 |
| `exclude_from_capture` | 布尔 | `true` | 不被录进视频：对 OBS 显示器采集、截图工具隐藏，人眼仍可见（Windows 10 2004+）。 |
| `position` | `top-left` / `top-right` / `bottom-left` / `bottom-right` | `top-right` | 窗口位于**主显示器**的哪个角。 |
| `margin_x` | 整数 | `24` | 与屏幕左/右边缘的距离（物理像素）。 |
| `margin_y` | 整数 | `24` | 与屏幕上/下边缘的距离（物理像素）。放在底部时注意避开任务栏。 |
| `font_size` | 整数 8–48 | `14` | 字号（磅）。 |
| `fg_color` | `#RRGGBB` | `#FFFFFF` | 普通文字颜色。 |
| `warn_color` | `#RRGGBB` | `#FF5555` | 警告色：录制中标识、跳帧数 > 0、OBS 未连接、目录扫描出错。 |
| `text_opacity` | 小数 0.1–1.0 | `0.85` | 文字（含阴影）的不透明度。 |
| `shadow` | 布尔 | `true` | 是否给文字加阴影（右下投影 + 1px 描边），在浅色画面上也能看清。 |
| `shadow_color` | `#RRGGBB` | `#202020` | 阴影颜色。 |
| `bg_color` | `#RRGGBB` | `#000000` | 背景方块颜色。 |
| `bg_opacity` | 小数 0–1.0 | `0` | 背景方块的不透明度，与文字不透明度相互独立。`0` = 不显示背景方块，只显示文字。 |

外观组合示例：

| 效果 | 设置 |
| --- | --- |
| 只有带阴影的白字（默认） | `bg_opacity: 0`, `shadow: true` |
| 白字 + 半透明黑色底板 | `bg_opacity: 0.5`, `bg_color: "#000000"` |
| 很淡的文字，不干扰画面 | `text_opacity: 0.5`, `bg_opacity: 0` |

## `rename` — 录制后重命名

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | 布尔 | `true` | 停止录制后是否把文件重命名为 `<原文件名> <应用标题> [<配置文件名>].<扩展名>`。 |
| `delay_sec` | 整数 5–600 | `30` | 停止录制后等待多少秒再重命名。开启了 OBS“自动重新封装”且录像很长时可以调大。 |

重命名使用 `recording.directory`（为空时用 OBS 的录制目录）和 `recording.extensions` 查找文件，
与 `recording.size_source` 选哪种无关。规则详见 [README](../README.md#录制后重命名)。

## `web` — 配置页面

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `port` | 整数 1–65535 | `8765` | 配置页面端口。修改后需重启程序。页面固定只监听 `127.0.0.1`。 |

## 完整示例

```json
{
  "obs": { "host": "127.0.0.1", "port": 4455, "password": "your-password", "poll_interval_sec": 1.0 },
  "recording": {
    "size_source": "directory",
    "directory": "",
    "scan_interval_sec": 20,
    "extensions": [".mkv", ".mp4"]
  },
  "overlay": {
    "enabled": true, "show_when_idle": false, "click_through": true, "exclude_from_capture": true,
    "position": "bottom-right", "margin_x": 24, "margin_y": 72,
    "font_size": 13, "fg_color": "#FFFFFF", "warn_color": "#FF5555",
    "text_opacity": 0.9, "shadow": true, "shadow_color": "#202020",
    "bg_color": "#000000", "bg_opacity": 0.4
  },
  "rename": { "enabled": true, "delay_sec": 30 },
  "web": { "port": 8765 }
}
```
