"""置顶、鼠标可穿透的屏幕叠加窗口（tkinter + Win32）。

由两个无边框窗口叠在一起组成，这样文字和背景可以分别设置不透明度：

* 文字窗口（``self.root``）：Canvas 绘制带阴影的文字。背景填充一个“色键”颜色，
  通过 Tk 的 ``-transparentcolor`` 把这个颜色的像素变成完全透明，于是只剩文字。
* 背景窗口（``self.bg_win``）：纯色方块，位于文字窗口正下方，不透明度为 0 时隐藏。

tkinter 不是线程安全的：本模块的所有 Tk 调用都在主线程中进行。其他线程
只修改 SharedState 或设置标志位，由 ``_tick()`` 每 500ms 读取并刷新界面。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont

from .config import ConfigStore
from .formatting import format_bytes, format_duration, format_skipped
from .state import SharedState

REFRESH_MS = 500
FONT_FAMILY = "Microsoft YaHei UI"

# Win32 扩展窗口样式
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020   # 鼠标事件穿透到下方窗口
WS_EX_TOOLWINDOW = 0x00000080    # 不在任务栏和 Alt+Tab 中显示
WS_EX_NOACTIVATE = 0x08000000    # 不抢占键盘焦点
# SetWindowDisplayAffinity：窗口对屏幕捕获不可见（Windows 10 2004+）
WDA_NONE = 0x00
WDA_EXCLUDEFROMCAPTURE = 0x11


def enable_dpi_awareness() -> None:
    """在创建 Tk 之前调用，避免高 DPI 屏幕上文字模糊。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _hwnd(win: tk.Misc) -> int:
    # winfo_id() 是 Tk 内部子窗口，真正的顶层窗口是它的父窗口
    return ctypes.windll.user32.GetParent(win.winfo_id())


def _window_rect(win: tk.Misc) -> tuple[int, int, int, int]:
    if sys.platform != "win32":
        return (win.winfo_rootx(), win.winfo_rooty(),
                win.winfo_rootx() + win.winfo_width(), win.winfo_rooty() + win.winfo_height())
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(_hwnd(win), ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def _move_window(win: tk.Misc, x: int, y: int, w: int, h: int) -> None:
    if sys.platform != "win32":
        win.geometry(f"{w}x{h}+{x}+{y}")
        return
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    ctypes.windll.user32.SetWindowPos(_hwnd(win), 0, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)


def _set_click_through(win: tk.Misc, enabled: bool) -> None:
    """设置/取消鼠标穿透。样式未变化时不做任何事，因此可以频繁调用。"""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    hwnd = _hwnd(win)
    current = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style = current | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    if enabled:
        style |= WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    if style != current:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def _set_capture_exclusion(win: tk.Misc, exclude: bool) -> None:
    """让窗口不被 OBS 显示器采集、截图工具等捕获，人眼仍然可见。"""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    hwnd = _hwnd(win)
    current = ctypes.c_uint32()
    user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(current))
    wanted = WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE
    if current.value != wanted:
        # 旧版 Windows 不支持 WDA_EXCLUDEFROMCAPTURE，调用会失败，忽略即可
        user32.SetWindowDisplayAffinity(hwnd, wanted)


def color_key_for(shadow_color: str) -> str:
    """选一个与阴影色只差 1 的颜色作为透明色键。

    色键抠图是“像素值完全等于色键才透明”。文字边缘的抗锯齿像素是文字色与
    背景（色键）的混合色，不会被抠掉；如果色键是任意颜色（比如品红），文字
    周围就会出现一圈彩色毛边。让色键几乎等于阴影色，毛边就和阴影融为一体。
    """
    r, g, b = (int(shadow_color[i:i + 2], 16) for i in (1, 3, 5))
    b = b + 1 if b < 255 else b - 1
    return f"#{r:02x}{g:02x}{b:02x}"


