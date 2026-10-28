# -*- coding: utf-8 -*-
"""自绘控件：按钮、图标按钮、开关、表格、日志视图。

统一遵循三条约定：

1. **每个控件是一个子 Canvas**，`place` 在父 Canvas 上。
   Tk 原生的 Button / Treeview 是系统绘制的，做不出玻璃质感。
2. **子 Canvas 不能留深色底**。圆角之外的四角会露出方框，
   所以父窗口重建背景时必须给每个控件切一块对应区域的底图
   （`set_background`），这就是 `GlassWindow.glass_slices()` 的用途。
3. **半透明元素一律走 PhotoImage**。Canvas 的纯色图元会盖住玻璃底。
"""

import time
import tkinter as tk

from .compat import Image, ImageChops, ImageDraw, ImageFilter, ImageTk
from .shapes import rrect
from .theme import (C_ACCENT, C_BG, C_ORANGE, C_PANEL, C_RED, FONT_MONO,
                    FONT_UI, LOG_COLORS)


def _rgb(color, fallback=(255, 255, 255)):
    """`#rrggbb` / `#rgb` → (r, g, b)。需要自己插值渐变时用。"""
    try:
        s = str(color).strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return fallback


def _mix(a, b, t):
    """两个 RGB 之间线性插值，t=0 取 a、t=1 取 b。"""
    t = max(0.0, min(1.0, float(t)))
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def _photo_slot(widget):
    """找出控件当前那张底图贴图存在哪个属性上（各类命名不统一）。"""
    for name in ("_bg_photo", "_bg"):
        ph = getattr(widget, name, None)
        if isinstance(ph, ImageTk.PhotoImage):
            return ph
    return None


def _fast_background(self, pil_img):
    """拖动帧专用：只换底图贴图的**像素**，不重建贴图、不重绘任何图元。

    为什么不能直接用 `set_background()`：它会顺带重建行高亮/图标贴图并把
    整块画布 `delete("all")` 重画一遍。拖动时每帧都走那条路，单是表格就要
    几十毫秒，主循环直接被拖死。

    第二层节省在贴图复用：`ImageTk.PhotoImage(piece)` 每次都要在 Tcl 侧
    新建一张 photo、再把整块像素灌过去；13 片切片一帧实测 21ms，比引擎
    合成（15ms）还贵。尺寸没变时改成往**同一张** photo 里 `paste`，Tk 会
    自己让所有显示它的图元重画，连 `itemconfigure` 都不用发 —— 同一帧
    1.6ms，差一个数量级。

    Returns:
        bool: False 表示没有现成的 bg 图元（首次绘制前），调用方应回退到
        `set_background()`。
    """
    if pil_img is None:
        return False
    # 图元 id 缓存：`find_withtag` 每次要遍历标签表，13 片一帧就是 13 次；
    # 缓存后只在重绘（delete("all") 会带走旧 id）时才重新查一次。
    # 必须验一下 id 上还挂着 "bg" 标签 —— 光靠 try/except 不够：Tk 图元
    # id 被回收复用时不报错，会静默改到别的图元（比如某一行的高亮条）上。
    it = getattr(self, "_bg_item", None)
    if it is not None:
        try:
            if "bg" not in self.cv.gettags(it):
                it = None
        except Exception:
            it = None
        if it is None:
            self._bg_item = None
    if it is None:
        try:
            ids = self.cv.find_withtag("bg")
        except Exception:
            return False
        if not ids:
            return False
        it = ids[0]

    ph = _photo_slot(self)
    same = False
    if ph is not None:
        try:
            same = (ph.width() == pil_img.width
                    and ph.height() == pil_img.height)
        except Exception:
            same = False
    if same:
        try:
            ph.paste(pil_img)
        except Exception:
            same = False
    if same:
        self._bg_item = it
        _hold_bg_ref(self, ph)
        return True

    try:
        photo = ImageTk.PhotoImage(pil_img)
        self.cv.itemconfigure(it, image=photo)
    except Exception:
        return False
    self._bg_item = it
    _hold_bg_ref(self, photo)
    return True


def _hold_bg_ref(widget, photo):
    """PhotoImage 必须挂住引用，否则被 GC 后底图变空白。"""
    if hasattr(widget, "_bg_photo"):
        widget._bg_photo = photo
    if hasattr(widget, "_bg"):
        widget._bg = photo


