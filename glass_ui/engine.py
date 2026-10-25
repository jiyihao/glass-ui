# -*- coding: utf-8 -*-
"""玻璃渲染引擎：抓屏 → 模糊 → 上色 → 合成静态玻璃层。

## 为什么必须自己抓屏

tkinter 完全没有任何模糊、半透明合成能力：

* `wm_attributes("-alpha", 0.9)` —— 整窗一起变透，文字跟着发虚，不可用。
* `wm_attributes("-transparentcolor", "#010203")` —— 挖洞，那个区域连鼠标
  事件一起消失，控件点不到。
* `ttk` 主题 —— 只能换配色，做不出模糊。

所以只能：BitBlt 抓窗口正后方的桌面 → 程序内高斯模糊 → 把卡片、标题栏、
按钮底面这些"静态玻璃层"一并画进这张图 → 整张当 Canvas 背景铺上去。
Canvas 上就只剩下文字和交互图元，重绘开销极低。

透出来的确实是真实桌面内容，不是渐变模拟出来的。

## 关于 alpha 的坑

`ImageDraw` 在 RGBA 图上**不混合 alpha，只是直接写像素**。
`d.rounded_rectangle(fill=(255, 255, 255, 20))` 之后再 `convert("RGB")`，
得到的是纯白实心块。所以本模块里所有半透明图元都遵循同一个写法：
先 `Image.new("RGBA", ..., (0, 0, 0, 0))` 建独立图层 → 画 → `alpha_composite`。
"""

from .compat import (HAVE_PIL, Image, ImageChops, ImageDraw, ImageEnhance,
                     ImageFilter, ImageGrab, ImageOps)
from .theme import R_CARD, R_WIN


