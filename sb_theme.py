# -*- coding: utf-8 -*-
"""主题、字体、DPI、屏幕几何与画布绘图小工具。

这个模块只依赖标准库 + tkinter, 不引入第三方包, 方便 PyInstaller 打包成单文件。
"""
from __future__ import annotations

import ctypes
import math
from ctypes import wintypes

import tkinter as tk
import tkinter.font as tkfont

# ---------------- 常量 ----------------
KEY_COLOR = "#010203"      # 色键: 该颜色在视觉窗口上完全透明 (不要用于任何真实配色)
UI_FAMILY = "Microsoft YaHei UI"
NUM_FAMILY = "Consolas"

MIN_FONT_SIZE = 10
MAX_FONT_SIZE = 16


# ---------------- 调色板 ----------------
DARK = {
    "name": "dark",
    "bar_bg": "#15171D",
    "bar_border": "#2A303C",
    "bar_shadow": "#0D0F14",
    "card_bg": "#171A21",
    "card_border": "#2B303C",
    "panel": "#101218",
    "panel_alt": "#181C24",
    "panel_soft": "#14171E",
    "border": "#262C38",
    "border_soft": "#20252F",
    "text": "#E9ECF3",
    "text_dim": "#A8AFBD",
    "muted": "#7C8496",
    "faint": "#5A6271",
    "pos": "#FF5A5F",
    "neg": "#2BC48A",
    "flat": "#E9ECF3",
    "pos_soft": "#2A171B",
    "neg_soft": "#12241E",
    "accent": "#3D7BFF",
    "accent_hover": "#548BFF",
    "accent_text": "#FFFFFF",
    "field": "#191D25",
    "field_border": "#2E3543",
    "track": "#2A3040",
    "hover": "#232833",
    "press": "#2C3342",
    "selected": "#2A3040",
    "menu_bg": "#171A21",
    "menu_hover": "#242936",
    "menu_border": "#2E3442",
    "danger": "#FF5A5F",
    "warn": "#F5A524",
    "scroll": "#2B3140",
}

LIGHT = {
    "name": "light",
    "bar_bg": "#FFFFFF",
    "bar_border": "#E3E7EF",
    "bar_shadow": "#C9D0DC",
    "card_bg": "#FCFCFE",
    "card_border": "#E1E5EE",
    "panel": "#F5F7FB",
    "panel_alt": "#EEF1F7",
    "panel_soft": "#F0F3F8",
    "border": "#DFE4ED",
    "border_soft": "#E8ECF3",
    "text": "#14181F",
    "text_dim": "#4C5566",
    "muted": "#6C7587",
    "faint": "#8C94A4",
    "pos": "#D93A45",
    "neg": "#0E9F6E",
    "flat": "#14181F",
    "pos_soft": "#FBE9EA",
    "neg_soft": "#E4F6EE",
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "accent_text": "#FFFFFF",
    "field": "#FFFFFF",
    "field_border": "#D6DDE9",
    "track": "#DCE2EC",
    "hover": "#EDF0F6",
    "press": "#E3E8F2",
    "selected": "#DCE7FF",
    "menu_bg": "#FFFFFF",
    "menu_hover": "#EEF1F7",
    "menu_border": "#DCE1EA",
    "danger": "#D93A45",
    "warn": "#C77700",
    "scroll": "#CBD3E0",
}


def palette(theme: str = "dark", cn_colors: bool = True) -> dict:
    """返回一套颜色; cn_colors=False 时切换为国际配色(绿涨红跌)。"""
    pal = dict(LIGHT if theme == "light" else DARK)
    if not cn_colors:
        pal["pos"], pal["neg"] = pal["neg"], pal["pos"]
        pal["pos_soft"], pal["neg_soft"] = pal["neg_soft"], pal["pos_soft"]
    return pal


# ---------------- 颜色工具 ----------------
def _to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _to_hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def mix(c1: str, c2: str, t: float) -> str:
    a, b = _to_rgb(c1), _to_rgb(c2)
    return _to_hex([x + (y - x) * t for x, y in zip(a, b)])


