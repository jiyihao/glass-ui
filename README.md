# glass-ui

[![PyPI](https://img.shields.io/pypi/v/glass-ui.svg)](https://pypi.org/project/glass-ui/)
[![Downloads](https://img.shields.io/pypi/dm/glass-ui.svg)](https://pypi.org/project/glass-ui/)
[![License: MIT](https://img.shields.io/badge/License-MIT-2ea44f.svg)](https://github.com/jiyihao/glass-ui/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776ab.svg)]()
[![Platform](https://img.shields.io/badge/Platform-Windows-0078d4.svg)]()
[![Deps](https://img.shields.io/badge/Deps-Pillow%20only-8a2be2.svg)]()

**给 tkinter 用的液态玻璃（Liquid Glass）界面层。**

```bash
pip install glass-ui
```


tkinter 本身没有任何模糊、半透明合成能力，所以 Python 桌面小工具默认长相都很朴素。
这个库把「看起来像一块真玻璃」需要的东西都包好了：抓取窗口后方的真实桌面 → 高斯模糊
→ 上色调色 → 把卡片/描边/按钮底面合成进同一张底图 → 整张铺成 Canvas 背景，
再配一套完全自绘的控件。

透出来的是**真实桌面内容**，不是渐变模拟。

![示例](https://raw.githubusercontent.com/jiyihao/glass-ui/main/examples/dashboard.png)

> 不想写代码？[**下载 MemTidy.exe**](https://github.com/jiyihao/glass-ui/releases/latest)
> 直接体验这个库的成品效果 —— 单文件免安装，双击就跑（Windows）。

---

## 这个库到底在做什么

tkinter 的三个「看起来能用其实不行」的方案：

| 做法 | 为什么不行 |
|---|---|
| `wm_attributes("-alpha", 0.9)` | 整窗一起变透，**文字跟着发虚**，中文尤其明显 |
| `wm_attributes("-transparentcolor", "#010203")` | 挖洞，那块区域**连鼠标事件一起消失**，控件点不到 |
| `ttk` 自定义主题 | 只能换配色，做不出模糊 |

所以只能自己合成。渲染管线是这样的：

```
 窗口后方的桌面
      │  ImageGrab / BitBlt          （抓屏时用 WDA_EXCLUDEFROMCAPTURE 排除自身）
      ▼
  高斯模糊 + 饱和度 ×2.05 + 深色面纱
      │
      ▼
  四角彩色氛围光（screen 混合） + 顶部镜面高光 + 极淡噪点
      │
      ▼
  GlassEngine.compose()
      ├─ 卡片外阴影（向下偏移 6px，模糊 14）
      ├─ 玻璃层：标题栏渐亮带 + 卡片白 16/255 填充 + 双层描边
      ├─ 每张卡片内部的顶部柔光
      ├─ 你插入的自定义图层（进度条凹槽、徽章底…）
      ├─ 窗口棱边：整圈亮边 + 上缘更亮
      └─ 按钮底面（一般留空，按钮有自己的画布）
      │
      ▼
  一张 RGBA 底图 → 当 Canvas 背景铺上去
      │
      ▼
  Canvas 上只画文字和交互图元（重绘开销极低）
```

窗口圆角由 `SetWindowRgn` 在系统层裁掉；无边框窗口补 `WS_EX_APPWINDOW`
才能进任务栏和 Alt+Tab。这些都在库里。

---

## 安装

```bash
pip install glass-ui
```

只依赖 Pillow。想改源码就用可编辑安装：

```bash
git clone https://github.com/jiyihao/glass-ui.git
cd glass-ui
pip install -e .
```

或者直接把 `glass_ui/` 文件夹丢进你的项目里，不需要装任何东西。

要求：Windows（用到了 Win32 接口）、Python 3.9+、Pillow 9+。
没有 Pillow 时库不会崩，会自动退化成纯色直角界面。

---

## 快速开始

```python
import tkinter as tk
from glass_ui import GlassWindow, GlassButton, FONT_UI

class Demo(GlassWindow):
    TITLE = "Hello Glass"
    WIN_W, WIN_H = 720, 420

    def build_ui(self):
        self.make_canvas()                       # 建主 Canvas + 绑定拖动
        self.btn = GlassButton(self, "点我", self.on_click,
                               24, 300, 120, 44, "primary")

    def glass_cards(self):                       # 哪些矩形是卡片
        return [(24, 80, 672, 190)]

    def glass_slices(self):                      # 哪些子控件要背景切片
        return [(self.btn, self.btn.x, self.btn.y, self.btn.w, self.btn.h)]

    def draw_chrome(self):                       # 画字
        cv = self.cv
        cv.delete("all")
        if self.bg_photo is not None:
            cv.create_image(0, 0, image=self.bg_photo, anchor="nw")
        cv.create_text(48, 120, text="你好，玻璃", anchor="w",
                       fill="#ffffff", font=(FONT_UI, 22, "bold"))

    def on_click(self):
        self.info("提示", "模糊的是窗口后面真实的桌面。")

root = tk.Tk()
Demo(root).run()
```

跑起来就有了圆角、无边框、可拖动、背后是真桌面的玻璃窗口。

---

## 五个钩子

`GlassWindow` 的子类只需要实现这几件事，其余（抓屏、模糊、圆角、拖动、
任务栏、背景切片分发、弹窗）都由基类负责：

| 方法 | 作用 |
|---|---|
| `build_ui()` | 建 `self.cv`（调 `self.make_canvas()`）和所有控件 |
| `glass_cards()` | 返回 `[(x, y, w, h)]`，这些矩形会变成玻璃卡片 |
| `glass_decorators()` | 返回 `[fn(img) -> img]`，画必须带 alpha 的静态层 |
| `glass_slices()` | 返回 `[(控件, x, y, w, h)]`，基类把底图切给控件 |
| `draw_chrome()` | 在 `self.cv` 上画文字和交互图元 |

还有一个可选钩子 `on_ready()`，在界面搭好、背景还没建时调用，适合塞首屏数据。

### `glass_slices()` 为什么必须写

每个控件都是一个子 Canvas。子 Canvas 有自己的底色，圆角之外的四角会露出一个
**深色方框**。解法是把主底图对应区域切一块贴给它 —— 这就是 `glass_slices()`。

漏写某个控件的后果就是它周围出现一个黑方块。这个坑我们在弹窗按钮上也踩过。

---

## 控件

| 控件 | 说明 |
|---|---|
| `GlassButton` | 胶囊按钮，`primary` / `ghost` 两种；自带常态 / 悬停 / 按下三态贴图 |
| `IconButton` | 标题栏圆形图标按钮（最小化 / 关闭 / 帮助 / 齿轮），可指定 `glyph_font` |
| `GlassToggle` | iOS 风格开关，轨道渐变 + 滑块投影 + 球体明暗 |
| `GlassSlider` | 圆角轨道 + 渐变填充 + 圆形滑块头；`on_drag` 实时、`on_change` 松手 |
| `GlassTable` | 自绘可滚动表格：滚轮 + **可拖动滚动条**（thumb 拖动 / 点轨道翻页），行底纹是内缩圆角条；带 Treeview 的一个子集接口 |
| `GlassLog` | 只读日志，时间戳等宽字体，正文按 tag 上色 |
| `GlassWindow` | 无边框圆角窗口基类，附带 `ask()` / `info()` 玻璃弹窗；窗口不参与屏幕捕获排除，Win+Shift+S 截屏时不会消失 |

表格和日志的 tag 颜色由类属性映射，库不认识你的 tag 叫什么：

```python
table.ROW_FG  = {"warn": C_ORANGE, "muted": "#949db0"}   # tag -> 主列颜色
table.ROW_DOT = ("warn",)                                # 这些 tag 左边画圆点
table.NOTE_FG = {"warn": C_ORANGE}                       # tag -> 备注列颜色
```

---

## 调参

`GlassEngine` 上的这些旋钮，每个窗口实例各有一份（改实例不会污染类和别的窗口）：

| 参数 | 默认 | 效果 |
|---|---|---|
| `BLUR` | `30` | 模糊半径。太小像磨砂贴膜，太大看不出桌面内容 |
| `VEIL` | `0.74` | 深色面纱强度。越低越透、文字越难读 |
| `CARD_R` | `18` | 卡片圆角 |
| `WIN_R` | `26` | 整窗圆角（同时是 `SetWindowRgn` 的半径） |
| `TITLE_H` | `58` | 标题栏高度，`0` 表示不要标题栏底色 |

具体到某个窗口：

```python
app = MyWindow(root)
app.engine.VEIL = 0.62      # 想更透一点
app.engine.BLUR = 26
app.rebuild_background()
```

### 让用户自己调透明度

如果你的程序想给用户一个「玻璃强度」滑块，别直接暴露 `VEIL`——
它越大越**不**透明，用户看着是反的。用这两个换算函数，语义才符合直觉：

```python
from glass_ui import GlassEngine

GlassEngine.opacity_to_veil(100)   # 0.42  ← 100 = 最通透
GlassEngine.opacity_to_veil(74)    # 0.74  ← 默认值，与出厂观感一致
GlassEngine.opacity_to_veil(0)     # 0.96  ← 0 = 最磨砂

# 或者直接把它交给窗口，在 build_ui() 之前生效
app = MyWindow(root, glass_opacity=74)
app.engine.set_opacity(40)         # 运行期改
app.rebuild_background()           # 松手后再重建，拖动时别每帧重建
```

`set_opacity()` 会同时缩放背景遮罩、卡片投影与玻璃强度、以及按钮底面贴图的
alpha。**按钮贴图是烘出来的，改完要调 `GlassButton.rebuild_images()`**，
否则按钮层次会和背景脱节。

出厂值下 `glass_k == 1.0`，所有缩放系数都乘 1，渲染与没有这个功能时逐像素一致。

字号、配色在 `glass_ui/theme.py`，都是普通常量，直接改或覆盖即可。

---

## 示例

```
examples/
├─ dashboard.py       最小示例：一块"系统概览"面板，也是本页顶部那张图
└─ memtidy/           MemTidy 内存整理器 —— 真实项目，完整源码
```

两个都能直接跑：

```bash
python examples/dashboard.py
python examples/memtidy/memtidy.py
```

都支持 `--shot out.png`：渲染完截一张图就退出，用来核对布局。
调界面时这个模式比反复手动截图舒服得多。

`examples/memtidy/` 是一个能打包成 exe 的真实应用（PyInstaller 单文件 19 MB），
里面有：内存卡的四级信息层次、进度条渐变填充、带状态点的进程表、整理进度实时回显。
界面代码全部调库，本体只剩 Win32 内存接口和「画什么字」。

---

## 已知限制

- **只在 Windows 上能用。** 抓屏、圆角、任务栏、抓屏排除都是 Win32 接口。
- **Alt+Tab 切换回来会短暂看到旧背景。** 窗口重新可见时会重新抓屏（`<Map>` 事件），
  但抓屏+合成要几十毫秒。
- **透明区域不能点击** 这个问题本库不存在（用的是系统级圆角裁剪，不是挖洞），
  但窗口**内部**的圆角处（比如卡片圆角外）仍然是主 Canvas，正常接收事件。
- **`WDA_EXCLUDEFROMCAPTURE` 需要 Windows 10 2004+。** 更老的系统上抓屏会拍到自己。
- **性能**：一次 `rebuild_background()` 大约 40~60ms（900×640，抓屏 + 模糊 + 合成）。
  只在窗口出现、移动结束、尺寸变化时重建，平时不重建。
- 表格没有多选拖拽、没有列排序，够用但不完整。

---

## 踩过的坑

写这个库的过程中踩到一批 tkinter / Pillow / Win32 的坑，全部记录在
[`docs/PITFALLS.md`](https://github.com/jiyihao/glass-ui/blob/main/docs/PITFALLS.md)。几条高频的：

- **`ImageDraw` 在 RGBA 图上不混合 alpha**，只是直接写像素。半透明图元必须先画到
  独立图层再 `alpha_composite`，否则 `convert("RGB")` 之后就是一个纯白实心块。
- **`create_polygon(..., smooth=True)` 会把胶囊画成菱形**：圆角半径接近半高时，
  贝塞尔的控制点会把圆弧拉直成 45° 斜边。
- **`ImageDraw.arc` 的包围盒是整个按钮**，画"高光弧"会变成一条横穿按钮的白线。
- **`Image.alpha_composite` 没有 `dest` 参数**，想在指定位置合成只能
  `crop → composite → paste`。
- **子画布会盖住主画布上的图元**，画在子画布起点之后的分隔线永远看不见。
- **`tkfont.measure()` 在大字号下偏大十几像素**，用 `cv.bbox(item)` 取实测宽度。

---

## License

[MIT](https://github.com/jiyihao/glass-ui/blob/main/LICENSE)
