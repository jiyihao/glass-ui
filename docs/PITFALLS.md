# 踩坑记录

写这个库的过程中撞到的问题，按「症状 → 根因 → 解法」记。多数是 tkinter 和 Pillow
的隐式行为，光看文档看不出来，只能靠放大截图逐像素核对才发现。

---

## 1. `ImageDraw` 在 RGBA 图上不混合 alpha

**症状**：想画一块半透明白色，结果画出来是纯白实心块，把底下全盖住了。

**根因**：`ImageDraw` 对 RGBA 图的 `fill=(255, 255, 255, 20)` 是**直接写像素**，
不是 alpha 混合。画完之后那张图的像素就是 `(255,255,255,20)`；再 `convert("RGB")`
丢掉 alpha，剩下的就是纯白。

**解法**：所有半透明图元先画到一张独立的透明图层，再合成。

```python
layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
ImageDraw.Draw(layer).rounded_rectangle([...], fill=(255, 255, 255, 20))
img = Image.alpha_composite(img, layer)          # 这一步才是混合
```

这个坑我们踩了两次（主界面卡片、弹窗底板），第二次才彻底想明白。

---

## 2. `create_polygon(..., smooth=True)` 会把胶囊画成菱形

**症状**：开关的轨道明明是胶囊（圆角半径 = 半高），画出来却是个**菱形**，
四个 45° 斜边。

**根因**：`smooth=True` 用的是贝塞尔平滑。圆角半径接近元素半高时，相邻圆弧上取的
控制点会把弧"拉直"，视觉上就是直线斜切。

**解法**：别用 `smooth`，自己沿四段圆弧采样取点。

```python
seg = 10                       # 每段圆弧切 10 份
for cx, cy, a0, a1 in ((x2-r, y1+r, -90, 0), (x2-r, y2-r, 0, 90),
                       (x1+r, y2-r, 90, 180), (x1+r, y1+r, 180, 270)):
    for i in range(seg + 1):
        a = math.radians(a0 + (a1 - a0) * i / seg)
        pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
cv.create_polygon(pts, **kw)   # 注意不要 smooth=True
```

顺带：**圆角半径大于元素自身高度时会退化成直线**。我们给开关轨道加过一条 1px 高的
"凹槽暗边"，`radius` 却给了 14，结果它变成一条黑线横在轨道上缘。

---

## 3. `ImageDraw.arc` / `create_arc` 的包围盒是「整个元素」

**症状**：主按钮里有一条白线横穿，位置在按钮下半部。

**根因**：想用 `20°~160°` 的弧模拟顶部高光：

```python
d.arc([x, y, x + w, y + h], 20, 160, fill=(255, 255, 255, 72), width=1)
```

弧的包围盒是**整个矩形**，所以那段弧是"一个横跨整个按钮的大椭圆的顶部弧"，
两端落在按钮左右边缘偏下的位置，弦正好横穿按钮。

**解法**：高光应该是「上半部由亮到无」的竖向渐变，再套圆角蒙版：

```python
gh = int(h * 0.54)
for i in range(gh):
    t = 1.0 - i / gh
    d.line([(0, i), (w, i)], fill=(255, 255, 255, int(peak * t * t)))
```

---

## 4. `Image.alpha_composite` 没有 `dest` 参数

**症状**：`Image.alpha_composite(base, layer, dest=(x, y))` 报
`TypeError: unexpected keyword argument 'dest'`（Pillow 13 实测）。

**根因**：模块级的 `alpha_composite` 只接受两张图，不支持定位。
（**方法**版 `img.alpha_composite(layer, dest)` 支持，注意区别。）

**解法**：按元素尺寸建独立小图层，画完再贴回大图：

```python
layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))   # 只建按钮那么大
...  # 在这个小图层上画
region = img.crop((x, y, x + w, y + h))
img.paste(Image.alpha_composite(region, layer), (x, y))
```

---

## 5. 子画布会盖住主画布上的图元

**症状**：表头下面那条分隔线怎么都看不见，换颜色、加粗都没用。

**根因**：表格是一个子 Canvas，`place` 在 `y + TABLE_HEAD_H` 处，并且被
`tk.Misc.lift()` 抬到了最上层。分隔线画在 `y + TABLE_HEAD_H - 3`，正好落在子画布
覆盖的范围内，永远被遮住。

**解法**：画在子画布上边界之前（`y + TABLE_HEAD_H - 1`）。改之前先想清楚
「这个坐标有没有被别的控件盖住」。

另外，`Canvas.lift()` 被重载成了画布内图元的 `tag_raise`，**抬控件必须用
`tk.Misc.lift(sub)`**，否则会报 `wrong # args`。

---

## 6. `stipple` 抖动线在平滑玻璃上等于噪点

**症状**：用 `create_line(..., fill="#9aa4b8", stipple="gray12")` 画半透明线，
在玻璃底上几乎看不见，放大看是零星噪点。

**根因**：`stipple` 不是 alpha，是 1bit 掩膜抖动（12.5% 的点着色）。

**解法**：真正的半透明线只能做成 1px 高的 RGBA 贴图：

```python
ph = ImageTk.PhotoImage(Image.new("RGBA", (width, 1), (255, 255, 255, 34)))
cv.create_image(x, y, image=ph, anchor="nw")
```

---

## 7. 子 Canvas 不能留深色底 → 圆角外露黑方块

**症状**：按钮、表格、日志的四角各有一块深色方块，把玻璃底盖住了。

