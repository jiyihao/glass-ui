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

## 透明度缩放的三件套（"锚点法"）

用户侧的透明度滑块映射为面纱系数 VEIL（0.18 ~ 0.96），但只缩放面纱
是不够的 —— 光斑强度（`glow_k` → `_glow` 的 strength）和提饱和幅度
（compose 里 `Color.enhance(1.0 + 1.05 * glow_k())`）都必须跟着淡出，
否则最透明端叠出来的是一块浓郁的蓝紫青渐变"调色板"，玻璃特征反而
最不明显。缩放规则：veil ≥ 0.74（出厂端）所有系数恒为出厂值，逐像素
不变；veil 越小光斑/饱和度越弱，下限 0.35 保底（全去掉会死白得像
亚克力板而不是玻璃）。`glass_k`（卡片/描边/按钮 alpha）是另一个方向
的缩放，互不影响。

## 两级缓存：把 121ms 的帧压到 18ms

`compose()` 原本每次调用都从零重建所有图层，实测 980×800 下要 121ms：

    GaussianBlur(18) 18ms  _glow 27ms  _noise 26ms  _card_shadow 22ms
    _sheen 6ms  _glass 4ms  card_glow 2ms  _rim 2ms   其余 ~15ms

但其中 87ms 花在**与底图无关**的静态图层上（光斑、噪点、卡片阴影、
玻璃面、窗口棱边都只跟窗口尺寸和 glass_k 有关）。于是拆成两级：

1. `prepare()` —— 抓一张带余量的大图（窗口四周各留 `PAD` 像素），
   一次做完模糊/提饱和/降亮/叠面纱，存为 `_prep`。窗口拖动时只是从
   这张大图里**换一个位置裁剪**，玻璃后面的桌面就跟着反向平移（视差）。
2. `build_layers()` —— 静态装饰层建一次，其中噪点+卡片阴影+玻璃面+
   卡片柔光按 alpha 结合律**预合并成一张 RGBA**，逐帧只合成一次。

逐帧降到 13~18ms（`frame()`），拖动才谈得上跟手。

