# -*- coding: utf-8 -*-
"""Canvas 上的形状绘制。

两条硬约束决定了这里的实现方式：

1. **Tk 的 `create_polygon(..., smooth=True)` 会把胶囊画成菱形。**
   平滑用的是贝塞尔，圆弧上取的控制点在圆角半径接近半高时会把弧"拉直"成
   45° 斜边。所以 `rrect` 沿四段圆角各采样 10 个点，直接画多边形。

2. **Canvas 没有透明度。**
   半透明的填充（进度条渐变、半透明分隔线）在 Canvas 上无解 —— 用扑色或者
   stipple 抖动都会在平滑玻璃底上变成噪点。这类元素必须用 Pillow 渲染成
   带 alpha 的 `PhotoImage` 再 `create_image` 贴上去。
"""

import math

from .compat import HAVE_PIL, Image, ImageChops, ImageDraw, ImageTk


def rrect(cv, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形（真正的圆弧，不是平滑多边形）。"""
    r = max(0.0, min(float(r), (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    if r < 1.0:
        return cv.create_rectangle(x1, y1, x2, y2, **kw)

    seg = 10
    pts = []
    # Tk 坐标 y 向下：0° 朝右、90° 朝下，顺时针走一圈
    for cx, cy, a0, a1 in (
        (x2 - r, y1 + r, -90.0, 0.0),      # 右上
        (x2 - r, y2 - r, 0.0, 90.0),       # 右下
        (x1 + r, y2 - r, 90.0, 180.0),     # 左下
        (x1 + r, y1 + r, 180.0, 270.0),    # 左上
    ):
        for i in range(seg + 1):
            a = math.radians(a0 + (a1 - a0) * i / seg)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return cv.create_polygon(pts, **kw)


def pill_grad(cv, x1, y1, x2, y2, top, bot, glow=0.0):
    """竖向渐变的胶囊填充，进度条专用。返回 PhotoImage（调用方需持有引用）。

    为什么不用「纯色块 + 叠一条亮线」：亮线的端点会被圆角切出硬边，
    右端再补一块浅色又会露出竖直分界，远看像打了补丁。
    按行插值渲染整条填充之后，亮度沿高度自然衰减，端点永远是圆的。
    """
    if not HAVE_PIL:
        rrect(cv, x1, y1, x2, y2, (y2 - y1) / 2.0,
              fill="#%02x%02x%02x" % tuple(bot), outline="")
        return None

    w = max(2, int(round(x2 - x1)))
    h = max(2, int(round(y2 - y1)))
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i in range(h):
        t = i / float(max(1, h - 1))
        mix = 1.0 - t                       # 1 在顶、0 在底
        add = 255.0 * glow * mix * mix
        d.line([(0, i), (w, i)],
               fill=(min(255, int(bot[0] + (top[0] - bot[0]) * mix + add)),
                     min(255, int(bot[1] + (top[1] - bot[1]) * mix + add)),
                     min(255, int(bot[2] + (top[2] - bot[2]) * mix + add)),
                     255))
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1],
                                        radius=h / 2.0, fill=255)
    img.putalpha(ImageChops.multiply(img.split()[3], m))
    ph = ImageTk.PhotoImage(img)
    cv.create_image(x1, y1, image=ph, anchor="nw")
    return ph


def hairline(cv, x, y, width, alpha=34, color=(255, 255, 255)):
    """1px 半透明分隔线。

    Canvas 的 `stipple="gray12"` 在平滑玻璃底上等于零星噪点，基本看不见；
    真正的半透明线只能做成 1px 高的 RGBA 贴图。
    """
    if not HAVE_PIL:
        cv.create_line(x, y, x + width, y, fill="#2a2e3a")
        return None
    ph = ImageTk.PhotoImage(
        Image.new("RGBA", (max(1, int(width)), 1), color + (alpha,)))
    cv.create_image(x, y, image=ph, anchor="nw")
    return ph