def lighten(color: str, t: float = 0.25) -> str:
    return mix(color, "#FFFFFF", t)


def darken(color: str, t: float = 0.2) -> str:
    return mix(color, "#000000", t)


def readable_on(bg: str) -> str:
    r, g, b = _to_rgb(bg)
    return "#101218" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#FFFFFF"


# ---------------- 画布圆角 ----------------
def round_rect_points(x1: float, y1: float, x2: float, y2: float, r: float,
                      steps: int = 10) -> list[float]:
    """生成圆角矩形顶点(顺时针), 用于 Canvas.create_polygon。"""
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    if r <= 0.5:
        return [x1, y1, x2, y1, x2, y2, x1, y2]
    pts: list[float] = []
    corners = (
        (x2 - r, y1 + r, -90.0, 0.0),
        (x2 - r, y2 - r, 0.0, 90.0),
        (x1 + r, y2 - r, 90.0, 180.0),
        (x1 + r, y1 + r, 180.0, 270.0),
    )
    for cx, cy, a0, a1 in corners:
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return pts


def draw_round_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                    r: float, fill: str, outline: str = "", width: int = 1,
                    tags=None) -> int:
    pts = round_rect_points(x1, y1, x2, y2, r)
    kwargs = {"fill": fill, "outline": outline or fill}
    if outline:
        kwargs["width"] = width
    if tags:
        kwargs["tags"] = tags
    return canvas.create_polygon(*pts, **kwargs)


def hline(canvas: tk.Canvas, x1: float, y: float, x2: float, color: str, width: int = 1):
    return canvas.create_line(x1, y, x2, y, fill=color, width=width)


# ---------------- 字体 ----------------
class Fonts:
    """按字号缓存的字体工厂, 全部使用 Tk 字体对象, 便于精确测量。"""

    def __init__(self, root: tk.Tk, base: int = 12):
        self.root = root
        self.base = int(base)
        self._cache: dict[tuple, tkfont.Font] = {}

    def get(self, size: int, weight: str = "normal", family: str = UI_FAMILY) -> tkfont.Font:
        size = max(7, int(size))
        key = (family, size, weight)
        f = self._cache.get(key)
        if f is None:
            f = tkfont.Font(root=self.root, family=family, size=size, weight=weight)
            self._cache[key] = f
        return f

    # 常用语义字体
    def num(self, delta: int = 0, bold: bool = True) -> tkfont.Font:
        return self.get(self.base + delta, "bold" if bold else "normal", NUM_FAMILY)

    def ui(self, delta: int = 0, bold: bool = False) -> tkfont.Font:
        return self.get(self.base + delta, "bold" if bold else "normal", UI_FAMILY)

    def name(self) -> tkfont.Font:
        return self.ui(-1)

    def label(self) -> tkfont.Font:
        return self.ui(-2)

    def tiny(self) -> tkfont.Font:
        return self.ui(-3)

    def card_title(self) -> tkfont.Font:
        return self.ui(2, True)

    def card_price(self) -> tkfont.Font:
        return self.num(9)

    def dialog_title(self) -> tkfont.Font:
        return self.ui(4, True)


