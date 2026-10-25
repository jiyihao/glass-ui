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


def make_taskbar_window(widget):
    """让无边框窗口进入任务栏与 Alt+Tab 列表。"""
    try:
        hwnd = get_hwnd(widget)
        if not hwnd:
            return
        st = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        st = (st | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, st)
    except Exception:
        pass


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