class GlassButton:
    """胶囊按钮：三态贴图（常态 / 悬停 / 按下）+ Canvas 文字。

    文字用 `create_text` 而不是画进贴图 —— Tk 的字体渲染带 ClearType，
    画进 PIL 后会失去亚像素抗锯齿，中文边缘会发毛。
    """

    def __init__(self, app, text, cmd, x, y, w, h, kind="ghost", parent=None):
        self.app = app
        self.text = text
        self.cmd = cmd
        self.kind = kind
        self.x, self.y, self.w, self.h = x, y, w, h
        self.enabled = True
        self.state = "normal"
        self._imgs = {}
        self._tkimg = {}
        self._bg = None

        master = parent or app.cv
        self.cv = tk.Canvas(master, width=w, height=h, bd=0,
                            highlightthickness=0, bg=C_BG, cursor="hand2")
        self.cv.place(x=x, y=y, width=w, height=h)
        self._build_images()
        self.cv.bind("<Enter>", lambda e: self._set_state("hover"))
        self.cv.bind("<Leave>", lambda e: self._set_state("normal"))
        self.cv.bind("<Button-1>", lambda e: self._on_press())
        self.cv.bind("<ButtonRelease-1>", lambda e: self._on_release())
        self.redraw()

    def set_background(self, pil_img):
        """铺所在区域的背景切片，圆角外的部分才不会露成方框。"""
        self._bg = None if pil_img is None else ImageTk.PhotoImage(pil_img)
        self.redraw()

    def _build_images(self):
        if not self.app.engine.ok:
            return
        for st in ("normal", "hover", "press"):
            img = self.app.engine.button_image(self.w, self.h, self.kind, st)
            if img is not None:
                self._imgs[st] = img
                self._tkimg[st] = ImageTk.PhotoImage(img)

    def rebuild_images(self):
        """按当前引擎参数重新生成三态贴图。

        窗口的玻璃强度（`GlassEngine.VEIL`）可以在运行期被改掉，
        而底面 alpha 是烘进贴图里的 —— 改完必须让按钮重画一次，
        否则按钮和背景的层次会对不上。
        """
        if not self.app.engine.ok:
            return
        self._imgs = {}
        self._tkimg = {}
        self._build_images()
        self.redraw()

    def _set_state(self, st):
        if not self.enabled:
            return
        self.state = st
        self.redraw()

    def _on_press(self):
        if self.enabled:
            self._set_state("press")

    def _on_release(self):
        if not self.enabled:
            return
        self._set_state("hover")
        try:
            self.cmd()
        except Exception:
            pass

    def configure(self, **kw):
        if "text" in kw:
            self.text = kw["text"]
        if "state" in kw:
            self.enabled = (kw["state"] != "disabled")
            self.cv.configure(cursor="hand2" if self.enabled else "arrow")
        self.redraw()

    config = configure

    def redraw(self):
        cv = self.cv
        cv.delete("all")
        if self._bg is not None:
            cv.create_image(0, 0, image=self._bg, anchor="nw", tags=("bg",))
        if self.enabled and self.state in self._tkimg:
            cv.create_image(0, 0, image=self._tkimg[self.state], anchor="nw")
        elif not self._tkimg:
            # 没有 Pillow 时的兜底
            rrect(cv, 0, 0, self.w, self.h, self.h / 2,
                  fill=C_ACCENT if self.kind == "primary" else "#33384a",
                  outline="")

        if self.kind == "primary":
            fg = "#ffffff" if self.enabled else "#c8d2e0"
        else:
            fg = "#eef2f9" if self.enabled else "#6a7183"
        cv.create_text(self.w / 2.0, self.h / 2.0 + 1, text=self.text,
                       fill=fg,
                       font=(FONT_UI, 10,
                             "bold" if self.kind == "primary" else "normal"))


class IconButton:
    """标题栏用的圆形图标按钮（最小化 / 关闭 / 帮助）。"""

    def __init__(self, app, glyph, cmd, x, y, size=30, danger=False,
                 glyph_font=None):
        """构造圆形图标按钮。

        Args:
            app: 宿主窗口（需要 `cv`）。
            glyph: 图标字符，如 "✕" "—" "?" "⚙"。
            cmd: 点击回调。
            x, y: 相对 `app.cv` 的左上角坐标。
            size: 按钮直径。标题栏三个键是靠 `w - 42/76/110` 排开的，
                改这个值要一并调整间距。
            danger: True 时悬停底色为红色（关闭键用）。
            glyph_font: 图标字体。某些符号（如 ⚙ U+2699）不在雅黑里，
                需要指定系统符号字体，否则会渲染成豆腐块。
        """
        self.app = app
        self.glyph = glyph
        self.cmd = cmd
        self.size = size
        self.danger = danger
        self.glyph_font = glyph_font
        self.x, self.y = x, y
        self._bg = None
        self.cv = tk.Canvas(app.cv, width=size, height=size, bd=0,
                            highlightthickness=0, bg=C_BG, cursor="hand2")
        self.cv.place(x=x, y=y, width=size, height=size)
        self.hover = False
        self.cv.bind("<Enter>", lambda e: self._hov(True))
        self.cv.bind("<Leave>", lambda e: self._hov(False))
        self.cv.bind("<Button-1>", lambda e: self.cmd())
        self.redraw()

    def set_background(self, pil_img):
        self._bg = None if pil_img is None else ImageTk.PhotoImage(pil_img)
        self.redraw()

    def _hov(self, v):
        self.hover = v
        self.redraw()

    def redraw(self):
        cv = self.cv
        cv.delete("all")
        s = self.size
        if self._bg is not None:
            cv.create_image(0, 0, image=self._bg, anchor="nw", tags=("bg",))
        if self.hover:
            # 白色圆底配深色字是 Windows 的通用约定，关闭键用红色
            if self.danger:
                cv.create_oval(1, 1, s - 1, s - 1, fill=C_RED, outline="")
            else:
                cv.create_oval(1, 1, s - 1, s - 1, fill="#ffffff", outline="")
        fg = "#0b0d13" if self.hover else "#aab2c4"
        if self.hover and self.danger:
            fg = "#ffffff"
        font = self.glyph_font or (FONT_UI, 10 if self.glyph != "✕" else 9,
                                   "bold")
        cv.create_text(s / 2.0, s / 2.0 + (1 if self.glyph == "—" else 0),
                       text=self.glyph, fill=fg, font=font)


