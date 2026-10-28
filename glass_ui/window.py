# -*- coding: utf-8 -*-
"""无边框圆角玻璃窗口基类。

把"让一个窗口看起来像一块玻璃"需要的那一堆脏活集中在这里：

* 去边框、居中、屏幕太矮时自动缩高
* 系统级圆角裁剪（SetWindowRgn）
* 补 WS_EX_APPWINDOW，让无边框窗口仍出现在任务栏和 Alt+Tab
* 首次抓屏安排在窗口映射之前，背景里不会混进自己的影像
* 空白处按住拖动窗口
* 重建背景时给每个子 Canvas 切一块对应的底图（圆角外才不会露方框）
* 配套的玻璃弹窗 ask() / info()

子类只需要：

1. 在 `build_ui()` 里创建 `self.cv`（用 `self.make_canvas()`）和所有控件
2. 实现 `glass_cards()`，返回本窗口的卡片矩形
3. 实现 `draw_chrome()`，在 Canvas 上画文字与交互图元
4. 实现 `glass_slices()`，告诉基类哪些控件需要背景切片
"""

import time
import tkinter as tk
from tkinter import font as tkfont

from .compat import HAVE_PIL, Image, ImageFilter, ImageTk
from .engine import GlassEngine
from .theme import C_BG, FONT_UI, R_CARD, R_WIN
from .widgets import GlassButton
from .win import (WS_EX_APPWINDOW, WS_EX_TOOLWINDOW, apply_round_region,
                  get_exstyle, make_taskbar_window, set_capture_affinity,
                  virtual_screen_rect)


