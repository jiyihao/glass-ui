# -*- coding: utf-8 -*-
"""glass-ui —— 给 tkinter 的液态玻璃（Liquid Glass）界面层。

tkinter 自己没有模糊和半透明合成能力，这个库把"看起来像一块玻璃"需要的
全部脏活包了起来：抓屏 → 高斯模糊 → 上色 → 合成静态玻璃层 → 整张铺成
Canvas 背景，再配上一套自绘控件。

快速开始::

    import tkinter as tk
    from glass_ui import GlassWindow

    class Demo(GlassWindow):
        TITLE = "Hello Glass"

        def build_ui(self):
            self.make_canvas()

        def glass_cards(self):
            return [(20, 70, self.win_w - 40, 200)]

        def draw_chrome(self):
            cv = self.cv
            cv.delete("all")
            cv.create_image(0, 0, image=self.bg_photo, anchor="nw")
            cv.create_text(44, 100, text="你好，玻璃", anchor="w",
                           fill="#ffffff", font=("Microsoft YaHei UI", 20, "bold"))

    root = tk.Tk()
    Demo(root, title="Hello Glass").run()

设计取舍、参数含义、以及踩过的坑见 docs/PITFALLS.md。
"""

from .compat import HAVE_PIL
from .engine import GlassEngine
from .shapes import hairline, pill_grad, rrect
from .theme import (C_ACCENT, C_BG, C_CYAN, C_FG, C_FG_DIM, C_FG_FAINT,
                    C_GREEN, C_ORANGE, C_PANEL, C_PURPLE, C_RED, FONT_MONO,
                    FONT_UI, LOG_COLORS, R_CARD, R_WIN)
from .widgets import (GlassButton, GlassLog, GlassSlider, GlassTable,
                      GlassToggle, IconButton, VarText)
from .window import GlassWindow

__version__ = "0.1.7"

__all__ = [
    "GlassEngine", "GlassWindow",
    "GlassButton", "IconButton", "GlassToggle", "GlassSlider",
    "GlassTable", "GlassLog",
    "VarText",
    "rrect", "pill_grad", "hairline",
    "HAVE_PIL",
    "C_ACCENT", "C_BG", "C_CYAN", "C_FG", "C_FG_DIM", "C_FG_FAINT",
    "C_GREEN", "C_ORANGE", "C_PANEL", "C_PURPLE", "C_RED",
    "FONT_UI", "FONT_MONO", "LOG_COLORS", "R_CARD", "R_WIN",
]