class Overlay:
    ROWS = ("status", "duration", "size", "encoder", "render")
    LABELS = {
        "status": "状态",
        "duration": "时长",
        "size": "大小",
        "encoder": "编码跳帧",
        "render": "渲染跳帧",
    }

    def __init__(self, store: ConfigStore, state: SharedState, quit_event: threading.Event) -> None:
        self.store = store
        self.state = state
        self.quit_event = quit_event
        self._config_dirty = threading.Event()
        store.subscribe(lambda old, new: self._config_dirty.set())

        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("OBS Recording Overlay")
        self.root.overrideredirect(True)  # 无边框
        self.canvas = tk.Canvas(self.root, highlightthickness=0, borderwidth=0)
        self.canvas.pack()

        self.bg_win = tk.Toplevel(self.root)
        self.bg_win.title("OBS Recording Overlay Background")
        self.bg_win.overrideredirect(True)

        self.cfg: dict = {}
        self._visible = True
        self._value_width = 0
        self._apply_config()

    # ---------- 样式与位置 ----------

    def _apply_config(self) -> None:
        self.cfg = self.store.get()["overlay"]
        c = self.cfg
        self.body_font = tkfont.Font(family=FONT_FAMILY, size=c["font_size"])
        self.bold_font = tkfont.Font(family=FONT_FAMILY, size=c["font_size"], weight="bold")
        self._value_width = 0  # 字号可能变了，重新计算列宽

        key = color_key_for(c["shadow_color"])
        self.canvas.configure(bg=key)
        self.root.configure(bg=key)
        self.root.attributes("-transparentcolor", key)
        self.root.attributes("-alpha", c["text_opacity"])
        self.root.attributes("-topmost", True)

        self.bg_win.configure(bg=c["bg_color"])
        self.bg_win.attributes("-alpha", max(c["bg_opacity"], 0.01))
        self.bg_win.attributes("-topmost", True)
        self._sync_bg_visibility()

        self.root.update_idletasks()
        self._apply_win32_styles()

    def _bg_wanted(self) -> bool:
        return self._visible and self.cfg["bg_opacity"] > 0

    def _sync_bg_visibility(self) -> None:
        if self._bg_wanted():
            self.bg_win.deiconify()
        else:
            self.bg_win.withdraw()

    def _apply_win32_styles(self) -> None:
        # Tk 在映射窗口、修改 -alpha/-topmost 时会重写扩展样式，导致穿透失效，
        # 因此每次刷新都检查一遍（样式未变时只是一次 GetWindowLong 调用）
        for win in (self.root, self.bg_win):
            _set_click_through(win, self.cfg["click_through"])
            _set_capture_exclusion(win, self.cfg["exclude_from_capture"])

    def _place(self, w: int, h: int) -> None:
        c = self.cfg
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = c["margin_x"] if c["position"].endswith("left") else sw - w - c["margin_x"]
        y = c["margin_y"] if c["position"].startswith("top") else sh - h - c["margin_y"]
        # 无论窗口多宽，都不超出屏幕
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h))
        target = (x, y, x + w, y + h)
        for win in (self.root, self.bg_win):
            # 以 Win32 的真实窗口位置为准判断是否需要移动：Tk 自己记录的 geometry()
            # 可能与实际位置不一致（例如窗口首次映射前设置的位置会丢失）
            if _window_rect(win) == target:
                continue
            win.geometry(f"{w}x{h}+{x}+{y}")
            win.update_idletasks()
            if _window_rect(win) != target:
                # Tk 认为窗口已经在目标位置时，wm geometry 不会真正移动窗口，
                # 于是直接用 SetWindowPos 移动
                _move_window(win, x, y, w, h)

    def _set_visible(self, visible: bool) -> None:
        if visible == self._visible:
            return
        self._visible = visible
        if visible:
            self.root.deiconify()
            self._apply_config()
        else:
            self.root.withdraw()
            self.bg_win.withdraw()

    # ---------- 内容刷新 ----------

    def render_rows(self, s: dict, size_source: str) -> dict[str, tuple[str, bool]]:
        """根据状态快照生成每一行的 (文本, 是否高亮警告)。"""
        rows: dict[str, tuple[str, bool]] = {}
        if not s["obs_connected"]:
            rows["status"] = ("OBS 未连接", True)
        elif s["recording"] and s["paused"]:
            rows["status"] = ("⏸ 已暂停", False)
        elif s["recording"]:
            rows["status"] = ("● 录制中", True)
        else:
            rows["status"] = ("未录制", False)

        rows["duration"] = (format_duration(s["duration_ms"]), False)

        if size_source == "directory":
            if s["file_error"] and not s["file_path"]:
                rows["size"] = ("未找到录制文件", True)
            elif not s["file_path"]:
                rows["size"] = ("-", False)
            else:
                rows["size"] = (format_bytes(s["file_size"]), bool(s["file_error"]))
        else:
            rows["size"] = (format_bytes(s["output_bytes"]), False)

        mark = "*" if s["skipped_partial"] else ""
        rows["encoder"] = (format_skipped(s["encoder_skipped"], s["encoder_total"]) + mark, s["encoder_skipped"] > 0)
        rows["render"] = (format_skipped(s["render_skipped"], s["render_total"]) + mark, s["render_skipped"] > 0)
        return rows

    def _draw(self, rows: dict[str, tuple[str, bool]]) -> None:
        c = self.cfg
        line_h = self.body_font.metrics("linespace")
        pad = line_h // 2
        gap = line_h
        label_w = max(self.body_font.measure(self.LABELS[k]) for k in self.ROWS)
        # 数值列宽只增不减，避免数字变化时窗口宽度来回抖动
        self._value_width = max(self._value_width, *(self.bold_font.measure(t) for t, _ in rows.values()))
        w = pad * 2 + label_w + gap + self._value_width
        h = pad * 2 + line_h * len(self.ROWS)

        # 阴影：右下方向的投影 + 四周 1px 描边，保证在白色背景上也看得清
        o = max(1, line_h // 14)
        shadow_offsets = [(o, o), (o + 1, o + 1), (-1, 0), (1, 0), (0, -1), (0, 1)] if c["shadow"] else []

        self.canvas.delete("all")
        self.canvas.configure(width=w, height=h)
        for i, key in enumerate(self.ROWS):
            y = pad + i * line_h
            value, warn = rows[key]
            items = (
                (pad, "nw", self.LABELS[key], self.body_font, c["fg_color"]),
                (w - pad, "ne", value, self.bold_font, c["warn_color"] if warn else c["fg_color"]),
            )
            for x, anchor, text, font, color in items:
                for dx, dy in shadow_offsets:
                    self.canvas.create_text(x + dx, y + dy, anchor=anchor, text=text, font=font, fill=c["shadow_color"])
                self.canvas.create_text(x, y, anchor=anchor, text=text, font=font, fill=color)
        self._place(w, h)

    def _tick(self) -> None:
        if self.quit_event.is_set():
            self.root.destroy()
            return
        if self._config_dirty.is_set():
            self._config_dirty.clear()
            if self._visible:
                self._apply_config()
            else:
                self.cfg = self.store.get()["overlay"]

        s = self.state.snapshot()
        visible = self.cfg["enabled"] and (self.cfg["show_when_idle"] or s["recording"])
        self._set_visible(visible)
        if visible:
            size_source = self.store.get()["recording"]["size_source"]
            self._draw(self.render_rows(s, size_source))
            # 定期重新置顶（全屏程序或其他置顶窗口可能把叠加窗口压到下面），
            # 并保证文字窗口在背景窗口之上
            if self._bg_wanted():
                self.bg_win.attributes("-topmost", True)
            self.root.attributes("-topmost", True)
            self.root.lift()
            self._apply_win32_styles()
        self.root.after(REFRESH_MS, self._tick)

    def run(self) -> None:
        self.root.after(0, self._tick)
        self.root.mainloop()
