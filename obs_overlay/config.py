"""配置的加载、校验与保存。

配置以 JSON 形式保存在项目根目录的 ``config.json``（可通过环境变量
``OBS_OVERLAY_CONFIG`` 指定其他路径）。所有模块都通过 :class:`ConfigStore`
读取配置；Web UI 保存配置后，会通知已注册的监听者（例如 OBS 轮询线程需要重连、
叠加窗口需要重新布局）。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = Path(os.environ.get("OBS_OVERLAY_CONFIG", PROJECT_ROOT / "config.json"))

# 每个配置项的默认值。字段含义见 docs/CONFIGURATION.md。
DEFAULTS: dict[str, Any] = {
    "obs": {
        "host": "127.0.0.1",
        "port": 4455,
        "password": "",
        "poll_interval_sec": 1.0,
    },
    "recording": {
        # "directory"：扫描录制目录，读取本次录制文件的实际大小
        # "websocket"：GetRecordStatus 返回的 outputBytes（实时，但在部分 OBS 版本中
        #              是本次 OBS 会话所有录制的累计值，而不是当前文件的大小）
        "size_source": "directory",
        # 为空时自动使用 OBS 的 GetRecordDirectory 结果
        "directory": "",
        "scan_interval_sec": 20,
        "extensions": [".mkv", ".mp4", ".mov", ".flv", ".ts", ".m3u8", ".hybrid_mp4"],
    },
    "overlay": {
        "enabled": True,
        # top-left / top-right / bottom-left / bottom-right
        "position": "top-right",
        "margin_x": 24,
        "margin_y": 24,
        "font_size": 14,
        # 文字（含阴影）的不透明度
        "text_opacity": 0.85,
        "fg_color": "#FFFFFF",
        "warn_color": "#FF5555",
        "shadow": True,
        "shadow_color": "#202020",
        # 背景方块；不透明度为 0 时不显示背景，只显示文字
        "bg_color": "#000000",
        "bg_opacity": 0.0,
        "click_through": True,
        # 不被 OBS 显示器采集录进视频（Windows 10 2004+）
        "exclude_from_capture": True,
        # 未在录制时是否仍显示叠加窗口
        "show_when_idle": True,
    },
    "rename": {
        # 录制停止后，把文件重命名为 "<原文件名> <应用标题> [<配置文件名>].<扩展名>"
        "enabled": True,
        # 录制停止后等待多少秒再重命名（给 OBS 关闭文件、remux 留出时间）
        "delay_sec": 30,
    },
    "web": {
        # 配置页面固定只监听 127.0.0.1，这里只能改端口
        "port": 8765,
    },
}

POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")
SIZE_SOURCES = ("websocket", "directory")


class ConfigError(ValueError):
    """配置值不合法。"""


def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 递归合并进 base 的副本；未知的键会被忽略。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key not in result:
            continue
        if isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _is_hex_color(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return False
    try:
        int(value[1:], 16)
    except ValueError:
        return False
    return True


def validate(cfg: dict, check_paths: bool = True) -> dict:
    """校验并规范化配置，返回新的字典；不合法时抛出 :class:`ConfigError`。

    ``check_paths=False`` 时跳过录制目录是否存在的检查（启动加载时使用，
    以免外接硬盘未挂载等情况导致整份配置被丢弃）。
    """
    cfg = _deep_merge(DEFAULTS, cfg)
    obs, rec, ov, web, ren = cfg["obs"], cfg["recording"], cfg["overlay"], cfg["web"], cfg["rename"]

    try:
        obs["port"] = int(obs["port"])
        obs["poll_interval_sec"] = float(obs["poll_interval_sec"])
        rec["scan_interval_sec"] = int(rec["scan_interval_sec"])
        ov["margin_x"] = int(ov["margin_x"])
        ov["margin_y"] = int(ov["margin_y"])
        ov["text_opacity"] = float(ov["text_opacity"])
        ov["bg_opacity"] = float(ov["bg_opacity"])
        ov["font_size"] = int(ov["font_size"])
        web["port"] = int(web["port"])
        ren["delay_sec"] = int(ren["delay_sec"])
    except (TypeError, ValueError) as e:
        raise ConfigError(f"数值字段格式错误：{e}") from e

    for name, port in (("obs.port", obs["port"]), ("web.port", web["port"])):
        if not 1 <= port <= 65535:
            raise ConfigError(f"{name} 必须在 1-65535 之间")
    if not 0.2 <= obs["poll_interval_sec"] <= 10:
        raise ConfigError("obs.poll_interval_sec 必须在 0.2-10 秒之间")
    if not 15 <= rec["scan_interval_sec"] <= 30:
        raise ConfigError("recording.scan_interval_sec 必须在 15-30 秒之间")
    if rec["size_source"] not in SIZE_SOURCES:
        raise ConfigError(f"recording.size_source 必须是 {SIZE_SOURCES} 之一")
    if check_paths and rec["directory"] and not Path(rec["directory"]).is_dir():
        raise ConfigError(f"录制目录不存在：{rec['directory']}")
    if isinstance(rec["extensions"], str):
        rec["extensions"] = [e.strip() for e in rec["extensions"].split(",") if e.strip()]
    rec["extensions"] = [e.lower() if e.startswith(".") else "." + e.lower() for e in rec["extensions"]]
    if ov["position"] not in POSITIONS:
        raise ConfigError(f"overlay.position 必须是 {POSITIONS} 之一")
    if not 0.1 <= ov["text_opacity"] <= 1.0:
        raise ConfigError("overlay.text_opacity 必须在 0.1-1.0 之间")
    if not 0.0 <= ov["bg_opacity"] <= 1.0:
        raise ConfigError("overlay.bg_opacity 必须在 0-1.0 之间")
    if not 8 <= ov["font_size"] <= 48:
        raise ConfigError("overlay.font_size 必须在 8-48 之间")
    for key in ("bg_color", "fg_color", "warn_color", "shadow_color"):
        if not _is_hex_color(ov[key]):
            raise ConfigError(f"overlay.{key} 必须是 #RRGGBB 格式的颜色")
    for key in ("enabled", "click_through", "exclude_from_capture", "show_when_idle", "shadow"):
        ov[key] = bool(ov[key])
    ren["enabled"] = bool(ren["enabled"])
    if not 5 <= ren["delay_sec"] <= 600:
        raise ConfigError("rename.delay_sec 必须在 5-600 秒之间")
    return cfg


Listener = Callable[[dict, dict], None]


class ConfigStore:
    """线程安全的配置容器。"""

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._listeners: list[Listener] = []
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            data = copy.deepcopy(DEFAULTS)
            self._write(data)
            return data
        with self.path.open(encoding="utf-8") as f:
            raw = json.load(f)
        try:
            return validate(raw, check_paths=False)
        except ConfigError:
            # 配置文件被手工改坏时退回默认值，避免程序无法启动（Web UI 中可重新保存）
            return copy.deepcopy(DEFAULTS)

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def get(self) -> dict:
        """返回当前配置的深拷贝，调用方可以随意修改而不影响存储。"""
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, new_cfg: dict) -> dict:
        """校验、保存并广播新配置；返回规范化后的配置。"""
        cfg = validate(new_cfg)
        with self._lock:
            old = self._data
            self._data = cfg
            self._write(cfg)
            listeners = list(self._listeners)
        for listener in listeners:
            listener(copy.deepcopy(old), copy.deepcopy(cfg))
        return copy.deepcopy(cfg)

    def subscribe(self, listener: Listener) -> None:
        """注册配置变更回调 ``listener(old_cfg, new_cfg)``。回调在保存配置的线程中执行。"""
        with self._lock:
            self._listeners.append(listener)
