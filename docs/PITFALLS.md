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

## 18. `AdjustTokenPrivileges` 失败时也返回 TRUE

**症状**：明明以管理员身份运行，`NtSetSystemInformation` 还是返回
`0xC0000061`（`STATUS_PRIVILEGE_NOT_HELD` = -1073741727），日志里一句
「清理失败，状态码 -1073741727」看不出任何线索。

**根因**：清理待机内存列表需要 `SeProfileSingleProcessPrivilege`。
这个特权在管理员令牌里**默认是「存在但未启用」**，必须显式
`AdjustTokenPrivileges` 打开。而 `AdjustTokenPrivileges` 有个坑：
**即使一个特权都没分配成功，它照样返回 TRUE**，
只在 `GetLastError()` 里留下 `ERROR_NOT_ALL_ASSIGNED (1300)`。

只看返回值 → 把失败当成功 → 下一句系统调用报一个毫不相关的错。

**解法**：BOOL 和 last error 两个都要查。

```python
ok = advapi32.AdjustTokenPrivileges(tok, False, byref(tp), 0, None, None)
return bool(ok) and ctypes.get_last_error() != 1300
```

**顺带**：`ShellExecuteW` 拉 UAC 时返回值 `<= 32` 一律是失败
（`5` = 拒绝访问，`1223` = 用户取消），且必须设
`restype = ctypes.c_void_p`，否则 64 位下返回值被截断。不看返回值就会出现
「用户点了『否』→ 程序自己关掉 → 什么都没做」这种最难排查的表现。

---

## 19. 自绘列表：`delete()` 会重绘，`insert()` 不会

**症状**：列表标题写着「共 339 个」，表体一行都没有。动一下鼠标、或者
拖动/最小化窗口，行又突然全冒出来了。时好时坏，截图还截不出来。

**根因**：`refresh()` 的典型写法是「先 `delete()` 清空，再批量 `insert()`」。
如果 `delete()` 内部会 `redraw()` 而 `insert()` 不会，那么画布就永久停在
`delete()` 之后的**空白状态**——数据在内存里是新的，画布是空的。
只有 `<Motion>` / 窗口重建这类意外触发重绘，行才会出现。

**这是最难发现的一类 bug**：你的截图工具如果恰好会触发一次重建，
截图看起来完全正常，于是「验证通过」，然后用户一用就发现是空的。

**解法**：
- `insert()` 里排一次 `after_idle` 合并重绘（339 次插入只重绘 1 次，不卡）；
- **调用方在批量插入后仍要显式 `redraw()` 一次**，不要依赖事件循环时机，
  否则 `--shot` / 自检这类不跑 `mainloop()` 的路径拿不到确定性结果；
- `redraw()` 开头把滚动位置 `top` 夹回 `[0, len(rows)-visible]`。
  行数变少后 `top` 停在旧最大值上，`idx >= end` 立刻 break，**整表一行都画不出来**。

---

## 20. `glass_slices()` 里别用 `winfo_*` 取坐标

**症状**：某个控件（开关、小图标按钮）右下角总有一块深色方块，其他控件却正常。

**根因**：`glass_slices()` 要给每个自绘控件切一块背景贴图。如果某处用了
`cv.winfo_x() / winfo_width()` 取坐标，而这时窗口**还没映射**，这些方法返回
`1` → 裁出 1×1 的背景 → 控件画布底色 `#0b0d13` 从圆角外露出来，就是黑方块。

这和第 15 条是同一个根因的两种表现：第 15 条坑的是主窗口尺寸，
这里坑的是子控件坐标。

**解法**：`glass_slices()` 里一律用创建控件时**记录的确定值**（比如布局字典里的
`(x, y, w, h)`），不要回过头去问 Tk。实在要问，也要先判 `> 1`。

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

4. **截图正常 ≠ 没有 bug。** 截图模式通常会额外触发一次背景重建
   （`--shot` 里调 `rebuild_background()` 是为了拿到完整底图），
   而重建会顺手把一些「依赖重绘才可见」的内容补画出来——第 19 条那个
   「表头有数字、表体空白」的 bug 就是这样被截图掩盖的。

   **凡涉及自绘列表 / 画布图元的验证，要直接数图元，不要用眼睛看截图：**

```python
items = table.cv.find_all()
texts = [i for i in items if table.cv.type(i) == "text"]
assert len(texts) > 0, "表格画布上没有文字图元"
```

   而且要走**真实交互路径**（模拟点按钮触发刷新），不要只跑截图模式。
