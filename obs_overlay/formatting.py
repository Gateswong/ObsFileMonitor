"""把原始数值格式化为叠加窗口上显示的文本。纯函数，便于单元测试。"""

from __future__ import annotations


def format_bytes(n: int) -> str:
    """1536 -> '1.50 KB'；使用 1024 进制，与 Windows 资源管理器一致。"""
    if n < 0:
        n = 0
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def format_duration(ms: int) -> str:
    """3723000 -> '01:02:03'。"""
    total = max(0, int(ms)) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_skipped(skipped: int, total: int) -> str:
    """12, 6000 -> '12 (0.20%)'。total 为 0 时只显示数量。"""
    if total <= 0:
        return str(skipped)
    return f"{skipped} ({skipped / total * 100:.2f}%)"