# ---------------- DPI / 屏幕 ----------------
def enable_dpi_awareness() -> None:
    """让文字在高分屏上保持清晰; 失败时静默回退。必须在创建 Tk 之前调用。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


class Screen:
    """虚拟屏幕/工作区信息, 支持多显示器与负坐标。"""

    def __init__(self):
        self.user32 = ctypes.windll.user32
        u = self.user32
        self.vx = u.GetSystemMetrics(76)
        self.vy = u.GetSystemMetrics(77)
        self.vw = u.GetSystemMetrics(78) or u.GetSystemMetrics(0)
        self.vh = u.GetSystemMetrics(79) or u.GetSystemMetrics(1)
        self.pw = u.GetSystemMetrics(0)
        self.ph = u.GetSystemMetrics(1)
        self.dpi = 96
        try:
            self.dpi = int(u.GetDpiForSystem())
        except Exception:
            pass

    def clamp(self, x: int, y: int, w: int, h: int, pad: int = 0) -> tuple[int, int]:
        """把窗口限制在虚拟屏幕内(允许覆盖任务栏)。"""
        left = self.vx + pad
        top = self.vy + pad
        right = self.vx + self.vw - w - pad
        bottom = self.vy + self.vh - h - pad
        return max(left, min(int(x), right)), max(top, min(int(y), bottom))

    def work_area(self, x: int, y: int) -> tuple[int, int, int, int]:
        """返回包含指定点的显示器工作区(不含任务栏); 失败时用虚拟屏幕。"""
        try:
            u = self.user32
            u.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            u.MonitorFromPoint.restype = ctypes.c_void_p
            u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
            hmon = u.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)
            if hmon:
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    r = mi.rcWork
                    return r.left, r.top, r.right - r.left, r.bottom - r.top
        except Exception:
            pass
        return self.vx, self.vy, self.vw, self.vh

    def find_free_span(self, x: int, y: int, w: int, h: int, need: int,
                       ignore: tuple[int, int] | None = None) -> int | None:
        """在屏幕条带上找第一个"无内容"的横向空位, 返回其起始 x; 找不到返回 None。

        逐列看整条带的亮度跨度: 图标/文字任意高度都会被识别为内容,
        只有任务栏底色的列才算空位。ignore=(x0, x1) 区段视为已有自身、跳过判定。
        """
        spans = screen_col_ranges(int(x), int(y), int(w), int(h))
        if len(spans) < need + 2:
            return None
        free = [s <= 24 for s in spans]
        if ignore is not None:
            i0 = max(0, ignore[0] - int(x))
            i1 = min(len(free), ignore[1] - int(x))
            for i in range(i0, i1):
                free[i] = True
        run = 0
        for i in range(len(free)):
            run = 0 if not free[i] else run + 1
            if run >= need:
                return int(x) + (i - need + 1)
        return None

    def taskbar_band(self, x: int, y: int) -> tuple[int, int, int, int]:
        """返回包含指定点的显示器上任务栏条带 (left, top, w, h)。

        用显示器矩形与工作区之差求出任务栏位置, 支持底/顶/左/右四个方向。
        """
        try:
            u = self.user32
            u.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            u.MonitorFromPoint.restype = ctypes.c_void_p
            u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
            hmon = u.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)
            if hmon:
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    m, w = mi.rcMonitor, mi.rcWork
                    if w.bottom < m.bottom:        # 底部任务栏
                        return m.left, w.bottom, m.right - m.left, m.bottom - w.bottom
                    if w.top > m.top:              # 顶部任务栏
                        return m.left, m.top, m.right - m.left, w.top - m.top
                    if w.left > m.left:            # 左侧任务栏
                        return m.left, m.top, w.left - m.left, m.bottom - m.top
                    if w.right < m.right:          # 右侧任务栏
                        return w.right, m.top, m.right - w.right, m.bottom - m.top
        except Exception:
            pass
        return self.vx, self.vy + self.vh - 48, self.vw, 48


def apply_dark_titlebar(win: tk.Misc, dark: bool = True) -> None:
    """Win10/11: 让对话框标题栏跟随深色主题; 失败时忽略。"""
    try:
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            return
        dwm = ctypes.windll.dwmapi
        value = ctypes.c_int(1 if dark else 0)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win11); 19 = 旧版 Win10
        if dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20,
                                     ctypes.byref(value), ctypes.sizeof(value)) != 0:
            dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 19,
                                      ctypes.byref(value), ctypes.sizeof(value))
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33,
                                  ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def center_on_bar(win: tk.Misc, bar_x: int, bar_y: int, bar_w: int, bar_h: int,
                  width: int, height: int, screen: Screen) -> None:
    """把对话框放在悬浮条附近, 并限制在屏幕内。"""
    x = bar_x + bar_w // 2 - width // 2
    y = bar_y + bar_h + 12
    if y + height > screen.vy + screen.vh - 12:
        y = max(screen.vy + 12, bar_y - height - 12)
    x, y = screen.clamp(x, y, width, height, pad=12)
    win.geometry(f"{width}x{height}+{x}+{y}")


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 1)]


def screen_strip(x: int, y: int, w: int, h: int = 2):
    """BitBlt 屏幕条带并返回逐列平均色列表 [(r,g,b), ...], 长度 = w。"""
    if w <= 0 or h <= 0:
        return []
    try:
        user32 = ctypes.windll.user32
        gdi = ctypes.windll.gdi32
        user32.GetDC.restype = ctypes.c_void_p
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
        gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi.SelectObject.restype = ctypes.c_void_p
        gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi.BitBlt.restype = ctypes.c_bool
        gdi.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_uint]
        gdi.GetDIBits.restype = ctypes.c_int
        gdi.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        gdi.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi.DeleteDC.argtypes = [ctypes.c_void_p]

        w, h = int(w), int(h)
        hdc = user32.GetDC(None)
        if not hdc:
            return []
        mdc = gdi.CreateCompatibleDC(hdc)
        bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
        if not mdc or not bmp:
            if bmp:
                gdi.DeleteObject(ctypes.c_void_p(bmp))
            if mdc:
                gdi.DeleteDC(ctypes.c_void_p(mdc))
            user32.ReleaseDC(None, ctypes.c_void_p(hdc))
            return []
        old = gdi.SelectObject(ctypes.c_void_p(mdc), ctypes.c_void_p(bmp))
        gdi.BitBlt(ctypes.c_void_p(mdc), 0, 0, w, h,
                   ctypes.c_void_p(hdc), int(x), int(y), 0x00CC0020)
        bi = _BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h
        bi.biPlanes = 1
        bi.biBitCount = 32
        bmi = _BITMAPINFO()
        bmi.bmiHeader = bi
        buf = (ctypes.c_ubyte * (w * h * 4))()
        ok = gdi.GetDIBits(ctypes.c_void_p(mdc), ctypes.c_void_p(bmp),
                           0, h, ctypes.byref(buf), ctypes.byref(bmi), 0)
        if old:
            gdi.SelectObject(ctypes.c_void_p(mdc), ctypes.c_void_p(old))
        gdi.DeleteObject(ctypes.c_void_p(bmp))
        gdi.DeleteDC(ctypes.c_void_p(mdc))
        user32.ReleaseDC(None, ctypes.c_void_p(hdc))
        if not ok:
            return []
        cols = []
        for xi in range(w):
            rs = gs = bs = 0
            for yi in range(h):
                j = (yi * w + xi) * 4
                bs += buf[j]
                gs += buf[j + 1]
                rs += buf[j + 2]
            cols.append((rs // h, gs // h, bs // h))
        return cols
    except Exception:
        return []


def screen_col_ranges(x: int, y: int, w: int, h: int):
    """BitBlt 屏幕矩形, 返回每列"偏离背景"的程度(越大越可能有内容)。

    判据: 该列像素亮度相对其中位数的最大偏差(取第 3 大以忽略条带边缘的干扰行)。
    图标/文字的偏差大, 纯底色(允许渐变)偏差小。用于在任务栏里找空位。
    """
    if w <= 0 or h <= 0:
        return []
    try:
        user32 = ctypes.windll.user32
        gdi = ctypes.windll.gdi32
        user32.GetDC.restype = ctypes.c_void_p
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
        gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi.SelectObject.restype = ctypes.c_void_p
        gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi.BitBlt.restype = ctypes.c_bool
        gdi.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_uint]
        gdi.GetDIBits.restype = ctypes.c_int
        gdi.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        gdi.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi.DeleteDC.argtypes = [ctypes.c_void_p]

        w, h = int(w), int(h)
        hdc = user32.GetDC(None)
        if not hdc:
            return []
        mdc = gdi.CreateCompatibleDC(hdc)
        bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
        if not mdc or not bmp:
            if bmp:
                gdi.DeleteObject(ctypes.c_void_p(bmp))
            if mdc:
                gdi.DeleteDC(ctypes.c_void_p(mdc))
            user32.ReleaseDC(None, ctypes.c_void_p(hdc))
            return []
        old = gdi.SelectObject(ctypes.c_void_p(mdc), ctypes.c_void_p(bmp))
        gdi.BitBlt(ctypes.c_void_p(mdc), 0, 0, w, h,
                   ctypes.c_void_p(hdc), int(x), int(y), 0x00CC0020)
        bi = _BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h
        bi.biPlanes = 1
        bi.biBitCount = 32
        bmi = _BITMAPINFO()
        bmi.bmiHeader = bi
        buf = (ctypes.c_ubyte * (w * h * 4))()
        ok = gdi.GetDIBits(ctypes.c_void_p(mdc), ctypes.c_void_p(bmp),
                           0, h, ctypes.byref(buf), ctypes.byref(bmi), 0)
        if old:
            gdi.SelectObject(ctypes.c_void_p(mdc), ctypes.c_void_p(old))
        gdi.DeleteObject(ctypes.c_void_p(bmp))
        gdi.DeleteDC(ctypes.c_void_p(mdc))
        user32.ReleaseDC(None, ctypes.c_void_p(hdc))
        if not ok:
            return []
        out = []
        for xi in range(w):
            lums = []
            for yi in range(h):
                j = (yi * w + xi) * 4
                lums.append((buf[j + 2] * 299 + buf[j + 1] * 587 + buf[j] * 114) // 1000)
            lums.sort()
            med = lums[len(lums) // 2]
            devs = [v - med if v > med else med - v for v in lums]
            devs.sort()
            # 取第 3 大偏差: 忽略条带上下边缘等个别干扰行
            out.append(devs[-3] if len(devs) >= 3 else devs[-1])
        return out
    except Exception:
        return []


def sample_screen_rect(x: int, y: int, w: int, h: int):
    """BitBlt 屏幕区域到内存位图后采样平均色, 返回 (r, g, b) 或 None。

    用于推算半透明胶囊背后的实际合成色, 让文字层的抗锯齿混色与背景一致。
    注意: 直接 GetPixel 读屏幕 DC 非常慢(每像素毫秒级), 必须先 BitBlt + GetDIBits。
    """
    if w <= 0 or h <= 0:
        return None
    try:
        user32 = ctypes.windll.user32
        gdi = ctypes.windll.gdi32
        user32.GetDC.restype = ctypes.c_void_p
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
        gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi.SelectObject.restype = ctypes.c_void_p
        gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi.BitBlt.restype = ctypes.c_bool
        gdi.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_uint]
        gdi.GetDIBits.restype = ctypes.c_int
        gdi.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        gdi.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi.DeleteDC.argtypes = [ctypes.c_void_p]

        w, h = int(w), int(h)
        hdc = user32.GetDC(None)
        if not hdc:
            return None
        mdc = gdi.CreateCompatibleDC(hdc)
        bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
        if not mdc or not bmp:
            if bmp:
                gdi.DeleteObject(ctypes.c_void_p(bmp))
            if mdc:
                gdi.DeleteDC(ctypes.c_void_p(mdc))
            user32.ReleaseDC(None, ctypes.c_void_p(hdc))
            return None
        old = gdi.SelectObject(ctypes.c_void_p(mdc), ctypes.c_void_p(bmp))
        gdi.BitBlt(ctypes.c_void_p(mdc), 0, 0, w, h,
                   ctypes.c_void_p(hdc), int(x), int(y), 0x00CC0020)  # SRCCOPY

        bi = _BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h          # 负值 = 自顶向下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bmi = _BITMAPINFO()
        bmi.bmiHeader = bi
        buf = (ctypes.c_ubyte * (w * h * 4))()
        ok = gdi.GetDIBits(ctypes.c_void_p(mdc), ctypes.c_void_p(bmp),
                           0, h, ctypes.byref(buf), ctypes.byref(bmi), 0)
        if old:
            gdi.SelectObject(ctypes.c_void_p(mdc), ctypes.c_void_p(old))
        gdi.DeleteObject(ctypes.c_void_p(bmp))
        gdi.DeleteDC(ctypes.c_void_p(mdc))
        user32.ReleaseDC(None, ctypes.c_void_p(hdc))
        if not ok:
            return None
        # 均匀采样至多 ~256 个像素求平均
        total = w * h
        step = max(1, total // 256)
        rs = gs = bs = n = 0
        for i in range(0, total, step):
            j = i * 4
            bs += buf[j]
            gs += buf[j + 1]
            rs += buf[j + 2]
            n += 1
        if n == 0:
            return None
        return (rs // n, gs // n, bs // n)
    except Exception:
        return None


def set_topmost(win: tk.Misc, topmost: bool = True) -> None:
    """真正设置/取消顶层框架的 WS_EX_TOPMOST。

    注意: Tk 的 wm attributes -topmost 只作用于内部子窗口,
    要控制"整个窗口是否压在别的程序上", 必须对顶层框架设置。
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        hwnd = user32.GetAncestor(win.winfo_id(), 2) or win.winfo_id()
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_bool
        # HWND_TOPMOST(-1) / HWND_NOTOPMOST(-2); NOSIZE|NOMOVE|NOACTIVATE
        user32.SetWindowPos(ctypes.c_void_p(hwnd),
                            ctypes.c_void_p(-1 if topmost else -2),
                            0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    except Exception:
        pass


def focus_window(win: tk.Misc) -> None:
    """把键盘焦点真正交给窗口(OS 层)。

    Tk 的 focus_force 在无边框窗口上只更新内部焦点,
    不保证拿到 OS 键盘焦点, 快捷键会因此失效。
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetFocus.argtypes = [ctypes.c_void_p]
        user32.SetFocus.restype = ctypes.c_void_p
        child = win.winfo_id()
        frame = user32.GetAncestor(child, 2) or child
        user32.SetForegroundWindow(ctypes.c_void_p(frame))
        user32.SetFocus(ctypes.c_void_p(child))
    except Exception:
        pass


def set_window_alpha(win: tk.Misc, byte: int) -> None:
    """直接设置分层窗口的 alpha 字节(1..255)。

    注意: alpha=0 的窗口不参与鼠标命中测试(点击会穿过去),
    需要接收鼠标时最小只能用 1(1/255 ≈ 0.4%, 肉眼不可见)。
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        hwnd = user32.GetAncestor(win.winfo_id(), 2) or win.winfo_id()  # GA_ROOT
        user32.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                      ctypes.c_ubyte, ctypes.c_uint]
        user32.SetLayeredWindowAttributes.restype = ctypes.c_bool
        # LWA_ALPHA = 0x02
        user32.SetLayeredWindowAttributes(ctypes.c_void_p(hwnd), 0,
                                          ctypes.c_ubyte(max(1, min(255, int(byte)))), 0x02)
    except Exception:
        pass


def insert_below(win: tk.Misc, above: tk.Misc) -> None:
    """把 win 放到 above 正下方(只调整内部相对层级, 不改变置顶状态)。"""
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p

        def frame(w):
            return user32.GetAncestor(w.winfo_id(), 2) or w.winfo_id()

        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_bool
        # NOSIZE | NOMOVE | NOACTIVATE
        user32.SetWindowPos(ctypes.c_void_p(frame(win)), ctypes.c_void_p(frame(above)),
                            0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    except Exception:
        pass


def force_topmost(win: tk.Misc) -> None:
    """把窗口提到 topmost 带最前(不抢焦点), 用于悬浮条/卡片/菜单。"""
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        hwnd = user32.GetAncestor(win.winfo_id(), 2) or win.winfo_id()  # GA_ROOT
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_bool
        # NOSIZE | NOMOVE | SHOWWINDOW | NOACTIVATE: 只调整层级, 不抢键盘焦点
        user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), 0, 0, 0, 0,
                            0x0001 | 0x0002 | 0x0040 | 0x0010)
    except Exception:
        pass