class GlassToggle:
    """iOS 风格开关。

    轨道和滑块全部用 PIL 渲染：Canvas 的椭圆 + 多边形拼不出精确的胶囊，
    也没法做滑块底部的柔和投影。
    """

    def __init__(self, app, var, x, y, label, w=48, h=28, label_w=152):
        self.app = app
        self.var = var
        self.label = label
        self.bw, self.bh = w, h
        self.pad_x, self.pad_y = 3, 4
        self.cw = w + 6
        self.ch = h + 10
        self.label_w = label_w
        self._imgs = {}
        self._bg = None

        self.cv = tk.Canvas(app.cv, width=self.cw + label_w, height=self.ch,
                            bd=0, highlightthickness=0, bg=C_BG,
                            cursor="hand2")
        self.cv.place(x=x, y=y, width=self.cw + label_w, height=self.ch)
        self._build()
        self.cv.bind("<Button-1>", lambda e: self._toggle())
        self.redraw()

    def _build(self):
        if not self.app.engine.ok:
            return
        for on in (True, False):
            try:
                self._imgs[on] = ImageTk.PhotoImage(self._render(on))
            except Exception:
                pass

    def _render(self, on):
        from .engine import GlassEngine

        w, h = self.bw, self.bh
        px, py = self.pad_x, self.pad_y
        r = h / 2.0
        img = Image.new("RGBA", (self.cw, self.ch), (0, 0, 0, 0))

        # --- 轨道：竖向渐变 + 圆角裁剪 + 内圈亮边 ---
        top, bot = ((30, 142, 255), (4, 92, 200)) if on else \
                   ((76, 84, 106), (50, 56, 74))
        grad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grad)
        for i in range(h):
            t = i / float(max(1, h - 1))
            gd.line([(0, i), (w, i)],
                    fill=(int(top[0] + (bot[0] - top[0]) * t),
                          int(top[1] + (bot[1] - top[1]) * t),
                          int(top[2] + (bot[2] - top[2]) * t), 255))
        grad = GlassEngine.mask_round(grad, r)
        img.paste(grad, (px, py))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([px, py, px + w - 1, py + h - 1], radius=r,
                            outline=(255, 255, 255, 52), width=1)

        # 这里原本还有一条"凹槽暗边"，高度 1px 而圆角半径 14，
        # 圆角会退化成正弦直线，看起来就是轨道上缘一道黑线，故删除。

        # --- 滑块 ---
        kd = h - 6
        kx = (px + w - 3 - kd) if on else (px + 3)
        ky = py + 3

        # 投影
        sh = Image.new("RGBA", (self.cw, self.ch), (0, 0, 0, 0))
        ImageDraw.Draw(sh).ellipse([kx, ky + 1, kx + kd, ky + kd + 1],
                                   fill=(0, 0, 0, 92))
        sh = sh.filter(ImageFilter.GaussianBlur(2.2))
        img = Image.alpha_composite(img, sh)

        # 白钮
        knob = Image.new("RGBA", (self.cw, self.ch), (0, 0, 0, 0))
        ImageDraw.Draw(knob).ellipse([kx, ky, kx + kd, ky + kd],
                                     fill=(255, 255, 255, 255))
        img = Image.alpha_composite(img, knob)

        # 球体明暗：下半部压一点暗，避免纯白圆片显得扁
        sph = Image.new("RGBA", (self.cw, self.ch), (0, 0, 0, 0))
        ImageDraw.Draw(sph).ellipse([kx, ky + kd * 0.52, kx + kd, ky + kd],
                                    fill=(70, 84, 108, 60))
        sph = sph.filter(ImageFilter.GaussianBlur(2.4))
        m = Image.new("L", (self.cw, self.ch), 0)
        ImageDraw.Draw(m).ellipse([kx, ky, kx + kd, ky + kd], fill=255)
        sph.putalpha(ImageChops.multiply(sph.split()[3], m))
        img = Image.alpha_composite(img, sph)

        # 轮廓线，让它从轨道上"浮"起来
        edge = Image.new("RGBA", (self.cw, self.ch), (0, 0, 0, 0))
        ImageDraw.Draw(edge).ellipse([kx, ky, kx + kd, ky + kd],
                                     outline=(255, 255, 255, 150), width=1)
        img = Image.alpha_composite(img, edge)
        return img

    def set_background(self, pil_img):
        self._bg = None if pil_img is None else ImageTk.PhotoImage(pil_img)
        self.redraw()

    def _toggle(self):
        self.var.set(not self.var.get())
        self.redraw()

    def redraw(self):
        cv = self.cv
        cv.delete("all")
        if self._bg is not None:
            cv.create_image(0, 0, image=self._bg, anchor="nw", tags=("bg",))
        on = bool(self.var.get())
        if on in self._imgs:
            cv.create_image(0, 0, image=self._imgs[on], anchor="nw")
        else:
            rrect(cv, self.pad_x, self.pad_y,
                  self.pad_x + self.bw, self.pad_y + self.bh, self.bh / 2,
                  fill=C_ACCENT if on else "#3a4054", outline="")
        cv.create_text(self.cw + 10, self.ch / 2.0, text=self.label, anchor="w",
                       fill="#b9c1d2" if on else "#8e96a8", font=(FONT_UI, 9))


