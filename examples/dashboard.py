# -*- coding: utf-8 -*-
"""glass-ui 最小示例：一块"系统概览"面板。

跑起来看效果：

    python examples/dashboard.py

不带参数时会弹出窗口；带 `--shot out.png` 则渲染后截图退出（用于核对布局）。

这个文件同时也是"怎么用这个库"的说明书，按顺序看 `build_ui` →
`glass_cards` → `glass_decorators` → `glass_slices` → `draw_chrome` 五个方法。
"""

import os
import sys
import time
import tkinter as tk
from tkinter import font as tkfont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import ImageGrab                                    # noqa: E402

from glass_ui import (C_ACCENT, C_CYAN, C_ORANGE, FONT_MONO,  # noqa: E402
                      FONT_UI, GlassButton, GlassLog, GlassTable,
                      GlassToggle, GlassWindow, IconButton, hairline,
                      pill_grad, rrect)
from glass_ui.compat import Image, ImageDraw                 # noqa: E402

APP_NAME = "Glass UI"
APP_VER = "0.1.0"

# 卡片矩形：(x, y, w, h)
CARD_HERO = (18, 70, 864, 150)
CARD_LIST = (18, 228, 864, 240)
CARD_LOG = (18, 532, 864, 84)
LIST_HEAD_H = 46
LOG_HEAD_H = 26
BTN_Y = 478
BTN_H = 46
HERO_BADGE = (742, 84, 124, 28)

COLS = (
    ("name", 0.00, 0.46, "w", "任务"),
    ("pid", 0.46, 0.56, "e", "PID"),
    ("mem", 0.56, 0.68, "e", "内存"),
    ("note", 0.72, 1.00, "w", "备注"),
)

# 假数据：写死是为了截图稳定。真项目里换成你自己的数据源。
ROWS = [
    ("build-worker", "6120", "1284.5 MB", "编译中 · 4 线程", None),
    ("browser", "8844", "842.0 MB", "", None),
    ("music-player", "2032", "180.4 MB", "可结束", "warn"),
    ("sync-daemon", "4512", "96.8 MB", "", None),
    ("editor", "7788", "412.6 MB", "", None),
    ("terminal", "3390", "38.2 MB", "", None),
    ("updater", "9014", "204.1 MB", "可结束", "warn"),
    ("shell-host", "1122", "88.7 MB", "", None),
    ("indexer", "5608", "310.2 MB", "", None),
    ("clipboard", "6620", "12.4 MB", "", None),
    ("notes", "7722", "64.9 MB", "", None),
    ("camera", "8801", "148.3 MB", "可结束", "warn"),
]


