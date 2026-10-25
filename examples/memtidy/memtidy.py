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
import json
import os
import subprocess
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
                      GlassSlider, GlassTable, GlassToggle, GlassWindow,
                      IconButton, pill_grad, rrect)
from glass_ui.compat import HAVE_PIL, Image, ImageDraw, ImageTk
from glass_ui.win import apply_round_region, make_taskbar_window

APP_NAME = "内存整理器"
APP_VER = "2.3"
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
#  设置持久化
# ============================================================================
SETTINGS_DIR = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "MemTidy")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")

# glass_opacity：界面透明度百分比。100 = 最通透，0 = 磨砂感最强。
# 74 是出厂值，对应玻璃库历史上的 VEIL = 0.74，界面与旧版完全一致。
DEFAULT_SETTINGS = {"glass_opacity": 74}
OPACITY_RANGE = (0, 100)


def load_settings():
    """读取设置文件，任何异常都静默回落到默认值。

    这个函数在窗口构造的最前面调用，绝对不能抛异常 —— 一旦抛出，
    `--windowed` 打包的程序没有控制台，用户看到的就是「双击图标毫无反应」。
    文件缺失、JSON 损坏、值越界、类型不对，一律当默认值处理。
    """
    data = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return data
    if not isinstance(raw, dict):
        return data

    for key, default in DEFAULT_SETTINGS.items():
        val = raw.get(key, default)
        # bool 是 int 的子类：{"glass_opacity": true} 会被 int(True) 悄悄变成
        # 1% —— 用户手改配置或别的程序写入 true 时就"最磨砂"了，直接回落默认
        if isinstance(val, bool):
            val = default
        else:
            try:
                val = int(round(float(val)))
            except (TypeError, ValueError):
                val = default
        data[key] = max(OPACITY_RANGE[0], min(OPACITY_RANGE[1], val))
    return data


def save_settings(data):
    """原子写入设置文件（先写临时文件再 replace）。失败返回 False，不抛异常。"""
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
        return True
    except Exception:
        return False

# ============================================================================
#  Win32 接口
# ============================================================================
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
user32 = ctypes.WinDLL("user32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

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
kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wt.HANDLE

# NTSTATUS 是 32 位有符号整数，用 c_long 接才不会在 64 位上被截断成垃圾值
ntdll.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                         ctypes.c_ulong]
ntdll.NtSetSystemInformation.restype = ctypes.c_long

# ---- 访问令牌与特权 ----
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002
ERROR_NOT_ALL_ASSIGNED = 1300       # AdjustTokenPrivileges 的"部分特权未分配"


class LUID(ctypes.Structure):
    """本地唯一标识符。`LookupPrivilegeValueW` 的输出就是这个结构。"""

    _fields_ = [
        ("LowPart", wt.DWORD),
        ("HighPart", wt.LONG),
    ]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    """一个 LUID 加属性位（SE_PRIVILEGE_ENABLED 等）。"""

    _fields_ = [
        ("Luid", LUID),
        ("Attributes", wt.DWORD),
    ]


class TOKEN_PRIVILEGES(ctypes.Structure):
    """TOKEN_PRIVILEGES。这里一次只启用一条特权，数组长度固定为 1。"""

    _fields_ = [
        ("PrivilegeCount", wt.DWORD),
        ("Privileges", LUID_AND_ATTRIBUTES * 1),
    ]


advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD,
                                      ctypes.POINTER(wt.HANDLE)]
advapi32.OpenProcessToken.restype = wt.BOOL
advapi32.LookupPrivilegeValueW.argtypes = [wt.LPCWSTR, wt.LPCWSTR,
                                           ctypes.POINTER(LUID)]
advapi32.LookupPrivilegeValueW.restype = wt.BOOL
advapi32.AdjustTokenPrivileges.argtypes = [
    wt.HANDLE, wt.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES), wt.DWORD,
    ctypes.c_void_p, ctypes.c_void_p]
advapi32.AdjustTokenPrivileges.restype = wt.BOOL

# ShellExecuteW 返回 HINSTANCE（指针）。restype 留默认的 c_int 会在 64 位下
# 把返回值截断，错误码判断就全错 —— 必须显式声明成 c_void_p。
shell32.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR,
                                  wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = ctypes.c_void_p
shell32.IsUserAnAdmin.argtypes = []
shell32.IsUserAnAdmin.restype = wt.BOOL

SHELLEXEC_NOERROR = 32      # ShellExecuteW 返回值 > 32 才算成功
ERROR_ACCESS_DENIED = 5     # 用户在 UAC 里点了「否」/ 策略不允许提权
ERROR_CANCELLED = 1223      # 用户关掉了 UAC 对话框
# 这两个错误码在数值上可能大于 32（1223 > 32），所以不能只靠大小判断，
# 必须显式排除，否则"用户取消"会被当成"提权成功"
SHELLEXEC_FAILURE_CODES = frozenset({0, ERROR_ACCESS_DENIED, ERROR_CANCELLED})


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
    """以管理员身份重启自己（用于清理待机内存）。

    `ShellExecuteW` 的返回值是 HINSTANCE，**不是** 布尔：`> 32` 才算成功，
    其余都是错误码（5 = 拒绝访问，即用户在 UAC 里点了「否」；
    1223 = 用户取消）。老实现不看返回值直接 `return True`，于是用户点了
    「否」之后调用方会走到 `self.close()` —— 窗口凭空消失，什么都没做，
    用户完全不知道发生了什么。

    Returns:
        (ok, code)：ok 为 True 表示新进程已经拉起；code 是 ShellExecuteW
        的原始返回值（失败时用于区分"用户取消"和"提权被拒"）。
    """
    try:
        if getattr(sys, "frozen", False):
            # 打包后 sys.executable 就是 exe 本身
            exe = sys.executable
            params = ""
            workdir = os.path.dirname(os.path.abspath(sys.executable))
        else:
            # 脚本方式运行：sys.executable 是解释器，必须把脚本和原参数带上，
            # 否则只会拉起一个裸 python.exe（什么都不开）
            script = os.path.abspath(sys.argv[0])
            exe = sys.executable
            params = subprocess.list2cmdline([script] + sys.argv[1:])
            workdir = os.path.dirname(script)

        ctypes.set_last_error(0)
        rc = shell32.ShellExecuteW(None, "runas", exe, params, workdir, 1)
        code = int(rc or 0)
        if code > SHELLEXEC_NOERROR and code not in SHELLEXEC_FAILURE_CODES:
            return True, code
        if code == 0:
            # 返回 0 时真正的错误码在 last error 里
            code = ctypes.get_last_error() or 0
        return False, code
    except Exception:
        return False, -1


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


