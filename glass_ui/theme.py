# -*- coding: utf-8 -*-
"""视觉常量：颜色、字体、圆角、日志配色。

这些值整体参考了 Apple 的 Liquid Glass 设计规范（深色变体）：底色压到接近黑，
强调色用高饱和的系统色，半透明层全部靠"白色 + 极低 alpha"叠出来，
而不是用灰色去调亮度 —— 灰调在玻璃上会显得脏。
"""

# ---- 底色（只在 Pillow 不可用、退化成纯 Canvas 时才看得见）----
C_BG = "#0b0d13"
C_PANEL = "#14161d"
C_BORDER = "#2a2e3a"

# ---- 前景 ----
C_FG = "#ffffff"
C_FG_DIM = "#98a0b3"
C_FG_FAINT = "#6b7280"

# ---- 系统强调色 ----
C_ACCENT = "#0a84ff"
C_ACCENT_HOVER = "#3fa2ff"
C_GREEN = "#32d74b"
C_ORANGE = "#ff9f0a"
C_RED = "#ff453a"
C_CYAN = "#64d2ff"
C_PURPLE = "#bf5af2"

# ---- 字体 ----
# 中文界面用雅黑 UI，等宽数字用 Consolas（读数时列能对齐）
FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

# ---- 圆角 ----
R_WIN = 26      # 整窗圆角，同时也是 SetWindowRgn 的半径
R_CARD = 18     # 卡片圆角

# ---- 日志配色 ----
LOG_COLORS = {
    None: "#e6eaf2",
    "ok": C_GREEN,
    "warn": C_ORANGE,
    "err": C_RED,
    "dim": "#8e96a8",
}
