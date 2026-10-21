"""各工作线程共享的运行时状态。

OBS 轮询线程和目录扫描线程写入，叠加窗口和 Web UI 只读。
所有读写都经过同一把锁；读取方拿到的是一份快照（普通 dict），不会被并发修改。
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field


@dataclass
class Status:
    # ---- OBS 连接 ----
    obs_connected: bool = False
    obs_error: str = ""
    obs_version: str = ""
    record_directory: str = ""          # OBS 中配置的录制目录（GetRecordDirectory）

    # ---- 录制状态（GetRecordStatus） ----
    recording: bool = False
    paused: bool = False
    duration_ms: int = 0
    timecode: str = ""
    output_bytes: int = 0               # OBS 报告的已写入字节数
    record_started_at: float = 0.0      # 本程序推算的录制开始时间（epoch 秒）

    # ---- 跳帧（GetStats，已减去录制开始时的基线） ----
    encoder_skipped: int = 0            # 编码过载导致的跳帧（outputSkippedFrames）
    encoder_total: int = 0
    render_skipped: int = 0             # 渲染延迟导致的跳帧（renderSkippedFrames）
    render_total: int = 0
    skipped_partial: bool = False       # True = 本程序启动时录制已在进行，计数不含之前的部分

    # ---- 目录扫描 ----
    file_path: str = ""
    file_size: int = 0
    file_checked_at: float = 0.0
    file_error: str = ""

    updated_at: float = field(default_factory=time.time)


class SharedState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = Status()

    def update(self, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                if not hasattr(self._status, key):
                    raise AttributeError(f"Status 没有字段 {key}")
                setattr(self._status, key, value)
            self._status.updated_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return asdict(self._status)
