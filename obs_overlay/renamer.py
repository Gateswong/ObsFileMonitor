"""录制结束后自动重命名录制文件。

新文件名：``<原文件名> <应用标题> [<配置文件名>].<扩展名>``

* 配置文件（Profile）：录制开始时通过 ``GetProfileList`` 记录，录制期间 OBS 不允许切换。
* 应用标题：录制期间**显示时间最长的场景**中，按以下优先级取第一个可用的来源：
  游戏采集的窗口标题 → 窗口采集的窗口标题 → 显示器采集（固定为 "Display Capture"）。
  都没有时省略应用标题。

录制停止后等待 ``rename.delay_sec`` 秒（默认 30 秒）再重命名，给 OBS 关闭文件、
自动重新封装（remux）留出时间。细节见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

import heapq
import itertools
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .file_monitor import list_recordings

log = logging.getLogger(__name__)

GAME_CAPTURE = "game_capture"
WINDOW_CAPTURE = "window_capture"
DISPLAY_CAPTURE = "monitor_capture"
DISPLAY_CAPTURE_TITLE = "Display Capture"

# 文件创建时间相对录制开始/停止时间允许的误差
CREATED_BEFORE_START_SLACK_SEC = 10
CREATED_AFTER_STOP_SLACK_SEC = 5
# 文件被占用（OBS 尚未关闭文件、正在 remux）时的重试策略
RETRY_INTERVAL_SEC = 10
MAX_ATTEMPTS = 30

# Windows 文件名中不允许的字符 → 外观相近的全角字符
_FORBIDDEN_CHARS = str.maketrans({
    "\\": "＼", "/": "／", ":": "：", "*": "＊", "?": "？",
    '"': "＂", "<": "＜", ">": "＞", "|": "｜",
})


# ---------- 纯函数 ----------

def parse_window_title(window: str | None) -> str | None:
    """从游戏采集/窗口采集的 ``window`` 设置中取出窗口标题。

    OBS 以 ``标题:窗口类名:进程名`` 保存，并把字段内的 ``:`` 编码为 ``#3A``、
    ``#`` 编码为 ``#22``。例如
    ``Don't Starve Together:opengles2.0:dontstarve_steam_x64.exe`` → ``Don't Starve Together``。
    """
    if not window:
        return None
    title = window.split(":", 1)[0].replace("#3A", ":").replace("#22", "#").strip()
    return title or None


def flatten_scene_items(
    items: list[dict], get_group_items: Callable[[str], list[dict]] | None = None
) -> list[dict]:
    """按 OBS 来源列表中从上到下的顺序返回已启用的来源，分组会被展开。

    ``GetSceneItemList`` 中 ``sceneItemIndex`` 越大越靠上（越靠前）。
    嵌套的场景不展开。
    """
    result: list[dict] = []
    for item in sorted(items, key=lambda i: i.get("sceneItemIndex", 0), reverse=True):
        if not item.get("sceneItemEnabled", True):
            continue
        if item.get("isGroup"):
            if get_group_items is not None:
                result.extend(flatten_scene_items(get_group_items(item["sourceName"]), get_group_items))
            continue
        result.append(item)
    return result


def pick_app_title(
    items: list[dict],
    get_settings: Callable[[str], dict],
    get_group_items: Callable[[str], list[dict]] | None = None,
) -> str | None:
    """按“游戏采集 → 窗口采集 → 显示器采集”的优先级确定应用标题。

    没有设置窗口的游戏采集（例如“捕获任意全屏应用程序”模式）不算数，继续往下找。
    """
    ordered = flatten_scene_items(items, get_group_items)
    for kind in (GAME_CAPTURE, WINDOW_CAPTURE):
        for item in ordered:
            if item.get("inputKind") != kind:
                continue
            title = parse_window_title(get_settings(item["sourceName"]).get("window"))
            if title:
                return title
    if any(item.get("inputKind") == DISPLAY_CAPTURE for item in ordered):
        return DISPLAY_CAPTURE_TITLE
    return None


def sanitize_filename_part(text: str | None) -> str:
    """把用于文件名的文本中的非法字符换成全角字符，并合并多余空白。"""
    if not text:
        return ""
    text = text.translate(_FORBIDDEN_CHARS)
    text = "".join(ch for ch in text if ch.isprintable())
    return re.sub(r"\s+", " ", text).strip().rstrip(".").strip()


def build_new_stem(stem: str, app_title: str | None, profile: str | None) -> str:
    """``<stem> <app_title> [<profile>]``，缺少的部分直接省略，不会出现双空格。"""
    parts = [stem.rstrip()]
    title = sanitize_filename_part(app_title)
    if title:
        parts.append(title)
    prof = sanitize_filename_part(profile)
    if prof:
        parts.append(f"[{prof}]")
    return " ".join(parts)


def unique_path(path: Path) -> Path:
    """目标已存在时追加 `` (2)``、`` (3)``……，保证不覆盖任何文件。"""
    if not path.exists():
        return path
    for i in itertools.count(2):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise AssertionError("unreachable")


class SceneTracker:
    """累计录制期间每个场景的显示时长。"""

    def __init__(self) -> None:
        self.durations: dict[str, float] = {}

    def start(self) -> None:
        self.durations = {}

    def tick(self, scene: str | None, seconds: float) -> None:
        if scene and seconds > 0:
            self.durations[scene] = self.durations.get(scene, 0.0) + seconds

    def longest(self) -> str | None:
        # 时长相同时取先出现的场景（dict 保持插入顺序，max 返回第一个最大值）
        return max(self.durations, key=self.durations.get) if self.durations else None


# ---------- 重命名任务 ----------

@dataclass
class RenameJob:
    directory: Path
    extensions: list[str]
    started_at: float           # 录制开始时间（epoch 秒）
    stopped_at: float           # 录制停止时间（epoch 秒）
    profile: str | None
    app_title: str | None
    due: float                  # 何时执行（epoch 秒）
    scene: str | None = None    # 时长最长的场景，仅用于日志
    attempts: int = 0
    # 第一次执行时确定的本次录制文件名（不含扩展名）。之后的重试只处理这些文件名的文件，
    # 包括录制停止后才出现的 remux 副本；已经改过名的文件文件名变了，不会被重复处理。
    original_stems: set[str] | None = field(default=None)


@dataclass
class RenameResult:
    renamed: list[tuple[Path, Path]]
    message: str
    retry: bool = False


def run_job(job: RenameJob) -> RenameResult:
    """执行一次重命名尝试。文件被占用时返回 ``retry=True``，其余文件已改名的不会回滚。"""
    job.attempts += 1
    if job.original_stems is None:
        originals = list_recordings(
            job.directory,
            job.extensions,
            job.started_at - CREATED_BEFORE_START_SLACK_SEC,
            job.stopped_at + CREATED_AFTER_STOP_SLACK_SEC,
        )
        job.original_stems = {p.stem for _, p in originals}
        if not originals:
            return RenameResult([], f"未在 {job.directory} 中找到本次录制的文件，未重命名")

    # 不设上限：录制停止后才生成的 remux 副本（同名、扩展名不同）也一起改名
    files = [
        p for _, p in list_recordings(job.directory, job.extensions, job.started_at - CREATED_BEFORE_START_SLACK_SEC)
        if p.stem in job.original_stems
    ]
    # 要追加的部分，例如 "Don't Starve Together [录制 4K60 单声道]"
    suffix = build_new_stem("", job.app_title, job.profile).strip()
    renamed: list[tuple[Path, Path]] = []
    busy: list[str] = []
    for path in files:
        if not suffix or path.stem.endswith(suffix):
            continue  # 没有可追加的内容，或者已经是新文件名
        new_stem = build_new_stem(path.stem, job.app_title, job.profile)
        target = unique_path(path.with_name(new_stem + path.suffix))
        try:
            # os.rename 在 Windows 上目标存在时会失败，不会覆盖文件
            os.rename(path, target)
        except PermissionError:
            busy.append(path.name)
            continue
        renamed.append((path, target))
        log.info("已重命名：%s → %s", path.name, target.name)

    parts = [f"{a.name} → {b.name}" for a, b in renamed]
    if busy:
        if job.attempts < MAX_ATTEMPTS:
            parts.append(f"{'、'.join(busy)} 正被占用，{RETRY_INTERVAL_SEC} 秒后重试")
            return RenameResult(renamed, "；".join(parts), retry=True)
        parts.append(f"{'、'.join(busy)} 一直被占用，放弃重命名")
    if not parts:
        parts.append("没有需要重命名的文件（缺少应用标题和配置文件名）")
    return RenameResult(renamed, "；".join(parts))


class RenameWorker(threading.Thread):
    """按到期时间顺序执行重命名任务的后台线程。"""

    def __init__(self, on_result: Callable[[str], None] | None = None) -> None:
        super().__init__(name="RenameWorker", daemon=True)
        self.on_result = on_result
        self._queue: list[tuple[float, int, RenameJob]] = []
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._stopped = False

    def submit(self, job: RenameJob) -> None:
        with self._cond:
            heapq.heappush(self._queue, (job.due, next(self._seq), job))
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify()

    def pending(self) -> int:
        with self._cond:
            return len(self._queue)

    def run(self) -> None:
        while True:
            with self._cond:
                while not self._stopped:
                    if self._queue and self._queue[0][0] <= time.time():
                        break
                    timeout = self._queue[0][0] - time.time() if self._queue else None
                    self._cond.wait(timeout)
                if self._stopped:
                    return
                _, _, job = heapq.heappop(self._queue)
            self._execute(job)

    def _execute(self, job: RenameJob) -> None:
        try:
            result = run_job(job)
        except Exception as e:  # noqa: BLE001 - 单个任务失败不能让线程退出
            log.exception("重命名失败")
            result = RenameResult([], f"重命名失败：{e}")
        if result.retry:
            job.due = time.time() + RETRY_INTERVAL_SEC
            self.submit(job)
        if self.on_result is not None:
            self.on_result(result.message)
