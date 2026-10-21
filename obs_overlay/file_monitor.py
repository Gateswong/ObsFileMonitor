"""录制目录扫描：找到正在写入的录制文件并读取其大小。

仅在 ``recording.size_source == "directory"`` 时启用。默认每 20 秒扫描一次
（可配置 15-30 秒），检测到新录制开始时会立即扫描一次。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from .config import ConfigStore
from .state import SharedState

log = logging.getLogger(__name__)

TICK_SEC = 1.0
# 文件创建时间与推算出的录制开始时间之间允许的误差
START_SLACK_SEC = 10


def _created_at(st: os.stat_result) -> float:
    # Windows 上 st_birthtime 是创建时间（Python 3.12+）；旧版本中 st_ctime 也表示创建时间
    return getattr(st, "st_birthtime", st.st_ctime)


def find_latest_recording(directory: Path, extensions: list[str], since: float | None) -> Path | None:
    """返回目录中最新创建的视频文件（不递归子目录）。

    ``since`` 不为空时只考虑在该时间点之后创建的文件，避免录制刚开始、
    新文件还没出现时误报上一次录制的文件。
    """
    exts = {e.lower() for e in extensions}
    best: tuple[float, Path] | None = None
    try:
        entries = list(os.scandir(directory))
    except OSError as e:
        raise FileNotFoundError(f"无法读取录制目录 {directory}：{e}") from e
    for entry in entries:
        if not entry.is_file() or Path(entry.name).suffix.lower() not in exts:
            continue
        try:
            created = _created_at(entry.stat())
        except OSError:
            continue
        if since is not None and created < since - START_SLACK_SEC:
            continue
        if best is None or created > best[0]:
            best = (created, Path(entry.path))
    return best[1] if best else None


def read_size(path: Path) -> int:
    """读取文件的当前大小。

    Windows 上 ``os.stat()`` 可能读到目录项中缓存的大小，对于 OBS 正在写入的
    文件会滞后；打开文件句柄后 ``fstat`` 拿到的是实时大小。OBS 以共享读方式
    打开录制文件，所以这里只读打开不会影响录制。
    """
    try:
        with open(path, "rb") as f:
            return os.fstat(f.fileno()).st_size
    except PermissionError:
        return path.stat().st_size


class DirectoryScanner(threading.Thread):
    def __init__(self, store: ConfigStore, state: SharedState) -> None:
        super().__init__(name="DirectoryScanner", daemon=True)
        self.store = store
        self.state = state
        self._stop = threading.Event()
        self._force = threading.Event()
        store.subscribe(lambda old, new: self._force.set())

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        last_scan = 0.0
        last_start = None
        while not self._stop.wait(TICK_SEC):
            cfg = self.store.get()["recording"]
            if cfg["size_source"] != "directory":
                continue
            snap = self.state.snapshot()
            if not snap["recording"]:
                continue
            due = time.monotonic() - last_scan >= cfg["scan_interval_sec"]
            new_recording = snap["record_started_at"] != last_start
            if not (due or new_recording or self._force.is_set()):
                continue
            self._force.clear()
            last_scan = time.monotonic()
            last_start = snap["record_started_at"]
            if not self.scan_once(cfg, snap):
                # 录制刚开始时文件可能还没出现，2 秒后重试而不是等满一个扫描周期
                last_scan -= cfg["scan_interval_sec"] - 2

    def scan_once(self, cfg: dict, snap: dict) -> bool:
        """扫描一次并更新状态；找到文件时返回 True。"""
        directory = cfg["directory"] or snap["record_directory"]
        if not directory:
            self.state.update(file_error="未配置录制目录，且尚未从 OBS 获取到录制目录")
            return False
        try:
            path = find_latest_recording(Path(directory), cfg["extensions"], snap["record_started_at"] or None)
            if path is None:
                self.state.update(file_error="录制目录中尚未找到本次录制的文件", file_checked_at=time.time())
                return False
            self.state.update(
                file_path=str(path), file_size=read_size(path), file_error="", file_checked_at=time.time()
            )
            return True
        except OSError as e:
            log.warning("扫描录制目录失败：%s", e)
            self.state.update(file_error=str(e), file_checked_at=time.time())
            return False