class GlassWindow:
    WIN_W = 980
    WIN_H = 744
    MIN_H = 620
    TITLE = "Glass UI"
    CAPTURE_SAFE = False      # 见 rebuild_background()：默认不再排除抓屏
    # 预处理大图的面积上限（像素）。余量取到"够拖遍整个桌面"时，大图的
    # 尺寸等于屏幕尺寸，模糊/提饱和的代价随面积涨；超过这个量级就按比例
    # 收小余量，宁可远处被夹住也别让启动卡顿几百毫秒。2560x1440 仍覆盖。
    PREP_MAX_PX = 4_400_000

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
        self._win_x = 0          # geometry 请求位置；窗口未映射时抓屏用
        self._win_y = 0
        # 拖动视差：_drag_target 是待绘制的最新位置，_drag_pending 保证同一
        # 时刻只有一个 after 在排队（鼠标上报 125Hz，一帧要 18ms，不合并会积压）
        self._drag_target = None
        self._drag_pending = False
        self._drag_frames = 0
        self._drag_t0 = 0.0
        self._drag_need_prep = False

        self.fit_screen()
        self.setup_window()
        self.build_ui()
        self.on_ready()
        # 窗口映射前先合成一次背景：此刻抓屏拍到的是干净桌面。
        # 等到 map 之后再抓（after(60) 那次），屏幕上已经有窗口自己了。
        self.rebuild_background()
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
        # 窗口还没映射时 winfo_rootx() 返回 0，首次抓屏只能用 geometry 的
        # 请求坐标，这里必须记下来
        self._win_x, self._win_y = x, y
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
        """开关"抓屏时排除自己"。

        默认不开（CAPTURE_SAFE=False）：排除标志会让窗口在 Win+Shift+S
        这类捕获会话期间从屏幕上消失。需要隐藏于截屏的场景可显式开启，
        或把类属性 CAPTURE_SAFE 改回 True 恢复旧行为。
        """
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
            # `overrideredirect` 改动取值时会重建顶层 HWND，新句柄的 exstyle
            # 回到 Tk 默认的 0x00000080（WS_EX_TOOLWINDOW），setup_window 里
            # 补的 APPWINDOW 就此丢失。最小化恢复走的就是 False→True 这条路，
            # 所以每次 map 都要补一遍。
            # 注意别写成「每调用一次都会重建」：实测同值重复调用不换句柄。
            if not self.shot:
                make_taskbar_window(self.root)
            self.root.after(80, self.after_map_settle)
            self.schedule_bg(200)

    def after_map_settle(self):
        """map 之后 80ms 的收尾：先补任务栏样式，再切圆角。

        顺序不能反 —— `apply_round_region` 会 `SetWindowRgn` 重设整个窗口
        region，放在 `SetWindowPos(SWP_FRAMECHANGED)` 之后才不会被框架重算
        覆盖掉。
        """
        if not self.shot:
            make_taskbar_window(self.root)
        self.apply_round_region()

    def taskbar_state(self):
        """当前任务栏相关的扩展样式状态（诊断用）。

        Returns:
            dict: `{"exstyle": int, "appwindow": bool, "toolwindow": bool}`
        """
        st = get_exstyle(self.root)
        return {
            "exstyle": st,
            "appwindow": bool(st & WS_EX_APPWINDOW),
            "toolwindow": bool(st & WS_EX_TOOLWINDOW),
        }

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
    #
    # 玻璃的底图是一张静止的桌面快照。窗口移动时如果底图跟着一起走，看起来
    # 就像把桌面截图贴在了卡片上 —— 物理感是错的。正确做法是：抓一张比窗口
    # 大两圈余量的大图，拖动时按位移**从里面换位置裁剪**，玻璃后的桌面就
    # 相对于窗口反向平移，这才是隔着玻璃看桌面的样子。
    #
    # 代价是每帧都要合成。所以引擎里做了两级缓存（见 engine 模块头），
    # 逐帧只要 13~18ms；事件还必须合并，否则 125Hz 的鼠标上报会积压成雪崩。
    def drag_start(self, e):
        self._drag = (e.x_root, e.y_root,
                      self.root.winfo_x(), self.root.winfo_y())
        self._drag_target = None
        self._drag_pending = False
        self._drag_frames = 0
        self._drag_t0 = time.perf_counter()
        self._slice_tick = 0

    def drag_move(self, e):
        if not self._drag:
            return
        x = self._drag[2] + e.x_root - self._drag[0]
        y = self._drag[3] + e.y_root - self._drag[1]
        try:
            self.root.geometry(f"+{x}+{y}")
        except Exception:
            return
        self._drag_target = (x, y)
        self._queue_drag_frame()

    def _queue_drag_frame(self):
        """合并连续的移动事件，一帧只合成一次。"""
        if self._drag_pending:
            return
        self._drag_pending = True
        self.root.after(1, self._paint_drag_frame)

    def _paint_drag_frame(self):
        self._drag_pending = False
        if self._drag_target is None or not self.engine.ok:
            return
        x, y = self._drag_target
        dx, dy = self.engine.shift_for(x, y)
        if not self.engine.can_shift(dx, dy):
            # 跑出余量：这一帧会被夹住（画面停住），松手后再重新抓
            self._drag_need_prep = True
        img = self.engine.frame(dx, dy)
        if img is None:
            return
        if self._apply_frame_image(img):
            self._drag_frames += 1

    def _apply_frame_image(self, img):
        """只换底图贴图的像素，不重绘任何图元 —— 拖动帧的快路。

        走 `rebuild_background()` 会连 draw_chrome() 一起重画（几百个图元），
        那是 100ms 级的开销，拖动时每帧来一次必然卡死。

        贴图本身也复用：尺寸没变就往同一张 PhotoImage 里 `paste`，省掉每次
        重建 Tk photo 的开销（实测 2.2ms → 1.0ms），并且不用再发
        `itemconfigure` —— Tk 会自己重画所有显示这张 photo 的图元。
        """
        try:
            rgb = img if img.mode == "RGB" else img.convert("RGB")
        except Exception:
            return False
        ids = self.cv.find_withtag("bg")
        if not ids:
            return False
        photo = self.bg_photo
        reused = False
        if photo is not None:
            try:
                reused = (photo.width() == rgb.width
                          and photo.height() == rgb.height)
            except Exception:
                reused = False
        if reused:
            try:
                photo.paste(rgb)
            except Exception:
                reused = False
        if not reused:
            try:
                photo = ImageTk.PhotoImage(rgb)
                self.cv.itemconfigure(ids[0], image=photo)
            except Exception:
                return False
        self.bg_photo = photo
        self._bg_img = rgb
        # 切片隔帧刷新：贴图复用后单片只要几十微秒，但一帧 13 片往 Tcl 里
        # 搬像素仍要 4ms 上下，占掉一帧的六分之一；滞后一帧（约 35ms）肉眼
        # 看不出来，硬刷每帧只会把帧率压下去。
        self._slice_tick = getattr(self, "_slice_tick", 0) + 1
        if self._slice_tick % 2 == 1:
            self._refresh_slices_fast(rgb)
        return True

    def _refresh_slices_fast(self, img):
        """从已转成 RGB 的整幅底图上切片，只换贴图不重绘。"""
        iw, ih = img.size
        try:
            for widget, x, y, w, h in self.glass_slices():
                x = max(0, int(x))
                y = max(0, int(y))
                w = max(1, min(iw - x, int(w)))
                h = max(1, min(ih - y, int(h)))
                piece = img.crop((x, y, x + w, y + h))
                if not widget.fast_background(piece):
                    widget.set_background(piece)
        except Exception:
            pass

    def drag_end(self, _e=None):
        self._drag = None
        self._drag_target = None
        self._drag_pending = False
        # 切片是隔帧刷的，最后一帧可能少刷一次，补上（否则松手后表格
        # 底下会留一帧错位的玻璃纹理，停下来盯着看能看出来）
        if self._drag_frames and self._bg_img is not None:
            try:
                self._refresh_slices_fast(self._bg_img)
            except Exception:
                pass
        if self._drag_need_prep:
            # 跑出了预处理范围，只能重新抓一张。此时窗口已经停在新位置，
            # 抓屏会把窗口自己模糊后的影像混进来 —— 老版本每次松手都这样，
            # 现在只在拖得很远之后才发生一次。
            self._drag_need_prep = False
            self.schedule_bg(60)
        # 没跑出余量就什么都不做：玻璃内容本来就停在正确位置，重抓反而
        # 会把窗口自己的影像烤进玻璃（旧行为），白白掉一档清晰度。

    def drag_stats(self):
        """最近一次拖动的帧统计（诊断用）。"""
        dt = max(1e-6, time.perf_counter() - self._drag_t0)
        n = self._drag_frames
        return {"frames": n, "seconds": round(dt, 3),
                "fps": round(n / dt, 1) if n else 0.0}

    # ------------------------------------------------------------ 背景重建
    def schedule_bg(self, delay=120):
        if self._bg_pending:
            return
        self._bg_pending = True
        self.root.after(delay, self.rebuild_background)

    def _grab_origin(self):
        """抓屏起点。

        窗口未映射时 `winfo_rootx()` 返回 0，直接用会把屏幕左上角抓进来，
        所以未映射阶段必须回退到 geometry 的请求坐标。
        """
        try:
            if self.root.winfo_ismapped():
                return self.root.winfo_rootx(), self.root.winfo_rooty()
        except Exception:
            pass
        return getattr(self, "_win_x", 0), getattr(self, "_win_y", 0)

    def _prep_still_valid(self, gx, gy, w, h):
        """现有预处理大图还能直接用吗（位置和尺寸都没变）。"""
        if not self.engine.ok or self.engine._prep is None:
            return False
        if self.engine._prep_wh != (w, h):
            return False
        ox, oy = self.engine.prep_origin
        pad = self.engine.prep_pad
        return (ox + pad[0], oy + pad[1]) == (int(gx), int(gy))

    def _prep_margin(self, gx, gy, w, h):
        """抓屏余量：取到"能拖遍整个桌面"为止。

        固定 220px 的问题：拖出 220 像素之后 `frame()` 就被夹住，窗口还在
        动、玻璃里的桌面却钉在原地 —— 比完全没有视差更违和（用户把窗口从
        屏幕中间拖到边上一定会撞到）。这里按窗口到四条屏幕边的距离取余量，
        配合 `prepare()` 的 bounds 夹取，预处理图的尺寸正好等于整个桌面：
        拖到哪都有真实桌面内容，且不再触发"松手后重抓"。

        面积超过 `PREP_MAX_PX` 时按 0.7 逐次收小 —— 超大桌面（双 4K）上
        保住启动速度比保住远处视差重要。
        """
        m = int(self.engine.PAD)
        try:
            vs = virtual_screen_rect()
        except Exception:
            vs = None
        if not vs:
            return m
        vx, vy, vw, vh = vs
        m = max(m, int(gx - vx), int(gy - vy),
                int(vx + vw - (gx + w)), int(vy + vh - (gy + h)))
        # 预处理图的实际尺寸是"窗口 + 余量"和桌面的交集，按交集算面积，
        # 不然会把屏幕外的部分也算进去，白收小余量
        while m > self.engine.PAD:
            x0, x1 = max(vx, gx - m), min(vx + vw, gx + w + m)
            y0, y1 = max(vy, gy - m), min(vy + vh, gy + h + m)
            if (x1 - x0) * (y1 - y0) <= self.PREP_MAX_PX:
                break
            m = int(m * 0.7)
        return m

    def rebuild_background(self, reuse_prep=None):
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
        # 抓屏排除自己 vs 捕获会话期间可见，二者只能选一个（v0.1.5 起默认可见）：
        # WDA_EXCLUDEFROMCAPTURE 能让抓屏不拍到窗口自身，但 Win+Shift+S 的
        # 全屏遮罩本身就是捕获会话，排除标志会让窗口在遮罩期间从屏幕上整个
        # 消失 —— 用户一截屏工具就"凭空蒸发"。默认不再排除。代价是窗口显示
        # 中重抓背景时，会把自身模糊后的影像混进玻璃底色，视觉上等同玻璃
        # 反光，且每次重抓都在衰减（旧影像被新影像替换而非叠加），不会恶化。
        # 启动首屏在映射前抓（见 __init__），完全干净。仍要排除的场景把
        # CAPTURE_SAFE 改回 True 即可。
        if self.CAPTURE_SAFE:
            set_capture_affinity(self.root, True)
        else:
            # 显式清成 WDA_NONE：HWND 重建后新句柄默认就是 0，这一步主要是
            # 把旧版本窗口或运行中动态切换过来的状态兜干净
            set_capture_affinity(self.root, False)

        if self.engine.ok:
            gx, gy = self._grab_origin()
            if reuse_prep is None:
                # 没显式要求时自动判断：位置尺寸没变就别重抓。重抓时窗口
                # 已经显示在屏幕上，抓到的会混进自己模糊后的影像 —— 首屏
                # 那张是映射前抓的、完全干净，能留就留。
                reuse_prep = self._prep_still_valid(gx, gy, w, h)
            # 抓一张带余量的大图：拖动时从里面换位置裁剪，玻璃后的桌面
            # 才会相对窗口反向平移。贴屏幕边时余量会被 bounds 夹小，
            # `can_shift()` 会自动跟着变小。
            if not reuse_prep:
                # 抓屏那一瞬间把自己排除在画面之外：窗口已经在屏幕上，不排除
                # 就会抓到它自己模糊后的影像，玻璃看起来像在"照镜子"（实测
                # 与真桌面的差异 44 vs 3.3）。DWM 立即生效，抓完立刻清掉，
                # 所以不会出现"按住 Win+Shift+S 时窗口消失"那个老问题。
                mapped = False
                try:
                    mapped = bool(self.root.winfo_ismapped())
                except Exception:
                    pass
                if mapped:
                    set_capture_affinity(self.root, True)
                try:
                    if not self.engine.prepare(gx, gy, w, h,
                                               self._prep_margin(gx, gy, w, h),
                                               virtual_screen_rect()):
                        self.engine.grab(gx, gy, w, h)  # 兜底：不带余量
                finally:
                    if mapped:
                        set_capture_affinity(self.root, bool(self.CAPTURE_SAFE))

        # 复用旧预处理大图时（例如拖动后改透明度），窗口已经不在原来的位置，
        # 必须把「当前位移」告诉引擎，否则玻璃内容会弹回拖动前的位置
        off = (0, 0)
        if self.engine._prep is not None:
            off = self.engine.shift_for(gx, gy)

        img = self.engine.compose(w, h,
                                  self.glass_cards(),
                                  self.glass_buttons(),
                                  self.glass_decorators(),
                                  offset=off)
        self._bg_img = img
        self._drag_need_prep = False
        if img is None:
            self.bg_photo = None
        else:
            self.bg_photo = ImageTk.PhotoImage(img.convert("RGB"))
            self.refresh_slices(img)

        self.draw_chrome()
        self._tag_bg_item()

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

    def recompose(self):
        """不重新抓屏，只按当前参数重算底图（改透明度走这条）。

        抓屏 30ms + 大图模糊 40ms 都与面纱无关，改透明度只需要重 blend 一次
        面纱、再重建静态层（glass_k 变了卡片 alpha 就得跟着变）。省下来的是
        滑块松手后那一百多毫秒的停顿感。
        """
        if not self.engine.ok or self.engine._prep is None:
            self.rebuild_background()
            return
        self.engine.reveil()
        self.engine.drop_layers()
        self.rebuild_background(reuse_prep=True)

    def _tag_bg_item(self):
        """给主画布的底图打上 "bg" 标记 —— 拖动帧靠它找到要替换的图元。

        底图是子类在 `draw_chrome()` 里画的，基类拿不到 item id。兜底办法：
        底图永远是最先创建的图元（后画的都压在它上面），取堆叠顺序里第一个
        image 就是它。子类显式写 `tags=("bg",)` 更稳妥，这条只是保险。
        """
        try:
            if self.cv.find_withtag("bg"):
                return
            for it in self.cv.find_all():
                if self.cv.type(it) == "image":
                    self.cv.addtag_withtag("bg", it)
                    return
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
