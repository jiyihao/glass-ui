# -*- coding: utf-8 -*-
"""无边框圆角玻璃窗口基类。

把"让一个窗口看起来像一块玻璃"需要的那一堆脏活集中在这里：

* 去边框、居中、屏幕太矮时自动缩高
* 系统级圆角裁剪（SetWindowRgn）
* 补 WS_EX_APPWINDOW，让无边框窗口仍出现在任务栏和 Alt+Tab
* 抓屏时把自己排除（WDA_EXCLUDEFROMCAPTURE），否则背景会拍到自己的递归影像
* 空白处按住拖动窗口
* 重建背景时给每个子 Canvas 切一块对应的底图（圆角外才不会露方框）
* 配套的玻璃弹窗 ask() / info()

子类只需要：

1. 在 `build_ui()` 里创建 `self.cv`（用 `self.make_canvas()`）和所有控件
2. 实现 `glass_cards()`，返回本窗口的卡片矩形
3. 实现 `draw_chrome()`，在 Canvas 上画文字与交互图元
4. 实现 `glass_slices()`，告诉基类哪些控件需要背景切片
"""

import tkinter as tk
from tkinter import font as tkfont

from .compat import HAVE_PIL, Image, ImageFilter, ImageTk
from .engine import GlassEngine
from .theme import C_BG, FONT_UI, R_CARD, R_WIN
from .widgets import GlassButton
from .win import (apply_round_region, make_taskbar_window,
                  set_capture_affinity)


