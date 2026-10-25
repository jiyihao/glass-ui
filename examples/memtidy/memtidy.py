# -*- coding: utf-8 -*-
"""
内存整理器 MemTidy

功能定位：在启动大型游戏 / 工程软件之前，把系统里各进程占用的物理内存页
集中回收一次，让即将启动的程序拿到更充裕的连续物理内存。

------------------------------ 关于"参考 PCL" ------------------------------
本程序的功能设计参考了 Plain Craft Launcher（PCL，作者：龙腾猫跃）中
「启动游戏前进行内存优化」这一功能的使用方式与执行语义。

需要说明的事实：PCL 的该功能实现并未开源。在其公开仓库中，
PageOtherTest.MemoryOptimize 被替换为空实现（提示"开源内容中不包含百宝箱功能"），
仅保留了调用点。因此本程序是依据 Windows 官方文档独立实现的等价功能，
不含任何 PCL 源代码。

本程序与 PCL 无隶属关系，也不是 PCL 的衍生作品（功能领域不重叠）。
署名仅为遵循 PCL 仓库使用指南的要求。
---------------------------------------------------------------------------

技术实现（三条，全部是 Windows 原生接口）：
  1. EmptyWorkingSet   —— 把进程工作集里的物理页移出（转为可回收状态）
  2. NtSetSystemInformation(MemoryPurgeStandbyList) —— 清空待机内存列表（需管理员）
  3. TerminateProcess  —— 结束用户手动选中的后台进程（真正的内存释放）

------------------------------ 界面实现说明 -------------------------------
界面部分全部来自 glass-ui（本仓库的 glass_ui 包）：tkinter 没有模糊和
半透明合成能力，所以那个库用「抓窗口后方桌面 → 高斯模糊 → 上色 →
把静态玻璃层合成进底图 → 整张铺成 Canvas 背景」的办法做出液态玻璃效果，
再配上一套自绘控件。窗口圆角、任务栏图标、拖动、玻璃弹窗也都在库里。

本文件因此只剩两件事：Windows 内存接口，以及「卡片摆在哪、画什么字」。
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

# 让示例在没有 pip install 的情况下也能直接跑（仓库根目录就在两级之上）
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

# 界面全部来自 glass-ui：抓屏、模糊、圆角窗口、自绘控件都不在本文件里
from glass_ui import (C_ACCENT, C_BG, C_CYAN, C_GREEN, C_ORANGE, C_RED,
                      FONT_MONO, FONT_UI, GlassButton, GlassEngine, GlassLog,
                      GlassTable, GlassToggle, GlassWindow, IconButton,
                      pill_grad, rrect)
from glass_ui.compat import HAVE_PIL, Image, ImageDraw, ImageTk
from glass_ui.win import apply_round_region

APP_NAME = "内存整理器"
APP_VER = "2.0"
APP_TAG = "MemTidy"
APP_TITLE = f"{APP_NAME}  {APP_VER}"

# ---------------------------------------------------------------- 布局常量
CARD_MEM = (18, 66, 944, 136)
CARD_TABLE = (18, 210, 944, 330)
TABLE_HEAD_H = 50
BTN_Y = 550
BTN_H = 50
CARD_LOG = (18, 608, 944, 116)
MEM_BADGE = (806, 82, 132, 30)      # 内存卡右上角「可用」徽章
LOG_HEAD_H = 26

# 列定义给表格和表头共用，避免两处各写一份而错位
COLS = (
    ("name", 0.00, 0.42, "w", "进程名"),
    ("pid", 0.42, 0.53, "e", "PID"),
    ("mem", 0.50, 0.62, "e", "工作集"),
    ("note", 0.66, 1.00, "w", "备注"),
)

# ============================================================================
#  Win32 接口
# ============================================================================
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
user32 = ctypes.WinDLL("user32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_VM_READ = 0x0010

kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
kernel32.GlobalMemoryStatusEx.restype = wt.BOOL
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD),
        ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wt.DWORD),
        ("ProcessCount", wt.DWORD),
        ("ThreadCount", wt.DWORD),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin():
    """以管理员身份重启自己（用于清理待机内存）。"""
    try:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, "", os.path.dirname(sys.executable), 1)
        return True
    except Exception:
        return False


def get_memory_status():
    """物理内存概况，单位 MB。"""
    ms = MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(ms)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))

    pi = PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(pi)
    cache_mb = 0.0
    commit_mb = 0.0
    if psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        page = pi.PageSize or 4096
        cache_mb = pi.SystemCache * page / 1048576.0
        commit_mb = pi.CommitTotal * page / 1048576.0

    total = ms.ullTotalPhys / 1048576.0
    avail = ms.ullAvailPhys / 1048576.0
    return {
        "total_mb": total,
        "avail_mb": avail,
        "used_mb": total - avail,
        "percent": ms.dwMemoryLoad,
        "cache_mb": cache_mb,
        "commit_mb": commit_mb,
        "pagefile_pct": (ms.ullTotalPageFile - ms.ullAvailPageFile) * 100.0 / max(1, ms.ullTotalPageFile),
    }


def enum_pids():
    size = 8192
    while True:
        buf = (wt.DWORD * size)()
        needed = wt.DWORD()
        if not psapi.EnumProcesses(buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return []
        cnt = needed.value // ctypes.sizeof(wt.DWORD)
        if cnt < size - 8:
            return [buf[i] for i in range(cnt) if buf[i] != 0]
        size *= 2
        if size > 262144:
            return [buf[i] for i in range(cnt) if buf[i] != 0]


def query_process(pid):
    """返回 (进程名, 工作集字节数)，无法访问返回 None。

    权限组合按门槛从高到低依次尝试：
      1. QUERY_INFORMATION | VM_READ   —— 能同时拿到名字和工作集
      2. QUERY_LIMITED_INFORMATION | VM_READ —— Win8+ 上多数受保护进程也能拿工作集
      3. QUERY_LIMITED_INFORMATION     —— 只能拿名字，工作集记为 0
    """
    combos = (
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ,
        PROCESS_QUERY_LIMITED_INFORMATION,
    )
    for access in combos:
        h = kernel32.OpenProcess(access, False, pid)
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wt.DWORD(1024)
            if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                continue
            name = os.path.basename(buf.value)
            if not name:
                continue

            pmc = PROCESS_MEMORY_COUNTERS_EX()
            pmc.cb = ctypes.sizeof(pmc)
            ws = 0
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                ws = pmc.WorkingSetSize
            return (name, ws)
        finally:
            kernel32.CloseHandle(h)
    return None


def empty_working_set(pid):
    """对单个进程执行工作集清空。"""
    h = kernel32.OpenProcess(
        PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return False
    try:
        return bool(psapi.EmptyWorkingSet(h))
    finally:
        kernel32.CloseHandle(h)


def purge_standby_list():
    """清空待机内存列表，需管理员权限。返回 (成功, 状态码)。"""
    cmd = ctypes.c_int(4)  # MemoryPurgeStandbyList
    st = ntdll.NtSetSystemInformation(80, ctypes.byref(cmd), ctypes.sizeof(cmd))
    return st == 0, st


def kill_process(pid):
    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(kernel32.TerminateProcess(h, 1))
    finally:
        kernel32.CloseHandle(h)


# ============================================================================
#  保护名单：这些进程不允许在界面里结束
# ============================================================================
PROTECTED = {
    "system", "idle", "registry", "memory compression", "memcompression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "lsaiso.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe",
    "spoolsv.exe", "audiodg.exe", "sihost.exe", "taskhostw.exe", "ctfmon.exe",
    "runtimebroker.exe", "explorer.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe",
    "searchindexer.exe", "textinputhost.exe", "lockapp.exe", "logonui.exe",
    "userinit.exe", "wudfhost.exe", "wmiprvse.exe", "dllhost.exe",
    "conhost.exe", "openconsole.exe", "windowsterminal.exe", "cmd.exe",
    "powershell.exe", "pwsh.exe", "wsmprovhost.exe", "taskmgr.exe",
    "sppsvc.exe", "trustedinstaller.exe", "msmpeng.exe", "nissrv.exe",
    "securityhealthservice.exe", "securityhealthsystray.exe", "mssense.exe",
    "sensecncproxy.exe", "sgrmbroker.exe", "easyanticheat.exe",
    "easyanticheat_eos.exe", "beservice.exe", "bedaisy.exe", "vgc.exe",
    "vgtray.exe", "riotclientservices.exe", "nvcontainer.exe",
    "nvdisplay.container.exe", "nvidia overlay.exe", "atiesrxx.exe",
    "amdow.exe", "radeonsoftware.exe", "memtidy.exe",
}

# 这些进程名属于"可以放心结束"的后台程序，界面上给个标记提示
BACKGROUND_HINTS = {
    "steam.exe": "Steam", "steamwebhelper.exe": "Steam", "weixin.exe": "微信",
    "wechat.exe": "微信", "qq.exe": "QQ", "tim.exe": "TIM", "dingtalk.exe": "钉钉",
    "discord.exe": "Discord", "douyin.exe": "抖音", "kuaishou.exe": "快手",
    "哔哩哔哩.exe": "B站", "chrome.exe": "浏览器", "msedge.exe": "Edge",
    "firefox.exe": "Firefox", "code.exe": "VS Code", "devenv.exe": "Visual Studio",
    "pycharm64.exe": "PyCharm", "idea64.exe": "IDEA", "keil.exe": "Keil",
    "uv4.exe": "Keil", "wps.exe": "WPS", "winword.exe": "Word", "excel.exe": "Excel",
}


def is_protected(name):
    return name.lower() in PROTECTED


# ============================================================================
#  主应用
# ============================================================================
class MemTidyApp(GlassWindow):
    """主窗口。

    这里只声明"长什么样"：卡片摆哪、哪些静态层需要 alpha、哪些子控件要切背景。
    抓屏、模糊、圆角裁剪、拖动、弹窗全部由 glass_ui.GlassWindow 负责。
    """

    TITLE = APP_TITLE
    WIN_W = 980
    WIN_H = 744

    def __init__(self, root, shot=False):
        # 业务状态先备好：super().__init__ 里会立刻调用 build_ui()
        self.busy = False
        self._pct = 0.0           # 整理进度；空闲时改存内存使用率
        self.snapshot = []
        self.before = None
        self.txt_detail = ""
        self.txt_count = ""
        self.txt_admin = ""
        self.var_purge = tk.BooleanVar(value=True)

        super().__init__(root, shot=shot, title=APP_TITLE)

        self._refresh_admin()
        self.refresh_memory()
        self.refresh_processes()
        self.log("程序已启动。建议先点「一键整理」查看效果。")
        self.root.after(1600, self._tick_memory)

    def _all_buttons(self):
        out = []
        for name in ("btn_opt", "btn_standby", "btn_kill", "_btn_refresh",
                 "_btn_clearlog"):
            b = getattr(self, name, None)
            if b is not None and b not in out:
                out.append(b)
        return out

    @staticmethod
    def _card_rect(c):
        return (c[0], c[1], c[2], c[3])

    # ------------------------------------------------ glass_ui 钩子
    def glass_cards(self):
        return [self._card_rect(CARD_MEM),
                self._card_rect(self.geo["table"]),
                self._card_rect(self.geo["log"])]

    def glass_decorators(self):
        return [self._deco_mem_card]

    def _deco_mem_card(self, img):
        """内存卡上的进度条凹槽 + 「可用」徽章底。

        这两样都要半透明，Canvas 画不了（会盖住玻璃底），
        只能画进底图 —— 这个钩子就是给这种元素准备的。
        """
        w, h = img.size
        mx, my, mw, _mh = CARD_MEM
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle([mx + 24, my + 86, mx + mw - 24, my + 98],
                            radius=6, fill=(0, 0, 0, 66),
                            outline=(255, 255, 255, 20), width=1)

        bx, by2, bw2, bh2 = MEM_BADGE
        d.rounded_rectangle([bx, by2, bx + bw2, by2 + bh2],
                            radius=bh2 / 2.0,
                            fill=(48, 209, 88, 34),
                            outline=(48, 209, 88, 88), width=1)
        return Image.alpha_composite(img, layer)

    def glass_slices(self):
        """每个子 Canvas 都要一块背景切片，否则圆角外会露出深色方框。"""
        tx, ty, tw, th = self.geo["table"]
        lx, ly, lw, lh = self.geo["log"]
        out = [
            (self.table, tx + 1, ty + TABLE_HEAD_H,
             tw - 2, th - TABLE_HEAD_H - 1),
            (self.logview, lx + 1, ly + LOG_HEAD_H,
             lw - 2, lh - LOG_HEAD_H - 1),
        ]
        for b in self._all_buttons():
            out.append((b, b.x, b.y, b.w, b.h))
        for ic in (self.btn_min, self.btn_close, self.btn_about):
            out.append((ic, ic.x, ic.y, ic.size, ic.size))
        t = self.toggle
        out.append((t, t.cv.winfo_x(), t.cv.winfo_y(),
                    t.cv.winfo_width(), t.cv.winfo_height()))
        return out

    def build_ui(self):
        w, h = self.win_w, self.win_h

        # 排版用的字体度量：数值与单位的对齐要算真实宽度
        self.f_num = tkfont.Font(family=FONT_UI, size=30, weight="bold")
        self.f_unit = tkfont.Font(family=FONT_UI, size=12)
        self.f_tot = tkfont.Font(family=FONT_UI, size=11)

        # 纵向重排：窗口偏矮时压缩表格卡
        table_h = CARD_TABLE[3] + (h - 744)
        self.geo = {
            "mem": CARD_MEM,
            "table": (CARD_TABLE[0], CARD_TABLE[1], CARD_TABLE[2], max(250, table_h)),
            "btn_y": BTN_Y + (h - 744),
            "log": (CARD_LOG[0], CARD_LOG[1] + (h - 744), CARD_LOG[2], CARD_LOG[3]),
        }

        self.make_canvas()

        # 表格
        tx, ty, tw, th = self.geo["table"]
        self.table = GlassTable(self, tx + 1, ty + TABLE_HEAD_H,
                                tw - 2, th - TABLE_HEAD_H - 1)
        self.table.COLS = COLS
        # 库不认识 tag 叫什么，这里把本程序的三种 tag 映射成颜色
        self.table.ROW_FG = {"bgapp": C_ORANGE, "prot": "#949db0"}
        self.table.ROW_DOT = ("bgapp",)
        self.table.NOTE_FG = {"bgapp": C_ORANGE}

        # 日志
        lx, ly, lw, lh = self.geo["log"]
        self.logview = GlassLog(self, lx + 1, ly + LOG_HEAD_H,
                                lw - 2, lh - LOG_HEAD_H - 1)

        # 按钮
        by = self.geo["btn_y"]
        self._btns_layout = []
        specs = (
            ("一键整理", 132, "primary"),
            ("清理待机内存", 148, "ghost"),
            ("结束选中进程", 148, "ghost"),
        )
        bx = 18
        self.btn_opt = self.btn_standby = self.btn_kill = None
        for text, bwid, kind in specs:
            self._btns_layout.append((bx, by, bwid, BTN_H, kind))
            bx += bwid + 10

        self.btn_opt = GlassButton(self, "一键整理", self.do_optimize,
                                   18, by, 132, BTN_H, "primary")
        self.btn_standby = GlassButton(self, "清理待机内存", self.do_purge_standby,
                                       160, by, 148, BTN_H, "ghost")
        self.btn_kill = GlassButton(self, "结束选中进程", self.do_kill,
                                    318, by, 148, BTN_H, "ghost")

        self.toggle = GlassToggle(self, self.var_purge, w - 248, by + 6,
                                  "整理后清理待机")

        # 标题栏图标按钮
        self.btn_close = IconButton(self, "✕", self.close, w - 42, 14, danger=True)
        self.btn_min = IconButton(self, "—", self.minimize, w - 76, 14)
        self.btn_about = IconButton(self, "?", lambda: self.show_about(), w - 110, 14)

        self.draw_chrome()

    # ---------------- 主绘制 ----------------
    def draw_chrome(self):
        cv = self.cv
        cv.delete("all")
        w, h = self.win_w, self.win_h

        if self.bg_photo is not None:
            cv.create_image(0, 0, image=self.bg_photo, anchor="nw")
        else:
            cv.create_rectangle(0, 0, w, h, fill=C_BG, outline="")

        self._draw_titlebar(cv, w)
        self._draw_mem_card(cv)
        self._draw_table_head(cv)
        self._draw_log_head(cv)

        # 子 Canvas 会被 create_* 之后的图元覆盖，重新抬到最前。
        # 注意：Canvas.lift 被重载成了画布内图元的 tag_raise，必须走 Misc.lift
        for sub in (self.table.cv, self.logview.cv):
            try:
                tk.Misc.lift(sub)
            except Exception:
                pass

    def _draw_titlebar(self, cv, w):
        # 彩色小标识 + 标题
        cv.create_oval(20, 23, 28, 31, fill=C_ACCENT, outline="")
        cv.create_text(40, 29, text=APP_NAME, anchor="w", fill="#ffffff",
                       font=(FONT_UI, 13, "bold"))
        cv.create_text(142, 30, text=f"v{APP_VER}", anchor="w", fill="#7d8698",
                       font=(FONT_UI, 8))
        cv.create_text(176, 30, text=self.txt_admin, anchor="w",
                       fill=C_GREEN if is_admin() else "#8e96a8",
                       font=(FONT_UI, 8))

    def _draw_mem_card(self, cv):
        """内存概况卡。

        信息层级分四档：分区标签 / 主数值 / 单位与总量 / 辅助说明。
        原来的写法把"已用 12.3 GB / 共 31.2 GB 可用 18.93 GB"塞成一行同字号，
        没有视觉重心，读数时要逐字扫。
        """
        x, y, cw, ch = CARD_MEM
        m = getattr(self, "_last_mem", None)
        used = m["used_mb"] / 1024 if m else 0.0
        total = m["total_mb"] / 1024 if m else 0.0
        avail = m["avail_mb"] / 1024 if m else 0.0
        pad = 24
        busy = bool(getattr(self, "busy", False))

        # 分区标签
        cv.create_text(x + pad, y + 20, text="已用", anchor="w",
                       fill="#8e96a8", font=(FONT_UI, 9))

        # 主数值行：主数值先落，再按它真实的 bbox 右端接单位与总量。
        # 不能拿 measure() 的结果累加 —— 30pt 粗体下 measure 返回的宽度比
        # 实际渲染宽出十几像素，累加两次之后 "GB" 和 "/ 共 …" 就被推散了。
        base = y + 76
        num = f"{used:.1f}"
        it = cv.create_text(x + pad, base, text=num, anchor="sw",
                            fill="#ffffff", font=(FONT_UI, 30, "bold"))
        bb = cv.bbox(it)
        nx = (bb[2] if bb else x + pad + self.f_num.measure(num)) + 7

        # 单位与总量按基线对齐：如果直接让 bbox 底边对齐，30pt 的 descent
        # 比 12pt 大，小字号会整体偏高，看着像浮在数值上。
        drop = self.f_num.metrics("descent")
        it2 = cv.create_text(nx, base - drop + self.f_unit.metrics("descent"),
                             text="GB", anchor="sw",
                             fill="#a9b2c5", font=(FONT_UI, 12))
        bb2 = cv.bbox(it2)
        tx = (bb2[2] if bb2 else nx + self.f_unit.measure("GB")) + 12
        cv.create_text(tx, base - drop + self.f_tot.metrics("descent"),
                       text=f"/ 共 {total:.1f} GB", anchor="sw",
                       fill="#767f90", font=(FONT_UI, 11))

        # 进度条（凹槽在底图里，这里只画填充）
        # 整理进行中时这条要显示整理进度，否则进度早就跑到 100% 了用户还在等，
        # 只能盯着一个恒定的内存占用值，完全看不出在动。
        pb_y = y + 86
        pb_h = 12
        pb_x1, pb_x2 = x + pad, x + cw - pad
        if busy:
            pct = max(0.0, min(100.0, float(getattr(self, "_pct", 0.0))))
        else:
            try:
                pct = max(0.0, min(100.0, float(m["percent"]))) if m else 0.0
            except Exception:
                pct = 0.0
            self._pct = pct
        if pct > 0:
            fill_w = max(float(pb_h), (pb_x2 - pb_x1) * pct / 100.0)
            if busy:
                self._bar_img = pill_grad(cv, pb_x1, pb_y, pb_x1 + fill_w,
                                          pb_y + pb_h, (96, 214, 255),
                                          (0, 132, 222), 0.06)
            else:
                self._bar_img = pill_grad(cv, pb_x1, pb_y, pb_x1 + fill_w,
                                          pb_y + pb_h, (74, 176, 255),
                                          (10, 116, 227), 0.12)

        # 辅助说明：空闲时是内存统计，整理中换成进度文字
        if busy:
            cv.create_text(x + pad, y + 116, anchor="w",
                           text=f"正在整理内存 …  已完成 {pct:.0f}%",
                           fill="#8fd0ff", font=(FONT_UI, 9))
        else:
            cv.create_text(x + pad, y + 116, text=self.txt_detail, anchor="w",
                           fill="#8e96a8", font=(FONT_MONO, 8))

        # 「可用」徽章里的数值
        bx, by2, bw2, bh2 = MEM_BADGE
        cv.create_text(bx + bw2 / 2.0, by2 + bh2 / 2.0 + 1,
                       text=f"可用 {avail:.2f} GB", anchor="center",
                       fill="#7fe3a6", font=(FONT_UI, 9, "bold"))

    def _draw_table_head(self, cv):
        x, y, cw, ch = self.geo["table"]
        rrect(cv, x + 18, y + 10, x + 21, y + 23, 1.5, fill=C_ACCENT, outline="")
        cv.create_text(x + 28, y + 17, text="进程列表", anchor="w",
                       fill="#eef2f9", font=(FONT_UI, 9, "bold"))
        cv.create_text(x + 94, y + 18, text=self.txt_count, anchor="w",
                       fill="#98a1b4", font=(FONT_UI, 8))

        # 列头
        hy = y + TABLE_HEAD_H - 3
        inner_w = cw - 2
        for key, a, b, anchor, label in COLS:
            x1 = x + 1 + 10 + (inner_w - 20) * a
            x2 = x + 1 + 10 + (inner_w - 20) * b
            if key == "name":
                tx, an = x1 + 12, "w"
            elif key == "note":
                tx, an = x1, "w"
            else:
                tx, an = x2 - 14, "e"
            cv.create_text(tx, hy - 9, text=label, anchor=an,
                           fill="#98a1b4", font=(FONT_UI, 8))

        # 表头分隔线：原来用 stipple="gray12" 的抖动线，在平滑玻璃底上只剩
        # 零星噪点；而且它画在 y+TABLE_HEAD_H-3 处，正好被表格子画布（从
        # y+TABLE_HEAD_H 起、又整体 lift 到最上层）盖住，从来没显示过。
        # 改成紧贴子画布上边界一根 1px 带 alpha 的贴图，才是真的半透明白线。
        dw = max(2, int(cw - 24))
        if getattr(self, "_hd_w", None) != dw:
            self._hd_img = ImageTk.PhotoImage(
                Image.new("RGBA", (dw, 1), (255, 255, 255, 34)))
            self._hd_w = dw
        cv.create_image(x + 12, y + TABLE_HEAD_H - 1,
                        image=self._hd_img, anchor="nw")

        # 刷新按钮（放右上角，做成小胶囊）
        if not hasattr(self, "_btn_refresh"):
            self._btn_refresh = GlassButton(
                self, "刷新", self.refresh_processes,
                x + cw - 88, y + 9, 70, 28, "ghost")

    def _draw_log_head(self, cv):
        x, y, cw, ch = self.geo["log"]
        # 与进程列表卡统一：竖条锚点 + 同字号同亮度的标题
        rrect(cv, x + 18, y + 10, x + 21, y + 23, 1.5, fill=C_CYAN, outline="")
        cv.create_text(x + 28, y + 17, text="运行日志", anchor="w",
                       fill="#eef2f9", font=(FONT_UI, 9, "bold"))
        if not hasattr(self, "_btn_clearlog"):
            self._btn_clearlog = GlassButton(
                self, "清空", self.logview.clear,
                x + cw - 76, y + 2, 58, 22, "ghost")

    # ---------------- 状态 ----------------
    def _refresh_admin(self):
        if is_admin():
            self.txt_admin = "● 管理员"
        else:
            self.txt_admin = "○ 普通权限"
        self.draw_chrome()

    def log(self, msg, tag=None):
        try:
            self.logview.push(msg, tag)
        except Exception:
            pass

    # ---------------- 内存 ----------------
    def refresh_memory(self):
        m = get_memory_status()
        self._last_mem = m
        self._pct = m["percent"]
        self.txt_detail = (f"使用率 {m['percent']:>5.1f}%    "
                           f"系统缓存 {m['cache_mb']/1024:.2f} GB    "
                           f"已提交 {m['commit_mb']/1024:.2f} GB    "
                           f"页面文件 {m['pagefile_pct']:.0f}%")
        self.draw_chrome()
        return m

    def _draw_progress(self, pct):
        try:
            self._pct = max(0.0, min(100.0, float(pct)))
            self.draw_chrome()
        except Exception:
            pass

    def _tick_memory(self):
        if not self.busy:
            try:
                self.refresh_memory()
            except Exception:
                pass
        self.root.after(1600, self._tick_memory)

    # ---------------- 进程列表 ----------------
    def refresh_processes(self):
        if self.busy:
            return
        pids = enum_pids()
        rows = []
        for pid in pids:
            r = query_process(pid)
            if r:
                rows.append((r[0], pid, r[1]))
        rows.sort(key=lambda x: x[2], reverse=True)
        self.snapshot = rows

        self.table.delete()
        for name, pid, ws in rows:
            if is_protected(name):
                tag, note = "prot", "系统关键 · 受保护"
            elif name.lower() in BACKGROUND_HINTS:
                tag, note = "bgapp", BACKGROUND_HINTS[name.lower()] + " · 可结束"
            else:
                tag, note = "normal", ""
            self.table.insert("", "end", iid=str(pid), tags=(tag,), values=(
                name, pid, f"{ws/1048576:.1f} MB" if ws else "—", note))
        self.txt_count = f"共 {len(rows)} 个可枚举进程"
        self.refresh_memory()

    # ---------------- 一键整理 ----------------
    def do_optimize(self):
        if self.busy:
            return
        self.busy = True
        self.btn_opt.configure(state="disabled", text="整理中…")
        self.btn_kill.configure(state="disabled")
        threading.Thread(target=self._optimize_worker, daemon=True).start()

    def _optimize_worker(self):
        t0 = time.time()
        before = self.refresh_memory_silent()
        self.log("开始内存整理 …")
        self.log(f"整理前：可用 {before['avail_mb']/1024:.2f} GB，"
                 f"使用率 {before['percent']:.1f}%", "dim")

        pids = enum_pids()
        total = len(pids)
        ok = fail = 0
        done = 0
        last_ui = 0.0

        for pid in pids:
            if pid == 0:
                continue
            if empty_working_set(pid):
                ok += 1
            else:
                fail += 1
            done += 1
            now = time.time()
            if now - last_ui > 0.06:
                last_ui = now
                pct = done * 100.0 / max(1, total)
                self._ui_progress(pct)

        self._ui_progress(100.0)
        self.log(f"工作集清空完成：成功 {ok} 个，失败 {fail} 个（失败通常是权限不足）")

        if self.var_purge.get():
            self.log("正在清理待机内存列表 …", "dim")
            good, st = purge_standby_list()
            if good:
                self.log("待机内存列表已清空。", "ok")
            elif not is_admin():
                self.log(f"待机内存清理失败（状态码 {st}）：需要管理员权限。"
                         f"可点右上角「?」查看提权方法。", "warn")
            else:
                self.log(f"待机内存清理失败，状态码 {st}。", "warn")

        time.sleep(0.8)
        after = self.refresh_memory_silent()
        gain = after["avail_mb"] - before["avail_mb"]
        used_before = before["used_mb"]
        used_after = after["used_mb"]

        self.log(f"整理后：可用 {after['avail_mb']/1024:.2f} GB，"
                 f"使用率 {after['percent']:.1f}%", "dim")
        msg = (f"物理内存占用 {used_before/1024:.2f} GB → {used_after/1024:.2f} GB，"
               f"可用增加 {gain:+.0f} MB，耗时 {time.time()-t0:.1f} 秒")
        self.log(msg, "ok" if gain > 0 else "warn")

        def _finish():
            self.busy = False
            self.btn_opt.configure(state="normal", text="一键整理")
            self.btn_kill.configure(state="normal")
            self.refresh_processes()
        self.root.after(0, _finish)

    def refresh_memory_silent(self):
        return get_memory_status()

    def _ui_progress(self, pct):
        def _do():
            try:
                self._pct = pct
                m = get_memory_status()
                self._last_mem = m
                self.txt_detail = (f"使用率 {m['percent']:>5.1f}%    "
                                   f"系统缓存 {m['cache_mb']/1024:.2f} GB    "
                                   f"已提交 {m['commit_mb']/1024:.2f} GB    "
                                   f"页面文件 {m['pagefile_pct']:.0f}%")
                self.draw_chrome()
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    # ---------------- 清理待机 ----------------
    def do_purge_standby(self):
        if self.busy:
            return
        before = get_memory_status()
        good, st = purge_standby_list()
        time.sleep(0.5)
        after = get_memory_status()
        if good:
            self.log(f"待机内存已清空：可用 {before['avail_mb']/1024:.2f} GB → "
                     f"{after['avail_mb']/1024:.2f} GB", "ok")
        elif not is_admin():
            if self.ask("需要管理员权限",
                        "清理待机内存需要管理员权限。\n\n是否以管理员身份重新启动本程序？",
                        "以管理员重启", "取消"):
                if not relaunch_as_admin():
                    self.info("提权失败", "无法以管理员身份重启，请手动右键选择「以管理员身份运行」。")
                else:
                    self.close()
                    return
            else:
                self.log("已取消提权。", "dim")
        else:
            self.log(f"清理失败，状态码 {st}。", "err")
        self.refresh_memory()

    # ---------------- 结束进程 ----------------
    def do_kill(self):
        if self.busy:
            return
        sel = self.table.selection()
        if not sel:
            self.info("提示", "请先在列表里选中要结束的进程（可按住 Ctrl 多选）。")
            return

        targets, blocked = [], []
        for iid in sel:
            vals = self.table.item(iid, "values")["values"]
            if len(vals) < 2:
                continue
            name, pid = vals[0], vals[1]
            if is_protected(name):
                blocked.append(name)
            else:
                targets.append((name, pid))

        if blocked:
            self.info("已跳过受保护进程",
                      "以下进程属于系统关键进程或反作弊组件，已跳过：\n\n"
                      + "\n".join(blocked))
        if not targets:
            return

        names = "\n".join(f"  {n}  (PID {p})" for n, p in targets[:12])
        if len(targets) > 12:
            names += f"\n  … 另有 {len(targets) - 12} 个"
        if not self.ask("确认结束进程",
                        f"将强制结束以下 {len(targets)} 个进程：\n\n{names}\n\n"
                        f"未保存的数据会丢失，是否继续？",
                        "结束", "取消"):
            return

        okc = 0
        for name, pid in targets:
            if kill_process(int(pid)):
                okc += 1
                self.log(f"已结束 {name} (PID {pid})", "ok")
            else:
                self.log(f"结束失败 {name} (PID {pid})，可能需要管理员权限", "warn")
        self.log(f"结束进程完成：{okc}/{len(targets)} 成功")
        self.refresh_processes()

    # ---------------- 玻璃对话框 ----------------
    # ---------------- 说明 ----------------
    ABOUT_BLOCKS = (
        ("h", "这个程序做什么"),
        ("p", "在你启动大型游戏或工程软件之前，把系统里各个进程占用的物理内存页集中回收一次，"
              "使即将启动的程序能拿到更充裕的物理内存，减少启动阶段的换页。"),
        ("h", "「一键整理」的实际动作（全部是 Windows 官方接口）"),
        ("p", "1. EmptyWorkingSet —— 遍历所有进程，把工作集里的物理页移出进程工作区。"
              "任务管理器的「使用中」会明显下降。"),
        ("p", "2. NtSetSystemInformation(MemoryPurgeStandbyList) —— 清空待机内存列表，"
              "这部分页会被真正释放。此项需要管理员权限。"),
        ("h", "必须清楚的一点"),
        ("w", "这一步本质上是把物理页从「使用中」挪到「可回收」，页本身还在内存条里。"
              "它降低的是任务管理器的账面数字，并不创造新的物理内存。"),
        ("p", "真正有用的场景是：启动一个要一次性申请大块内存的程序之前，"
              "让后台程序把物理页先让出来，避免启动瞬间与后台程序抢页。"),
        ("w", "如果目标是让游戏帧数变高，这个操作没有帮助 —— 那属于显存和 GPU 的问题。"),
        ("h", "想让后台程序真正吐出内存"),
        ("p", "用列表里的「结束选中进程」。进程退出后，它占的内存是真的还给了系统。"
              "橙色标注的是常见后台程序，灰色标注的是受保护的系统进程（无法结束）。"),
        ("h", "关于界面"),
        ("d", "界面的模糊背景是对窗口后方桌面的实时采样（BitBlt + 高斯模糊），"
              "不是渐变模拟。参数与配色参考了 Liquid Glass 设计规范。"),
        ("h", "功能来源与署名"),
        ("d", "本程序的功能设计参考了 Minecraft 启动器 Plain Craft Launcher（PCL，作者：龙腾猫跃）"
              "中「启动游戏前进行内存优化」这一功能的使用方式与执行语义。"),
        ("d", "需要说明：PCL 该功能的实现并未开源 —— 其公开仓库中 PageOtherTest.MemoryOptimize "
              "被替换为空实现，仅保留调用点。因此本程序是依据 Windows 官方文档独立编写的等价实现，"
              "不含任何 PCL 源代码，与 PCL 无隶属关系，也不是其衍生作品。"),
    )

    def show_about(self):
        dw = 660
        pad = 28
        txt_w = dw - pad * 2
        color = {"h": "#ffffff", "p": "#c8cfdc", "d": "#8a92a6", "w": C_ORANGE}
        size = {"h": 10, "p": 9, "d": 8, "w": 9}

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.configure(bg=C_BG)
        win.transient(self.root)

        cv = tk.Canvas(win, width=dw, height=900, bd=0,
                       highlightthickness=0, bg=C_BG)
        cv.place(x=0, y=0, width=dw, height=900)

        # 先排版量高度：Tk 自己换行最准，不要再手算字符数
        y = 64                      # 让出标题与分隔线的高度
        for kind, text in self.ABOUT_BLOCKS:
            if kind == "h":
                y += 4
                it = cv.create_text(pad, y, text=text, anchor="nw", fill=color[kind],
                                    font=(FONT_UI, size[kind], "bold"), width=txt_w)
                bb = cv.bbox(it)
                y = (bb[3] if bb else y + 20) + 6
            else:
                it = cv.create_text(pad, y, text=text, anchor="nw", fill=color[kind],
                                    font=(FONT_UI, size[kind]), width=txt_w)
                bb = cv.bbox(it)
                y = (bb[3] if bb else y + 18) + (7 if kind == "p" else 6)

        dh = int(y + 62)
        dh = min(dh, 760)

        px = self.root.winfo_rootx() + (self.win_w - dw) // 2
        py = max(16, self.root.winfo_rooty() + (self.win_h - dh) // 2)
        win.geometry(f"{dw}x{dh}+{px}+{py}")
        cv.configure(height=dh)
        cv.place(x=0, y=0, width=dw, height=dh)

        photo = self.glass_plate(dw, dh, 0.38, radius=22)
        if photo is not None:
            tkimg = ImageTk.PhotoImage(photo.convert("RGB"))
            cv.create_image(0, 0, image=tkimg, anchor="nw", tags=("plate",))
            cv.image = tkimg
            cv.tag_lower("plate")

        cv.create_text(pad, 26, text="说明", anchor="w", fill="#ffffff",
                       font=(FONT_UI, 12, "bold"))
        cv.create_line(pad, 46, dw - pad, 46, fill="#7d8698", width=1, stipple="gray25")

        btn = GlassButton(self, "关闭", win.destroy, dw - pad - 96, dh - 50,
                          96, 34, "ghost", parent=cv)
        btn.cv.place(x=dw - pad - 96, y=dh - 50, width=96, height=34)
        if photo is not None:
            # 不给按钮铺背景切片，胶囊四角会露出子 Canvas 的深色底
            btn.set_background(photo.crop(
                (dw - pad - 96, dh - 50, dw - pad, dh - 50 + 34)).convert("RGB"))

        win.deiconify()
        win.attributes("-topmost", True)
        win.lift()
        apply_round_region(win, 22)
        try:
            win.grab_set()
        except Exception:
            pass

    # ---------------- 运行 ----------------
# ============================================================================
#  自检模式：不打开界面，只验证 API 是否正常（开发和排错用）
# ============================================================================
def selftest():
    lines = []

    def p(s=""):
        lines.append(str(s))
        try:
            print(s)
        except Exception:
            pass  # --noconsole 打包后没有 stdout

    p(f"=== {APP_NAME} {APP_VER} 自检 ===")
    p(f"Python: {sys.version.split()[0]}")
    p(f"打包运行: {getattr(sys, 'frozen', False)}")
    p(f"管理员权限: {is_admin()}")
    p(f"Pillow 可用: {HAVE_PIL}")

    m = get_memory_status()
    p(f"物理内存: 总 {m['total_mb']:.0f} MB, 可用 {m['avail_mb']:.0f} MB, "
      f"使用率 {m['percent']}%")
    p(f"系统缓存: {m['cache_mb']:.0f} MB, 已提交 {m['commit_mb']:.0f} MB")

    pids = enum_pids()
    p(f"枚举进程: {len(pids)} 个")

    rows = []
    for pid in pids:
        r = query_process(pid)
        if r:
            rows.append((r[0], pid, r[1]))
    rows.sort(key=lambda x: x[2], reverse=True)
    p(f"可查询进程: {len(rows)} 个")
    p("占用前 5:")
    for name, pid, ws in rows[:5]:
        p(f"  {name:<34} PID={pid:<7} {ws/1048576:>8.1f} MB")

    # 玻璃引擎自测：抓屏 + 合成耗时
    if HAVE_PIL:
        try:
            eng = GlassEngine()
            t0 = time.time()
            src = eng.grab(0, 0, 200, 120)
            t1 = time.time()
            img = eng.compose(200, 120, [(10, 10, 180, 100)], [])
            t2 = time.time()
            p(f"玻璃引擎: 抓屏 {'成功' if src is not None else '失败'} "
              f"{int((t1-t0)*1000)}ms / 合成 {int((t2-t1)*1000)}ms "
              f"/ 输出 {img.size if img else None}")
        except Exception as e:
            p(f"玻璃引擎: 异常 {e!r}")

    me = os.getpid()
    before = query_process(me)
    ok = empty_working_set(me)
    after = query_process(me)
    if before and after:
        p(f"EmptyWorkingSet 自测: {'成功' if ok else '失败'}  "
          f"自身工作集 {before[1]/1048576:.1f} MB -> {after[1]/1048576:.1f} MB")

    good, st = purge_standby_list()
    p(f"清理待机内存: {'成功' if good else '失败'} (状态码 {st})")

    # GUI 初始化检查
    try:
        r = tk.Tk()
        r.withdraw()
        app = MemTidyApp(r, shot=True)
        r.update_idletasks()
        r.update()
        p(f"GUI 初始化: 成功  列表 {len(app.table.get_children())} 行")
        p(f"窗口尺寸: {app.win_w}x{app.win_h}  背景图: {app.bg_photo is not None}")
        r.destroy()
    except Exception as e:
        import traceback
        p(f"GUI 初始化: 失败 {e!r}")
        p(traceback.format_exc())

    p("=== 自检结束 ===")

    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    try:
        fp = os.path.join(base, "selftest_report.txt")
        with open(fp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print("report:", fp)
    except Exception:
        pass


def screenshot_mode(out_path):
    """把界面渲染出来并截图，用于开发者核对布局（普通用户用不到）。"""
    root = tk.Tk()
    app = MemTidyApp(root, shot=True)
    root.attributes("-topmost", True)
    app.rebuild_background()
    root.update_idletasks()
    for _ in range(60):
        root.update()
        time.sleep(0.03)

    try:
        import importlib
        ImageGrab = importlib.import_module("PIL.ImageGrab")
        # 截图前解除抓屏排除，否则只能拍到桌面
        app.set_capture_affinity(False)
        time.sleep(0.25)
        x = root.winfo_rootx()
        y = root.winfo_rooty()
        bbox = (x, y, x + root.winfo_width(), y + root.winfo_height())
        ImageGrab.grab(bbox=bbox, all_screens=True).save(out_path)
        print("截图已保存:", out_path, bbox)
    except Exception as e:
        print("截图失败:", repr(e))
    finally:
        root.destroy()


def main():
    if "--selftest" in sys.argv:
        selftest()
        return

    # 高 DPI 适配
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "shot.png"
        screenshot_mode(out)
        return

    root = tk.Tk()
    app = MemTidyApp(root)
    app.run()


if __name__ == "__main__":
    main()