**根因**：每个控件都是一个独立子 Canvas，有自己的 `bg` 色。Canvas 是矩形，
控件画的圆角之外会露出它的底色。

**解法**：主窗口重建背景后，把底图对应区域切一块 `set_background()` 给控件。

漏写某个控件（比如在 `draw_chrome()` 里懒创建的按钮，此时背景切片早已分发完）
就会出现黑方块。**所有控件都必须在 `build_ui()` 里创建好。**

---

## 8. 弹窗里重新抓屏会拍到自己

**症状**：弹窗底板发黑、色调和主窗口对不上。

**根因**：弹窗出现时它在最前面，此时抓屏抓到的是弹窗自己。

**解法**：弹窗底板复用主窗口**已经合成好**的 `_bg_img`，缩放 + 模糊 + 混合，
不重新抓屏。这样色调天然一致，也省一次抓屏。

---

## 9. `tkfont.measure()` 在大字号下偏大

**症状**：`9.5` `GB` `/ 共 31.2 GB` 三段用 `measure()` 累加定位，
结果越往后越散，最后一段被推出卡片。

**根因**：30pt 粗体下 `measure()` 返回值比实际渲染宽度大十几像素，累加两次就废了。

**解法**：先画，再取真实 bbox 接下一段：

```python
it = cv.create_text(x, base, text="9.5", anchor="sw", font=(FONT_UI, 30, "bold"))
bb = cv.bbox(it)
nx = bb[2] + 7          # 这一段之后从哪开始
```

同理，**排版高度不要按字符数估算**，用 `cv.bbox(item)[3]` 实测。

---

## 10. 不同字号要按基线对齐，不是按包围盒底边

**症状**：`9.5`（30pt）后面的 `GB`（12pt）看起来"浮"在数字上方。

**根因**：`anchor="sw"` 对齐的是 bbox 底边，而 bbox 底边 = 基线 + descent。
30pt 的 descent 比 12pt 大得多，所以小字号整体偏高。

**解法**：按 descent 差值修正 y：

```python
drop = f_big.metrics("descent")
y_small = base - drop + f_small.metrics("descent")
```

---

## 11. Tk 的换行按空格断「词」

**症状**：`第 1 步本质上是…` 被排成 `第 1` / `步本质上是…` 两行。

**根因**：`create_text(..., width=...)` 换行时把连续中文当成一个"词"，
只在空格处断。`第` 和 `1` 之间有空格，于是断在那儿。

**解法**：文案里别在中文中间插空格；或者手动指定每行内容。

---

## 12. 无边框窗口默认不在任务栏 / Alt+Tab

**症状**：`overrideredirect(True)` 之后窗口从任务栏消失了。

**解法**：补 `WS_EX_APPWINDOW`、去掉 `WS_EX_TOOLWINDOW`：

```python
hwnd = get_hwnd(root)     # winfo_id() 是子窗口，要用 GetAncestor(h, GA_ROOT)
st = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                      (st | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW)
```

最小化也要先 `overrideredirect(False)` 再 `iconify()`，否则回不来。

---

## 13. 抓屏会把自己拍进去（递归）

**症状**：玻璃背景里出现了本程序自己的画面，无限套娃。

**解法**：`SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`（0x11），
需要 Windows 10 2004+。截图核对界面时要临时关掉
（`WDA_NONE`），否则截出来的是一块空白。

---

## 14. `-alpha` 和 `-transparentcolor` 都不能用

| 方案 | 后果 |
|---|---|
| `-alpha 0.85` | 整窗变透，**文字跟着发虚**，中文字形会糊成一团 |
| `-transparentcolor #010203` | 该颜色区域变成**点击穿透**，落在那儿的控件点不到 |

这两个都不能拿来做玻璃效果，只能自己合成底图。

---

## 15. 背景重建时 `winfo_width()` 可能是 1

**症状**：偶发整窗白屏 / 内容全丢。

**根因**：窗口还没 map 时 `winfo_width()` 返回 1。`w = winfo_width() or 默认值`
这个写法挡不住 1（它是真值），于是合成了一张 1×1 的底图。

**解法**：显式判断 `if w > 1 and h > 1`，否则用设计尺寸。

---

## 16. `ttk.Treeview` 没法玻璃化

**根因**：Treeview 的单元格由系统主题绘制，背景是实色，铺不上半透明底；
行高、选中色、字体也不能精确控制。

**解法**：自绘表格（`GlassTable`）。保留了 Treeview 的一个子集接口
（`get_children` / `insert` / `delete` / `selection` / `item`），方便迁移。

---

## 17. 高 DPI 下一切都会错位

启动时记得：

```python
ctypes.windll.shcore.SetProcessDpiAwareness(1)     # Win8.1+
# 失败再退回 ctypes.windll.user32.SetProcessDPIAware()
```

不做的话，125% 缩放下 Tk 会自己缩放，而你算的像素坐标没缩放，全部错位。

---

## 调界面时的建议

1. **必须放大截图逐像素核对**，不能靠代码"应该没问题"。第 2、3 条那两个
   几何 bug 都是裁一小块放大 4 倍才看出来的。
2. 每个示例都留一个 `--shot out.png` 的截图模式，改完立刻看成品，
   比反复手动截图快得多。
3. 想知道某个像素到底是什么颜色 / 某个元素到底画到哪了，直接采样：

```python
from PIL import Image
im = Image.open("shot.png").convert("RGB")
print(im.getpixel((60, 553)))
```
