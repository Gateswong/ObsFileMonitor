"""OBS WebSocket（v5 协议）轮询线程。

每隔 ``obs.poll_interval_sec`` 秒发送两个请求：

* ``GetRecordStatus`` → 是否在录制 / 暂停、已录制时长、已写入字节数
* ``GetStats``        → 编码跳帧、渲染跳帧（OBS 启动以来的累计值）
* ``GetCurrentProgramScene`` → 仅录制期间：累计各场景的显示时长（用于录制后重命名）

录制开始时额外请求 ``GetProfileList`` 记录配置文件名；录制停止时找出显示时间最长的
场景，读取其中的采集来源确定应用标题，然后把重命名任务交给 :class:`RenameWorker`。

连接断开或配置变化时自动重连。协议文档：
https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import obsws_python as obs
from obsws_python.error import OBSSDKError, OBSSDKRequestError

from .config import ConfigStore
from .renamer import RenameJob, RenameWorker, SceneTracker, pick_app_title
from .state import SharedState

log = logging.getLogger(__name__)

# obsws_python 在调试日志里会打印明文密码，且会把每个失败请求的堆栈打到控制台；
# 错误由本模块统一处理并显示到 UI，因此屏蔽它自身的日志。
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)

CONNECT_TIMEOUT_SEC = 3
RETRY_DELAY_SEC = 3
# 第一次观察到录制时，如果已录制超过这么久，就认为录制是在本程序启动前开始的
PARTIAL_THRESHOLD_MS = 5000


def describe_error(exc: BaseException) -> str:
    """把各种连接异常转换成给用户看的中文提示。"""
    text = str(exc)
    if isinstance(exc, ConnectionRefusedError) or "refused" in text.lower():
        return "无法连接：OBS 未运行，或未在 工具 → WebSocket 服务器设置 中启用服务器"
    if "no password provided" in text:
        return "OBS 启用了身份验证，请在配置页填写 WebSocket 密码"
    if "4009" in text or "Authentication" in text or "closed" in text.lower():
        return "身份验证失败：WebSocket 密码错误"
    if isinstance(exc, (TimeoutError, OBSSDKError)) and "timed out" in text.lower():
        return "连接超时：请检查主机地址和端口"
    if isinstance(exc, OSError):
        return f"网络错误：{text}"
    return f"{type(exc).__name__}: {text}"


def connect(host: str, port: int, password: str) -> obs.ReqClient:
    # 必须显式传入 host/port/password，否则 obsws_python 会去读取 ~/config.toml
    return obs.ReqClient(host=host, port=port, password=password, timeout=CONNECT_TIMEOUT_SEC)


def test_connection(obs_cfg: dict) -> tuple[bool, str]:
    """供 Web UI 的“测试连接”按钮使用，返回 (是否成功, 提示信息)。"""
    try:
        client = connect(obs_cfg["host"], int(obs_cfg["port"]), obs_cfg.get("password", ""))
    except Exception as e:  # noqa: BLE001 - 任何异常都要转成提示返回给浏览器
        return False, describe_error(e)
    try:
        v = client.send("GetVersion", raw=True)
        d = client.send("GetRecordDirectory", raw=True)
        return True, (
            f"连接成功：OBS {v['obsVersion']}，obs-websocket {v['obsWebSocketVersion']}，"
            f"录制目录 {d['recordDirectory']}"
        )
    except Exception as e:  # noqa: BLE001
        return False, describe_error(e)
    finally:
        _safe_disconnect(client)


def _safe_disconnect(client) -> None:
    try:
        client.disconnect()
    except Exception:  # noqa: BLE001
        pass


@dataclass
class SkipCounters:
    encoder_skipped: int
    encoder_total: int
    render_skipped: int
    render_total: int

    @classmethod
    def from_stats(cls, stats: dict) -> "SkipCounters":
        return cls(
            encoder_skipped=int(stats.get("outputSkippedFrames", 0)),
            encoder_total=int(stats.get("outputTotalFrames", 0)),
            render_skipped=int(stats.get("renderSkippedFrames", 0)),
            render_total=int(stats.get("renderTotalFrames", 0)),
        )

    def minus(self, other: "SkipCounters") -> "SkipCounters":
        return SkipCounters(
            self.encoder_skipped - other.encoder_skipped,
            self.encoder_total - other.encoder_total,
            self.render_skipped - other.render_skipped,
            self.render_total - other.render_total,
        )


class SkipTracker:
    """把 GetStats 的“OBS 启动以来累计值”换算成“本次录制以来的值”。

    * 观察到录制开始时记录一份基线，之后显示 当前值 - 基线。
    * 录制停止后保留最后一次的数值，直到下一次录制开始。
    * 如果程序启动时录制已经在进行，无法得知录制开始时的计数，此时以第一次
      观察到的值为基线并把 ``partial`` 置为 True（叠加窗口会显示“*”提示）。
    * OBS 重启会让累计值归零，此时任何计数小于基线，自动重置基线。
    """

    def __init__(self) -> None:
        self.baseline: SkipCounters | None = None
        self.partial = False
        self.last = SkipCounters(0, 0, 0, 0)

    def update(self, recording: bool, current: SkipCounters, duration_ms: int) -> SkipCounters:
        if not recording:
            self.baseline = None
            return self.last
        if self.baseline is None:
            self.baseline = current
            self.partial = duration_ms > PARTIAL_THRESHOLD_MS
        delta = current.minus(self.baseline)
        if min(delta.encoder_skipped, delta.encoder_total, delta.render_skipped, delta.render_total) < 0:
            self.baseline = current
            delta = SkipCounters(0, 0, 0, 0)
        self.last = delta
        return delta

    def reset(self) -> None:
        """断线重连后调用：重连期间的数据未知，需要重新建立基线。"""
        self.baseline = None


class ObsPoller(threading.Thread):
    def __init__(self, store: ConfigStore, state: SharedState, renamer: RenameWorker | None = None) -> None:
        super().__init__(name="ObsPoller", daemon=True)
        self.store = store
        self.state = state
        self.renamer = renamer
        self.skips = SkipTracker()
        self.scenes = SceneTracker()
        self._profile: str | None = None
        self._record_started_at = 0.0
        self._last_scene_tick: float | None = None
        self._stop = threading.Event()
        self._reconnect = threading.Event()
        self._was_recording = False
        store.subscribe(self._on_config_changed)

    def _on_config_changed(self, old: dict, new: dict) -> None:
        if old["obs"] != new["obs"]:
            self._reconnect.set()

    def stop(self) -> None:
        self._stop.set()
        self._reconnect.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self._reconnect.clear()
            cfg = self.store.get()["obs"]
            try:
                client = connect(cfg["host"], cfg["port"], cfg["password"])
            except Exception as e:  # noqa: BLE001
                self._set_disconnected(describe_error(e))
                self._reconnect.wait(RETRY_DELAY_SEC)
                continue

            try:
                self._on_connected(client)
                while not self._stop.is_set() and not self._reconnect.is_set():
                    self._poll(client)
                    self._reconnect.wait(cfg["poll_interval_sec"])
            except Exception as e:  # noqa: BLE001
                log.warning("OBS 连接中断：%s", e)
                self._set_disconnected(describe_error(e))
                self._reconnect.wait(RETRY_DELAY_SEC)
            finally:
                _safe_disconnect(client)

    def _set_disconnected(self, error: str) -> None:
        self.skips.reset()
        self._was_recording = False
        self.state.update(obs_connected=False, obs_error=error, recording=False, paused=False)

    def _on_connected(self, client: obs.ReqClient) -> None:
        version = client.send("GetVersion", raw=True)
        self.state.update(
            obs_connected=True,
            obs_error="",
            obs_version=f"OBS {version['obsVersion']} / ws {version['obsWebSocketVersion']}",
        )
        self._refresh_record_directory(client)
        log.info("已连接到 %s", version["obsVersion"])

    def _refresh_record_directory(self, client: obs.ReqClient) -> None:
        try:
            d = client.send("GetRecordDirectory", raw=True)
            self.state.update(record_directory=d["recordDirectory"])
        except OBSSDKRequestError as e:
            log.warning("获取录制目录失败：%s", e)

    def _poll(self, client: obs.ReqClient) -> None:
        rec = client.send("GetRecordStatus", raw=True)
        stats = client.send("GetStats", raw=True)

        recording = bool(rec["outputActive"])
        duration_ms = int(rec.get("outputDuration", 0))
        changes: dict = {"recording": recording, "paused": bool(rec.get("outputPaused", False))}

        paused = changes["paused"]

        if recording and not self._was_recording:
            # 新的一次录制：推算开始时间，并刷新录制目录（用户可能在 OBS 中改过）
            self._record_started_at = time.time() - duration_ms / 1000
            changes["record_started_at"] = self._record_started_at
            self._refresh_record_directory(client)
            # 录制期间 OBS 不允许切换配置文件，开始时记录一次即可
            self._profile = client.send("GetProfileList", raw=True)["currentProfileName"]
            self.scenes.start()
            self._last_scene_tick = None
            changes["profile"] = self._profile
        elif not recording and self._was_recording:
            self._schedule_rename(client)

        if recording:
            scene = client.send("GetCurrentProgramScene", raw=True)
            # obs-websocket 5.3 起字段名为 currentProgramSceneName，旧版本为 sceneName
            scene_name = scene.get("currentProgramSceneName") or scene.get("sceneName")
            now = time.monotonic()
            if self._last_scene_tick is not None and not paused:
                # 两次轮询之间的时间计入当前场景；暂停期间不计
                self.scenes.tick(scene_name, now - self._last_scene_tick)
            self._last_scene_tick = now
            changes["current_scene"] = scene_name

        skipped = self.skips.update(recording, SkipCounters.from_stats(stats), duration_ms)
        changes.update(
            encoder_skipped=skipped.encoder_skipped,
            encoder_total=skipped.encoder_total,
            render_skipped=skipped.render_skipped,
            render_total=skipped.render_total,
            skipped_partial=self.skips.partial,
        )
        if recording:
            # 停止录制后 OBS 会把这些值清零；不覆盖它们，好让叠加窗口保留最终结果
            changes.update(
                duration_ms=duration_ms,
                timecode=rec.get("outputTimecode", ""),
                output_bytes=int(rec.get("outputBytes", 0)),
            )

        self._was_recording = recording
        self.state.update(**changes)

    # ---------- 录制后重命名 ----------

    def _resolve_app_title(self, client: obs.ReqClient, scene: str) -> str | None:
        def items(scene_name: str) -> list[dict]:
            return client.send("GetSceneItemList", {"sceneName": scene_name}, raw=True)["sceneItems"]

        def group_items(group_name: str) -> list[dict]:
            return client.send("GetGroupSceneItemList", {"sceneName": group_name}, raw=True)["sceneItems"]

        def settings(input_name: str) -> dict:
            return client.send("GetInputSettings", {"inputName": input_name}, raw=True)["inputSettings"]

        return pick_app_title(items(scene), settings, group_items)

    def _schedule_rename(self, client: obs.ReqClient) -> None:
        """录制刚停止：趁 OBS 仍连接时确定应用标题，然后提交延迟重命名任务。"""
        cfg = self.store.get()
        if self.renamer is None or not cfg["rename"]["enabled"]:
            return
        stopped_at = time.time()
        directory = cfg["recording"]["directory"] or self.state.snapshot()["record_directory"]
        if not directory:
            self.state.update(last_rename="未知录制目录，未重命名")
            return

        scene = self.scenes.longest()
        app_title = None
        if scene:
            try:
                app_title = self._resolve_app_title(client, scene)
            except OBSSDKRequestError as e:
                # 例如场景在录制结束前被删除了；仍然按“省略应用标题”重命名
                log.warning("读取场景 %s 的来源失败：%s", scene, e)
        log.info(
            "录制结束。各场景时长（秒）：%s；最长场景：%s；应用标题：%s；配置文件：%s",
            {k: round(v) for k, v in self.scenes.durations.items()}, scene, app_title, self._profile,
        )

        delay = cfg["rename"]["delay_sec"]
        self.renamer.submit(RenameJob(
            directory=Path(directory),
            extensions=cfg["recording"]["extensions"],
            started_at=self._record_started_at,
            stopped_at=stopped_at,
            profile=self._profile,
            app_title=app_title,
            due=stopped_at + delay,
            scene=scene,
        ))
        self.state.update(
            last_rename=f"{delay} 秒后重命名（场景：{scene or '未知'}，应用：{app_title or '无'}，"
                        f"配置文件：{self._profile or '未知'}）",
            last_rename_at=time.time(),
        )