class GlassSlider:
    """横向玻璃滑块：圆角轨道 + 主题色渐变填充 + 白色圆形滑块头。

    为什么整条用 PIL 渲染成 PhotoImage：

    * Canvas 的 `create_line` 画不出带圆角的轨道，端点是硬方角；
    * 纯色填充会盖住玻璃底（Canvas 没有 alpha）；
    * 滑块头需要投影和球面明暗，`create_oval` 做不出来。

    交互按"轨道任意位置点击立即跳值 + 按住拖动实时跟随 + 松手回调"实现：

    * `on_drag(value)` —— 拖动过程中回调，节流到 `drag_interval`（默认 60ms），
      给需要"实时预览"的调用方用；
    * `on_change(value)` —— 松手时才回调，给"比较贵、不能每帧做"的动作用
      （比如重建玻璃背景）。
    """

    def __init__(self, app, x, y, w, value=0.5, on_change=None, h=8,
                 parent=None, on_drag=None, knob=18, drag_interval=0.06):
        """构造滑块。

        Args:
            app: 宿主窗口，需要有 `cv` 与 `engine`（GlassWindow 系列都满足）。
            x, y: 滑块画布左上角坐标（相对 `parent`）。
            w: 轨道长度（像素，不含两侧给滑块头留的余量）。
            value: 初始值，0.0~1.0。
            on_change: 松手回调，签名 `fn(value: float)`。
            h: 轨道高度（像素）。
            parent: 放置滑块画布的父容器，默认 `app.cv`。
            on_drag: 拖动中的节流回调，签名 `fn(value: float)`。
            knob: 滑块头直径（像素）。
            drag_interval: `on_drag` 的最小触发间隔（秒）。
        """
        self.app = app
        self.track_w = max(40, int(w))
        self.track_h = max(4, int(h))
        self.knob_d = max(12, int(knob))
        self.value = max(0.0, min(1.0, float(value)))
        self.on_change = on_change
        self.on_drag = on_drag
        self.drag_interval = max(0.0, float(drag_interval))
        self.enabled = True

        # 滑块头半径的两侧余量：值为 0 / 1 时圆头都不能越出画布
        self.pad = self.knob_d / 2.0 + 1.0
        self.cw = int(round(self.track_w + self.pad * 2))
        self.ch = int(max(self.knob_d, self.track_h) + 6)

        self._bg = None
        self._img = None
        self._tkimg = None
        self._key = None
        self._drag = False
        self._last_drag_ts = 0.0

        master = parent or app.cv
        self.cv = tk.Canvas(master, width=self.cw, height=self.ch, bd=0,
                            highlightthickness=0, bg=C_BG, cursor="hand2")
        self.cv.place(x=x, y=y, width=self.cw, height=self.ch)
        # 拖动时指针常常跑到轨道外面，Tk 的隐式抓取会继续把 Motion 发给本画布
        self.cv.bind("<Button-1>", self._on_press)
        self.cv.bind("<B1-Motion>", self._on_move)
        self.cv.bind("<ButtonRelease-1>", self._on_release)
        self.redraw()

    # ------------------------------------------------------------ 对外接口
    def set_background(self, pil_img):
        """铺所在区域的背景切片，否则圆角外会露出深色方块（同 GlassButton）。"""
        self._bg = None if pil_img is None else ImageTk.PhotoImage(pil_img)
        self.redraw()

    def get_value(self):
        return self.value

    def set_value(self, value, notify=False):
        """外部改值（不触发拖动）。`notify=True` 时按松手处理，回调 on_change。"""
        self.value = max(0.0, min(1.0, float(value)))
        self.redraw()
        if notify and self.on_change is not None:
            try:
                self.on_change(self.value)
            except Exception:
                pass
        return self.value

    def configure(self, **kw):
        if "value" in kw:
            self.set_value(kw["value"])
        if "state" in kw:
            self.enabled = (kw["state"] != "disabled")
            self.cv.configure(cursor="hand2" if self.enabled else "arrow")

    config = configure

    # -------------------------------------------------------------- 交互
    def _value_at(self, px):
        """画布横坐标 → 0~1 的值。轨道两端之间线性映射，越界夹紧。"""
        span = float(self.track_w)
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (float(px) - self.pad) / span))

    def _on_press(self, event):
        if not self.enabled:
            return
        self._drag = True
        self._last_drag_ts = 0.0
        self._apply(self._value_at(event.x), drag=True)

    def _on_move(self, event):
        if not self._drag or not self.enabled:
            return
        self._apply(self._value_at(event.x), drag=True)

    def _on_release(self, event):
        if not self._drag:
            return
        self._drag = False
        self._apply(self._value_at(event.x), drag=False)
        if self.on_change is not None:
            try:
                self.on_change(self.value)
            except Exception:
                pass

    def _apply(self, value, drag):
        self.value = max(0.0, min(1.0, float(value)))
        self.redraw()
        if drag and self.on_drag is not None:
            now = time.time()
            if now - self._last_drag_ts >= self.drag_interval:
                self._last_drag_ts = now
                try:
                    self.on_drag(self.value)
                except Exception:
                    pass

    # -------------------------------------------------------------- 绘制
    @staticmethod
    def _mask_round(layer, r):
        """按圆角轮廓裁掉渐变溢出到端点的像素。"""
        w, h = layer.size
        m = Image.new("L", (w, h), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1],
                                            radius=r, fill=255)
        layer.putalpha(ImageChops.multiply(layer.split()[3], m))
        return layer

    def _render(self):
        """渲染整条滑块（轨道 + 填充 + 滑块头）。失败返回 None。"""
        if not self.app.engine.ok:
            return None
        try:
            return self._render_unsafe()
        except Exception:
            return None

    def _render_unsafe(self):
        w, h = self.cw, self.ch
        tw, th = self.track_w, self.track_h
        x1 = int(round(self.pad))
        ty = int((h - th) // 2)
        x2 = x1 + tw
        r = th / 2.0
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))

        # --- 1. 轨道底：半透明深色 + 1px 亮边（独立图层再合成）---
        base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(base).rounded_rectangle(
            [x1, ty, x2 - 1, ty + th - 1], radius=r,
            fill=(0, 0, 0, 86), outline=(255, 255, 255, 40), width=1)
        img = Image.alpha_composite(img, base)

        # --- 2. 已填充段：主题色竖向渐变（顶部加高光，不用 arc）---
        fw = int(round(tw * self.value))
        if fw > 0:
            fw = min(tw, max(int(th), fw))      # 极短时保持胶囊形，不留尖角
            accent = _rgb(C_ACCENT)
            top = _mix(accent, (255, 255, 255), 0.34)
            bot = _mix(accent, (0, 0, 0), 0.22)
            grad = Image.new("RGBA", (fw, th), (0, 0, 0, 0))
            gd = ImageDraw.Draw(grad)
            for i in range(th):
                t = i / float(max(1, th - 1))
                add = int(38 * (1.0 - t) * (1.0 - t))     # 顶部柔光
                c = _mix(top, bot, t)
                gd.line([(0, i), (fw, i)],
                        fill=(min(255, c[0] + add), min(255, c[1] + add),
                              min(255, c[2] + add), 255))
            grad = self._mask_round(grad, min(r, fw / 2.0))
            img.alpha_composite(grad, (x1, ty))

        # --- 3. 滑块头：投影 → 白色主体 → 球面明暗 → 轮廓 ---
        cx = self.pad + tw * self.value
        kd = float(self.knob_d)
        kx = cx - kd / 2.0
        ky = (h - kd) / 2.0

        sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(sh).ellipse([kx + 0.5, ky + 1.5,
                                    kx + kd + 0.5, ky + kd + 1.5],
                                   fill=(0, 0, 0, 110))
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(2.0)))

        knob = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(knob).ellipse([kx, ky, kx + kd, ky + kd],
                                     fill=(255, 255, 255, 255))
        img = Image.alpha_composite(img, knob)

        sph = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(sph).ellipse([kx, ky + kd * 0.50, kx + kd, ky + kd],
                                    fill=(70, 84, 108, 70))
        sph = sph.filter(ImageFilter.GaussianBlur(2.2))
        m = Image.new("L", (w, h), 0)
        ImageDraw.Draw(m).ellipse([kx, ky, kx + kd, ky + kd], fill=255)
        sph.putalpha(ImageChops.multiply(sph.split()[3], m))
        img = Image.alpha_composite(img, sph)

        edge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(edge).ellipse([kx, ky, kx + kd, ky + kd],
                                     outline=(255, 255, 255, 165), width=1)
        img = Image.alpha_composite(img, edge)
        return img

    def redraw(self):
        cv = self.cv
        cv.delete("all")
        if self._bg is not None:
            cv.create_image(0, 0, image=self._bg, anchor="nw", tags=("bg",))

        # 拖动时每帧都会走这里：按 1/400 量化缓存，避免重复做高斯模糊
        key = (int(round(self.value * 400)), self.cw, self.ch)
        if self._tkimg is None or key != self._key:
            img = self._render()
            self._key = key
            self._img = img
            self._tkimg = None if img is None else ImageTk.PhotoImage(img)

        if self._tkimg is not None:
            cv.create_image(0, 0, image=self._tkimg, anchor="nw")
        else:
            self._redraw_fallback(cv)

    def _redraw_fallback(self, cv):
        """没有 Pillow 时的兜底：纯色胶囊 + 实心圆点。"""
        x1 = self.pad
        x2 = x1 + self.track_w
        ty = (self.ch - self.track_h) / 2.0
        rrect(cv, x1, ty, x2, ty + self.track_h, self.track_h / 2.0,
              fill="#3a4054", outline="")
        fx = x1 + self.track_w * self.value
        if self.value > 0:
            rrect(cv, x1, ty, max(x1 + self.track_h, fx), ty + self.track_h,
                  self.track_h / 2.0, fill=C_ACCENT, outline="")
        kd = self.knob_d
        cx = x1 + self.track_w * self.value
        cv.create_oval(cx - kd / 2.0, (self.ch - kd) / 2.0,
                       cx + kd / 2.0, (self.ch + kd) / 2.0,
                       fill="#ffffff", outline="")