class GlassWindow:
    WIN_W = 980
    WIN_H = 744
    MIN_H = 620
    TITLE = "Glass UI"
    CAPTURE_SAFE = True       # 抓屏时排除自身（Win10 2004+）

    # -------------------------------------------------------------- 生命周期
    def __init__(self, root, shot=False, title=None, glass_opacity=None):
        """初始化窗口。

        Args:
            root: `tk.Tk()` 实例。
            shot: 截图/自检模式。True 时不改任务栏样式，避免干扰截图。
            title: 窗口标题，默认用类属性 `TITLE`。
            glass_opacity: 玻璃透明度 0~100（用户语义，100 最通透）。
                必须在 `build_ui()` 之前生效 —— 按钮底面的 alpha 是烘进
                贴图的，晚一步设就会拿到出厂贴图。
        """
        self.root = root
        self.shot = shot
        self.title = self.TITLE if title is None else title

        self.engine = GlassEngine()
        self.engine.CARD_R = R_CARD
        self.engine.WIN_R = R_WIN
        if glass_opacity is not None:
            self.engine.set_opacity(glass_opacity)

        self.win_w, self.win_h = self.WIN_W, self.WIN_H
        self.bg_photo = None
        self._bg_img = None
        self._bg_pending = False
        self._drag = None

        self.fit_screen()
        self.setup_window()
        self.build_ui()
        self.on_ready()
        self.root.after(60, self.rebuild_background)

    # ---- 子类要实现的钩子 ----
    def build_ui(self):
        raise NotImplementedError

    def on_ready(self):
        """界面搭好、背景还没建时调用。适合做首屏数据填充。"""

    def glass_cards(self):
        """返回卡片矩形列表 [(x, y, w, h)]，会被画成半透明玻璃块。"""
        return []

    def glass_buttons(self):
        """返回需要画进底图的按钮 [(x, y, w, h, kind)]。

        一般留空 —— GlassButton 有自己独立的画布和贴图，
        底图里再画一遍是浪费。
        """
        return []

    def glass_decorators(self):
        """返回自定义图层函数 [fn(img) -> img]。

        在卡片画完之后、窗口棱边之前调用，用来画进度条凹槽、徽章底
        这类"必须带 alpha"的静态元素。
        """
        return []

    def glass_slices(self):
        """返回 [(控件, x, y, w, h)]，基类会把底图对应区域切给控件。

        子 Canvas 如果没有铺背景切片，圆角之外的四角会露出方框。
        """
        return []

    def draw_chrome(self):
        """在 self.cv 上画文字和交互图元。"""
        raise NotImplementedError

    # ---------------------------------------------------------------- 窗口
    def fit_screen(self):
        try:
            avail = self.root.winfo_screenheight() - 96
            if avail < self.win_h:
                self.win_h = max(self.MIN_H, avail)
        except Exception:
            pass

    def setup_window(self):
        r = self.root
        r.title(self.title)
        r.configure(bg=C_BG)
        r.resizable(False, False)
        x = max(0, (r.winfo_screenwidth() - self.win_w) // 2)
        y = max(10, (r.winfo_screenheight() - 80 - self.win_h) // 2)
        r.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")
        r.overrideredirect(True)
        r.attributes("-topmost", False)
        if not self.shot:
            make_taskbar_window(r)

    def make_canvas(self):
        """创建主 Canvas 并绑上拖动。"""
        self.cv = tk.Canvas(self.root, width=self.win_w, height=self.win_h,
                            bd=0, highlightthickness=0, bg=C_BG)
        self.cv.place(x=0, y=0, width=self.win_w, height=self.win_h)
        self.cv.bind("<Button-1>", self.drag_start)
        self.cv.bind("<B1-Motion>", self.drag_move)
        self.cv.bind("<ButtonRelease-1>", self.drag_end)
        return self.cv

    def apply_round_region(self):
        apply_round_region(self.root, R_WIN)

    def set_capture_affinity(self, enable=True):
        if self.CAPTURE_SAFE:
            set_capture_affinity(self.root, enable)

    def minimize(self):
        """最小化：先恢复边框再 iconify，否则无边框窗口无法进任务栏。"""
        try:
            self.root.overrideredirect(False)
            self.root.iconify()
        except Exception:
            pass

    def on_map(self, _e=None):
        if self.root.state() == "normal":
            self.root.overrideredirect(True)
            self.root.after(80, self.apply_round_region)
            self.schedule_bg(200)

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def bind_map(self):
        self.root.bind("<Map>", self.on_map)

    def run(self):
        self.bind_map()
        self.root.mainloop()

    # ---------------------------------------------------------------- 拖动
    def drag_start(self, e):
        self._drag = (e.x_root, e.y_root,
                      self.root.winfo_x(), self.root.winfo_y())

    def drag_move(self, e):
        if not self._drag:
            return
        self.root.geometry(
            f"+{self._drag[2] + e.x_root - self._drag[0]}+"
            f"{self._drag[3] + e.y_root - self._drag[1]}")

    def drag_end(self, _e=None):
        self._drag = None
        # 松手后桌面已经完全露出来，此时重新抓屏才干净
        self.schedule_bg(180)

    # ------------------------------------------------------------ 背景重建
    def schedule_bg(self, delay=120):
        if self._bg_pending:
            return
        self._bg_pending = True
        self.root.after(delay, self.rebuild_background)

    def rebuild_background(self):
        self._bg_pending = False
        w, h = self.win_w, self.win_h
        try:
            # 窗口还没 map 时 winfo_width() 会返回 1，不能当成有效值用，
            # 否则会合成一张 1x1 的底图，整窗变成白屏
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            if rw > 1 and rh > 1:
                w, h = rw, rh
        except Exception:
            pass
        self.win_w, self.win_h = w, h

        self.apply_round_region()
        self.set_capture_affinity(True)

        if self.engine.ok:
            self.engine.grab(self.root.winfo_rootx(), self.root.winfo_rooty(),
                             w, h)

        img = self.engine.compose(w, h,
                                  self.glass_cards(),
                                  self.glass_buttons(),
                                  self.glass_decorators())
        self._bg_img = img
        if img is None:
            self.bg_photo = None
        else:
            self.bg_photo = ImageTk.PhotoImage(img.convert("RGB"))
            self.refresh_slices(img)

        self.draw_chrome()

    def refresh_slices(self, img):
        """给每个子 Canvas 切出所在区域的背景块。"""
        iw, ih = img.size

        def cut(x, y, w, h):
            x = max(0, int(x))
            y = max(0, int(y))
            w = max(1, min(iw - x, int(w)))
            h = max(1, min(ih - y, int(h)))
            return img.crop((x, y, x + w, y + h)).convert("RGB")

        try:
            for widget, x, y, w, h in self.glass_slices():
                widget.set_background(cut(x, y, w, h))
        except Exception:
            pass

    # ------------------------------------------------------------ 弹窗
    @staticmethod
    def button_width(text, minimum=88, pad_x=16):
        """按文字实测宽度算弹窗按钮宽度。

        固定 88px 时，「以管理员重启」这类 6 字文案实测字宽 78px，左右各只剩
        5px，挤得几乎贴边（QA 截图 `03_purge_need_admin.png` 就是这么暴露的）。
        这里量一次字宽再补左右内边距；短文案（「确定」「知道了」）仍然走
        `minimum`，观感不变。
        """
        try:
            f = tkfont.Font(family=FONT_UI, size=10, weight="bold")
            tw = f.measure(str(text))
        except Exception:
            tw = 0
        return int(max(minimum, tw + pad_x * 2))

    def glass_plate(self, w, h, dark=0.44, radius=20):
        """弹窗底板：复用主窗口已合成的背景，保证色调完全一致。

        不要重新抓屏 —— 弹窗出现时主窗口在最前面，重新抓屏会把自己拍进去。
        """
        if not HAVE_PIL:
            return None
        src = self._bg_img
        if src is None:
            img = self.engine._synthetic(w, h)
        else:
            try:
                img = src.resize((w, h), Image.BILINEAR)
            except Exception:
                img = self.engine._synthetic(w, h)
        img = img.convert("RGB").filter(ImageFilter.GaussianBlur(20))
        veil = Image.new("RGB", (w, h), (10, 12, 18))
        # 弹窗底板的暗度跟着玻璃强度走一点点：主界面调得很通透时，
        # 弹窗若还压那么黑会像另一个程序的窗口。出厂系数下乘 1，逐像素不变。
        k = self.engine.glass_k
        dark = max(0.0, min(0.92, float(dark) * (0.80 + 0.20 * k)))
        out = Image.blend(img, veil, dark).convert("RGBA")
        return self.engine.dialog_plate(out, radius)

    def _dialog(self, title, msg, ok_text="确定", cancel_text=None,
                danger=False):
        pad = 24
        lines = msg.split("\n")
        # 中文按字数粗估宽度，再夹到 [340, 560]；高度用 bbox 实测
        est = max([len(t) for t in lines] + [len(title) * 1.3, 12])
        dw = int(max(340, min(560, est * 12.2 + pad * 2)))

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.configure(bg=C_BG)
        win.transient(self.root)

        cv = tk.Canvas(win, width=dw, height=800, bd=0,
                       highlightthickness=0, bg=C_BG)
        cv.place(x=0, y=0, width=dw, height=800)

        cv.create_text(pad, 28, text=title, anchor="w", fill="#ffffff",
                       font=(FONT_UI, 11, "bold"))

        # 逐行排，空行当段间距；用 bbox 拿真实高度，不靠字符数猜
        y = 62
        for line in lines:
            if not line.strip():
                y += 10
                continue
            it = cv.create_text(pad, y, text=line, anchor="nw",
                                fill="#c8cfdc", font=(FONT_UI, 9),
                                width=dw - pad * 2)
            bb = cv.bbox(it)
            y = (bb[3] if bb else y + 18) + 4

        dh = int(y + 50)

        px = self.root.winfo_rootx() + (self.win_w - dw) // 2
        py = max(16, self.root.winfo_rooty() + max(20, (self.win_h - dh) // 3))
        win.geometry(f"{dw}x{dh}+{px}+{py}")
        cv.configure(height=dh)
        cv.place(x=0, y=0, width=dw, height=dh)

        plate = self.glass_plate(dw, dh, 0.44)
        if plate is not None:
            tkimg = ImageTk.PhotoImage(plate.convert("RGB"))
            cv.create_image(0, 0, image=tkimg, anchor="nw", tags=("plate",))
            cv.image = tkimg          # 必须挂住引用，否则被 GC 后底图消失
            cv.tag_lower("plate")

        result = {"ok": False}

        def _ok():
            result["ok"] = True
            win.destroy()

        bh = 34
        by = dh - bh - 16
        bw_ok = self.button_width(ok_text)
        bx = dw - pad - bw_ok
        btns = [GlassButton(self, ok_text, _ok, bx, by, bw_ok, bh, "primary",
                            parent=cv)]
        if cancel_text:
            bw_cancel = self.button_width(cancel_text)
            btns.append(GlassButton(self, cancel_text, win.destroy,
                                    bx - bw_cancel - 10, by, bw_cancel, bh,
                                    "ghost", parent=cv))

        for b in btns:
            # 两个按钮宽度现在可能不同，place 和切片都必须用各自的 w/h
            b.cv.place(x=b.x, y=b.y, width=b.w, height=b.h)
            # 弹窗按钮也得铺背景切片，否则胶囊四角会露出子 Canvas 的深色底，
            # 在浅色底板上就是一个黑方块（主窗口里由 glass_slices 负责这件事）
            if plate is not None:
                b.set_background(
                    plate.crop((int(b.x), int(b.y),
                                int(b.x) + b.w, int(b.y) + b.h)).convert("RGB"))

        win.deiconify()
        win.attributes("-topmost", True)
        win.lift()
        try:
            win.update_idletasks()
            apply_round_region(win, 20)
            win.grab_set()
        except Exception:
            pass

        try:
            self.root.wait_window(win)
        except Exception:
            pass
        return result["ok"]

    def ask(self, title, msg, ok_text="确定", cancel_text="取消"):
        return self._dialog(title, msg, ok_text, cancel_text)

    def info(self, title, msg):
        return self._dialog(title, msg, "知道了")
