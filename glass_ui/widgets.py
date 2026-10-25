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
            cv.create_image(0, 0, image=self._bg, anchor="nw")
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

    def __init__(self, app, glyph, cmd, x, y, size=30, danger=False):
        self.app = app
        self.glyph = glyph
        self.cmd = cmd
        self.size = size
        self.danger = danger
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
            cv.create_image(0, 0, image=self._bg, anchor="nw")
        if self.hover:
            # 白色圆底配深色字是 Windows 的通用约定，关闭键用红色
            if self.danger:
                cv.create_oval(1, 1, s - 1, s - 1, fill=C_RED, outline="")
            else:
                cv.create_oval(1, 1, s - 1, s - 1, fill="#ffffff", outline="")
        fg = "#0b0d13" if self.hover else "#aab2c4"
        if self.hover and self.danger:
            fg = "#ffffff"
        cv.create_text(s / 2.0, s / 2.0 + (1 if self.glyph == "—" else 0),
                       text=self.glyph, fill=fg,
                       font=(FONT_UI, 10 if self.glyph != "✕" else 9, "bold"))


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
            cv.create_image(0, 0, image=self._bg, anchor="nw")
        on = bool(self.var.get())
        if on in self._imgs:
            cv.create_image(0, 0, image=self._imgs[on], anchor="nw")
        else:
            rrect(cv, self.pad_x, self.pad_y,
                  self.pad_x + self.bw, self.pad_y + self.bh, self.bh / 2,
                  fill=C_ACCENT if on else "#3a4054", outline="")
        cv.create_text(self.cw + 10, self.ch / 2.0, text=self.label, anchor="w",
                       fill="#b9c1d2" if on else "#8e96a8", font=(FONT_UI, 9))


class GlassTable:
    """可滚动的自绘表格。

    为什么不用 ttk.Treeview：Treeview 由系统主题绘制，背景是实色，
    铺不上玻璃底；而且行高、选中色、字体都无法精确控制。
    这个实现保持了 Treeview 的一小部分接口（get_children / insert / delete /
    selection / item），方便把已有代码迁移过来。
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

        self.cv.bind("<MouseWheel>", self._on_wheel)
        self.cv.bind("<Button-4>", lambda e: self._scroll(-3))
        self.cv.bind("<Button-5>", lambda e: self._scroll(3))
        self.cv.bind("<Button-1>", self._on_click)
        self.cv.bind("<Motion>", self._on_motion)
        self.cv.bind("<Leave>", lambda e: self._set_hover(-1))
        self.cv.bind("<Configure>", lambda e: self.redraw())

    # ---------- 兼容 Treeview 的接口 ----------
    def get_children(self):
        return tuple(str(r[1]) for r in self.rows)

    def selection(self):
        return tuple(self.sel)

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
        name, pid = values[0], values[1]
        memtxt, note = values[2], values[3]
        try:
            ws = int(round(float(memtxt.split()[0]) * 1048576))
        except Exception:
            ws = 0
        tag = tags[0] if tags else "normal"
        self.rows.append((name, pid, ws, tag, note))

    def tag_configure(self, tag, **kw):
        self._tags[tag] = kw

    def yview(self):
        n = max(1, len(self.rows) - self.visible_rows())
        return (self.top / n,
                min(1.0, (self.top + self.visible_rows()) / max(1, len(self.rows))))

    # ---------- 内部 ----------
    def visible_rows(self):
        return max(1, (self.h - 4) // self.ROW_H)

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

    def _set_hover(self, i):
        if i != self.hover_i:
            self.hover_i = i
            self.redraw()

    def _on_motion(self, e):
        self._set_hover(self._row_at(e.y))

    def _on_click(self, e):
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
            cv.create_image(0, 0, image=self._bg_photo, anchor="nw")
        else:
            cv.create_rectangle(0, 0, w, h, fill=C_PANEL, outline="")

        rows = self.rows
        n_vis = self.visible_rows()
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

        # 滚动条
        total = len(rows)
        if total > n_vis:
            track_h = h - 4
            bar_h = max(30, int(track_h * n_vis / total))
            max_top = max(1, total - n_vis)
            bar_y = 2 + int((track_h - bar_h) * self.top / max_top)
            rrect(cv, w - 8, bar_y, w - 2, bar_y + bar_h, 3,
                  fill="#7d879c", outline="")


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
            cv.create_image(0, 0, image=self._bg_photo, anchor="nw")
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