class GlassTable:
    """可滚动的自绘表格。

    为什么不用 ttk.Treeview：Treeview 由系统主题绘制，背景是实色，
    铺不上玻璃底；而且行高、选中色、字体都无法精确控制。
    这个实现保持了 Treeview 的一小部分接口（get_children / insert / delete /
    selection / selection_set / selection_add / selection_remove / item），
    方便把已有代码迁移过来。

    ⚠️ **`insert()` 不会立刻重绘**，只在空闲时（`after_idle`）合并重绘一次 ——
    批量灌几百行时逐行重绘会卡住界面。需要"插完马上看到"的场合
    （比如自检、截图、`--shot`），请显式调一次 `redraw()`；否则在事件循环
    跑起来之前画布上会是空的（这个坑真的踩过：表头显示「共 339 个进程」，
    下面一行都没有）。
    """

    ROW_H = 27
    ROW_PAD = 6             # 行底纹左右内缩，避免直边贴死卡片描边
    ROW_BH = 24             # 行底纹高度（比行高低 3px，行间留一道缝）
    ROW_R = 8               # 行底纹圆角
    COLS = (
        ("name", 0.00, 0.42, "w", "名称"),
        ("pid", 0.42, 0.53, "e", "PID"),
        ("mem", 0.50, 0.62, "e", "内存"),
        ("note", 0.66, 1.00, "w", "备注"),
    )

    # 行样式由 tag 驱动。库不认识具体的 tag 名，只按这三张表查色，
    # 用的人自己往 insert(tags=[...]) 里塞字符串即可。
    ROW_FG = {"warn": C_ORANGE, "muted": "#949db0"}   # tag -> 主列前景色
    ROW_DOT = ("warn",)                               # 这些 tag 左侧画圆点
    NOTE_FG = {"warn": C_ORANGE}                      # tag -> 备注列颜色
    NOTE_FG_DEFAULT = "#8e96a8"
    NOTE_FG_SELECTED = "#c3cad8"

    # 滚动条：thumb 画在画布右缘（w-8 至 w-2），命中区比可见宽一截，
    # 不然 6px 的目标点起来太费劲。三种状态色：默认 / 悬停 / 拖动中。
    SB_HIT = 16
    SB_COLORS = {"idle": "#4d5468", "hover": "#7d879c", "drag": "#aeb8cc"}

    def __init__(self, app, x, y, w, h):
        self.app = app
        self.w, self.h = w, h
        self.cv = tk.Canvas(app.cv, width=w, height=h, bd=0,
                            highlightthickness=0, bg=C_BG)
        self.cv.place(x=x, y=y, width=w, height=h)

        self.rows = []          # [(name, pid, ws, tag, note)]
        self.top = 0
        self.sel = set()        # 选中的 pid(str)
        self.hover_i = -1
        self._tags = {}
        self._bg_photo = None
        self._row_imgs = {}
        self._dirty = False     # 是否已有排队的空闲重绘

        # 滚动条状态。_sb_drag 记"鼠标相对 thumb 顶端的偏移"，
        # None 表示没在拖动 —— 拖动中点击与悬停逻辑都要给它让路
        self._sb_drag = None
        self._sb_hover = False

        self.cv.bind("<MouseWheel>", self._on_wheel)
        self.cv.bind("<Button-4>", lambda e: self._scroll(-3))
        self.cv.bind("<Button-5>", lambda e: self._scroll(3))
        self.cv.bind("<Button-1>", self._on_click)
        self.cv.bind("<B1-Motion>", self._on_drag_motion)
        self.cv.bind("<ButtonRelease-1>", self._on_release)
        self.cv.bind("<Motion>", self._on_motion)
        self.cv.bind("<Leave>", self._on_leave)
        self.cv.bind("<Configure>", lambda e: self.redraw())

    # ---------- 兼容 Treeview 的接口 ----------
    def get_children(self):
        return tuple(str(r[1]) for r in self.rows)

    def selection(self):
        return tuple(self.sel)

    @staticmethod
    def _as_ids(items):
        """把 "123" / 123 / ("1","2") 统一成字符串列表。"""
        if items is None:
            return []
        if isinstance(items, (str, int)):
            return [str(items)]
        return [str(i) for i in items]

    def selection_set(self, items):
        """整体替换选中项（Treeview 兼容）。"""
        self.sel = set(self._as_ids(items))
        self.redraw()

    def selection_add(self, items):
        """追加选中项（等价于按住 Ctrl 点选）。"""
        self.sel |= set(self._as_ids(items))
        self.redraw()

    def selection_remove(self, items):
        """取消选中。"""
        self.sel -= set(self._as_ids(items))
        self.redraw()

    def selection_toggle(self, iid):
        """点一下切换选中态，返回切换后的状态。"""
        iid = str(iid)
        if iid in self.sel:
            self.sel.discard(iid)
            on = False
        else:
            self.sel.add(iid)
            on = True
        self.redraw()
        return on

    def item(self, iid, opt=None):
        for r in self.rows:
            if str(r[1]) == str(iid):
                return {"values": (r[0], str(r[1]),
                                   f"{r[2]/1048576:.1f} MB" if r[2] else "—",
                                   r[4])}
        return {"values": ()}

    def delete(self, *items):
        if not items:
            self.rows = []
            self.top = 0
            self.sel.clear()
            self.redraw()
            return
        drop = {str(i) for i in items}
        self.rows = [r for r in self.rows if str(r[1]) not in drop]
        self.sel -= drop
        self.redraw()

    def insert(self, parent, index, iid=None, tags=(), values=()):
        """追加一行。**不立刻重绘**，只排一次空闲重绘（批量灌数据友好）。

        需要"插完立刻可见"时请显式调 `redraw()`。
        """
        name, pid = values[0], values[1]
        memtxt, note = values[2], values[3]
        try:
            ws = int(round(float(memtxt.split()[0]) * 1048576))
        except Exception:
            ws = 0
        tag = tags[0] if tags else "normal"
        self.rows.append((name, pid, ws, tag, note))
        self._schedule_redraw()

    def _schedule_redraw(self):
        """把 N 次 insert 合并成一次重绘：只排一个 after_idle。

        没有这一步，`delete() → 批量 insert()` 之后画布会一直停在"已清空"
        的状态，表头写着「共 339 个进程」而下面一行都没有 —— 直到鼠标划过
        表格（`_set_hover` 触发重绘）才突然出现。
        """
        if self._dirty:
            return
        self._dirty = True
        try:
            self.cv.after_idle(self._flush_redraw)
        except Exception:
            self._dirty = False

    def _flush_redraw(self):
        self._dirty = False
        try:
            if self.cv.winfo_exists():
                self.redraw()
        except Exception:
            pass

    def tag_configure(self, tag, **kw):
        self._tags[tag] = kw

    def yview(self):
        n = max(1, len(self.rows) - self.visible_rows())
        return (self.top / n,
                min(1.0, (self.top + self.visible_rows()) / max(1, len(self.rows))))

    # ---------- 内部 ----------
    def visible_rows(self):
        return max(1, (self.h - 4) // self.ROW_H)

    def _sb_geometry(self):
        """滚动条几何：内容不超一屏时返回 None（不显示也不响应）。"""
        total = len(self.rows)
        n_vis = self.visible_rows()
        if total <= n_vis:
            return None
        track_h = self.h - 4
        bar_h = max(30, int(track_h * n_vis / total))
        max_top = max(1, total - n_vis)
        bar_y = 2 + int((track_h - bar_h) * self.top / max_top)
        return {"track_y": 2, "track_h": track_h, "bar_y": bar_y,
                "bar_h": bar_h, "max_top": max_top}

    def _on_wheel(self, e):
        self._scroll(-1 if e.delta > 0 else 1)

    def _scroll(self, d):
        n = max(0, len(self.rows) - self.visible_rows())
        self.top = max(0, min(n, self.top + d))
        self.redraw()

    def _row_at(self, y):
        i = int((y - 2) // self.ROW_H)
        idx = self.top + i
        if 0 <= i < self.visible_rows() and idx < len(self.rows):
            return idx
        return -1

    def _on_click(self, e):
        # 命中滚动条：点 thumb 进入拖动，点轨道朝点击方向翻一屏。
        # 必须赶在行选择之前分流，否则 e.x 落在行区域判定之外
        # 直接 return，滚动条永远点不着。
        if self.w - e.x <= self.SB_HIT:
            g = self._sb_geometry()
            if g is None:
                return
            if g["bar_y"] <= e.y <= g["bar_y"] + g["bar_h"]:
                self._sb_drag = e.y - g["bar_y"]
            elif e.y > g["bar_y"] + g["bar_h"]:
                self._scroll(self.visible_rows())
            else:
                self._scroll(-self.visible_rows())
            return
        self._sb_drag = None
        idx = self._row_at(e.y)
        if idx < 0:
            return
        pid = str(self.rows[idx][1])
        ctrl = bool(e.state & 0x0004)
        if ctrl:
            if pid in self.sel:
                self.sel.discard(pid)
            else:
                self.sel.add(pid)
        else:
            self.sel = {pid}
        self.redraw()

    def _on_drag_motion(self, e):
        """按住滚动条拖动：按 thumb 的位移比例换算滚动位置。"""
        if self._sb_drag is None:
            return
        g = self._sb_geometry()
        if g is None:
            return
        y = e.y - self._sb_drag
        span = max(1, g["track_h"] - g["bar_h"])
        ratio = (y - g["track_y"]) / span
        self.top = max(0, min(g["max_top"],
                              int(round(ratio * g["max_top"]))))
        self.redraw()

    def _on_release(self, _e=None):
        if self._sb_drag is not None:
            self._sb_drag = None
            self.redraw()

    def _set_hover(self, i):
        if i != self.hover_i:
            self.hover_i = i
            self.redraw()

    def _on_motion(self, e):
        # 拖动中悬停高亮会跟着事件乱跳，直接让路给 B1-Motion
        if self._sb_drag is not None:
            return
        want = (self.w - e.x) <= self.SB_HIT
        if want != self._sb_hover:
            self._sb_hover = want
            self.redraw()
        self._set_hover(self._row_at(e.y))

    def _on_leave(self, _e=None):
        if self._sb_hover:
            self._sb_hover = False
            self.redraw()
        self._set_hover(-1)

    def set_background(self, pil_img):
        self._bg_photo = None if pil_img is None else ImageTk.PhotoImage(pil_img)
        self._build_row_images()
        self.redraw()

    def _build_row_images(self):
        """行高亮必须做成带 alpha 的贴图：Canvas 纯色会盖住玻璃底。

        形状做成左右内缩的圆角条而不是通栏矩形 —— 通栏的直边会一直顶到
        卡片描边上，在圆角玻璃卡里显得很"钝"。
        """
        self._row_imgs = {}
        if not self.app.engine.ok or self.w <= 2:
            return
        try:
            bw = max(8, int(self.w) - self.ROW_PAD * 2)
            bh = self.ROW_BH
            for name, rgba in (("hover", (255, 255, 255, 26)),
                               ("sel", (10, 132, 255, 96)),
                               ("alt", (255, 255, 255, 11))):
                im = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
                ImageDraw.Draw(im).rounded_rectangle(
                    [0, 0, bw - 1, bh - 1], radius=self.ROW_R, fill=rgba)
                self._row_imgs[name] = ImageTk.PhotoImage(im)
        except Exception:
            self._row_imgs = {}

    def redraw(self):
        cv = self.cv
        cv.delete("all")
        w, h = self.w, self.h
        if self._bg_photo is not None:
            cv.create_image(0, 0, image=self._bg_photo, anchor="nw",
                            tags=("bg",))
        else:
            cv.create_rectangle(0, 0, w, h, fill=C_PANEL, outline="")

        rows = self.rows
        n_vis = self.visible_rows()
        # 行数变少后滚动位置必须夹回来：否则 top 停在旧的最大值上，
        # `idx >= end` 立刻 break，整张表一行都画不出来
        self.top = max(0, min(self.top, max(0, len(rows) - n_vis)))
        end = min(len(rows), self.top + n_vis)

        for i in range(n_vis):
            y1 = 2 + i * self.ROW_H
            y2 = y1 + self.ROW_H - 1
            idx = self.top + i
            if idx >= end:
                break
            name, pid, ws, tag, note = rows[idx]
            pid_s = str(pid)

            if pid_s in self.sel:
                mark = "sel"
            elif idx == self.hover_i:
                mark = "hover"
            elif i % 2 == 1:
                mark = "alt"
            else:
                mark = None

            by = y1 + (self.ROW_H - self.ROW_BH) // 2
            if mark and mark in self._row_imgs:
                cv.create_image(self.ROW_PAD, by,
                                image=self._row_imgs[mark], anchor="nw")
            elif mark:
                rrect(cv, self.ROW_PAD, by, w - self.ROW_PAD,
                      by + self.ROW_BH, self.ROW_R,
                      fill=C_ACCENT if mark == "sel"
                      else ("#22283a" if mark == "hover" else "#1e2433"),
                      outline="")

            fg = self.ROW_FG.get(tag, "#eef2f9")
            if pid_s in self.sel:
                fg = "#ffffff"

            cy = (y1 + y2) / 2.0
            for key, a, b, anchor, _h in self.COLS:
                x1 = 10 + (w - 20) * a
                x2 = 10 + (w - 20) * b
                if key == "name":
                    # 所有行统一缩进，点只在需要提示的类别上点亮，
                    # 名称左边界才是一条直线，不会因有无圆点而参差
                    txt, x, f = name, x1 + 12, (FONT_UI, 9)
                    if tag in self.ROW_DOT:
                        cv.create_oval(x1 + 0.5, cy - 2.6, x1 + 5.5, cy + 2.4,
                                       fill=C_ORANGE, outline="")
                elif key == "pid":
                    txt, x, f = pid_s, x2 - 14, (FONT_MONO, 9)
                elif key == "mem":
                    txt = f"{ws/1048576:.1f} MB" if ws else "—"
                    x, f = x2 - 14, (FONT_MONO, 9)
                else:
                    txt, x, f = note, x1, (FONT_UI, 8)
                if key == "note":
                    nfg = self.NOTE_FG.get(
                        tag, self.NOTE_FG_SELECTED if pid_s in self.sel
                        else self.NOTE_FG_DEFAULT)
                    if pid_s in self.sel and tag in self.NOTE_FG:
                        nfg = "#ffc46b"
                    cv.create_text(x, cy, text=txt, anchor="w", fill=nfg, font=f)
                else:
                    cv.create_text(x, cy, text=txt, anchor=anchor,
                                   fill=fg, font=f)

        # 滚动条：thumb 颜色跟随交互状态，拖动中最亮。
        # 轨道不单独画底 —— Canvas 实色会盖住玻璃底图，留空反而干净
        if self._sb_geometry() is not None:
            state = ("drag" if self._sb_drag is not None
                     else "hover" if self._sb_hover else "idle")
            g = self._sb_geometry()
            rrect(cv, w - 8, g["bar_y"], w - 2, g["bar_y"] + g["bar_h"], 3,
                  fill=self.SB_COLORS[state], outline="")


class GlassLog:
    """只读日志视图：时间戳用等宽字体，正文按 tag 上色。"""

    LINE_H = 21

    def __init__(self, app, x, y, w, h):
        self.app = app
        self.w, self.h = w, h
        self.lines = []
        self.cv = tk.Canvas(app.cv, width=w, height=h, bd=0,
                            highlightthickness=0, bg=C_BG)
        self.cv.place(x=x, y=y, width=w, height=h)
        self._bg_photo = None
        self.cv.bind("<Configure>", lambda e: self.redraw())

    def set_background(self, pil_img):
        self._bg_photo = None if pil_img is None else ImageTk.PhotoImage(pil_img)
        self.redraw()

    def clear(self):
        self.lines = []
        self.redraw()

    def push(self, msg, tag=None):
        self.lines.append((time.strftime("%H:%M:%S"), msg, tag))
        if len(self.lines) > 300:
            del self.lines[:100]
        self.redraw()

    def redraw(self):
        cv = self.cv
        cv.delete("all")
        w, h = self.w, self.h
        if self._bg_photo is not None:
            cv.create_image(0, 0, image=self._bg_photo, anchor="nw",
                            tags=("bg",))
        else:
            cv.create_rectangle(0, 0, w, h, fill=C_PANEL, outline="")

        n = max(1, h // self.LINE_H)
        shown = self.lines[-n:]
        y = 6
        for ts, msg, tag in shown:
            cv.create_text(10, y, text=ts, anchor="nw", fill="#7d8698",
                           font=(FONT_MONO, 8))
            cv.create_text(76, y, text=msg, anchor="nw",
                           fill=LOG_COLORS.get(tag, LOG_COLORS[None]),
                           font=(FONT_UI, 9), width=max(60, w - 90))
            y += self.LINE_H


class VarText:
    """让 `label.configure(text=...)` 这类旧写法继续可用的小代理。

    只支持 text，够用且不误导 —— 需要别的属性就直接操作 Canvas。
    """

    def __init__(self, app, attr, redraw):
        self.app = app
        self.attr = attr
        self._redraw = redraw

    def configure(self, **kw):
        if "text" in kw:
            setattr(self.app, self.attr, kw["text"])
            self._redraw()

    config = configure


# 六个控件都需要同一条"拖动帧只换底图"的快路，逐个抄一遍容易漏。
# 谁少写一个，拖动到那个控件上方时画面就会停住 —— 静默的视觉 bug。
for _cls in (GlassButton, IconButton, GlassToggle, GlassSlider,
             GlassTable, GlassLog):
    _cls.fast_background = _fast_background
del _cls