合并 alpha 层会引入 ≤2/255 的舍入差（多次 8 位合成合并成一次），肉眼
不可辨；换来的是每帧省掉 3 次全尺寸 alpha 合成。

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

    BLUR = 18            # 高斯模糊半径。30 是历史值，但实测它单独就吃掉
                         # 约一半的桌面结构（见 docs/PITFALLS.md 第 23 条），
                         # 面纱再一压就只剩色块。18 仍保留磨砂质感，纹理可辨
    VEIL = 0.40          # 深色面纱强度。越低越透、文字越难读
    VEIL_DEFAULT = 0.40  # 出厂值，`glass_k` 用它做归一化；改它等于改整条基线
    VEIL_MIN = 0.12      # 透明度 100% 时的面纱强度。历史上是 0.42、后来 0.18，
                         # 但光斑、饱和度不随之缩放，导致最透明端反而像一块
                         # 浓郁的蓝紫青渐变"调色板"——配合 glow_k() 才通透
    VEIL_MAX = 0.92      # 透明度 0% 时的面纱强度
    OPACITY_DEFAULT = 74 # 用户侧的默认透明度（百分比），与 VEIL_DEFAULT 对应
    CARD_R = R_CARD
    WIN_R = R_WIN
    TITLE_H = 58         # 顶部标题栏高度，0 表示不要标题栏底色
    PAD = 220            # 视差余量：预处理大图比窗口每边多抓这么多像素。
                         # 越大能拖得越远不重抓，但模糊的图更大更慢
                         # （980x800 下 220 → 1420x1240，prepare 约 100ms）

    def __init__(self):
        self.src = None
        self.ok = HAVE_PIL
        # 下面这些原本都是类属性。遮罩系数需要在运行期被「界面设置」改掉，
        # 而类属性一改会波及所有实例，所以统一复制一份到实例上。
        self.BLUR = type(self).BLUR
        self.VEIL = type(self).VEIL
        self.CARD_R = type(self).CARD_R
        self.WIN_R = type(self).WIN_R
        self.TITLE_H = type(self).TITLE_H

        # ---- 两级缓存（见模块头）----
        self._raw = None         # 预处理大图：模糊+提饱和+降亮，未叠面纱
        self._prep = None        # _raw 再叠面纱，逐帧从这里裁
        self._prep_wh = (0, 0)   # 预处理中心区域（= 窗口）的尺寸
        self._prep_pad = (0, 0, 0, 0)   # 实际可用余量 left/top/right/bottom
        self._prep_origin = (0, 0)      # 预处理大图左上角的屏幕坐标
        self._layers = None      # 静态装饰层缓存
        self._layers_key = None
        self._veil_dirty = False  # 面纱待重算（见 reveil）

    # --------------------------------------------------- 透明度 / 玻璃强度
    @classmethod
    def opacity_to_veil(cls, opacity):
        """用户语义的「透明度 0~100」→ 引擎的面纱系数。

        两者是反向的：透明度越大 → 面纱越薄（系数越小）→ 越通透。
        三个锚点由产品定义，必须同时命中，因此按 74 分段线性插值：

        ========  ==========  ==============================
        透明度     面纱系数     说明
        ========  ==========  ==============================
        0         0.92        磨砂感最强
        74        0.40        出厂值。旧版这里是 0.74，配合 BLUR=30 实测桌面
                             只透出 5%，观感就是一块"自带底色的色板"，被用户
                             判定为"玻璃感没了"。0.40 + BLUR=18 透出约 15%
        100       0.12        最通透，桌面内容清楚可辨
        ========  ==========  ==============================
        """
        try:
            o = float(opacity)
        except (TypeError, ValueError):
            o = float(cls.OPACITY_DEFAULT)
        o = max(0.0, min(100.0, o))
        pivot = float(cls.OPACITY_DEFAULT)
        if o >= pivot:
            t = (o - pivot) / max(1e-6, 100.0 - pivot)
            return cls.VEIL_DEFAULT + t * (cls.VEIL_MIN - cls.VEIL_DEFAULT)
        t = o / max(1e-6, pivot)
        return cls.VEIL_MAX + t * (cls.VEIL_DEFAULT - cls.VEIL_MAX)

    @classmethod
    def veil_to_opacity(cls, veil):
        """`opacity_to_veil` 的逆运算，给自检与持久化回读用。"""
        try:
            v = float(veil)
        except (TypeError, ValueError):
            v = cls.VEIL_DEFAULT
        v = max(cls.VEIL_MIN, min(cls.VEIL_MAX, v))
        if v <= cls.VEIL_DEFAULT:
            span = max(1e-6, cls.VEIL_DEFAULT - cls.VEIL_MIN)
            t = (cls.VEIL_DEFAULT - v) / span
            return float(cls.OPACITY_DEFAULT) + t * (100.0 - cls.OPACITY_DEFAULT)
        span = max(1e-6, cls.VEIL_MAX - cls.VEIL_DEFAULT)
        t = (cls.VEIL_MAX - v) / span
        return float(cls.OPACITY_DEFAULT) * t

    def set_opacity(self, opacity):
        """按用户语义设置透明度，返回实际生效的百分比（已夹到 0~100）。"""
        self.VEIL = self.opacity_to_veil(opacity)
        # 面纱 -> 底图要重 blend；glass_k -> 静态层（卡片 alpha）要重建。
        # 模糊/提饱和那一步不依赖 VEIL，`_raw` 保留，省掉 40ms 的模糊。
        self.reveil()
        self.drop_layers()
        return int(round(self.veil_to_opacity(self.VEIL)))

    def reveil(self):
        """标记面纱需要重算（改透明度走这里）。

        不做立即计算：滑块拖动时每秒会来几十个事件，每次都重 blend 一张
        1420×1240 的大图要 14ms，滑块会拖出涩感。标记脏，等到真要出图
        时再算一次。
        """
        if self._raw is not None:
            self._veil_dirty = True

    def _ensure_prep(self):
        """面纱脏了就重算一次（惰性）。"""
        if self._raw is not None and self._veil_dirty:
            self._prep = self._apply_veil(self._raw)
            self._veil_dirty = False

    def drop_layers(self):
        """丢弃静态装饰层缓存，下次合成时重建。"""
        self._layers = None
        self._layers_key = None

    def invalidate(self):
        """全部作废：换窗口尺寸、重新抓屏、换卡片布局时调用。"""
        self._raw = None
        self._prep = None
        self._prep_wh = (0, 0)
        self._prep_pad = (0, 0, 0, 0)
        self._prep_origin = (0, 0)
        self._veil_dirty = False
        self.drop_layers()

    @property
    def glass_k(self):
        """玻璃强度系数 = 当前面纱 / 出厂面纱。出厂值恒为 1.0。

        卡片填充、描边、按钮底面都是「白色 + 极低 alpha」的薄层，
        不能写死：面纱变薄后底图更亮，同样的 alpha 会糊得看不见；
        面纱变厚则要更亮一点才能维持层次。所有这类 alpha 都乘这个系数，
        出厂值下乘 1，输出与历史版本逐像素一致。
        """
        try:
            k = float(self.VEIL) / float(self.VEIL_DEFAULT)
        except (TypeError, ValueError, ZeroDivisionError):
            return 1.0
        return max(0.35, min(1.75, k))

    def glow_k(self):
        """彩色光斑/饱和度的强度系数：只在通透端减弱，磨砂端恒为 1。

        与 glass_k 同理的"恒定量不随透明度缩放"问题：四角彩色光斑和
        提饱和都是写死的，面纱越薄底图越透，这两样叠上去的"油彩感"
        越重 —— 用户调到 100% 看到的不是清透玻璃，而是一块浓郁的
        蓝紫青渐变调色板。所以让它们跟着面纱一起淡出：

            veil = 0.18（透明度 100%）→ 0.35   光斑饱和度剩三成半
            veil >= 0.74（≤出厂透明度）→ 1.0    出厂端逐像素不变

        返回值下限 0.35 而不是 0：完全去掉光斑会显得死白，留一点
        色散感才像"玻璃"而不是"亚克力板"。
        """
        try:
            span = float(self.VEIL_DEFAULT) - float(self.VEIL_MIN)
            t = (float(self.VEIL) - float(self.VEIL_MIN)) / max(1e-6, span)
        except (TypeError, ValueError):
            return 1.0
        t = max(0.0, min(1.0, t))
        return 0.35 + 0.65 * t

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
            self.invalidate()
            return img
        except Exception:
            return None

    # ------------------------------------------------- 预处理（带视差余量）
    @property
    def prep_origin(self):
        """预处理大图左上角的屏幕坐标。拖动时用它换算裁剪偏移。"""
        return self._prep_origin

    @property
    def prep_pad(self):
        """实际可用余量 (left, top, right, bottom)。贴屏幕边时会小于 PAD。"""
        return self._prep_pad

    def can_shift(self, dx, dy):
        """这个位移还在余量范围内吗？出了范围 `frame()` 会夹住（画面停住）。"""
        l, t, r, b = self._prep_pad
        return -l <= dx <= r and -t <= dy <= b

    def shift_for(self, x, y):
        """窗口左上角在屏幕 (x, y) 时，相对「预处理基准位置」的位移。

        基准位置 = 抓那张大图时窗口所在的位置，此时返回 (0, 0)。
        把这段算术收在引擎里，调用方不用关心余量是怎么存的。
        """
        ox, oy = self._prep_origin
        return x - ox - self._prep_pad[0], y - oy - self._prep_pad[1]

    @staticmethod
    def _pad_to(img, ox, oy, pw, ph):
        """把抓到的核心图贴到固定尺寸画布上，四周用边缘像素拉伸补齐。

        为什么不用 `paste` 留黑边：贴到屏幕边缘时余量不足，黑边会在玻璃
        里露成一条深色带；边缘拉伸看起来像桌面继续延伸，肉眼基本看不出来。
        """
        cw, ch = img.size
        mid = Image.new("RGB", (pw, ch), (0, 0, 0))
        mid.paste(img, (ox, 0))
        if ox > 0:
            mid.paste(img.crop((0, 0, 1, ch)).resize((ox, ch), Image.NEAREST),
                      (0, 0))
        rx = ox + cw
        if rx < pw:
            mid.paste(img.crop((cw - 1, 0, cw, ch)).resize((pw - rx, ch),
                                                           Image.NEAREST),
                      (rx, 0))
        full = Image.new("RGB", (pw, ph), (0, 0, 0))
        full.paste(mid, (0, oy))
        if oy > 0:
            full.paste(mid.crop((0, 0, pw, 1)).resize((pw, oy), Image.NEAREST),
                       (0, 0))
        by = oy + ch
        if by < ph:
            full.paste(mid.crop((0, ch - 1, pw, ch)).resize((pw, ph - by),
                                                            Image.NEAREST),
                       (0, by))
        return full

    def prepare(self, x, y, w, h, margin=None, bounds=None):
        """抓一张比窗口大 `margin` 的桌面图并预处理，供拖动时逐帧裁剪。

        Args:
            x, y: 窗口左上角屏幕坐标。
            w, h: 窗口尺寸。
            margin: 四周余量，默认 `PAD`。
            bounds: 虚拟桌面 (x, y, w, h)。给了就用来夹抓屏范围，避免
                抓出屏幕后拿到黑块。不给则按 `PAD` 原样抓。

        Returns:
            bool: 成功与否。失败时 `self._prep` 为 None，`compose()` 会
            退回到不带余量的旧路径。
        """
        if not HAVE_PIL or w <= 0 or h <= 0:
            return False
        m = int(self.PAD if margin is None else margin)
        x0, y0 = x - m, y - m
        x1, y1 = x + w + m, y + h + m
        if bounds:
            vx, vy, vw, vh = bounds
            x0 = max(vx, x0)
            y0 = max(vy, y0)
            x1 = min(vx + vw, x1)
            y1 = min(vy + vh, y1)
        if x1 <= x0 or y1 <= y0:
            return False
        try:
            core = ImageGrab.grab(bbox=(x0, y0, x1, y1), all_screens=True)
            core = core.convert("RGB")
        except Exception:
            return False
        if core.size != (x1 - x0, y1 - y0):
            try:
                core = core.resize((x1 - x0, y1 - y0), Image.BILINEAR)
            except Exception:
                return False

        pw, ph = w + 2 * m, h + 2 * m
        ox, oy = x0 - (x - m), y0 - (y - m)
        full = self._pad_to(core, ox, oy, pw, ph)

        self._raw = self._finish_base(full)
        self._prep = self._apply_veil(self._raw)
        self._veil_dirty = False
        self._prep_wh = (w, h)
        self._prep_pad = (x - x0, y - y0, x1 - (x + w), y1 - (y + h))
        self._prep_origin = (x0, y0)
        self._layers = None
        self._layers_key = None
        return True

    def _finish_base(self, img):
        """底图的像素级处理：模糊 → 提饱和 → 降亮。不依赖窗口位置。"""
        img = img.filter(ImageFilter.GaussianBlur(self.BLUR))
        # 提饱和的幅度跟着 glow_k 走：74% 时恰为 1.0 + 1.05 = 2.05，与
        # 旧版逐像素一致；越通透加得越少，否则薄面纱 + 高饱和 = 油彩感
        img = ImageEnhance.Color(img).enhance(1.0 + 1.05 * self.glow_k())
        return ImageEnhance.Brightness(img).enhance(0.96)

    def _apply_veil(self, img):
        veil = Image.new("RGB", img.size, (13, 15, 23))
        return Image.blend(img, veil, max(0.0, min(1.0, float(self.VEIL))))

    # ------------------------------------------------------- 静态图层缓存
    def build_layers(self, w, h, cards=(), decorators=()):
        """把只跟窗口尺寸/glass_k 有关的图层建好缓存起来（一次约 57ms）。

        噪点 + 卡片阴影 + 玻璃面 + 卡片柔光这四层都是 alpha 叠加，且顺序
        相邻，按结合律预合并成一张 RGBA，逐帧只合成一次（省 3 次全尺寸
        合成，代价是 ≤2/255 的舍入差）。光斑是 screen、顶部镜面是 add，
        不能并进 alpha 层，各自保留为独立图层。
        """
        blank = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        black = Image.new("RGB", (w, h), (0, 0, 0))

        # screen(黑, 图层) 就等于图层本身 —— 借这个性质把图层单独取出来
        glow_layer = self._glow(black, self.glow_k())
        sheen_layer = self._sheen(black)
        rim_layer = self._rim(blank)

        n = Image.effect_noise((w, h), 9).convert("L")
        a = Image.new("L", (w, h), 11)
        overlay = Image.merge("RGBA", (n, n, n, a))
        overlay = Image.alpha_composite(overlay, self._card_shadow(blank, cards))
        overlay = Image.alpha_composite(overlay, self._glass(blank, cards))
        for (x, y, cw, ch) in cards:
            overlay.alpha_composite(self.card_glow(cw, ch), (x, y))

        self._layers = {
            "glow": glow_layer, "sheen": sheen_layer,
            "overlay": overlay, "rim": rim_layer,
            "decorators": tuple(decorators),
        }
        self._layers_key = (w, h, round(self.glass_k, 4), tuple(cards),
                            tuple(decorators))
        return self._layers

    def _ensure_layers(self, w, h, cards, decorators):
        key = (w, h, round(self.glass_k, 4), tuple(cards), tuple(decorators))
        if self._layers is None or self._layers_key != key:
            self.build_layers(w, h, cards, decorators)
        return self._layers

    # ------------------------------------------------------------------ 合成
    def frame(self, dx=0, dy=0, buttons=()):
        """从预处理大图里裁一帧并贴回静态图层（约 13~18ms）。

        Args:
            dx, dy: 窗口相对「预处理时位置」的位移。玻璃后的桌面会跟着
                反向平移，这就是拖动视差。超出余量会被夹住。
            buttons: 需要在最终图上画底面的按钮，一般留空。
        """
        self._ensure_prep()
        if self._prep is None or self._layers is None:
            return None
        w, h = self._prep_wh
        l, t = self._prep_pad[0], self._prep_pad[1]
        px = max(0, min(self._prep.size[0] - w, l + int(dx)))
        py = max(0, min(self._prep.size[1] - h, t + int(dy)))
        img = self._prep.crop((px, py, px + w, py + h))

        L = self._layers
        img = ImageChops.screen(img, L["glow"])
        img = ImageChops.add(img, L["sheen"])
        img = Image.alpha_composite(img.convert("RGBA"), L["overlay"])
        for fn in L["decorators"]:
            img = fn(img)
        img = Image.alpha_composite(img, L["rim"])
        for (bx, by, bw, bh, kind) in buttons:
            img = self._paint_button(img, bx, by, bw, bh, kind, "normal")
        return img

    def compose(self, w, h, cards=(), buttons=(), decorators=(), offset=None):
        """生成最终底图（接口不变）。有预处理大图就直接裁一帧，否则现场做。

        cards      : [(x, y, w, h)] 卡片矩形，会被画成半透明玻璃块
        buttons    : [(x, y, w, h, kind)] 按钮底面，一般留空（按钮有自己的画布）
        decorators : [fn(img) -> img]，卡片画完之后、窗口棱边之前调用的自定义层
        offset     : (dx, dy) 窗口相对预处理基准位置的位移。复用旧预处理图
                     重绘时（比如拖动后改透明度）必须传，否则玻璃内容会弹回
                     拖动前的位置。
        """
        if not HAVE_PIL:
            return None

        if self._prep is None or self._prep_wh != (w, h):
            # 没走 prepare()（老调用点）或尺寸变了：现场从 self.src 做一张
            # 不带余量的，行为与改造前一致
            base = None
            if self.src is not None:
                try:
                    base = self.src.resize((w, h), Image.BILINEAR)
                except Exception:
                    base = None
            if base is None:
                base = self._synthetic(w, h)
            self._raw = self._finish_base(base)
            self._prep = self._apply_veil(self._raw)
            self._veil_dirty = False
            self._prep_wh = (w, h)
            self._prep_pad = (0, 0, 0, 0)
            self._prep_origin = (0, 0)

        self._ensure_layers(w, h, cards, decorators)
        dx, dy = offset if offset else (0, 0)
        return self.frame(dx, dy, buttons)

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
    def _glow(img, strength=1.0):
        """四个角的彩色光斑，screen 混合上去 —— 玻璃边缘的色散感来自这里。

        strength 由 compose 按 glow_k() 传入：面纱越薄光斑越弱（亮度
        整体缩放即可，等效于光斑减弱），否则最透明端会叠出一块浓郁的
        彩色调色板。保留默认值 1.0 兼容潜在外部直接调用。
        """
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
        layer = ImageEnhance.Brightness(layer).enhance(0.52 * strength)
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
        k = self.glass_k
        sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ds = ImageDraw.Draw(sh)
        for (x, y, cw, ch) in cards:
            # 往下偏移 6px 再模糊，做出"卡片浮在玻璃上"的投影
            ds.rounded_rectangle([x + 2, y + 6, x + cw - 2, y + ch + 8],
                                 radius=self.CARD_R,
                                 fill=(0, 0, 0, int(120 * k)))
        sh = sh.filter(ImageFilter.GaussianBlur(14))
        return Image.alpha_composite(img, sh)

    def _glass(self, img, cards):
        w, h = img.size
        k = self.glass_k
        gl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(gl)

        # 标题栏：比卡片更亮，突出"顶盖"层次
        if self.TITLE_H > 0:
            d.rectangle([0, 0, w, self.TITLE_H],
                        fill=(255, 255, 255, int(12 * k)))
            for i in range(self.TITLE_H):
                t = 1.0 - i / float(self.TITLE_H)
                d.line([(0, i), (w, i)],
                       fill=(255, 255, 255, int(18 * k * t * t)))
            d.line([(0, self.TITLE_H), (w, self.TITLE_H)],
                   fill=(255, 255, 255, int(30 * k)))

        # 卡片本体：白色极低 alpha 填充 + 双层描边。alpha 随面纱系数缩放，
        # 否则调到最通透时卡片边界会整个消失
        for (x, y, cw, ch) in cards:
            d.rounded_rectangle([x, y, x + cw, y + ch],
                                radius=self.CARD_R,
                                fill=(255, 255, 255, int(16 * k)))
            d.rounded_rectangle([x, y, x + cw, y + ch], radius=self.CARD_R,
                                outline=(255, 255, 255, int(52 * k)), width=1)
            d.rounded_rectangle([x + 1, y + 1, x + cw - 1, y + ch - 1],
                                radius=self.CARD_R - 1,
                                outline=(255, 255, 255, int(14 * k)), width=1)

        return Image.alpha_composite(img, gl)

    def card_glow(self, cw, ch):
        """卡片内部顶部柔光，给一点"厚度"感。返回独立小图层。"""
        k = self.glass_k
        top_h = max(2, int(ch * 0.55))
        g = Image.new("RGBA", (cw, top_h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(g)
        for i in range(top_h):
            t = 1.0 - i / float(top_h)
            gd.line([(0, i), (cw, i)],
                    fill=(255, 255, 255, int(11 * k * t * t)))
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

    def _paint_button(self, img, x, y, w, h, kind, state):
        """按钮底面：按按钮尺寸建独立小图层画完，再贴回大图。

        注意 `Image.alpha_composite` 没有 `dest` 参数（Pillow 13 实测），
        在指定位置合成只能靠 crop → composite → paste。

        ghost 按钮的底面是半透明白，alpha 要跟着面纱系数走；primary 按钮
        是实色渐变，不受影响。
        """
        w, h = int(w), int(h)
        if w <= 1 or h <= 1:
            return img
        k = self.glass_k
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
            layer = self.mask_round(layer, r)
            layer = self.gloss(layer, r, 66)
            d = ImageDraw.Draw(layer)
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r,
                                outline=(255, 255, 255, 112), width=1)
        else:
            fill = {"normal": (255, 255, 255, int(26 * k)),
                    "hover": (255, 255, 255, int(48 * k)),
                    "press": (255, 255, 255, int(16 * k))}[state]
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=fill)
            layer = self.gloss(layer, r, int(40 * k))
            d = ImageDraw.Draw(layer)
            d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r,
                                outline=(255, 255, 255, int(70 * k)), width=1)

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
