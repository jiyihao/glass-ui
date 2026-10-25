# -*- coding: utf-8 -*-
"""Win32 底层：窗口句柄、系统级圆角裁剪、抓屏排除。

tkinter 做无边框窗口有三件事必须自己接系统接口，否则会有明显缺陷：

1. 圆角 —— 无边框窗口默认是直角矩形，`SetWindowRgn` + `CreateRoundRectRgn`
   能让系统层面把四角裁掉，任务栏预览和阴影也跟着变圆。
2. 任务栏 / Alt+Tab —— `overrideredirect(True)` 的窗口默认不会出现在任务栏，
   要补上 `WS_EX_APPWINDOW` 并去掉 `WS_EX_TOOLWINDOW`。
3. 抓自己 —— 窗口内容来自"抓取后方桌面"，抓的时候必须把自己排除在画面外，
   否则会拍到自己形成递归。`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`
   需要 Windows 10 2004 及以上。
"""

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# SetWindowPos 的标志位。改完扩展样式必须带 SWP_FRAMECHANGED 再打一次
# SetWindowPos，系统才会重算窗口框架（WM_NCCALCSIZE），任务栏按钮也才会真的
# 出现 —— 只调 SetWindowLongW(GWL_EXSTYLE) 时样式改了但外壳没刷新。
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_TASKBAR_REFRESH = (SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER |
                       SWP_NOACTIVATE | SWP_FRAMECHANGED)

user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.SetWindowRgn.argtypes = [wt.HWND, wt.HANDLE, wt.BOOL]
user32.SetWindowRgn.restype = ctypes.c_int
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
user32.SetWindowDisplayAffinity.restype = wt.BOOL
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
gdi32.CreateRoundRectRgn.restype = wt.HANDLE
gdi32.DeleteObject.argtypes = [wt.HANDLE]


def get_hwnd(widget):
    """取 Tk 控件的顶层窗口句柄。

    `winfo_id()` 拿到的通常是子窗口（Tk 的内部 wrapper），必须用
    `GetAncestor(h, GA_ROOT)` 往上找到真正的顶层窗口，否则
    SetWindowRgn / SetWindowDisplayAffinity 打在子窗口上不会生效。
    """
    try:
        h = widget.winfo_id()
        root = user32.GetAncestor(h, GA_ROOT)
        return root or h
    except Exception:
        try:
            return widget.winfo_id()
        except Exception:
            return 0


def _to_signed_long(value):
    """把无符号 32 位值转成 `c_long` 能接受的有符号值。

    `SetWindowLongW` 的 argtypes 里样式是 `c_long`，直接传 0x8xxxxxxx
    这类高位为 1 的值会抛 `ArgumentError`。
    """
    value = int(value) & 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def get_exstyle(widget):
    """取顶层窗口的扩展样式，返回无符号 32 位整数。

    `GetWindowLongW` 返回的是有符号 long，高位为 1 时是个负数。位判断本身
    不受影响 —— Python 整数是二补码语义，`-2147221504 & 0x40000` 照样得到
    `0x40000`。掩码真正的收益是**格式化输出**：`"%08X" % -2147221504` 会印成
    `-7FFFFF80`，既难读也对不上文档里的 `WS_EX_*` 常量。统一 `& 0xFFFFFFFF`
    之后，返回值可以直接 `0x%08X` 打印、直接和无符号常量比较。
    """
    try:
        hwnd = get_hwnd(widget)
        if not hwnd:
            return 0
        return int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF
    except Exception:
        return 0


def make_taskbar_window(widget):
    """让无边框窗口进入任务栏与 Alt+Tab 列表。

    `overrideredirect(True)` 会把扩展样式重设成含 `WS_EX_TOOLWINDOW`
    的 0x00000080（工具窗口按定义不进任务栏、不进 Alt+Tab），所以这里补上
    `WS_EX_APPWINDOW` 并清掉 `WS_EX_TOOLWINDOW`。

    只调 `SetWindowLongW` 是不够的 —— 样式位改了，但系统不会重算窗口框架，
    任务栏按钮不会真的出现。必须再打一次 `SetWindowPos(..., SWP_FRAMECHANGED)`
    触发外壳刷新。这一步同时刷新任务栏与 Alt+Tab 列表。

    Args:
        widget: 任意 Tk 控件，内部会用 `get_hwnd` 找到顶层窗口。

    Returns:
        bool: 回读校验通过（APPWINDOW 已置上且 TOOLWINDOW 已清）时为 True。
    """
    try:
        hwnd = get_hwnd(widget)
        if not hwnd:
            return False
        old = get_exstyle(widget)
        new = (old | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW & 0xFFFFFFFF
        if new != old:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, _to_signed_long(new))
        # 样式本来就对时也要补一次框架刷新：外壳（任务栏 / Alt+Tab）是按
        # 框架变更消息重算窗口归属的，只有样式位不一定立刻反映到任务栏上。
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_TASKBAR_REFRESH)
        cur = get_exstyle(widget)
        return bool(cur & WS_EX_APPWINDOW) and not (cur & WS_EX_TOOLWINDOW)
    except Exception:
        return False


def apply_round_region(widget, radius):
    """系统级圆角裁剪。窗口外不会再留方形直角。"""
    try:
        hwnd = get_hwnd(widget)
        w = widget.winfo_width()
        h = widget.winfo_height()
        if w <= 1 or h <= 1 or not hwnd:
            return
        # +1 是因为 CreateRoundRectRgn 的右/下边界是开区间
        rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1,
                                       int(radius) * 2, int(radius) * 2)
        if rgn:
            user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


def set_capture_affinity(widget, enable=True):
    """抓屏时把自己排除在画面之外，避免"套娃"。"""
    try:
        hwnd = get_hwnd(widget)
        if not hwnd:
            return
        user32.SetWindowDisplayAffinity(
            hwnd, WDA_EXCLUDEFROMCAPTURE if enable else WDA_NONE)
    except Exception:
        pass