class Dashboard(GlassWindow):
    TITLE = f"{APP_NAME} {APP_VER}"
    WIN_W = 900
    WIN_H = 640

    def __init__(self, root, shot=False):
        self.pct = 63.0
        self.detail = "采样窗口 1 秒    平均 12.4%   峰值 78.1%   共 12 个任务"
        self._hero_img = None
        self._log_lines = []
        super().__init__(root, shot=shot, title=self.TITLE)

    # ----------------------------------------------------------- 1. 建界面
    def build_ui(self):
        self.make_canvas()

        # 排版用的字体度量：数值与单位要按真实宽度对齐，建一次复用
        self.f_num = tkfont.Font(family=FONT_UI, size=30, weight="bold")
        self.f_unit = tkfont.Font(family=FONT_UI, size=12)
        self.f_tot = tkfont.Font(family=FONT_UI, size=11)

        self.table = GlassTable(self, CARD_LIST[0] + 1,
                                CARD_LIST[1] + LIST_HEAD_H,
                                CARD_LIST[2] - 2,
                                CARD_LIST[3] - LIST_HEAD_H - 1)
        self.table.COLS = COLS
        # tag -> 颜色，库不关心 tag 叫什么
        self.table.ROW_FG = {"warn": C_ORANGE, "muted": "#949db0"}
        self.table.ROW_DOT = ("warn",)
        self.table.NOTE_FG = {"warn": C_ORANGE}
        for name, pid, mem, note, tag in ROWS:
            self.table.insert("", "end", iid=pid, tags=(tag or "normal",),
                              values=(name, pid, mem, note))

        self.logview = GlassLog(self, CARD_LOG[0] + 1, CARD_LOG[1] + LOG_HEAD_H,
                                CARD_LOG[2] - 2, CARD_LOG[3] - LOG_HEAD_H - 1)

        self.btn_run = GlassButton(self, "开始采样", self.do_run,
                                   18, BTN_Y, 120, BTN_H, "primary")
        self.btn_clear = GlassButton(self, "清空列表", self.do_clear,
                                     148, BTN_Y, 124, BTN_H, "ghost")

        # 卡片标题右上的小按钮必须在 build_ui 里就建好：
        # 如果等 draw_chrome 首次执行时才懒创建，rebuild_background 枚举
        # glass_slices() 的时候它还不在，切不到背景，会露出一个黑色方块。
        self.btn_clearlog = GlassButton(
            self, "清空", self.logview.clear,
            CARD_LOG[0] + CARD_LOG[2] - 76, CARD_LOG[1] + 2, 58, 22, "ghost")

        self.var_auto = tk.BooleanVar(value=True)
        self.toggle = GlassToggle(self, self.var_auto,
                                  self.win_w - 250, BTN_Y + 4, "自动刷新")

        self.btn_about = IconButton(self, "?", self.do_about,
                                    self.win_w - 112, 14)
        self.btn_min = IconButton(self, "—", self.minimize, self.win_w - 76, 14)
        self.btn_close = IconButton(self, "✕", self.close, self.win_w - 42, 14,
                                    danger=True)

    def on_ready(self):
        self.log("示例已启动，拖动窗口会重新采样背后的桌面。")
        self.log("模糊底来自真实桌面，不是渐变模拟。", "dim")

    # --------------------------------------------------- 2. 哪些是卡片
    def glass_cards(self):
        return [CARD_HERO, CARD_LIST, CARD_LOG]

    # ------------------------------------- 3. 需要 alpha 的静态层
    def glass_decorators(self):
        return [self._deco_hero]

    def _deco_hero(self, img):
        """主卡上的进度条凹槽 + 右上角徽章底。

        这两样都要半透明，Canvas 画不出来（会盖住玻璃底），
        必须画进底图 —— 也就是这个钩子的用途。
        """
        w, h = img.size
        x, y, cw, _ch = CARD_HERO

        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle([x + 24, y + 92, x + cw - 24, y + 104],
                            radius=6, fill=(0, 0, 0, 66),
                            outline=(255, 255, 255, 20), width=1)

        bx, by, bw, bh = HERO_BADGE
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh / 2.0,
                            fill=(48, 209, 88, 34),
                            outline=(48, 209, 88, 88), width=1)
        return Image.alpha_composite(img, layer)

    # ------------------------------------------- 4. 哪些控件要背景切片
    def glass_slices(self):
        out = [
            (self.table, CARD_LIST[0] + 1, CARD_LIST[1] + LIST_HEAD_H,
             CARD_LIST[2] - 2, CARD_LIST[3] - LIST_HEAD_H - 1),
            (self.logview, CARD_LOG[0] + 1, CARD_LOG[1] + LOG_HEAD_H,
             CARD_LOG[2] - 2, CARD_LOG[3] - LOG_HEAD_H - 1),
        ]
        for b in (self.btn_run, self.btn_clear, self.btn_clearlog):
            out.append((b, b.x, b.y, b.w, b.h))
        for ic in (self.btn_about, self.btn_min, self.btn_close):
            out.append((ic, ic.x, ic.y, ic.size, ic.size))
        t = self.toggle
        out.append((t, t.cv.winfo_x(), t.cv.winfo_y(),
                    t.cv.winfo_width(), t.cv.winfo_height()))
        return out

    # ------------------------------------------------- 5. 画文字和交互
    def draw_chrome(self):
        cv = self.cv
        cv.delete("all")
        if self.bg_photo is not None:
            cv.create_image(0, 0, image=self.bg_photo, anchor="nw")

        self._draw_titlebar(cv)
        self._draw_hero(cv)
        self._draw_list_head(cv)
        self._draw_log_head(cv)

    def _draw_titlebar(self, cv):
        cv.create_oval(20, 23, 28, 31, fill=C_ACCENT, outline="")
        cv.create_text(40, 29, text="Glass UI", anchor="w", fill="#ffffff",
                       font=(FONT_UI, 13, "bold"))
        cv.create_text(120, 30, text=f"v{APP_VER}", anchor="w",
                       fill="#7d8698", font=(FONT_UI, 8))
        cv.create_text(158, 30, text="● 已连接", anchor="w", fill="#32d74b",
                       font=(FONT_UI, 8))

    def _draw_hero(self, cv):
        x, y, cw, _ch = CARD_HERO
        pad = 24

        cv.create_text(x + pad, y + 22, text="当前占用", anchor="w",
                       fill="#8e96a8", font=(FONT_UI, 9))

        f_num = self.f_num
        f_unit = self.f_unit
        f_tot = self.f_tot

        # 主数值先落，再用它真实的 bbox 右端接单位和总量。
        # 不要用 measure() 累加 —— 30pt 粗体下 measure 比实际渲染宽十几像素。
        base = y + 80
        num = f"{self.pct * 8.2 / 100:.1f}"
        it = cv.create_text(x + pad, base, text=num, anchor="sw",
                            fill="#ffffff", font=(FONT_UI, 30, "bold"))
        bb = cv.bbox(it)
        nx = (bb[2] if bb else x + pad + f_num.measure(num)) + 7

        # 不同字号要按基线对齐：anchor="sw" 对齐的是 bbox 底边，
        # 30pt 的 descent 比 12pt 大，直接对齐会让小字号整体偏高。
        drop = f_num.metrics("descent")
        it2 = cv.create_text(nx, base - drop + f_unit.metrics("descent"),
                             text="GB", anchor="sw", fill="#a9b2c5",
                             font=(FONT_UI, 12))
        bb2 = cv.bbox(it2)
        tx = (bb2[2] if bb2 else nx + f_unit.measure("GB")) + 12
        cv.create_text(tx, base - drop + f_tot.metrics("descent"),
                       text="/ 共 8.2 GB", anchor="sw", fill="#767f90",
                       font=(FONT_UI, 11))

        # 进度条：凹槽在底图里，这里只贴渐变填充
        px1, px2 = x + pad, x + cw - pad
        py, ph = y + 92, 12
        fill_w = max(float(ph), (px2 - px1) * self.pct / 100.0)
        self._hero_img = pill_grad(cv, px1, py, px1 + fill_w, py + ph,
                                   (74, 176, 255), (10, 116, 227), 0.12)

        cv.create_text(x + pad, y + 128, text=self.detail, anchor="w",
                       fill="#8e96a8", font=(FONT_MONO, 8))

        bx, by, bw, bh = HERO_BADGE
        cv.create_text(bx + bw / 2.0, by + bh / 2.0 + 1, text="● 正常运行",
                       anchor="center", fill="#7fe3a6",
                       font=(FONT_UI, 9, "bold"))

    def _draw_list_head(self, cv):
        x, y, cw, _ch = CARD_LIST
        rrect(cv, x + 18, y + 10, x + 21, y + 23, 1.5, fill=C_ACCENT,
              outline="")
        cv.create_text(x + 28, y + 17, text="任务列表", anchor="w",
                       fill="#eef2f9", font=(FONT_UI, 9, "bold"))
        cv.create_text(x + 92, y + 18, text=f"共 {len(ROWS)} 项", anchor="w",
                       fill="#98a1b4", font=(FONT_UI, 8))

        hy = y + LIST_HEAD_H - 11
        inner = cw - 2
        for key, a, b, anchor, label in COLS:
            x1 = x + 1 + 10 + (inner - 20) * a
            x2 = x + 1 + 10 + (inner - 20) * b
            if key == "name":
                tx, an = x1 + 12, "w"
            elif key == "note":
                tx, an = x1, "w"
            else:
                tx, an = x2 - 14, "e"
            cv.create_text(tx, hy, text=label, anchor=an, fill="#98a1b4",
                           font=(FONT_UI, 8))

        # 半透明分隔线：1px 高的 RGBA 贴图，Canvas 画不出真正的半透明线
        self._hair = hairline(cv, x + 12, y + LIST_HEAD_H - 1, cw - 24, 34)

    def _draw_log_head(self, cv):
        x, y, cw, _ch = CARD_LOG
        rrect(cv, x + 18, y + 10, x + 21, y + 23, 1.5, fill=C_CYAN, outline="")
        cv.create_text(x + 28, y + 17, text="事件", anchor="w",
                       fill="#eef2f9", font=(FONT_UI, 9, "bold"))

    # ------------------------------------------------------------- 交互
    def log(self, msg, tag=None):
        self.logview.push(msg, tag)

    def do_run(self):
        self.pct = min(98.0, self.pct + 7.5)
        self.detail = (f"采样窗口 1 秒    平均 {self.pct * 0.19:.1f}%    "
                       f"峰值 {min(99.9, self.pct * 1.24):.1f}%    "
                       f"共 {len(ROWS)} 个任务")
        self.draw_chrome()
        self.log(f"重新采样完成，当前占用 {self.pct:.1f}%", "ok")

    def do_clear(self):
        self.table.delete()
        self.log("列表已清空。", "dim")

    def do_about(self):
        self.info("关于这个示例",
                  "这是一份 glass-ui 的最小示例，界面里的模糊背景是对窗口后方"
                  "桌面的实时采样（BitBlt + 高斯模糊），不是渐变模拟。\n\n"
                  "卡片、按钮、开关、表格全部是自绘的：tkinter 原生控件由系统"
                  "绘制，做不出玻璃质感。\n\n"
                  "拖动窗口试试 —— 松手后会重新抓屏，玻璃后面的内容跟着变。")


def screenshot_mode(out_path):
    root = tk.Tk()
    app = Dashboard(root, shot=True)
    root.attributes("-topmost", True)
    app.rebuild_background()
    root.update_idletasks()
    for _ in range(60):
        root.update()
        time.sleep(0.03)
    try:
        # 截图前解除"排除自身"，否则抓到的是纯桌面
        app.set_capture_affinity(False)
        time.sleep(0.25)
        x, y = root.winfo_rootx(), root.winfo_rooty()
        bbox = (x, y, x + root.winfo_width(), y + root.winfo_height())
        ImageGrab.grab(bbox=bbox, all_screens=True).save(out_path)
        print("saved:", out_path, bbox)
    except Exception as e:
        print("screenshot failed:", repr(e))
    finally:
        root.destroy()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "dashboard.png"
        screenshot_mode(out)
        return

    root = tk.Tk()
    Dashboard(root).run()


if __name__ == "__main__":
    main()