class GlassEngine:
    """抓屏 + 合成。一个窗口一个实例，重复 compose 不会重复抓屏。"""

    BLUR = 30            # 高斯模糊半径。太小像磨砂贴膜，太大看不出桌面
    VEIL = 0.74          # 深色面纱强度。越低越透、文字越难读
    CARD_R = R_CARD
    WIN_R = R_WIN
    TITLE_H = 58         # 顶部标题栏高度，0 表示不要标题栏底色

    def __init__(self):
        self.src = None
        self.ok = HAVE_PIL

    # ------------------------------------------------------------------ 抓屏
    def grab(self, x, y, w, h):
        """抓取屏幕区域作为底图来源。失败返回 None（compose 会用合成底图兜底）。"""
        if not HAVE_PIL or w <= 0 or h <= 0:
            return None
        try:
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
            img = img.convert("RGB")
            if img.size != (w, h):
                img = img.resize((w, h), Image.BILINEAR)
            self.src = img
            return img
        except Exception:
            return None

    # ------------------------------------------------------------------ 合成
    def compose(self, w, h, cards=(), buttons=(), decorators=()):
        """生成最终底图。

        cards      : [(x, y, w, h)] 卡片矩形，会被画成半透明玻璃块
        buttons    : [(x, y, w, h, kind)] 按钮底面，一般留空（按钮有自己的画布）
        decorators : [fn(img) -> img]，卡片画完之后、窗口棱边之前调用的自定义层
        """
        if not HAVE_PIL:
            return None

        base = None
        if self.src is not None:
            try:
                base = self.src.resize((w, h), Image.BILINEAR)
            except Exception:
                base = None
        if base is None:
            base = self._synthetic(w, h)

        img = base.filter(ImageFilter.GaussianBlur(self.BLUR))
        img = ImageEnhance.Color(img).enhance(2.05)      # 提饱和，液体色散感
        img = ImageEnhance.Brightness(img).enhance(0.96)

        veil = Image.new("RGB", (w, h), (13, 15, 23))
        img = Image.blend(img, veil, self.VEIL)
        img = self._glow(img)
        img = self._sheen(img)
        img = self._noise(img)

        img = img.convert("RGBA")
        img = self._card_shadow(img, cards)
        img = self._glass(img, cards)
        for (x, y, cw, ch) in cards:
            img.alpha_composite(self.card_glow(cw, ch), (x, y))
        for fn in decorators:
            img = fn(img)
        img = self._rim(img)
        for (x, y, cw, ch, kind) in buttons:
            img = self._paint_button(img, x, y, cw, ch, kind, "normal")
        return img

    # ------------------------------------------------------------ 兜底底图
    @staticmethod
    def _synthetic(w, h):
        """抓不到屏（远程桌面、锁屏）时的深色渐变底图。"""
        img = Image.new("RGB", (w, h), (18, 22, 34))
        d = ImageDraw.Draw(img)
        for i in range(h):
            t = i / max(1, h - 1)
            d.line([(0, i), (w, i)],
                   fill=(int(16 + 14 * t), int(20 + 18 * t), int(32 + 26 * t)))
        return img

    # ---------------------------------------------------------- 彩色氛围光
    @staticmethod
    def _glow(img):
        """四个角的彩色光斑，screen 混合上去 —— 玻璃边缘的色散感来自这里。"""
        w, h = img.size
        layer = Image.new("RGB", (w, h), (0, 0, 0))
        d = ImageDraw.Draw(layer)
        spots = (
            (0.10, 0.04, (24, 98, 255)),
            (0.94, 0.00, (146, 58, 224)),
            (0.80, 0.94, (0, 148, 214)),
            (0.04, 0.90, (28, 196, 172)),
        )
        for fx, fy, col in spots:
            cx, cy = int(w * fx), int(h * fy)
            rx, ry = int(w * 0.44), int(h * 0.36)
            d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=col)
        layer = layer.filter(ImageFilter.GaussianBlur(int(min(w, h) * 0.20)))
        layer = ImageEnhance.Brightness(layer).enhance(0.52)
        return ImageChops.screen(img, layer)

    # ------------------------------------------------------------ 顶部镜面
    @staticmethod
    def _sheen(img):
        w, h = img.size
        g = ImageOps.invert(Image.linear_gradient("L"))
        g = g.resize((w, h), Image.BILINEAR)
        g = g.point(lambda v: int(v * 0.13))
        return ImageChops.add(img, Image.merge("RGB", (g, g, g)))

    # -------------------------------------------------------- 细噪点
    @staticmethod
    def _noise(img):
        """一层极淡的噪点。没有它，大块渐变看着像塑料。"""
        w, h = img.size
        n = Image.effect_noise((w, h), 9).convert("L")
        a = Image.new("L", (w, h), 11)
        rgba = Image.merge("RGBA", (n, n, n, a))
        out = Image.alpha_composite(img.convert("RGBA"), rgba)
        return out.convert("RGB")

    # ---------------------------------------------------------- 静态玻璃层
    def _card_shadow(self, img, cards):
        if not cards:
            return img
        w, h = img.size
        sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ds = ImageDraw.Draw(sh)
        for (x, y, cw, ch) in cards:
            # 往下偏移 6px 再模糊，做出"卡片浮在玻璃上"的投影
            ds.rounded_rectangle([x + 2, y + 6, x + cw - 2, y + ch + 8],
                                 radius=self.CARD_R, fill=(0, 0, 0, 120))
        sh = sh.filter(ImageFilter.GaussianBlur(14))
        return Image.alpha_composite(img, sh)

    def _glass(self, img, cards):
        w, h = img.size
        gl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(gl)

        # 标题栏：比卡片更亮，突出"顶盖"层次
        if self.TITLE_H > 0:
            d.rectangle([0, 0, w, self.TITLE_H], fill=(255, 255, 255, 12))
            for i in range(self.TITLE_H):
                t = 1.0 - i / float(self.TITLE_H)
                d.line([(0, i), (w, i)], fill=(255, 255, 255, int(18 * t * t)))
            d.line([(0, self.TITLE_H), (w, self.TITLE_H)],
                   fill=(255, 255, 255, 30))

        # 卡片本体：白色极低 alpha 填充 + 双层描边
        for (x, y, cw, ch) in cards:
            d.rounded_rectangle([x, y, x + cw, y + ch],
                                radius=self.CARD_R, fill=(255, 255, 255, 16))
            d.rounded_rectangle([x, y, x + cw, y + ch], radius=self.CARD_R,
                                outline=(255, 255, 255, 52), width=1)
            d.rounded_rectangle([x + 1, y + 1, x + cw - 1, y + ch - 1],
                                radius=self.CARD_R - 1,
                                outline=(255, 255, 255, 14), width=1)

        return Image.alpha_composite(img, gl)

    def card_glow(self, cw, ch):
        """卡片内部顶部柔光，给一点"厚度"感。返回独立小图层。"""
        top_h = max(2, int(ch * 0.55))
        g = Image.new("RGBA", (cw, top_h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(g)
        for i in range(top_h):
            t = 1.0 - i / float(top_h)
            gd.line([(0, i), (cw, i)], fill=(255, 255, 255, int(11 * t * t)))
        m = Image.new("L", (cw, top_h), 0)
        ImageDraw.Draw(m).rounded_rectangle(
            [0, 0, cw - 1, top_h + self.CARD_R], radius=self.CARD_R, fill=255)
        g.putalpha(ImageChops.multiply(g.split()[3], m))
        return g

    def _rim(self, img):
        """窗口棱边：整圈一道亮边 + 顶部更亮，模拟玻璃截面折射。"""
        w, h = img.size
        rim = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        dr = ImageDraw.Draw(rim)
        dr.rounded_rectangle([0, 0, w - 1, h - 1], radius=self.WIN_R,
                             outline=(255, 255, 255, 62), width=1)
        for i in range(w):
            t = 1.0 - abs(i - w / 2.0) / (w / 2.0)
            a = int(46 * t * t)
            if a > 2:
                dr.point((i, 0), fill=(255, 255, 255, a))
                dr.point((i, 1), fill=(255, 255, 255, a // 2))
        return Image.alpha_composite(img, rim)

    # ------------------------------------------------------------- 按钮贴图
    @staticmethod
    def mask_round(layer, r):
        """按圆角轮廓裁掉渐变溢出到四角的像素。"""
        w, h = layer.size
        m = Image.new("L", (w, h), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1],
                                            radius=r, fill=255)
        layer.putalpha(ImageChops.multiply(layer.split()[3], m))
        return layer

    @staticmethod
    def gloss(layer, r, peak=64):
        """顶部柔光。

        不要用 `arc` 去画高光：arc 的包围盒是整个按钮，弧的弦会横穿按钮，
        远看就是一条白线（这是本项目修过的一个真 bug）。
        正确做法是"上半部由亮到无"的竖向渐变，再套圆角蒙版。
        """
        w, h = layer.size
        gh = max(2, int(h * 0.54))
        g = Image.new("RGBA", (w, gh), (0, 0, 0, 0))
        gd = ImageDraw.Draw(g)
        for i in range(gh):
            t = 1.0 - i / float(gh)
            gd.line([(0, i), (w, i)], fill=(255, 255, 255, int(peak * t * t)))
        m = Image.new("L", (w, gh), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, gh + r],
                                            radius=r, fill=255)
        g.putalpha(ImageChops.multiply(g.split()[3], m))

        band = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        band.paste(g, (0, 0))
        return Image.alpha_composite(layer, band)

    @classmethod
    def _paint_button(cls, img, x, y, w, h, kind, state):
        """按钮底面：按按钮尺寸建独立小图层画完，再贴回大图。

        注意 `Image.alpha_composite` 没有 `dest` 参数（Pillow 13 实测），
        在指定位置合成只能靠 crop → composite → paste。
        """
        w, h = int(w), int(h)
        if w <= 1 or h <= 1:
            return img
        r = h / 2.0
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)

        if kind == "primary":
            top = {"normal": (38, 152, 255), "hover": (76, 176, 255),
                   "press": (12, 116, 214)}[state]
            bot = {"normal": (4, 102, 202), "hover": (24, 126, 230),
                   "press": (2, 82, 166)}[state]
            for i in range(h):
                t = i / float(max(1, h - 1))
                d.line([(0, i), (w, i)],
                       fill=(int(top[0] + (bot[0] - top[0]) * t),
                             int(top[1] + (bot[1] - top[1]) * t),
                             int(top[2] + (bot[2] - top[2]) * t), 255))
            layer = cls.mask_round(layer, r)
            layer = cls.gloss(layer, r, 66)
            d = ImageDraw.Draw(layer)
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r,
                                outline=(255, 255, 255, 112), width=1)
        else:
            fill = {"normal": (255, 255, 255, 26), "hover": (255, 255, 255, 48),
                    "press": (255, 255, 255, 16)}[state]
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=fill)
            layer = cls.gloss(layer, r, 40)
            d = ImageDraw.Draw(layer)
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r,
                                outline=(255, 255, 255, 70), width=1)

        region = img.crop((x, y, x + w, y + h))
        img.paste(Image.alpha_composite(region, layer), (x, y))
        return img

    def button_image(self, w, h, kind, state):
        """生成单张按钮贴图（三态各一张，由 GlassButton 缓存）。"""
        if not HAVE_PIL:
            return None
        base = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
        return self._paint_button(base, 0, 0, int(w), int(h), kind, state)

    # ------------------------------------------------------------- 弹窗底板
    def dialog_plate(self, img, radius=20):
        """把一张已模糊/混合好的图加工成弹窗底板。

        弹窗的圆角由 SetWindowRgn 在系统层裁掉，这里只负责"玻璃感"：
        顶部一层渐隐镜面高光 + 双层亮边。颜色不做二次调整 —— 底图是从
        主窗口已合成的 `_bg_img` 缩放来的，色调天然一致。
        """
        if not HAVE_PIL or img is None:
            return None
        w, h = img.size
        img = img.convert("RGBA")

        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        gh = max(2, int(h * 0.5))
        g = Image.new("RGBA", (w, gh), (0, 0, 0, 0))
        gd = ImageDraw.Draw(g)
        for i in range(gh):
            t = 1.0 - i / float(gh)
            gd.line([(0, i), (w, i)], fill=(255, 255, 255, int(20 * t * t)))
        m = Image.new("L", (w, gh), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, gh + radius],
                                            radius=radius, fill=255)
        g.putalpha(ImageChops.multiply(g.split()[3], m))
        layer.alpha_composite(g, (0, 0))

        d = ImageDraw.Draw(layer)
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius,
                            outline=(255, 255, 255, 84), width=1)
        d.rounded_rectangle([1, 1, w - 2, h - 2], radius=max(1, radius - 1),
                            outline=(255, 255, 255, 20), width=1)
        return Image.alpha_composite(img, layer)