def enable_privilege(name):
    """启用当前进程令牌中的一个特权，成功返回 True。

    为什么需要它：`SeProfileSingleProcessPrivilege`、`SeDebugPrivilege` 这类
    特权在管理员令牌里也是「存在但未启用」状态（默认只启用
    SeChangeNotifyPrivilege / SeIncreaseWorkingSetPrivilege 等少数几个）。
    必须自己 `AdjustTokenPrivileges` 打开，系统调用才不会返回
    0xC0000061 STATUS_PRIVILEGE_NOT_HELD。

    另一个坑：`AdjustTokenPrivileges` 在「部分特权未分配」时仍返回 TRUE，
    真正的结论在 GetLastError 里 —— 必须查 ERROR_NOT_ALL_ASSIGNED(1300)。

    Args:
        name: 特权名，如 "SeProfileSingleProcessPrivilege"。

    Returns:
        True 表示该特权此刻处于启用状态；False 表示打开失败（权限不足、
        被组策略剥夺、或调用了不存在的特权名）。
    """
    token = wt.HANDLE()
    try:
        opened = advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(token))
    except Exception:
        return False
    if not opened:
        return False

    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False

        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED

        ctypes.set_last_error(0)
        ok = advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(tp), 0, None, None)
        err = ctypes.get_last_error()
        if not ok:
            return False
        return err != ERROR_NOT_ALL_ASSIGNED
    except Exception:
        return False
    finally:
        try:
            kernel32.CloseHandle(token)
        except Exception:
            pass


def purge_standby_list():
    """清空待机内存列表（NtSetSystemInformation / MemoryPurgeStandbyList）。

    这个接口要求调用进程的令牌里 **SeProfileSingleProcessPrivilege 处于启用
    状态**。关键点在于：即便是管理员账户，该特权在令牌里默认也是
    「存在但未启用」（SE_PRIVILEGE_ENABLED_BY_DEFAULT 不含它），必须显式
    `AdjustTokenPrivileges` 打开，否则内核直接返回 0xC0000061
    （STATUS_PRIVILEGE_NOT_HELD）。历史版本漏了这一步，所以管理员也必然失败。

    Returns:
        (ok, status, reason)
            ok     : 布尔，系统调用是否返回 STATUS_SUCCESS。
            status : NTSTATUS 原始值（成功时为 0）。
            reason : 失败原因分类，取值
                     "ok" / "no_admin"（缺管理员权限）/
                     "no_privilege"（特权启用失败，如被组策略剥夺）/
                     "system"（系统调用返回其它错误码）。
    """
    priv_on = enable_privilege("SeProfileSingleProcessPrivilege")

    cmd = ctypes.c_int(4)  # MemoryPurgeStandbyList
    status = int(ntdll.NtSetSystemInformation(
        80, ctypes.byref(cmd), ctypes.sizeof(cmd)))

    if status == 0:
        return True, 0, "ok"
    if not is_admin():
        return False, status, "no_admin"
    if not priv_on:
        return False, status, "no_privilege"
    return False, status, "system"


def purge_reason_text(reason, status):
    """把 purge_standby_list 的失败原因翻译成给用户看的一句话。"""
    if reason == "no_admin":
        return "需要管理员权限，是否以管理员身份重启？"
    if reason == "no_privilege":
        return ("无法启用 SeProfileSingleProcessPrivilege"
                "（可能被组策略限制）")
    if reason == "system":
        return "系统调用返回 0x%08X" % (int(status) & 0xFFFFFFFF)
    return "未知原因"


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


def kill_tier(name, pid):
    """给进程分"能不能结束"的三档，排序和备注都靠它。

    名单优先于实测：explorer.exe 这类当前用户可打开终止句柄、
    但杀掉会桌面重启的进程，必须留在保护档里。

    Returns:
        0 = 当前权限下可直接结束；1 = 拿不到终止句柄（系统服务 /
        受保护进程，普通用户无解）；2 = 保护名单内的进程。
    """
    if is_protected(name):
        return 2
    try:
        h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    except Exception:
        return 1
    if h:
        kernel32.CloseHandle(h)
        return 0
    return 1


