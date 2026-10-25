# -*- coding: utf-8 -*-
"""Pillow 可用性探测。

全部透明合成能力都来自 Pillow。没有 Pillow 时库不会崩，但会自动退化成
"纯色块 + 直角"的朴素界面（见各处 `elif not HAVE_PIL` 分支），
所以调用方可以无条件使用，不必自己判断。
"""

try:
    from PIL import Image, ImageChops, ImageDraw, ImageEnhance
    from PIL import ImageFilter, ImageGrab, ImageOps, ImageTk
    HAVE_PIL = True
except Exception:                                        # pragma: no cover
    Image = ImageChops = ImageDraw = ImageEnhance = None
    ImageFilter = ImageGrab = ImageOps = ImageTk = None
    HAVE_PIL = False

__all__ = [
    "Image", "ImageChops", "ImageDraw", "ImageEnhance",
    "ImageFilter", "ImageGrab", "ImageOps", "ImageTk", "HAVE_PIL",
]
