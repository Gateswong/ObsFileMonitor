"""程序入口：``python -m obs_overlay``。

线程模型：
    主线程           tkinter 叠加窗口（Tk 只能在创建它的线程中使用）
    ObsPoller        轮询 OBS WebSocket
    DirectoryScanner 扫描录制目录（仅 size_source=directory 时工作）
    RenameWorker     录制停止后延迟重命名录制文件
    WebServer        本地配置页面
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, ConfigStore
from .file_monitor import DirectoryScanner
from .obs_client import ObsPoller
from .overlay import Overlay
from .renamer import RenameWorker
from .state import SharedState
from .web_ui import WebServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obs_overlay", description="OBS 录制信息屏幕叠加")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径（默认：项目根目录 config.json）")
    parser.add_argument("--open-config", action="store_true", help="启动后在浏览器中打开配置页面")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    first_run = not Path(args.config).exists()
    store = ConfigStore(args.config)
    state = SharedState()
    quit_event = threading.Event()

    try:
        web = WebServer(store, state, quit_event)
    except OSError as e:
        logging.error("无法启动配置页面（端口 %s 被占用？）：%s", store.get()["web"]["port"], e)
        return 1

    renamer = RenameWorker(on_result=lambda msg: state.update(last_rename=msg, last_rename_at=time.time()))
    poller = ObsPoller(store, state, renamer)
    scanner = DirectoryScanner(store, state)
    for t in (web, renamer, poller, scanner):
        t.start()

    print(f"配置页面：{web.url}  （按 Ctrl+C 或在配置页面点击“退出程序”结束）")
    if first_run or args.open_config:
        webbrowser.open(web.url)

    # Ctrl+C：tkinter 的 mainloop 不会直接响应 KeyboardInterrupt，
    # 这里只设置标志位，由叠加窗口的定时刷新负责退出
    signal.signal(signal.SIGINT, lambda *_: quit_event.set())

    try:
        Overlay(store, state, quit_event).run()
    finally:
        poller.stop()
        if renamer.pending():
            logging.warning("还有 %d 个重命名任务未执行，程序退出后这些文件将保持原名", renamer.pending())
        renamer.stop()
        scanner.stop()
        web.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