def row_tag_note(name, tier):
    """把档位翻成表格行的 (tag, 备注文字)。"""
    if tier == 2:
        return "prot", "系统关键 · 受保护"
    if tier == 1:
        return "locked", "受系统保护"
    low = name.lower()
    if low in BACKGROUND_HINTS:
        return "bgapp", BACKGROUND_HINTS[low] + " · 可结束"
    return "normal", ""


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
        self.snapshot = []        # [(name, pid, ws, tier)] 全量进程
        self.before = None
        self.txt_detail = ""
        self.txt_count = ""
        self.txt_admin = ""
        self.var_purge = tk.BooleanVar(value=True)
        # 列表筛选：排序模式与搜索词（搜索只影响显示层，不动 snapshot）
        self.sort_mode = "clean"
        self._search_text = ""
        self._search_job = None

        # 设置必须在 super().__init__ 之前读好：按钮底面的 alpha 是烘进贴图的，
        # build_ui() 里创建的按钮要拿到正确的玻璃强度
        self.settings = load_settings()

        # 设置弹窗的运行时状态
        self._settings_win = None
        self._settings_cv = None
        self._settings_size = (0, 0)
        self._settings_plate = None
        self._settings_plate_item = None
        self._settings_plate_img = None
        self._settings_hair = None
        self._settings_pct_item = None
        self._settings_slider = None
        self._settings_btn_done = None
        self._settings_btn_reset = None
        self._settings_slider_xy = (0, 0)
        self._settings_hidden = False
        self._bg_debounce = None

        super().__init__(root, shot=shot, title=APP_TITLE,
                         glass_opacity=self.settings["glass_opacity"])

        self._refresh_admin()
        self.refresh_memory()
        self.refresh_processes()
        self.log("程序已启动。建议先点「一键整理」查看效果。")
        self.root.after(1600, self._tick_memory)

    def _all_buttons(self):
        out = []
        for name in ("btn_opt", "btn_standby", "btn_kill", "btn_sort",
                 "_btn_refresh", "_btn_clearlog"):
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
        # getattr 兜底：给控件加一次漏一次会把圆角外的深色方块露出来，
        # 所以这里宁可跳过也不能 AttributeError 整段失效
        for name in ("btn_min", "btn_close", "btn_about", "btn_settings"):
            ic = getattr(self, name, None)
            if ic is not None:
                out.append((ic, ic.x, ic.y, ic.size, ic.size))
        t = self.toggle
        tgx, tgy, tgw, tgh = self.geo.get("toggle", (0, 0, 0, 0))
        if tgw > 0 and tgh > 0:
            out.append((t, tgx, tgy, tgw, tgh))
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
        # 库不认识 tag 叫什么，这里把本程序的四档 tag 映射成颜色：
        # bgapp 橙（可结束的后台程序）/ prot 灰（保护名单）/
        # locked 暗灰（拿不到终止句柄的受保护进程）
        self.table.ROW_FG = {"bgapp": C_ORANGE, "prot": "#949db0",
                             "locked": "#8a92a6"}
        self.table.ROW_DOT = ("bgapp",)
        self.table.NOTE_FG = {"bgapp": C_ORANGE}

        # 表头右侧：搜索框 + 排序循环按钮（刷新按钮在 _draw_table_head 里建）
        self.search_var = tk.StringVar(value=self.SEARCH_HINT)
        self.search_entry = tk.Entry(
            self.cv, textvariable=self.search_var, bg="#1e2434",
            fg="#6b7386", insertbackground="#eef2f9", relief="flat",
            highlightthickness=1, highlightbackground="#3d465c",
            highlightcolor="#2f6fdb", font=(FONT_UI, 9))
        # Tk Entry 没有透明能力，深色实底在这里当作"输入槽"处理，
        # 与内存卡进度条凹槽是同一套视觉语言。
        # 高度 24 且顶到 y+6：列头文字上缘在 y+41 附近，再低就会压住
        # "工作集 / 备注"列头（首版 26 高放到 y+10，实测盖掉了半行字）
        self.search_entry.place(
            x=tx + tw - 88 - 10 - 96 - 10 - 190, y=ty + 6,
            width=190, height=24)
        self.search_entry.bind("<FocusIn>", self._search_focus_in)
        self.search_entry.bind("<FocusOut>", self._search_focus_out)
        self.search_var.trace_add("write", self._on_search_changed)
        self.btn_sort = GlassButton(self, self.SORT_MODES[0][1], self._cycle_sort,
                                    tx + tw - 88 - 10 - 96, ty + 9, 96, 28,
                                    "ghost")

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
        # 开关的几何写进 geo：它的画布比轨道宽（左右留白 + 右侧标签），
        # 尺寸由控件自己算，但坐标必须记下来 —— 背景切片靠它裁，
        # 不能用 winfo_*（窗口还没映射时那是 1×1，会裁出一块黑方块）
        self.geo["toggle"] = (w - 248, by + 6,
                              self.toggle.cw + self.toggle.label_w,
                              self.toggle.ch)

        # 标题栏图标按钮。右侧四个键从外往内：关闭(w-42) / 最小化(w-76) /
        # 帮助(w-110) / 设置(w-144)，互相之间留 4px
        self.btn_close = IconButton(self, "✕", self.close, w - 42, 14, danger=True)
        self.btn_min = IconButton(self, "—", self.minimize, w - 76, 14)
        self.btn_about = IconButton(self, "?", lambda: self.show_about(), w - 110, 14)
        gear_text, gear_font = self._gear_glyph()
        self.btn_settings = IconButton(self, gear_text, self.show_settings,
                                       w - 144, 14, glyph_font=gear_font)

        self.draw_chrome()

    @staticmethod
    def _gear_glyph():
        """挑一个能真正画出齿轮的字体，返回 (字形, 字体)。

        只看字体名是不够的：雅黑、Segoe UI、Consolas 都没有 U+2699，缺字形时
        Tk 会拿私用区占位字形（U+E000）顶上，画出来就是一个豆腐块（QA 用位图
        差分证明过）。而它们的 `measure("⚙")` 与 `measure("\\uE000")` 又互不
        相等（实测 22 vs 13），所以**量宽无法可靠判定缺字**。

        与其猜，不如只认 Windows 自带的 Segoe UI Symbol（实测它确实含
        U+2699：measure = 13，和占位字形不同）；没有这个字体就退化成文字
        「设置」，保证用户看得懂，绝不画豆腐块。
        """
        probe, tofu = "\u2699", "\uE000"
        try:
            fams = set(tkfont.families())
        except Exception:
            fams = set()
        if "Segoe UI Symbol" in fams:
            try:
                f = tkfont.Font(family="Segoe UI Symbol", size=12)
                if f.measure(probe) > 0 and f.measure(probe) != f.measure(tofu):
                    return probe, ("Segoe UI Symbol", 12)
            except Exception:
                pass
        return "设置", (FONT_UI, 7)

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
    # 排序模式循环：可清理优先（默认）/ 按内存 / 按名称
    SORT_MODES = (
        ("clean", "排序：可清理"),
        ("mem", "排序：内存"),
        ("name", "排序：名称"),
    )
    SEARCH_HINT = "搜索进程名…"

    def refresh_processes(self):
        if self.busy:
            return
        pids = enum_pids()
        rows = []
        for pid in pids:
            r = query_process(pid)
            if r:
                rows.append((r[0], pid, r[1], kill_tier(r[0], pid)))
        self.snapshot = rows
        self._rebuild_table_rows()
        self.refresh_memory()

    def _rebuild_table_rows(self):
        """按当前搜索词过滤 + 按当前模式排序后重灌表格。

        snapshot 保存全部进程（含档位），搜索只影响显示层；选中项以
        PID 记录，重灌后仍然有效。
        """
        query = (self._search_text or "").strip().lower()
        rows = [r for r in self.snapshot
                if not query or query in r[0].lower()]

        if self.sort_mode == "mem":
            rows.sort(key=lambda r: (-r[2], r[0].lower()))
        elif self.sort_mode == "name":
            rows.sort(key=lambda r: (r[0].lower(), r[1]))
        else:   # clean：可结束的排前面，同档内按内存从大到小
            rows.sort(key=lambda r: (r[3], -r[2]))

        self.table.delete()
        for name, pid, ws, tier in rows:
            tag, note = row_tag_note(name, tier)
            self.table.insert("", "end", iid=str(pid), tags=(tag,), values=(
                name, pid, f"{ws/1048576:.1f} MB" if ws else "—", note))
        # 批量插完必须显式重绘一次：GlassTable.insert 只排队一次空闲重绘，
        # 不在这里落笔的话，点「刷新」之后表头写着「共 N 个进程」而表格是空的
        self.table.redraw()
        n = len(self.snapshot)
        self.txt_count = (f"匹配 {len(rows)} / 共 {n} 个进程" if query
                          else f"共 {n} 个可枚举进程")
        self.draw_chrome()

    def _cycle_sort(self):
        idx = next((i for i, (m, _t) in enumerate(self.SORT_MODES)
                    if m == self.sort_mode), 0)
        self.sort_mode, label = self.SORT_MODES[(idx + 1) % len(self.SORT_MODES)]
        try:
            self.btn_sort.configure(text=label)
        except Exception:
            pass
        self._rebuild_table_rows()

    def _on_search_changed(self, *_):
        """搜索词变化防抖 200ms：逐键重灌几百行会卡输入。"""
        if self._search_job is not None:
            try:
                self.root.after_cancel(self._search_job)
            except Exception:
                pass
        try:
            self._search_job = self.root.after(200, self._apply_search)
        except Exception:
            self._search_job = None

    def _apply_search(self):
        self._search_job = None
        text = self.search_var.get()
        self._search_text = "" if text == self.SEARCH_HINT else text
        self._rebuild_table_rows()

    def _search_focus_in(self, _e=None):
        if self.search_var.get() == self.SEARCH_HINT:
            self.search_var.set("")
            try:
                self.search_entry.configure(fg="#eef2f9")
            except Exception:
                pass

    def _search_focus_out(self, _e=None):
        if not self.search_var.get():
            self.search_var.set(self.SEARCH_HINT)
            try:
                self.search_entry.configure(fg="#6b7386")
            except Exception:
                pass

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
            good, st, reason = purge_standby_list()
            if good:
                self.log("待机内存列表已清空。", "ok")
            else:
                # 三种失败原因必须分开说，否则用户只会看到"失败"两个字，
                # 不知道是自己没提权、还是特权被组策略摘掉了
                self.log(f"待机内存清理失败：{purge_reason_text(reason, st)}",
                         "warn")
                if reason == "no_admin":
                    self.log("整理流程不会自行提权：需要时请右键「以管理员身份运行」"
                             "后再点「清理待机内存」。", "dim")

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
    def _admin_relaunch_failed(self, code):
        """提权失败后的统一解释：日志 + 弹窗，绝不关闭主窗口。

        用户点了 UAC「否」（5）、关掉 UAC（1223）、系统拒绝（0）都要说清楚
        发生了什么 —— 以前不看返回值直接 return True，失败时窗口凭空消失。
        """
        if code == ERROR_CANCELLED:
            title = "已取消提权"
            msg = ("UAC 对话框被关掉了，提权没有执行，程序继续运行。\n\n"
                   "需要清理待机内存时，可以右键 exe → 「以管理员身份运行」。")
        elif code == ERROR_ACCESS_DENIED:
            title = "提权被拒绝"
            msg = ("系统拒绝了提权请求（错误码 5：拒绝访问）。\n\n"
                   "通常是在 UAC 对话框里点了「否」，或当前账户不允许提权。"
                   "可以右键 exe → 「以管理员身份运行」，或在管理员账户下运行。")
        else:
            title = "提权失败"
            msg = (f"没能拉起管理员实例（ShellExecute 返回 {code}）。\n\n"
                   "请右键 exe → 「以管理员身份运行」，再试一次。")
        self.log(f"未能以管理员身份重启（返回 {code}），程序继续运行。", "warn")
        self.info(title, msg)

    def ask_admin_startup(self):
        """启动时让用户选择是否以管理员身份运行（普通权限时才问）。

        走 `root.after` 延迟触发：等背景合成完、界面落定后再弹，
        弹窗底板才能取到正确的主窗口底图。
        """
        if self.shot or getattr(self, "busy", False):
            return
        if is_admin():
            return
        go = self.ask(
            "选择运行模式",
            "以管理员身份运行，才能清空待机内存，\n"
            "也能结束更多占用内存的后台进程。\n"
            "普通模式下「一键整理」仍可用。\n\n"
            "是否以管理员身份重启本程序？",
            "以管理员重启", "普通模式继续")
        if not go:
            self.log("以普通模式运行。「清理待机内存」需要管理员权限。", "dim")
            return
        ok, code = relaunch_as_admin()
        if ok:
            self.log("已拉起管理员实例，当前窗口即将关闭。")
            self.close()
            return
        self._admin_relaunch_failed(code)

    def do_purge_standby(self):
        if self.busy:
            return
        before = get_memory_status()
        good, st, reason = purge_standby_list()
        time.sleep(0.5)
        after = get_memory_status()
        if good:
            self.log(f"待机内存已清空：可用 {before['avail_mb']/1024:.2f} GB → "
                     f"{after['avail_mb']/1024:.2f} GB", "ok")
        elif reason == "no_admin":
            self.log(f"待机内存清理失败：{purge_reason_text(reason, st)}", "warn")
            if self.ask("需要管理员权限",
                        "清理待机内存需要管理员权限。\n\n是否以管理员身份重新启动本程序？",
                        "以管理员重启", "取消"):
                ok, code = relaunch_as_admin()
                if ok:
                    # 新实例已经拉起，关掉自己让出位置
                    self.close()
                    return
                # 失败绝对不能关窗口 —— 否则用户看到的就是程序凭空消失
                self._admin_relaunch_failed(code)
            else:
                self.log("已取消提权。", "dim")
        else:
            self.log(f"待机内存清理失败：{purge_reason_text(reason, st)}", "warn")
            if reason == "no_privilege":
                self.info(
                    "无法启用特权",
                    "系统拒绝了 SeProfileSingleProcessPrivilege 的启用请求。\n\n"
                    "通常是组策略或安全软件收紧了这个特权（「配置文件单一进程」）。"
                    "可以在「本地安全策略 → 用户权限分配」里把它授予当前用户后再试。")
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
            # 名单优先，其次实测终止句柄 —— 两道检查对应表格里的
            # 「受保护」和「受系统保护」两档灰字
            if is_protected(name):
                blocked.append(name)
            elif kill_tier(name, int(pid)) != 0:
                blocked.append(name)
            else:
                targets.append((name, pid))

        if blocked:
            self.info("已跳过受保护进程",
                      "以下进程受系统保护，或当前账户没有结束它们的权限，"
                      "已跳过：\n\n" + "\n".join(blocked))
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

    # ---------------- 界面设置：玻璃透明度 ----------------
    SETTINGS_W = 460
    SETTINGS_H = 260

    def rebuild_background(self):
        """重建主窗口背景。设置弹窗打开时先把它藏起来。

        抓屏抓的是屏幕像素，弹窗浮在主窗口上方就会被一起拍进底图
        （PITFALLS 第 8 条）。WDA_EXCLUDEFROMCAPTURE 在 Win10 2004 以下
        不生效，所以这里在应用层也兜一道 —— 任何路径触发的重建
        （拖动窗口、从最小化恢复、改透明度）都会走这个入口。
        """
        win = self._settings_win
        if win is None or self._settings_hidden:
            return super().rebuild_background()
        self._settings_hidden = True
        hidden = False
        try:
            win.withdraw()
            hidden = True
            self.root.update_idletasks()
        except Exception:
            hidden = False
        try:
            return super().rebuild_background()
        finally:
            if hidden:
                self._restore_settings_window()
            self._settings_hidden = False

    def _restore_settings_window(self):
        """把设置弹窗重新显示出来，并恢复顶层与圆角。

        不再给弹窗挂抓屏排除（WDA_EXCLUDEFROMCAPTURE）：那个标志会让弹窗
        在 Win+Shift+S 截屏期间同样从屏幕上消失。主窗口重建背景前本来就
        会先 withdraw 这个弹窗（见 rebuild_background），排除没有存在必要。
        """
        win = self._settings_win
        if win is None:
            return
        try:
            win.deiconify()
            win.attributes("-topmost", True)
            win.lift()
            apply_round_region(win, 22)
        except Exception:
            pass

    def show_settings(self):
        """打开「界面设置」玻璃弹窗（拖动滑块调界面透明度）。

        设计约束（踩过的坑都在这三条里）：

        1. 弹窗底板复用主窗口已合成的 `_bg_img`，不重新抓屏 —— 重新抓屏会
           把弹窗自己拍进去（PITFALLS 第 8 条）。
        2. 弹窗里每个子控件都要 `set_background(plate.crop(...))`，否则圆角外
           会露出子 Canvas 的深色底，在浅底板上就是一块黑方（第 7 条）。
        3. 拖动过程中**不重建背景**。重建一次要抓屏 + 高斯模糊，约 45ms，
           每帧做滑块会跟手变糊；只在松手后 120ms 防抖重建一次。
        """
        win = self._settings_win
        if win is not None:
            try:
                if win.winfo_exists():
                    win.lift()
                    return
            except Exception:
                pass
            self._settings_win = None

        dw, dh = self.SETTINGS_W, self.SETTINGS_H
        pad = 26

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.configure(bg=C_BG)
        win.transient(self.root)

        cv = tk.Canvas(win, width=dw, height=dh, bd=0,
                       highlightthickness=0, bg=C_BG)
        cv.place(x=0, y=0, width=dw, height=dh)

        self._settings_win = win
        self._settings_cv = cv
        self._settings_size = (dw, dh)
        self._settings_plate = None
        self._settings_plate_item = None
        self._settings_plate_img = None
        self._settings_pct_item = None
        self._settings_hair = None

        # --- 子控件一次性建好（背景切片是之后统一分发的，晚建就拿不到）---
        cur = int(self.settings["glass_opacity"])
        slider_x, slider_y = pad + 2, 120
        self._settings_slider_xy = (slider_x, slider_y)
        self._settings_slider = GlassSlider(
            self, slider_x, slider_y, dw - pad * 2 - 4,
            value=cur / 100.0,
            on_change=self._on_opacity_commit,
            on_drag=self._on_opacity_drag,
            parent=cv, h=8, knob=18)

        btn_w, btn_h = 96, 34
        by = dh - btn_h - 20
        self._settings_btn_done = GlassButton(
            self, "完成", self._close_settings, dw - pad - btn_w, by,
            btn_w, btn_h, "primary", parent=cv)
        self._settings_btn_reset = GlassButton(
            self, "恢复默认", self._reset_opacity, dw - pad - btn_w * 2 - 10, by,
            btn_w, btn_h, "ghost", parent=cv)

        px = self.root.winfo_rootx() + max(0, (self.win_w - dw) // 2)
        py = max(16, self.root.winfo_rooty() + max(20, (self.win_h - dh) // 2))
        win.geometry(f"{dw}x{dh}+{px}+{py}")

        self._paint_settings_plate()

        win.deiconify()
        win.attributes("-topmost", True)
        win.lift()
        # 不挂抓屏排除：标志会让弹窗在 Win+Shift+S 期间从屏幕上消失。
        # 主窗口重建背景前会先 withdraw 本弹窗，不需要排除来防串影
        try:
            win.update_idletasks()
            apply_round_region(win, 22)
        except Exception:
            pass

    def _paint_settings_plate(self):
        """重建设置弹窗的底板、静态文字与控件背景切片。

        只在两处调用：打开弹窗时、松手后的防抖重建里。**不要**放进拖动回调 ——
        一次几十毫秒，滑块会明显卡手。
        """
        cv = self._settings_cv
        if cv is None:
            return
        dw, dh = self._settings_size
        pad = 26

        plate = self.glass_plate(dw, dh, 0.38, radius=22)
        self._settings_plate = plate

        if plate is not None:
            tkimg = ImageTk.PhotoImage(plate.convert("RGB"))
            if self._settings_plate_item is None:
                self._settings_plate_item = cv.create_image(
                    0, 0, image=tkimg, anchor="nw")
            else:
                cv.itemconfigure(self._settings_plate_item, image=tkimg)
                cv.tag_lower(self._settings_plate_item)
            # 必须挂住引用，PhotoImage 被 GC 之后整块底板会消失
            self._settings_plate_img = tkimg

        # --- 静态元素全部挂在 "s_static" tag 上，重建时先删再画 ---
        cv.delete("s_static")
        cv.create_text(pad, 28, text="界面设置", anchor="w", fill="#ffffff",
                       font=(FONT_UI, 12, "bold"), tags=("s_static",))

        if HAVE_PIL:
            self._settings_hair = ImageTk.PhotoImage(
                Image.new("RGBA", (max(2, dw - pad * 2), 1), (255, 255, 255, 34)))
            cv.create_image(pad, 48, image=self._settings_hair, anchor="nw",
                            tags=("s_static",))

        # 中文换行只在空格处断（PITFALLS 第 11 条），文案里不留多余空格。
        # 这句话必须能排在一行内 —— 换成三行会把下面的滑块顶到按钮上去
        cv.create_text(pad, 62, anchor="nw", fill="#c8cfdc", font=(FONT_UI, 9),
                       width=dw - pad * 2, tags=("s_static",),
                       text="数值越大越通透，越小越磨砂，松手后立即生效并记住设置。")

        cv.create_text(pad, 104, text="界面透明度", anchor="w", fill="#98a1b4",
                       font=(FONT_UI, 9), tags=("s_static",))
        cv.create_text(pad, 158, text="0% 磨砂", anchor="w", fill="#767f90",
                       font=(FONT_UI, 8), tags=("s_static",))
        cv.create_text(dw - pad, 158, text="100% 通透", anchor="e",
                       fill="#767f90", font=(FONT_UI, 8), tags=("s_static",))

        if self._settings_pct_item is None:
            self._settings_pct_item = cv.create_text(
                dw - pad, 104, text="", anchor="e", fill="#ffffff",
                font=(FONT_MONO, 12, "bold"), tags=("s_pct",))

        # 让滑块头的物理位置与设置值保持一致。拖动中不碰它，否则会和用户的
        # 手打架（拖动回调也会走到这里）。
        sl_now = self._settings_slider
        if sl_now is not None and not getattr(sl_now, "_drag", False):
            want = max(0.0, min(1.0, self.settings["glass_opacity"] / 100.0))
            if abs(sl_now.get_value() - want) > 1e-6:
                sl_now.set_value(want)
        self._update_settings_pct(self.settings["glass_opacity"])

        # --- 控件背景切片 ---
        if plate is not None:
            for b in (self._settings_btn_done, self._settings_btn_reset):
                if b is None:
                    continue
                self._safe_set_bg(b, plate, b.x, b.y, b.w, b.h)
            sl = self._settings_slider
            if sl is not None:
                sx, sy = self._settings_slider_xy
                self._safe_set_bg(sl, plate, sx, sy, sl.cw, sl.ch)

    @staticmethod
    def _safe_set_bg(widget, plate, x, y, w, h):
        """给控件铺底板切片，坐标先夹到图内，避免 crop 越界留下深色边。"""
        try:
            iw, ih = plate.size
            x = max(0, min(iw - 1, int(x)))
            y = max(0, min(ih - 1, int(y)))
            w = max(1, min(iw - x, int(w)))
            h = max(1, min(ih - y, int(h)))
            widget.set_background(
                plate.crop((x, y, x + w, y + h)).convert("RGB"))
        except Exception:
            pass

    def _update_settings_pct(self, pct):
        cv = self._settings_cv
        item = self._settings_pct_item
        if cv is None or item is None:
            return
        try:
            cv.itemconfigure(item, text=f"{int(round(float(pct)))}%")
        except Exception:
            pass

    def _apply_opacity(self, percent, persist=False, rebuild=False):
        """把用户语义的透明度落到引擎上，返回实际生效的整数百分比。

        Args:
            percent: 0~100 的透明度（100 最通透）。
            persist: 是否写入 settings.json。
            rebuild: 是否立即重建背景。拖动中一律 False —— 走防抖。
        """
        pct = self.engine.set_opacity(percent)
        self.settings["glass_opacity"] = pct
        if persist:
            save_settings(self.settings)
        self._update_settings_pct(pct)
        if rebuild:
            try:
                self.rebuild_background()
                self._refresh_buttons_style()
            except Exception:
                pass
        return pct

    def _on_opacity_drag(self, value):
        """拖动中：只改遮罩系数与百分比数字，不重建背景。"""
        self._apply_opacity(float(value) * 100.0, persist=False)

    def _on_opacity_commit(self, value):
        """松手：落盘 + 120ms 防抖重建背景。"""
        self._apply_opacity(float(value) * 100.0, persist=True)
        self._schedule_settings_rebuild(120)

    def _reset_opacity(self):
        default = int(DEFAULT_SETTINGS["glass_opacity"])
        sl = self._settings_slider
        if sl is not None:
            try:
                sl.set_value(default / 100.0)
            except Exception:
                pass
        self._apply_opacity(default, persist=True)
        self.log(f"界面透明度已恢复默认（{default}%）。", "dim")
        self._schedule_settings_rebuild(60)

    def _schedule_settings_rebuild(self, delay=120):
        """松手后延迟重建背景，期间继续拖动会重新计时（防抖）。"""
        if self._bg_debounce is not None:
            try:
                self.root.after_cancel(self._bg_debounce)
            except Exception:
                pass
            self._bg_debounce = None
        try:
            self._bg_debounce = self.root.after(delay, self._rebuild_settings_bg)
        except Exception:
            self._bg_debounce = None

    def _rebuild_settings_bg(self):
        """防抖到点后：重建主窗口背景，并让弹窗底板跟上新的色调。

        藏/显弹窗的动作在 `rebuild_background()` 里统一处理，这里不用重复。
        """
        self._bg_debounce = None
        try:
            self.rebuild_background()
            self._refresh_buttons_style()
            self._paint_settings_plate()
        except Exception:
            pass

    def _refresh_buttons_style(self):
        """玻璃强度变了之后重烘按钮底面贴图。

        ghost 按钮的半透明白底是烘进 PhotoImage 的，引擎系数改了不会自动
        更新，不重烘就会出现"背景变透了、按钮还是原来那层灰"的割裂感。
        """
        targets = list(self._all_buttons())
        for extra in (self._settings_btn_done, self._settings_btn_reset):
            if extra is not None and extra not in targets:
                targets.append(extra)
        for b in targets:
            try:
                b.rebuild_images()
            except Exception:
                pass

    def _close_settings(self):
        if self._bg_debounce is not None:
            try:
                self.root.after_cancel(self._bg_debounce)
            except Exception:
                pass
            self._bg_debounce = None
        save_settings(self.settings)

        win = self._settings_win
        self._settings_win = None
        self._settings_cv = None
        self._settings_slider = None
        self._settings_btn_done = None
        self._settings_btn_reset = None
        self._settings_pct_item = None
        self._settings_plate_item = None
        self._settings_plate_img = None
        self._settings_hair = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

        # 先销毁弹窗再重建背景：弹窗还在就会被抓进底图。
        # 这样关闭后主窗口的画面与滑块的最终值一定一致。
        try:
            self.rebuild_background()
            self._refresh_buttons_style()
        except Exception:
            pass

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

    # 特权自检：这一步是"清理待机内存"成功的前提
    priv = enable_privilege("SeProfileSingleProcessPrivilege")
    p(f"SeProfileSingleProcessPrivilege 启用: {'成功' if priv else '失败'}")

    good, st, reason = purge_standby_list()
    p(f"清理待机内存: {'成功' if good else '失败'} "
      f"(状态码 0x{st & 0xFFFFFFFF:08X} / 原因 {reason} / "
      f"{purge_reason_text(reason, st)})")

    # 设置持久化自检
    p(f"设置文件: {SETTINGS_PATH}")
    p(f"设置内容: {load_settings()}")
    p(f"透明度映射: 100→{GlassEngine.opacity_to_veil(100):.2f} / "
      f"74→{GlassEngine.opacity_to_veil(74):.2f} / "
      f"0→{GlassEngine.opacity_to_veil(0):.2f}")

    # GUI 初始化检查
    try:
        r = tk.Tk()
        r.withdraw()
        app = MemTidyApp(r, shot=True)
        r.update_idletasks()
        r.update()
        p(f"GUI 初始化: 成功  列表 {len(app.table.get_children())} 行")
        p(f"窗口尺寸: {app.win_w}x{app.win_h}  背景图: {app.bg_photo is not None}")
        # 自检的窗口是 shot 模式：按设计不碰任务栏样式，光读会读到 Tk 默认的
        # 0x00000080（TOOLWINDOW），看不出库这条路到底通没通。所以这里显式调
        # 一次库函数再回读 —— withdraw 的窗口一样能读写 exstyle，值是真的。
        installed = make_taskbar_window(r)
        ts = app.taskbar_state()
        if not ts["exstyle"]:
            p("任务栏样式: 读取失败，取不到窗口句柄")
        else:
            p(f"任务栏样式: exstyle=0x{ts['exstyle']:08X} "
              f"APPWINDOW={'有' if ts['appwindow'] else '无'} "
              f"TOOLWINDOW={'有' if ts['toolwindow'] else '无'}"
              f"{'' if installed else '（设置失败）'}")
        bg = app._bg_img
        p(f"背景图尺寸: {bg.size if bg is not None else None}")
        p(f"控件: 按钮 {len(app._all_buttons())} 个 / "
          f"标题栏图标 4 个 / 表格行 {len(app.table.rows)} / "
          f"日志行 {len(app.logview.lines)}")
        p(f"玻璃系数: VEIL={app.engine.VEIL:.4f} glass_k={app.engine.glass_k:.3f}")
        p(f"弹窗方法: glass_plate={'有' if callable(getattr(app, 'glass_plate', None)) else '缺失'} "
          f"show_about={'有' if callable(getattr(app, 'show_about', None)) else '缺失'} "
          f"info={'有' if callable(getattr(app, 'info', None)) else '缺失'} "
          f"ask={'有' if callable(getattr(app, 'ask', None)) else '缺失'} "
          f"show_settings={'有' if callable(getattr(app, 'show_settings', None)) else '缺失'} "
          f"ask_admin_startup={'有' if callable(getattr(app, 'ask_admin_startup', None)) else '缺失'}")

        # ---- v2.3 新功能自检 ----
        # 抓屏排除：默认必须关着，否则 Win+Shift+S 时窗口会从屏幕上消失
        p(f"抓屏排除(CAPTURE_SAFE): {'开（截屏会看不见窗口）' if app.CAPTURE_SAFE else '关（截屏可见）'}")
        # 滚动条：行数超过一屏时必须给出可拖动几何
        g = app.table._sb_geometry()
        p(f"滚动条: {'可见 bar_y=%d bar_h=%d' % (g['bar_y'], g['bar_h']) if g else '隐藏'} "
          f"拖动状态={app.table._sb_drag}")
        # 搜索：模拟输入并数匹配行，测完恢复
        try:
            app.search_var.set("svchost")
            app._apply_search()
            hit = len(app.table.rows)
            p(f"搜索过滤: 'svchost' 匹配 {hit} 行 "
              f"(计数文字: {app.txt_count})")
        except Exception as e:
            p(f"搜索过滤: 异常 {e!r}")
        finally:
            app._search_text = ""
            app._rebuild_table_rows()
        # 排序：三档计数（可结束 / 受系统保护 / 名单内）
        tiers = {0: 0, 1: 0, 2: 0}
        for _n, _p2, _w, t in app.snapshot:
            tiers[t] = tiers.get(t, 0) + 1
        p(f"可结束分档: 可结束 {tiers.get(0, 0)} / 受系统保护 "
          f"{tiers.get(1, 0)} / 名单内 {tiers.get(2, 0)}")
        p(f"排序模式: {app.sort_mode}（按钮 {getattr(app.btn_sort, 'text', '?')}"
          f"{'' if hasattr(app, 'btn_sort') else '缺失'}）")
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


def screenshot_mode(out_path, opacity=None):
    """把界面渲染出来并截图，用于开发者核对布局（普通用户用不到）。

    Args:
        out_path: 截图保存路径。
        opacity: 可选，指定玻璃透明度（0~100）。不传则用设置文件里的值。
            调界面时用不同值各出一张，比对层次是否正常。
    """
    root = tk.Tk()
    app = MemTidyApp(root, shot=True)
    if opacity is not None:
        app._apply_opacity(opacity, persist=False)
    root.attributes("-topmost", True)
    app.rebuild_background()
    app._refresh_buttons_style()
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
        opacity = None
        if "--opacity" in sys.argv:
            j = sys.argv.index("--opacity")
            if len(sys.argv) > j + 1:
                try:
                    opacity = int(float(sys.argv[j + 1]))
                except ValueError:
                    opacity = None
        screenshot_mode(out, opacity)
        return

    root = tk.Tk()
    app = MemTidyApp(root)
    # 启动后让用户选一次运行模式：管理员重启 or 普通模式继续。
    # 延迟到背景合成完再弹，玻璃弹窗的底板才有正确的图可取。
    root.after(400, app.ask_admin_startup)
    app.run()


if __name__ == "__main__":
    main()
