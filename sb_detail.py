# -*- coding: utf-8 -*-
"""个股/指数详细窗口: 自选、分时/5日/K线、五档、逐笔、十字光标与 AI 研判。

只依赖标准库 + tkinter, 绘图全部走 Canvas, 不引入 matplotlib。
刷新节奏与悬停卡片一致: 行情 2s、分时 30s 缓存。
1.7.0: 左栏全高自选 + 顶部搜索、可拖动分隔线，右侧整页滚动到 AI 分模块研判。
1.8.0: 图表下方新增板块/概念标签与规则候选狙击点位；AI 只选编号。
1.8.1: 设置变更后同步详细窗口配色并重绘；修复港美股涨跌幅被日期判断清零。
"""
from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

import sb_ai as A
import sb_data as D
import sb_levels as LV
import sb_sector as SEC
import sb_ui
from sb_theme import draw_round_rect
from sb_widgets import CanvasButton, Segmented, make_entry, setup_ttk

SEARCH_TIP = "搜索代码/名称/拼音"

MINUTES = 240
PERIODS = [("minute", "分时"), ("5day", "5日"), ("day", "日K"),
           ("week", "周K"), ("month", "月K")]
INDICATORS = [("vol", "量"), ("macd", "MACD"), ("kdj", "KDJ")]


def minute_pos(t: str) -> int:
    """HH:MM -> 交易分钟序(9:30=0, 11:30=120, 13:00=120, 15:00=240)。"""
    try:
        hh, mm = int(t[:2]), int(t[3:5])
    except Exception:
        return 0
    mins = hh * 60 + mm
    if mins <= 9 * 60 + 25:
        return 0
    if mins <= 11 * 60 + 30:
        return max(0, min(120, mins - (9 * 60 + 30)))
    if mins < 13 * 60:
        return 120
    return min(240, 120 + (mins - 13 * 60))


def ema(values, n: int):
    if not values:
        return []
    k = 2.0 / (n + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def macd_values(closes):
    if len(closes) < 2:
        return [], [], []
    dif = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    dea = ema(dif, 9)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    return dif, dea, hist


def kdj_values(closes, highs=None, lows=None):
    n = len(closes)
    if n == 0:
        return [], [], []
    highs = highs or closes
    lows = lows or closes
    k = d = 50.0
    ks, ds, js = [], [], []
    for i in range(n):
        lo = min(lows[max(0, i - 8):i + 1])
        hi = max(highs[max(0, i - 8):i + 1])
        rsv = 50.0 if hi <= lo else (closes[i] - lo) / (hi - lo) * 100.0
        k = (2.0 * k + rsv) / 3.0
        d = (2.0 * d + k) / 3.0
        ks.append(k)
        ds.append(d)
        js.append(3.0 * k - 2.0 * d)
    return ks, ds, js


def _fmt_lots(v: float) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    if v >= 10000:
        return "%.1f万" % (v / 10000.0)
    return "%d" % int(v)


class StockDetailWindow(sb_ui.BaseDialog):
    """可缩放、可切换周期、带十字光标与五档/逐笔的详细窗口。"""

    key = "stock_detail"

    def __init__(self, app, code: str):
        self.app = app
        self.code = code
        self.q = app.quotes.get(code)
        self.mode = "minute"
        self.sub = "vol"
        self._series = None
        self._five_days = []
        self._bars = []
        self._ticks = []
        self._tick_scroll = 0
        self._tick_page = -1
        self._tick_last_page = -1
        self._tick_busy = False
        self._tick_date = ""
        self._tick_note = ""
        self._gen = 0
        self._async_q = queue.Queue()
        self._view = None                 # (start, end), 单位: 分时点/K线根
        self._cursor = None               # (x, y) 图表坐标
        self._cursor_lock = False
        self._price_line = None
        self._drag = None
        self._plot = {}
        self._side_rows = []
        self._side_visible = True
        # 1.7.0 布局 / 左栏 / 页面滚动 / AI
        self._left_width = int(app.cfg.get("detail_left_width") or 220)
        self._left_rows = []
        self._search_results = []
        self._search_job = None
        self._search_gen = 0
        self._search_focus = False
        self._split_drag = None
        self._page_syncing = False
        self._ai_result = None
        self._ai_results: dict[str, A.AIResult] = {}
        self._ai_busy = False
        self._ai_gen = 0
        self._ai_auto_job = None
        self._ai_last_trigger = 0.0
        self._ai_auto_ready = False
        self._ai_wrap_labels = []
        # 1.8.0 板块标签 / 规则候选点位 / AI 选点
        self._sector_tags: list = []
        self._level_plan = None
        self._level_ts = 0.0
        self._aux_gen = 0
        self._aux_busy = False
        self._daily_bars_cache: dict[str, tuple[float, list]] = {}
        self._research_visible = True
        self._after_ids = []
        self._topmost = bool(app.cfg.get("detail_topmost", True))
        self._top_h = int(468 * app.scale)
        name = (self.q.name if self.q else
                (getattr(app, "_detail_names", {}) or {}).get(code) or
                D.code_digits(code)) or code
        super().__init__(app, "%s %s" % (name, D.code_digits(code)),
                         1280, 800, min_size=(1120, 680))
        self._restore_geometry()
        self._build_ui()
        self.set_resize_handler(on_resize=self._on_resize_detail,
                                on_release=self._save_geometry)
        try:
            self.win.attributes("-topmost", self._topmost)
        except tk.TclError:
            pass
        self.show(code)
        self.win.after(100, self._poll_async)
        self._timer_quote()
        self._timer_minute()
        self._timer_ticks()
        self._timer_kline()
        try:
            # Tk 创建 Toplevel 后偶尔会被 Windows 以“最小化”状态落地，
            # 等布局稳定后再确保一次可见。
            self.win.after(80, self._ensure_visible)
        except tk.TclError:
            pass

    def on_app_settings(self):
        """设置变更后同步详细窗口配色；红涨绿跌切换必须立即重绘。"""
        new_pal = self.app.pal
        old_name = (self.pal or {}).get("name")
        if new_pal.get("name") != old_name:
            # 主题切换时大量 Tk 控件的背景色已在构建时固化，重建窗口最稳。
            code = self.code
            try:
                self.close()
            except Exception:
                pass
            try:
                self.app.open_stock_detail(code)
            except Exception:
                pass
            return
        self.pal = new_pal
        self.fonts = self.app.ui_fonts
        for widget, key in (
            (getattr(self, "win", None), "panel"),
            (getattr(self, "shell", None), "panel"),
            (getattr(self, "body", None), "panel"),
            (getattr(self, "split", None), "panel"),
            (getattr(self, "left", None), "panel"),
            (getattr(self, "right", None), "panel"),
            (getattr(self, "head", None), "panel"),
            (getattr(self, "page_host", None), "panel"),
            (getattr(self, "page_canvas", None), "panel"),
            (getattr(self, "page_inner", None), "panel"),
            (getattr(self, "top", None), "panel"),
            (getattr(self, "chart", None), "panel"),
            (getattr(self, "side", None), "panel_alt"),
        ):
            if widget is None:
                continue
            try:
                widget.configure(bg=self.pal[key])
            except Exception:
                pass
        for fn in (self._draw_header, self._draw_side, self._draw_left,
                   self._draw_all, self._draw_tags, self._draw_levels,
                   self._render_ai):
            try:
                fn()
            except Exception:
                pass

    def _ensure_visible(self):
        try:
            if self.win.state() in ("iconic", "withdrawn"):
                self.win.deiconify()
            self.win.lift()
        except tk.TclError:
            pass

    # ---------------- 窗口/构建 ----------------
    def _restore_geometry(self):
        g = self.app.cfg.get("detail_geometry")
        if not isinstance(g, dict):
            return
        try:
            w, h = int(g.get("w") or 0), int(g.get("h") or 0)
            x, y = int(g.get("x") or 0), int(g.get("y") or 0)
        except (TypeError, ValueError):
            return
        min_w, min_h = int(1120 * self.S), int(680 * self.S)
        if w < min_w or h < min_h:
            # 1.6.0 的旧几何可能小于 1.7.0 下限: 夹到新下限，避免控件被裁切
            w, h = max(w, min_w), max(h, min_h)
        x, y = self.app.screen.clamp(x, y, w, h, 8)
        try:
            self.win.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except tk.TclError:
            pass

    def _save_geometry(self):
        try:
            self.app.cfg["detail_geometry"] = {
                "w": int(self.win.winfo_width()),
                "h": int(self.win.winfo_height()),
                "x": int(self.win.winfo_x()),
                "y": int(self.win.winfo_y()),
            }
            self.app.cfg["detail_topmost"] = bool(self._topmost)
            self.app.cfg["detail_left_width"] = int(self._left_width)
            self.app.save_config()
        except Exception:
            pass

    def _build_ui(self):
        pal, S = self.pal, self.S
        F = self.fonts
        setup_ttk(self.win, pal, F, S)

        self.split = tk.Frame(self.body, bg=pal["panel"])
        self.split.pack(fill="both", expand=True)

        # ---------------- 左栏: 顶部搜索 + 全高自选 ----------------
        self.left = tk.Frame(self.split, bg=pal["panel"], width=self._left_width,
                             highlightthickness=1,
                             highlightbackground=pal["border"])
        self.left.pack(side="left", fill="y")
        self.left.pack_propagate(False)
        self.search_var = tk.StringVar()
        self.search = make_entry(self.left, pal, F, width=1,
                                 textvariable=self.search_var, font_delta=-2)
        self.search.pack(fill="x", padx=int(10 * S), pady=(int(10 * S), int(4 * S)),
                         ipady=int(5 * S))
        self.search.insert(0, SEARCH_TIP)
        self.search.configure(fg=pal["faint"])
        self.search.bind("<FocusIn>", self._on_search_focus)
        self.search.bind("<FocusOut>", self._on_search_blur)
        self.search.bind("<KeyRelease>", self._on_search_key)
        self.search.bind("<Escape>", self._on_search_escape)
        self.search.bind("<Return>", self._on_search_enter)

        self.left_note = tk.Label(self.left, text="", bg=pal["panel"],
                                  fg=pal["faint"], font=F.tiny(), anchor="w")
        self.left_note.pack(fill="x", padx=int(12 * S))
        self.left_body = tk.Frame(self.left, bg=pal["panel"])
        self.left_body.pack(fill="both", expand=True, padx=(int(1 * S), 0))
        self.left_list = tk.Canvas(self.left_body, bg=pal["panel"],
                                   highlightthickness=0, bd=0)
        self.left_list.pack(side="left", fill="both", expand=True)
        self.left_sb = ttk.Scrollbar(self.left_body, orient="vertical",
                                     command=self.left_list.yview)
        self.left_sb.pack(side="right", fill="y")
        self.left_list.configure(yscrollcommand=self.left_sb.set,
                                 yscrollincrement=int(16 * S))
        self.left_list.bind("<Configure>", lambda e: self._draw_left())
        self.left_list.bind("<Button-1>", self._on_left_click)
        self.left_list.bind("<MouseWheel>", self._on_left_wheel)
        self.left.bind("<MouseWheel>", self._on_left_wheel)
        self.left_note.bind("<MouseWheel>", self._on_left_wheel)

        # ---------------- 可拖动分隔线 ----------------
        self.splitter = tk.Frame(self.split, bg=pal["border"], width=int(5 * S),
                                 cursor="sb_h_double_arrow")
        self.splitter.pack(side="left", fill="y")
        self.splitter.bind("<Button-1>", self._on_split_press)
        self.splitter.bind("<B1-Motion>", self._on_split_drag)
        self.splitter.bind("<ButtonRelease-1>", self._on_split_release)
        self.splitter.bind("<Double-Button-1>", self._on_split_reset)

        # ---------------- 右侧: 固定头部 + 整页滚动 ----------------
        self.right = tk.Frame(self.split, bg=pal["panel"])
        self.right.pack(side="left", fill="both", expand=True)
        self.head = tk.Canvas(self.right, bg=pal["panel"], highlightthickness=0,
                              bd=0, height=int(102 * S))
        self.head.pack(fill="x", padx=int(12 * S), pady=(int(6 * S), 0))
        self.head.bind("<Configure>", lambda e: self._draw_header())
        self.head.bind("<MouseWheel>", self._on_page_wheel)
        tk.Frame(self.right, bg=pal["border"], height=1).pack(
            fill="x", padx=int(12 * S), pady=(int(4 * S), 0))

        self.page_host = tk.Frame(self.right, bg=pal["panel"])
        self.page_host.pack(fill="both", expand=True, padx=(int(12 * S), 0))
        self.page_canvas = tk.Canvas(self.page_host, bg=pal["panel"],
                                     highlightthickness=0, bd=0,
                                     yscrollincrement=int(18 * S))
        self.page_scroll = ttk.Scrollbar(self.page_host, orient="vertical",
                                         command=self.page_canvas.yview)
        self.page_scroll.pack(side="right", fill="y")
        self.page_canvas.pack(side="left", fill="both", expand=True)
        self.page_canvas.configure(yscrollcommand=self.page_scroll.set)
        self.page_scroll.bind("<ButtonRelease-1>", lambda e: self._enable_ai_auto())
        self.page_inner = tk.Frame(self.page_canvas, bg=pal["panel"])
        self._page_window = self.page_canvas.create_window(
            (0, 0), window=self.page_inner, anchor="nw")
        self.page_canvas.bind("<Configure>", self._on_page_configure)
        self.page_inner.bind("<Configure>", lambda e: self._sync_page())
        self.page_canvas.bind("<MouseWheel>", self._on_page_wheel)
        self.page_inner.bind("<MouseWheel>", self._on_page_wheel)
        self.page_scroll.bind("<MouseWheel>", self._on_page_wheel)

        # 图表 + 五档/逐笔: 同一行、严格等高；盘口随图表高度，不占全高
        self.top = tk.Frame(self.page_inner, bg=pal["panel"], height=self._top_h)
        self.top.pack(fill="x", padx=int(12 * S), pady=(int(2 * S), 0))
        self.top.pack_propagate(False)
        self.chart = tk.Canvas(self.top, bg=pal["panel"], highlightthickness=0,
                               bd=0, cursor="crosshair")
        self.chart.pack(side="left", fill="both", expand=True)
        self.side = tk.Canvas(self.top, bg=pal["panel_alt"], highlightthickness=1,
                              highlightbackground=pal["border"], bd=0,
                              width=int(236 * S))
        self.side.pack(side="right", fill="y")
        self.side.pack_propagate(False)

        self.chart.bind("<Configure>", lambda e: self._draw_all())
        self.chart.bind("<Motion>", self._on_chart_motion)
        self.chart.bind("<Leave>", self._on_chart_leave)
        self.chart.bind("<Button-1>", self._on_chart_press)
        self.chart.bind("<B1-Motion>", self._on_chart_drag)
        self.chart.bind("<ButtonRelease-1>", self._on_chart_release)
        self.chart.bind("<Double-Button-1>", self._on_chart_double)
        self.chart.bind("<MouseWheel>", self._on_wheel)
        self.side.bind("<Configure>", lambda e: self._draw_side())
        self.side.bind("<Button-1>", self._on_side_click)
        self.side.bind("<MouseWheel>", self._on_side_wheel)

        self._build_control_row()
        self._build_tags_panel()
        self._build_level_panel()
        self._build_ai_panel()
        self._bind_page_wheel(self.page_inner)
        self.win.after(60, self._sync_page)
        self.win.after(60, self._draw_left)
        self.win.after(80, lambda: self._set_left_width(self._left_width))

    def _build_control_row(self):
        pal, S = self.pal, self.S
        F = self.fonts
        self.ctrl = tk.Frame(self.page_inner, bg=pal["panel"])
        self.ctrl.pack(fill="x", padx=int(12 * S), pady=(int(4 * S), int(2 * S)))
        self.ctrl.bind("<MouseWheel>", self._on_page_wheel)
        self.period_seg = Segmented(self.ctrl, PERIODS, "minute", self._set_mode,
                                    pal, F, scale=S, height=28)
        self.period_seg.pack(side="left")
        self.ind_seg = Segmented(self.ctrl, INDICATORS, "vol", self._set_sub,
                                 pal, F, scale=S, height=26)
        self.ind_seg.pack(side="left", padx=(int(12 * S), 0))
        self.hint = tk.Label(self.ctrl, text="图表滚轮缩放 · 拖动平移 · 滚轮下翻 AI",
                             bg=pal["panel"], fg=pal["faint"], font=F.tiny())
        self.hint.pack(side="left", padx=(int(10 * S), 0))
        self.hint.bind("<MouseWheel>", self._on_page_wheel)
        self.ai_jump = CanvasButton(self.ctrl, "AI ↓", self._jump_ai,
                                    pal, F, kind="quiet", height=26, scale=S)
        self.ai_jump.pack(side="right")
        self.top_btn = CanvasButton(self.ctrl, self._top_text(), self._toggle_topmost,
                                    pal, F, kind="quiet", height=26, scale=S)
        self.top_btn.pack(side="right", padx=(0, int(6 * S)))

    def _build_tags_panel(self):
        pal, S = self.pal, self.S
        F = self.fonts
        self.tags_panel = tk.Frame(
            self.page_inner, bg=pal["panel_alt"], height=int(94 * S),
            highlightthickness=1, highlightbackground=pal["border"])
        self.tags_panel.pack(fill="x", padx=int(12 * S), pady=(int(6 * S), 0))
        self.tags_panel.pack_propagate(False)
        head = tk.Frame(self.tags_panel, bg=pal["panel_alt"])
        head.pack(fill="x", padx=int(14 * S), pady=(int(8 * S), int(2 * S)))
        tk.Label(head, text="板块 / 概念", bg=pal["panel_alt"], fg=pal["text"],
                 font=F.ui(0, True)).pack(side="left")
        self.tags_note = tk.Label(head, text="等待加载", bg=pal["panel_alt"],
                                  fg=pal["faint"], font=F.tiny())
        self.tags_note.pack(side="right")
        self.tags_canvas = tk.Canvas(
            self.tags_panel, bg=pal["panel_alt"], highlightthickness=0, bd=0,
            height=int(60 * S))
        self.tags_canvas.pack(fill="x", expand=True, padx=int(12 * S),
                              pady=(0, int(4 * S)))
        self.tags_canvas.bind("<Configure>", lambda e: self._draw_tags())

    def _build_level_panel(self):
        pal, S = self.pal, self.S
        F = self.fonts
        self.level_panel = tk.Frame(
            self.page_inner, bg=pal["panel_alt"], height=int(272 * S),
            highlightthickness=1, highlightbackground=pal["border"])
        self.level_panel.pack(fill="x", padx=int(12 * S), pady=(int(6 * S), 0))
        self.level_panel.pack_propagate(False)
        head = tk.Frame(self.level_panel, bg=pal["panel_alt"])
        head.pack(fill="x", padx=int(14 * S), pady=(int(8 * S), int(1 * S)))
        tk.Label(head, text="狙击点位", bg=pal["panel_alt"], fg=pal["text"],
                 font=F.ui(0, True)).pack(side="left")
        self.level_trend = tk.Label(head, text="趋势：待计算",
                                    bg=pal["panel_alt"], fg=pal["faint"],
                                    font=F.tiny())
        self.level_trend.pack(side="left", padx=(int(8 * S), 0))
        self.level_source = tk.Label(head, text="规则候选", bg=pal["panel_alt"],
                                     fg=pal["faint"], font=F.tiny())
        self.level_source.pack(side="right")
        tk.Frame(self.level_panel, bg=pal["border"], height=1).pack(
            fill="x", padx=int(14 * S))
        self.level_canvas = tk.Canvas(
            self.level_panel, bg=pal["panel_alt"], highlightthickness=0, bd=0)
        self.level_canvas.pack(fill="both", expand=True, padx=int(12 * S),
                               pady=(int(4 * S), int(6 * S)))
        self.level_canvas.bind("<Configure>", lambda e: self._draw_levels())

    def _fit_text(self, text, font, max_width):
        text = str(text or "")
        if max_width <= 0:
            return ""
        if font.measure(text) <= max_width:
            return text
        ell = "…"
        cut = text
        while cut and font.measure(cut + ell) > max_width:
            cut = cut[:-1]
        return (cut + ell) if cut else ""

    def _draw_tags(self):
        c = getattr(self, "tags_canvas", None)
        if c is None:
            return
        try:
            if not c.winfo_exists():
                return
            c.delete("all")
        except tk.TclError:
            return
        pal, S = self.pal, self.S
        F = self.fonts
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        is_index = bool(self.q and self.q.is_index) or D.is_index_code(self.code)
        if is_index:
            c.create_text(int(10 * S), int(20 * S), text="指数无板块标签",
                          anchor="w", fill=pal["faint"], font=F.tiny())
            return
        tags = list(self._sector_tags or [])
        if not tags:
            c.create_text(int(10 * S), int(20 * S), text="暂无板块数据",
                          anchor="w", fill=pal["faint"], font=F.tiny())
            try:
                self.tags_note.configure(text="暂缺")
            except tk.TclError:
                pass
            return

        def pick(kind, cap):
            return [t for t in tags if t.kind == kind][:cap]

        shown = pick("industry", 2) + pick("region", 1) + pick("concept", 6)
        total = len(tags)
        try:
            self.tags_note.configure(text="%d 个 · %s" % (
                total, time.strftime("%H:%M")))
        except tk.TclError:
            pass

        x = int(2 * S)
        y = int(3 * S)
        row_h = int(24 * S)
        gap = int(6 * S)
        max_x = w - int(2 * S)
        for tag in shown:
            name = str(tag.name or "")
            pct = float(getattr(tag, "pct", 0) or 0)
            pct_txt = "%+.2f%%" % pct
            nw = F.tiny().measure(name)
            pw = F.tiny().measure(pct_txt)
            chip_w = nw + pw + int(28 * S)
            if x + chip_w > max_x:
                x = int(2 * S)
                y += row_h + int(3 * S)
            if y + row_h > h:
                break
            if tag.kind == "industry":
                border, fill = pal["accent"], pal["panel"]
            elif tag.kind == "region":
                border, fill = pal["warn"], pal["panel_soft"]
            else:
                border, fill = pal["border"], pal["panel_soft"]
            draw_round_rect(c, x, y, x + chip_w, y + row_h,
                            int(7 * S), fill, border, 1)
            c.create_text(x + int(9 * S), y + row_h / 2, text=name,
                          anchor="w", fill=pal["text_dim"], font=F.tiny())
            c.create_text(x + chip_w - int(8 * S), y + row_h / 2,
                          text=pct_txt, anchor="e",
                          fill=D.change_color(pct, pal), font=F.tiny())
            x += chip_w + gap

    def _level_status_color(self, status: str):
        pal = self.pal
        if any(k in status for k in ("失效", "跌破", "止损")):
            return pal["danger"]
        if "止盈" in status:
            return pal["warn"]
        if "不足" in status:
            return pal["warn"]
        if any(k in status for k in ("已进入", "已达到")):
            return pal["pos"]
        return pal["muted"]

    def _draw_levels(self):
        c = getattr(self, "level_canvas", None)
        if c is None:
            return
        try:
            if not c.winfo_exists():
                return
            c.delete("all")
        except tk.TclError:
            return
        pal, S = self.pal, self.S
        F = self.fonts
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        plan = self._level_plan if (
            self._level_plan and getattr(self._level_plan, "code", "") == self.code
        ) else None

        if plan is None:
            try:
                self.level_trend.configure(text="趋势：待计算", fg=pal["faint"])
                self.level_source.configure(text="规则候选", fg=pal["faint"])
            except tk.TclError:
                pass
            c.create_text(int(10 * S), int(16 * S),
                          text="等待日线数据，程序先生成候选位…", anchor="w",
                          fill=pal["faint"], font=F.tiny())
            return

        trend_color = {"up": pal["pos"], "pullback": pal["accent"],
                       "weak": pal["neg"], "range": pal["muted"]}.get(
                           plan.trend, pal["faint"])
        source_map = {"rule": "规则候选", "ai": "AI 选择",
                      "fallback": "规则回退"}
        source_color = {"rule": pal["faint"], "ai": pal["accent"],
                        "fallback": pal["warn"]}.get(plan.source, pal["faint"])
        try:
            self.level_trend.configure(
                text="趋势：%s" % LV.trend_label(plan.trend), fg=trend_color)
            self.level_source.configure(
                text=source_map.get(plan.source, "规则候选"), fg=source_color)
        except tk.TclError:
            pass

        if plan.quality == "poor":
            c.create_text(int(10 * S), int(18 * S),
                          text=plan.note or "K线不足，暂不计算",
                          anchor="w", fill=pal["warn"], font=F.tiny())
            return

        current = float(getattr(self.q, "price", 0) or 0) if self.q else plan.current
        if current <= 0:
            current = plan.current
        dp = plan.dp
        label_w = int(82 * S)
        status_w = int(150 * S)
        x0 = int(4 * S)
        x_price = x0 + label_w
        basis_x = x_price + int(155 * S)
        status_x = max(basis_x + int(80 * S), w - status_w - int(4 * S))
        basis_w = max(int(80 * S), status_x - basis_x - int(10 * S))

        header_y = int(5 * S)
        c.create_text(x0, header_y, text="点位", anchor="nw",
                      fill=pal["faint"], font=F.tiny())
        c.create_text(x_price, header_y, text="价格 / 区间", anchor="nw",
                      fill=pal["faint"], font=F.tiny())
        c.create_text(basis_x, header_y, text="依据", anchor="nw",
                      fill=pal["faint"], font=F.tiny())
        c.create_text(w - int(4 * S), header_y, text="状态", anchor="ne",
                      fill=pal["faint"], font=F.tiny())
        c.create_line(x0, int(23 * S), w - int(4 * S), int(23 * S),
                      fill=pal["border_soft"])

        rows = (("ideal", "理想买入", pal["accent"]),
                ("secondary", "二次买入", pal["accent"]),
                ("stop", "止损", pal["danger"]),
                ("tp1", "止盈1", pal["warn"]),
                ("tp2", "止盈2", pal["warn"]))
        row_h = int(30 * S)
        y = int(27 * S)
        for key, label, color in rows:
            cand = plan.selected(key)
            price = cand.range_text(dp) if cand else "—"
            basis = cand.basis if cand else (plan.note or "暂无候选")
            status = plan.status(key, current) if cand else "暂无"
            c.create_text(x0, y + int(5 * S), text=label, anchor="nw",
                          fill=color, font=F.ui(-2, True))
            c.create_text(x_price, y + int(5 * S), text=price, anchor="nw",
                          fill=pal["text"], font=F.num(-2, True))
            c.create_text(basis_x, y + int(6 * S),
                          text=self._fit_text(basis, F.tiny(), basis_w),
                          anchor="nw", fill=pal["text_dim"], font=F.tiny())
            c.create_text(w - int(4 * S), y + int(6 * S),
                          text=self._fit_text(status, F.tiny(), status_w),
                          anchor="ne",
                          fill=self._level_status_color(status), font=F.tiny())
            c.create_line(x0, y + row_h - 2, w - int(4 * S), y + row_h - 2,
                          fill=pal["border_soft"])
            y += row_h

        rr = plan.rr()
        rr_text = "—"
        rr_color = pal["faint"]
        if rr is not None:
            rr_text = "风报比 1:%.2f" % rr
            rr_color = pal["pos"] if rr >= 1.5 else pal["warn"]
        c.create_text(x0, y + int(3 * S), text=rr_text, anchor="nw",
                      fill=rr_color, font=F.tiny())
        c.create_text(w - int(4 * S), y + int(3 * S),
                      text="日线 %d 根 · 前复权 · 截至 %s" % (
                          plan.bars_count, plan.bars_date),
                      anchor="ne", fill=pal["faint"], font=F.tiny())
        if plan.note:
            c.create_text(x0, y + int(16 * S), text=plan.note, anchor="nw",
                          fill=pal["faint"], font=F.tiny())

    def _set_research_visible(self, visible: bool):
        """指数模式隐藏板块/点位；恢复时保持它们在 AI 之前。"""
        visible = bool(visible)
        if visible == self._research_visible:
            return
        self._research_visible = visible
        panels = [getattr(self, "tags_panel", None),
                  getattr(self, "level_panel", None)]
        for panel in panels:
            if panel is None:
                continue
            try:
                panel.pack_forget()
                if visible:
                    panel.pack(fill="x", padx=int(12 * self.S),
                               pady=(int(6 * self.S), 0),
                               before=self.ai_panel)
            except tk.TclError:
                pass
        try:
            self.page_inner.after_idle(self._sync_page)
        except tk.TclError:
            pass

    def _build_ai_panel(self):
        pal, S = self.pal, self.S
        F = self.fonts
        self.ai_panel = tk.Frame(self.page_inner, bg=pal["panel_alt"],
                                 highlightthickness=1,
                                 highlightbackground=pal["border"])
        self.ai_panel.pack(fill="x", padx=int(12 * S), pady=(int(6 * S), int(16 * S)))
        self.ai_head = tk.Frame(self.ai_panel, bg=pal["panel_alt"])
        self.ai_head.pack(fill="x", padx=int(14 * S), pady=(int(10 * S), int(4 * S)))
        tk.Label(self.ai_head, text="AI 研判", bg=pal["panel_alt"], fg=pal["text"],
                 font=F.ui(2, True)).pack(side="left")
        self.ai_phase = tk.Label(self.ai_head, text="", bg=pal["panel_alt"],
                                 fg=pal["muted"], font=F.tiny())
        self.ai_phase.pack(side="left", padx=(int(8 * S), 0))
        self.ai_quality = tk.Label(self.ai_head, text="数据：待研判",
                                   bg=pal["panel_alt"], fg=pal["faint"], font=F.tiny())
        self.ai_quality.pack(side="left", padx=(int(6 * S), 0))
        self.ai_top = CanvasButton(self.ai_head, "↑ 回到图表", self._jump_top,
                                   pal, F, kind="quiet", height=24, scale=S)
        self.ai_top.pack(side="right")
        self.ai_retry = CanvasButton(self.ai_head, "重新研判", self._retry_ai,
                                     pal, F, kind="quiet", height=24, scale=S)
        self.ai_retry.pack(side="right", padx=(0, int(6 * S)))
        tk.Frame(self.ai_panel, bg=pal["border"], height=1).pack(
            fill="x", padx=int(14 * S))
        self.ai_body = tk.Frame(self.ai_panel, bg=pal["panel_alt"])
        self.ai_body.pack(fill="x", padx=int(14 * S), pady=(int(6 * S), int(10 * S)))
        self.ai_body.bind("<Configure>", self._on_ai_body_configure)

    def _bind_page_wheel(self, widget):
        """把中立区域的滚轮绑到整页滚动；图表/盘口/左栏/搜索框不参与。"""
        try:
            widget.bind("<MouseWheel>", self._on_page_wheel)
        except tk.TclError:
            return
        for child in widget.winfo_children():
            if child is getattr(self, "chart", None) or child is getattr(self, "side", None):
                continue
            if child is getattr(self, "period_seg", None) or child is getattr(self, "ind_seg", None):
                continue
            if child is getattr(self, "ai_top", None) or child is getattr(self, "ai_retry", None):
                continue
            self._bind_page_wheel(child)

    def _top_text(self):
        return "置顶：开" if self._topmost else "置顶：关"

    # ---------------- 页面滚动 / 分隔线 ----------------
    def _sync_page(self):
        if self._page_syncing:
            return
        self._page_syncing = True
        try:
            w = max(1, self.page_canvas.winfo_width())
            if w > 1:
                self.page_canvas.itemconfigure(self._page_window, width=w)
            self.page_inner.update_idletasks()
            bbox = self.page_canvas.bbox("all")
            content_h = int(bbox[3]) if bbox else 0
            self.page_canvas.configure(scrollregion=(0, 0, w, max(content_h, 0)))
        except tk.TclError:
            pass
        finally:
            self._page_syncing = False

    def _on_page_configure(self, _event=None):
        try:
            w = max(1, self.page_canvas.winfo_width())
            self.page_canvas.itemconfigure(self._page_window, width=w)
        except tk.TclError:
            pass
        self._sync_page()

    def _on_page_wheel(self, event):
        """中立区域滚轮: 整页上下滚动，不改变图表缩放/逐笔翻页/左栏列表。"""
        delta = int(getattr(event, "delta", 0) or 0)
        if not delta:
            return "break"
        try:
            first, last = self.page_canvas.yview()
            if (delta > 0 and first <= 0.0) or (delta < 0 and last >= 1.0):
                return "break"
            amount = max(1, abs(delta) // 120) * 3
            self.page_canvas.yview_scroll(-amount if delta > 0 else amount, "units")
            self._enable_ai_auto()
        except tk.TclError:
            pass
        self._schedule_ai_check()
        return "break"

    def _enable_ai_auto(self):
        self._ai_auto_ready = True
        self._schedule_ai_check(delay=120)

    def _scroll_page_to_widget(self, widget, margin=6):
        try:
            self.page_inner.update_idletasks()
            y = widget.winfo_rooty() - self.page_inner.winfo_rooty() - int(margin * self.S)
            content_h = max(1, self.page_inner.winfo_height())
            view_h = max(1, self.page_canvas.winfo_height())
            if content_h <= view_h:
                return
            y = max(0, min(y, content_h - view_h))
            self.page_canvas.yview_moveto(float(y) / float(content_h))
        except tk.TclError:
            pass

    def _jump_ai(self):
        self._ai_auto_ready = True
        self._scroll_page_to_widget(self.ai_panel, margin=8)
        self._schedule_ai_check(delay=260)

    def _jump_top(self):
        self._ai_auto_ready = False
        try:
            self.page_canvas.yview_moveto(0.0)
        except tk.TclError:
            pass

    def _on_resize_detail(self, w, h):
        """窗口缩放时保持左栏/图表比例，并让图表与五档继续等高。"""
        S = self.S
        self._top_h = int(max(400 * S, min(468 * S, h - 250 * S)))
        try:
            self.top.configure(height=self._top_h)
        except tk.TclError:
            pass
        self._set_left_width(self._left_width)
        self._sync_page()

    def _on_split_press(self, event):
        self._split_drag = (event.x_root, self.left.winfo_width())
        return "break"

    def _on_split_drag(self, event):
        if not self._split_drag:
            return
        x0, w0 = self._split_drag
        self._set_left_width(w0 + (event.x_root - x0))
        return "break"

    def _on_split_release(self, _event=None):
        if self._split_drag:
            self._left_width = int(self.left.winfo_width())
            self._save_geometry()
        self._split_drag = None
        return "break"

    def _on_split_reset(self, _event=None):
        self._set_left_width(int(220 * self.S))
        self._left_width = int(self.left.winfo_width())
        self._save_geometry()
        return "break"

    def _left_limits(self):
        total = max(1, self.split.winfo_width())
        side_w = int(236 * self.S) if self._side_visible else 0
        # 右侧至少保留约 500px 图表区 + 盘口 + 分隔线
        max_w = total - side_w - int(500 * self.S) - int(12 * self.S)
        max_w = min(max_w, int(360 * self.S))
        min_w = int(180 * self.S)
        return max(min_w, max_w), min_w

    def _set_left_width(self, width):
        max_w, min_w = self._left_limits()
        width = int(max(min_w, min(width, max_w)))
        if width == self._left_width and self.left.winfo_width() == width:
            return
        self._left_width = width
        try:
            self.left.configure(width=width)
            self.left.pack_propagate(False)
            self.left.update_idletasks()
        except tk.TclError:
            return
        self._draw_left()
        self._sync_page()
        self._draw_all()

    # ---------------- 左栏自选 / 搜索 ----------------
    def _search_text(self) -> str:
        try:
            text = self.search_var.get().strip()
        except tk.TclError:
            return ""
        return "" if text == SEARCH_TIP else text

    def _on_search_focus(self, _event=None):
        if self._search_text() == "" and self.search_var.get() == SEARCH_TIP:
            self.search_var.set("")
            try:
                self.search.configure(fg=self.pal["text"])
            except tk.TclError:
                pass

    def _on_search_blur(self, _event=None):
        if not self._search_text():
            try:
                self.search_var.set(SEARCH_TIP)
                self.search.configure(fg=self.pal["faint"])
            except tk.TclError:
                pass
            self._search_results = []
            self._search_gen += 1
            self._draw_left()

    def _on_search_key(self, _event=None):
        if self.search_var.get() == SEARCH_TIP:
            return
        try:
            self.search.configure(fg=self.pal["text"])
        except tk.TclError:
            pass
        if self._search_job:
            try:
                self.win.after_cancel(self._search_job)
            except tk.TclError:
                pass
        self._search_job = self.win.after(260, self._run_search)

    def _on_search_escape(self, _event=None):
        try:
            self.search_var.set(SEARCH_TIP)
            self.search.configure(fg=self.pal["faint"])
            self.search.icursor("end")
        except tk.TclError:
            pass
        self._search_results = []
        self._search_gen += 1
        self._draw_left()
        return "break"

    def _on_search_enter(self, _event=None):
        if self._search_results:
            self._open_symbol(self._search_results[0])
        return "break"

    def _run_search(self):
        self._search_job = None
        text = self._search_text()
        self._search_gen += 1
        gen = self._search_gen
        if not text:
            self._search_results = []
            self._draw_left()
            return
        self._left_note("搜索中…")

        def work():
            try:
                found = D.search_symbols(text)
            except Exception:
                found = []
            # 行情条是 A 股工作台: 搜索里过滤港股/美股等同名噪声
            results = [
                s for s in (found or [])
                if str(getattr(s, "code", "")).startswith(("sh", "sz", "bj"))
            ][:30]
            try:
                self._async_q.put(("search", gen, text, results))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _draw_left(self):
        c = getattr(self, "left_list", None)
        if c is None:
            return
        try:
            if not c.winfo_exists():
                return
            c.delete("all")
        except tk.TclError:
            return
        pal, S = self.pal, self.S
        F = self.fonts
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        self._left_rows = []
        searching = bool(self._search_text()) and bool(self._search_results)
        if searching:
            rows = [(s.code, s.name) for s in self._search_results]
        else:
            rows = [(code, "") for code in (self.app.pick_codes or [])]
        if not rows:
            tip = "没有匹配结果" if self._search_text() else "自选为空"
            c.create_text(w / 2, int(22 * S), text=tip, anchor="n",
                          fill=pal["faint"], font=F.tiny())
            c.configure(scrollregion=(0, 0, w, h))
            return
        row_h = int(43 * S)
        y = int(2 * S)
        plus_w = int(30 * S)
        for code, name_hint in rows:
            q = (self.app.quotes or {}).get(code)
            name = ((q.name if q else "") or name_hint or
                    (getattr(self.app, "_detail_names", {}) or {}).get(code) or
                    D.code_digits(code))
            selected = (code == self.code)
            if selected:
                draw_round_rect(c, int(2 * S), y + int(1 * S), w - int(2 * S),
                                y + row_h - int(1 * S), int(5 * S),
                                pal["selected"], "", 0)
            name_font = F.ui(-2, True)
            price_w = int(62 * S) if q else int(0 * S)
            max_name = max(20, w - int(10 * S) - plus_w - price_w - int(6 * S))
            label = sb_ui._fit_text(name_font, name, max_name)
            c.create_text(int(10 * S), y + int(7 * S), text=label, anchor="nw",
                          fill=pal["accent"] if selected else pal["text"],
                          font=name_font)
            code_txt = D.code_digits(code)
            if q:
                pct = D.today_pct(q)
                code_txt += "  " + (D.fmt_pct(pct) if pct is not None else "-")
            c.create_text(int(10 * S), y + int(25 * S), text=code_txt, anchor="nw",
                          fill=pal["muted"], font=F.num(-3, False))
            if q:
                col = D.change_color(D.today_pct(q), pal)
                price_x = (w - plus_w - int(2 * S)) if searching else (w - int(8 * S))
                c.create_text(price_x, y + int(8 * S),
                              text=D.fmt_price(q.price, q.dp), anchor="ne",
                              fill=col, font=F.num(-2, False))
            if searching:
                exists = code in (self.app.pick_codes or [])
                bx1, bx2 = w - plus_w, w - int(4 * S)
                by1, by2 = y + int(9 * S), y + int(31 * S)
                c.create_text((bx1 + bx2) / 2, (by1 + by2) / 2,
                              text="✓" if exists else "+",
                              fill=pal["faint"] if exists else pal["accent"],
                              font=F.ui(0, True))
                self._left_rows.append((code, y, y + row_h,
                                        bx1 if not exists else w + 1))
            else:
                self._left_rows.append((code, y, y + row_h, w + 1))
            y += row_h
        c.configure(scrollregion=(0, 0, w, max(h, y + int(4 * S))))

    def _on_left_wheel(self, event):
        try:
            c = self.left_list
            first, last = c.yview()
            if (event.delta > 0 and first <= 0.0) or (event.delta < 0 and last >= 1.0):
                return "break"
            c.yview_scroll(-3 if event.delta > 0 else 3, "units")
        except tk.TclError:
            pass
        return "break"

    def _on_left_click(self, event):
        try:
            y = self.left_list.canvasy(event.y)
        except tk.TclError:
            y = event.y
        for code, y1, y2, add_x in self._left_rows:
            if y1 <= y <= y2:
                if event.x >= add_x:
                    self._add_pick(code)
                else:
                    self._open_symbol(code)
                return "break"
        return "break"

    def _open_symbol(self, code):
        code = D.normalize_code(code)
        if not code:
            return
        for sym in self._search_results:
            if getattr(sym, "code", "") == code and getattr(sym, "name", ""):
                self.app._detail_names[code] = sym.name
                break
        self.show(code)
        if code not in (self.app.quotes or {}):
            self._fetch_quote(code)

    def _fetch_quote(self, code):
        code = D.normalize_code(code)
        if not code:
            return
        is_idx = code.startswith(("sh000", "sz399"))

        def work():
            try:
                qs = D.fetch_quotes([code], index_codes=[code] if is_idx else [], timeout=6.0)
                q = qs.get(code)
                if q:
                    self._async_q.put(("quote", code, q))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _add_pick(self, code):
        code = D.normalize_code(code)
        if not code:
            return
        ok = False
        try:
            ok = bool(self.app.add_pick(code))
        except Exception:
            ok = False
        if ok:
            self._left_note("已加入自选")
            self._draw_left()
        elif code in (self.app.pick_codes or []):
            self._left_note("已在自选")
        else:
            self._left_note("自选已满或添加失败")

    def _left_note(self, text):
        try:
            self.left_note.configure(text=text)
        except tk.TclError:
            return
        job = getattr(self, "_left_note_job", None)
        if job:
            try:
                self.win.after_cancel(job)
            except tk.TclError:
                pass
        self._left_note_job = self.win.after(
            2600, lambda: self.left_note.configure(text="")
            if getattr(self, "left_note", None) else None)

    def _schedule_ai_check(self, delay=180):
        job = getattr(self, "_ai_auto_job", None)
        if job:
            try:
                self.win.after_cancel(job)
            except tk.TclError:
                pass
        try:
            self._ai_auto_job = self.win.after(delay, self._maybe_autogen_ai)
        except tk.TclError:
            self._ai_auto_job = None

    def _maybe_autogen_ai(self):
        self._ai_auto_job = None
        if not self._ai_auto_ready or self._ai_busy or not self._viewable():
            return
        if not self.app.cfg.get("llm_enabled") or not D.llm_key(self.app.cfg):
            self._render_ai()
            return
        if self._ai_visible() is False:
            return
        phase = A.phase_of()
        r = self._ai_result
        if r and r.phase_key == phase["key"] and \
                time.strftime("%Y-%m-%d", time.localtime(r.generated_at or 0)) == phase["date"]:
            return
        if time.time() - self._ai_last_trigger < 12.0:
            return
        self._start_ai()

    def _ai_visible(self):
        """AI 面板露出足够多才自动请求，避免一开窗就偷偷消耗 token。"""
        try:
            if not self.ai_panel.winfo_ismapped():
                return False
            top = self.page_canvas.winfo_rooty()
            bottom = top + self.page_canvas.winfo_height()
            ay = self.ai_panel.winfo_rooty()
            ah = max(1, self.ai_panel.winfo_height())
            visible = min(bottom, ay + ah) - max(top, ay)
            need = min(ah * 0.8, 260 * self.S)
            return visible >= need and ay < bottom - int(12 * self.S)
        except tk.TclError:
            return False

    def _retry_ai(self):
        if not self.app.cfg.get("llm_enabled") or not D.llm_key(self.app.cfg):
            try:
                self.app.open_settings()
            except Exception:
                pass
            return
        if self._ai_busy:
            return
        self._start_ai(force=True)

    def _start_ai(self, force=False):
        if self._ai_busy:
            return
        if not self.app.cfg.get("llm_enabled") or not D.llm_key(self.app.cfg):
            self._render_ai()
            return
        phase = A.phase_of()
        r = self._ai_result
        if not force and r and r.phase_key == phase["key"] and \
                time.strftime("%Y-%m-%d", time.localtime(r.generated_at or 0)) == phase["date"]:
            return
        ctx = self._ai_context(phase)
        if ctx is None:
            return
        gen = self._ai_gen
        code = self.code
        self._ai_busy = True
        self._ai_last_trigger = time.time()
        self._render_ai()

        def work():
            try:
                need_bars = list(ctx.bars or [])
                if not ctx.is_index:
                    try:
                        need_bars = self._daily_bars(code, 180) or need_bars
                    except Exception:
                        pass
                ctx.bars = need_bars
                if not ctx.is_index:
                    plan = self._level_plan
                    if not plan or getattr(plan, "code", "") != code:
                        try:
                            plan = LV.build_plan(code, need_bars, self.q, is_index=False)
                        except Exception:
                            plan = None
                    if plan:
                        ctx.level_candidates = plan.prompt_lines()
                        if plan is not self._level_plan:
                            try:
                                self._async_q.put(("aux", self._aux_gen, code,
                                                   list(self._sector_tags or []), plan))
                            except Exception:
                                pass
                if not ctx.is_index:
                    if not ctx.brief:
                        try:
                            ctx.brief = D.mcp_stock_brief(code, level="full")
                        except Exception:
                            ctx.brief = ""
                    if not ctx.strength:
                        try:
                            ctx.strength = D.mcp_strength(code)
                        except Exception:
                            ctx.strength = None
                result = A.analyze(self.app.cfg, ctx)
            except Exception as exc:
                result = A.AIResult(code=code, phase_key=phase["key"],
                                    phase_label=phase["label"], clock=phase["clock"],
                                    error="研判失败：%s" % exc,
                                    generated_at=time.time())
            try:
                self._async_q.put(("ai", gen, code, result))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _ai_context(self, phase=None) -> A.AIContext | None:
        code = self.code
        if not code:
            return None
        phase = phase or A.phase_of()
        q = self.q
        raw = (getattr(self.app, "_auction", {}) or {}).get(code, [])
        samples = []
        try:
            today = str(getattr(q, "time", "") or "")[:8] if q else ""
            if not today:
                today = phase["date"].replace("-", "")
            samples = [s for s in raw if str(s[0])[:8] == today]
            if not samples and raw:
                days = sorted({str(s[0])[:8] for s in raw})
                if days:
                    d = days[-1]
                    samples = [s for s in raw if str(s[0])[:8] == d]
        except Exception:
            samples = []
        prev = (q.prev_close if q and q.prev_close else
                float((getattr(self.app, "_auction_prev", {}) or {}).get(code) or 0))
        try:
            score = D.auction_score(samples, prev,
                                    getattr(self.app, "_score_weights", None))
        except Exception:
            score = None
        holds = (self.app.cfg.get("holdings") or {}).get(code) or {}
        try:
            pos, _rows = D.position_of(holds.get("trades") or [])
        except Exception:
            pos = None
        idx = getattr(self.app, "index_code", "")
        iq = self.app.quotes.get(idx) if idx and idx != code else None
        plan = self._level_plan
        level_lines = plan.prompt_lines() if (
            plan and getattr(plan, "code", "") == code) else []
        tags = [t.name for t in (self._sector_tags or [])[:10]]
        return A.AIContext(
            code=code,
            name=((q.name if q else "") or
                  (getattr(self.app, "_detail_names", {}) or {}).get(code) or ""),
            quote=q,
            series=self._series,
            bars=list(self._bars),
            auction_samples=samples,
            auction_prev=prev,
            auction_score=score,
            position=pos,
            index_quote=iq,
            ticks_count=len(self._ticks),
            sector_tags=tags,
            level_candidates=level_lines,
            now=A.now_dt(),
        )

    def _daily_bars(self, code: str, count: int = 180) -> list:
        """详细窗口的日线缓存: 点位与 AI 共用，5 分钟内不重复请求。"""
        rec = self._daily_bars_cache.get(code)
        if rec and time.time() - rec[0] < 300.0:
            return list(rec[1])
        bars = D.fetch_kline(code, "day", count)
        if bars:
            self._daily_bars_cache[code] = (time.time(), list(bars))
        return bars

    def _start_aux(self):
        """后台拉板块标签和日线，先渲染规则候选；AI 选点随后覆盖。"""
        code = self.code
        if not code or D.is_index_code(code):
            return
        if self._aux_busy:
            return
        plan = self._level_plan
        if plan and getattr(plan, "code", "") == code and \
                time.time() - self._level_ts < 300.0:
            self._draw_tags()
            self._draw_levels()
            return
        self._aux_busy = True
        gen = self._aux_gen
        q = self.q

        def work():
            tags = []
            plan = None
            try:
                tags = SEC.fetch_tags(code)
            except Exception:
                tags = []
            try:
                bars = self._daily_bars(code, 180)
                plan = LV.build_plan(code, bars, q, is_index=False)
            except Exception:
                plan = None
            try:
                self._async_q.put(("aux", gen, code, tags, plan))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _apply_ai_points(self, result):
        plan = self._level_plan
        if not plan or getattr(plan, "code", "") != self.code or not result:
            return
        choices = dict(getattr(result, "sniper_choices", {}) or {})
        if not choices:
            self._draw_levels()
            return
        ok, reason = plan.apply_choices(choices)
        if ok:
            plan.source = "ai"
            plan.note = str(getattr(result, "sniper_reason", "") or "").strip()
        else:
            plan.source = "fallback"
            plan.note = "AI 选点待复核：%s" % reason
        self._draw_levels()
        try:
            self.page_inner.after_idle(self._sync_page)
        except tk.TclError:
            pass

    def _on_ai_body_configure(self, event):
        wrap = max(int(220 * self.S), int(event.width - int(8 * self.S)))
        changed = False
        for lbl in self._ai_wrap_labels:
            try:
                if abs(int(float(lbl.cget("wraplength") or 0)) - wrap) > 8:
                    lbl.configure(wraplength=wrap)
                    changed = True
            except (tk.TclError, ValueError):
                pass
        if changed:
            self._sync_page()

    def _render_ai(self):
        body = getattr(self, "ai_body", None)
        if body is None:
            return
        try:
            if not body.winfo_exists():
                return
            for child in body.winfo_children():
                child.destroy()
            body.update_idletasks()
        except tk.TclError:
            return
        pal, S = self.pal, self.S
        F = self.fonts
        self._ai_wrap_labels = []
        phase = A.phase_of()
        r = self._ai_result if self._ai_result and self._ai_result.code == self.code else None
        stale = bool(r and r.phase_key and r.phase_key != phase["key"])
        try:
            self.ai_phase.configure(
                text="阶段：%s %s%s" % (phase["label"], phase["clock"],
                                     "（当前结果来自上一阶段）" if stale else ""))
        except tk.TclError:
            pass
        if not self.app.cfg.get("llm_enabled") or not D.llm_key(self.app.cfg):
            qtext, qcolor = "数据：未启用", pal["faint"]
        elif self._ai_busy:
            qtext, qcolor = "数据：研判中", pal["accent"]
        elif r:
            qtext = "数据：%s" % (r.quality.label if r.quality else "未知")
            qcolor = {"good": pal["accent"], "usable": pal["text_dim"],
                      "limited": pal["warn"], "poor": pal["danger"]}.get(
                          (r.quality.level if r.quality else "unknown"), pal["faint"])
        else:
            qtext, qcolor = "数据：待研判", pal["faint"]
        try:
            self.ai_quality.configure(text=qtext, fg=qcolor)
        except tk.TclError:
            pass
        try:
            self.ai_retry.text = ("重新研判" if (self.app.cfg.get("llm_enabled")
                                 and D.llm_key(self.app.cfg)) else "去设置")
            self.ai_retry._draw()
        except Exception:
            pass

        wrap = max(int(220 * S), int(body.winfo_width() - int(8 * S))
                   if body.winfo_width() > 1 else int(520 * S))

        def mk(parent, text, font, fg, pady=(0, 0), padx=(0, 0), wrap_it=True):
            lbl = tk.Label(parent, text=text, bg=parent.cget("bg"), fg=fg,
                           font=font, anchor="w", justify="left",
                           wraplength=wrap if wrap_it else 0)
            lbl.pack(fill="x", pady=pady, padx=padx)
            if wrap_it:
                self._ai_wrap_labels.append(lbl)
            return lbl

        def section(no, title, color=None):
            outer = tk.Frame(body, bg=pal["panel_alt"])
            outer.pack(fill="x", pady=(int(8 * S), 0))
            head = tk.Frame(outer, bg=pal["panel_alt"])
            head.pack(fill="x")
            tk.Label(head, text=no, bg=pal["panel_alt"], fg=pal["faint"],
                     font=F.num(-3, False)).pack(side="left")
            tk.Label(head, text=title, bg=pal["panel_alt"],
                     fg=color or pal["text"], font=F.ui(-1, True)).pack(
                         side="left", padx=(int(7 * S), 0))
            tk.Frame(outer, bg=pal["border_soft"], height=1).pack(
                fill="x", pady=(int(5 * S), int(3 * S)))
            return outer

        if not self.app.cfg.get("llm_enabled") or not D.llm_key(self.app.cfg):
            box = section("AI", "尚未启用")
            mk(box, "在设置 → AI 研判中启用并填写 API Key 后，这里会按当前阶段自动分析。",
               F.ui(-2), pal["text_dim"])
            row = tk.Frame(box, bg=pal["panel_alt"])
            row.pack(fill="x", pady=(int(6 * S), 0))
            CanvasButton(row, "打开设置", lambda: self.app.open_settings(),
                         pal, F, kind="primary", height=28, scale=S).pack(side="left")
            mk(body, "AI 输出仅供辅助判断，不构成投资建议。", F.tiny(),
               pal["faint"], pady=(int(8 * S), 0))
            self._ai_wrap_ready()
            return

        if self._ai_busy:
            box = section("AI", "正在研判")
            mk(box, "正在读取行情、分时、K线、竞价与基本面摘要，生成 0–100 评分和动作建议…",
               F.ui(-2), pal["text_dim"])
            self._ai_wrap_ready()
            return

        if r is None:
            box = section("AI", "等待研判")
            mk(box, "滚动到这里会自动研判；也可以点右上角“重新研判”立即生成。",
               F.ui(-2), pal["text_dim"])
            row = tk.Frame(box, bg=pal["panel_alt"])
            row.pack(fill="x", pady=(int(6 * S), 0))
            CanvasButton(row, "立即研判", self._retry_ai,
                         pal, F, kind="primary", height=28, scale=S).pack(side="left")
            mk(body, "AI 输出仅供辅助判断，不构成投资建议。", F.tiny(),
               pal["faint"], pady=(int(8 * S), 0))
            self._ai_wrap_ready()
            return

        if r.error:
            box = section("AI", "研判失败", pal["danger"])
            mk(box, r.error, F.ui(-2), pal["danger"])
            row = tk.Frame(box, bg=pal["panel_alt"])
            row.pack(fill="x", pady=(int(6 * S), 0))
            CanvasButton(row, "重试", self._retry_ai,
                         pal, F, kind="primary", height=28, scale=S).pack(side="left")
            mk(body, "AI 输出仅供辅助判断，不构成投资建议。", F.tiny(),
               pal["faint"], pady=(int(8 * S), 0))
            self._ai_wrap_ready()
            return

        # 01 评分 / 02 推荐
        top = tk.Frame(body, bg=pal["panel_alt"])
        top.pack(fill="x")
        score_card = tk.Frame(top, bg=pal["panel"], highlightthickness=1,
                              highlightbackground=pal["border"])
        score_card.pack(side="left", fill="both", expand=True)
        action_card = tk.Frame(top, bg=pal["panel"], highlightthickness=1,
                               highlightbackground=pal["border"])
        action_card.pack(side="left", fill="both", expand=True,
                         padx=(int(8 * S), 0))
        tk.Label(score_card, text="01 评分", bg=pal["panel"], fg=pal["faint"],
                 font=F.tiny()).pack(anchor="w", padx=int(12 * S),
                                     pady=(int(7 * S), 0))
        score_txt = "%d" % r.score if r.score is not None else "--"
        score_row = tk.Frame(score_card, bg=pal["panel"])
        score_row.pack(anchor="w", padx=int(12 * S))
        tk.Label(score_row, text=score_txt, bg=pal["panel"], fg=pal["text"],
                 font=F.num(8, True)).pack(side="left")
        tk.Label(score_row, text="/100", bg=pal["panel"], fg=pal["faint"],
                 font=F.tiny()).pack(side="left", padx=(int(2 * S), 0),
                                     pady=(int(14 * S), 0))
        band = r.score_band or (r.quality.label if r.score is None else "")
        tk.Label(score_card, text=band or "评分缺失", bg=pal["panel"],
                 fg=pal["accent"] if r.score is not None else pal["muted"],
                 font=F.ui(-2, True)).pack(anchor="w", padx=int(12 * S),
                                           pady=(0, int(8 * S)))

        tk.Label(action_card, text="02 推荐", bg=pal["panel"], fg=pal["faint"],
                 font=F.tiny()).pack(anchor="w", padx=int(12 * S),
                                     pady=(int(7 * S), 0))
        action = r.action_label or "—"
        action_col = {"买入": pal["pos"], "加仓": pal["pos"], "持有": pal["accent"],
                      "减仓": pal["warn"], "卖出": pal["neg"],
                      "观望": pal["muted"], "回避": pal["warn"],
                      "预警": pal["danger"]}.get(action, pal["text"])
        tk.Label(action_card, text=action, bg=pal["panel"], fg=action_col,
                 font=F.ui(5, True)).pack(anchor="w", padx=int(12 * S),
                                         pady=(int(3 * S), int(8 * S)))

        if r.conclusion:
            mk(body, r.conclusion, F.ui(0, True), pal["text"],
               pady=(int(7 * S), 0))
        if r.conflict:
            mk(body, "口径待复核：%s" % r.conflict_reason,
               F.ui(-2), pal["warn"], pady=(int(5 * S), 0))

        # 03 建议
        box = section("03", "建议 / 理由")
        if r.reasons:
            for i, text in enumerate(r.reasons[:4], 1):
                mk(box, "%d. %s" % (i, text), F.ui(-2), pal["text_dim"],
                   pady=(int(2 * S), 0))
        else:
            mk(box, "暂无结构化理由。", F.ui(-2), pal["faint"])

        # 04 风险
        box = section("04", "风险", pal["warn"])
        if r.risks:
            for text in r.risks[:3]:
                mk(box, "· " + text, F.ui(-2), pal["text_dim"],
                   pady=(int(2 * S), 0))
        else:
            mk(box, "暂无结构化风险提示。", F.ui(-2), pal["faint"])

        # 05 观察计划
        box = section("05", "观察计划")
        if r.watch:
            for text in r.watch[:4]:
                if r.next_check and ("下次检查" in text or text.startswith("下次")):
                    continue
                mk(box, "· " + text, F.ui(-2), pal["text_dim"],
                   pady=(int(2 * S), 0))
        if r.next_check:
            mk(box, "下次检查：%s" % r.next_check, F.ui(-2), pal["accent"],
               pady=(int(4 * S), 0))
        elif not r.watch:
            mk(box, "暂无观察条件。", F.ui(-2), pal["faint"])

        # 06 数据质量
        box = section("06", "数据质量", pal["muted"])
        quality = r.quality
        mk(box, "%s：%s" % (quality.label, quality.text), F.ui(-2), pal["text_dim"])
        if stale:
            mk(box, "该结果生成于上一阶段，建议重新研判。", F.ui(-2), pal["warn"],
               pady=(int(3 * S), 0))

        mk(body, "AI 输出仅供辅助判断，不构成投资建议。", F.tiny(),
           pal["faint"], pady=(int(8 * S), 0))
        self._ai_wrap_ready()

    def _ai_wrap_ready(self):
        try:
            self.ai_body.update_idletasks()
            self._bind_page_wheel(self.ai_body)
            self.page_inner.after_idle(self._sync_page)
        except tk.TclError:
            pass

    def _set_side_visible(self, show: bool):
        if show == self._side_visible and self.side.winfo_ismapped() == show:
            return
        self._side_visible = show
        try:
            if show:
                if not self.side.winfo_ismapped():
                    self.side.pack(side="right", fill="y")
            else:
                self.side.pack_forget()
        except tk.TclError:
            pass

    def _toggle_topmost(self):
        self._topmost = not self._topmost
        try:
            self.win.attributes("-topmost", self._topmost)
        except tk.TclError:
            pass
        self.top_btn.text = self._top_text()
        self.top_btn._draw()
        self._save_geometry()

    # ---------------- 数据/股票切换 ----------------
    def show(self, code: str):
        changed = (code != self.code) or (self._series is None)
        self.code = code
        self.q = self.app.quotes.get(code)
        self._set_side_visible(not bool(self.q and self.q.is_index))
        if changed:
            self._gen += 1
            self._ai_gen += 1
            self._ai_busy = False
            self._aux_gen += 1
            self._aux_busy = False
            self._sector_tags = []
            self._level_plan = None
            self._level_ts = 0.0
            self._series = None
            self._five_days = []
            self._bars = []
            self._ticks = []
            self._tick_scroll = 0
            self._tick_page = -1
            self._tick_last_page = -1
            self._tick_busy = False
            self._tick_date = ""
            self._tick_note = ""
            self._view = None
            self._cursor = None
            self._cursor_lock = False
            self._price_line = None
            self.mode = "minute"
            self.sub = "vol"
            try:
                self.period_seg.set("minute")
                self.ind_seg.set("vol")
            except Exception:
                pass
            self._request_minute()
            if self.q and not self.q.is_index:
                self._start_tick_init()
            self._jump_top()
        self._set_research_visible(not (
            bool(self.q and self.q.is_index) or D.is_index_code(self.code)))
        self._draw_tags()
        self._draw_levels()
        self._ai_result = self._ai_results.get(code)
        self._render_ai()
        self._apply_ai_points(self._ai_result)
        self._start_aux()
        name = ((self.q.name if self.q else "") or
                (getattr(self.app, "_detail_names", {}) or {}).get(code) or
                D.code_digits(code)) or code
        try:
            self.win.title("%s %s" % (name, D.code_digits(code)))
        except tk.TclError:
            pass
        try:
            if self.win.state() == "iconic":
                self.win.deiconify()
            self.win.lift()
        except tk.TclError:
            pass
        self._draw_all()
        self._draw_left()
        self._schedule_ai_check(delay=400)

    def _request_minute(self):
        self.app.request_minutes(self.code, self._on_minutes)

    def _on_minutes(self, code, series):
        if code != self.code:
            return
        self._series = series
        if self.mode == "5day":
            self._build_five()
        self._draw_all()

    def _set_mode(self, mode):
        self.mode = mode
        self._cursor = None
        self._cursor_lock = False
        self._price_line = None
        self._view = None
        if mode == "minute":
            self._request_minute()
        elif mode == "5day":
            self._request_minute()
            self._build_five()
        else:
            self._bars = []
            self._load_kline(mode)
        self._draw_all()

    def _set_sub(self, sub):
        self.sub = sub
        self._draw_all()

    def _build_five(self):
        hist = D.load_minute_history(self.code, self.app.base_dir, 5)
        days = {s.date: s for s in hist if s.date}
        if self._series and self._series.date:
            days[self._series.date] = self._series
        self._five_days = [days[d] for d in sorted(days)][-5:]
        self._tick_note = ""
        if len(self._five_days) < 5:
            self._tick_note = "5日分时从首次使用起累积：当前 %d/5 日" % len(self._five_days)
        self._view = None

    def _load_kline(self, period):
        gen = self._gen
        code = self.code

        def work():
            try:
                bars = D.fetch_kline(code, period, 320)
            except Exception:
                bars = []
            self._async_q.put(("kline", gen, period, bars))

        threading.Thread(target=work, daemon=True).start()

    def _start_tick_init(self):
        gen = self._gen
        code = self.code
        self._tick_busy = True

        def work():
            try:
                page = D.find_last_tick_page(code)
                ticks = D.fetch_ticks(code, page)
            except Exception:
                page, ticks = -1, []
            self._async_q.put(("tick_init", gen, code, page, ticks))

        threading.Thread(target=work, daemon=True).start()

    def _poll_async(self):
        try:
            while True:
                msg = self._async_q.get_nowait()
                kind = msg[0]
                if kind == "kline":
                    _, gen, period, bars = msg
                    if gen == self._gen and period == self.mode:
                        had_bars = bool(self._bars)
                        self._bars = bars or []
                        if not had_bars:
                            self._view = None
                        self._draw_all()
                elif kind == "tick_init":
                    _, gen, code, page, ticks = msg
                    if gen == self._gen and code == self.code:
                        self._tick_page = page
                        self._tick_last_page = page
                        self._tick_busy = False
                        self._merge_ticks(ticks)
                        self._draw_side()
                elif kind == "tick_page":
                    _, gen, page, ticks, latest = msg
                    if gen == self._gen:
                        self._tick_busy = False
                        if latest:
                            self._tick_page = page
                        self._merge_ticks(ticks, latest=latest)
                        if latest:
                            self._draw_side()
                elif kind == "tick_older":
                    _, gen, page, ticks = msg
                    if gen == self._gen:
                        self._tick_busy = False
                        self._merge_ticks(ticks, latest=False)
                        self._tick_page = page
                        self._draw_side()
                elif kind == "quote":
                    _, code, q = msg
                    if code:
                        self.app.quotes[code] = q
                    if code == self.code:
                        self.q = q
                        self._set_side_visible(not bool(q and q.is_index))
                        if q and not q.is_index and self._tick_page < 0:
                            self._start_tick_init()
                        self._draw_header()
                        self._draw_side()
                        self._draw_all()
                    self._draw_left()
                elif kind == "search":
                    _, gen, text, results = msg
                    if gen == self._search_gen and text == self._search_text():
                        self._search_results = results
                        self._left_note(("%d 条结果" % len(results)) if results else "无匹配")
                        self._draw_left()
                elif kind == "aux":
                    _, gen, code, tags, plan = msg
                    if gen == self._aux_gen and code == self.code:
                        self._aux_busy = False
                        self._sector_tags = list(tags or [])
                        self._level_plan = plan
                        self._level_ts = time.time()
                        self._draw_tags()
                        self._draw_levels()
                        self._apply_ai_points(self._ai_result)
                        try:
                            self._bind_page_wheel(self.page_inner)
                            self.page_inner.after_idle(self._sync_page)
                        except tk.TclError:
                            pass
                elif kind == "ai":
                    _, gen, code, result = msg
                    if gen == self._ai_gen and code == self.code:
                        self._ai_busy = False
                        self._ai_results[code] = result
                        self._ai_result = result
                        self._apply_ai_points(result)
                        self._render_ai()
                        self._schedule_ai_check(delay=300)
        except queue.Empty:
            pass
        try:
            self._after_ids.append(self.win.after(100, self._poll_async))
        except tk.TclError:
            pass

    def _merge_ticks(self, ticks, latest=True):
        if not ticks:
            return
        merged = {t.seq: t for t in self._ticks}
        for t in ticks:
            merged[t.seq] = t
        self._ticks = sorted(merged.values(), key=lambda t: t.seq, reverse=True)
        if latest:
            self._tick_scroll = 0

    # ---------------- 定时刷新 ----------------
    def _viewable(self):
        try:
            return bool(self.win.winfo_viewable())
        except tk.TclError:
            return False

    def _timer_quote(self):
        if self._viewable():
            self.q = self.app.quotes.get(self.code)
            self._draw_header()
            self._draw_side()
            self._draw_left()
            self._draw_levels()
        try:
            self._after_ids.append(self.win.after(2000, self._timer_quote))
        except tk.TclError:
            pass

    def _timer_minute(self):
        if self._viewable() and self.mode in ("minute", "5day"):
            self._request_minute()
        try:
            self._after_ids.append(self.win.after(30000, self._timer_minute))
        except tk.TclError:
            pass

    def _timer_ticks(self):
        if (self._viewable() and self.q and not self.q.is_index
                and self._tick_page >= 0 and not self._tick_busy):
            today = time.strftime("%Y%m%d")
            if self._tick_date != today:
                self._tick_date = today
                self._ticks = []
                self._tick_scroll = 0
                self._start_tick_init()
            else:
                self._tick_busy = True
                gen, page = self._gen, self._tick_page

                def work():
                    try:
                        ticks = D.fetch_ticks(self.code, page)
                    except Exception:
                        ticks = []
                    self._async_q.put(("tick_page", gen, page, ticks, True))

                threading.Thread(target=work, daemon=True).start()
        try:
            self._after_ids.append(self.win.after(2000, self._timer_ticks))
        except tk.TclError:
            pass

    def _timer_kline(self):
        if self._viewable() and self.mode in ("day", "week", "month"):
            self._load_kline(self.mode)
        try:
            self._after_ids.append(self.win.after(60000, self._timer_kline))
        except tk.TclError:
            pass

    # ---------------- 头部 ----------------
    def _draw_header(self):
        c = self.head
        if not c.winfo_exists():
            return
        c.delete("all")
        pal, S = self.pal, self.S
        F = self.fonts
        w = max(1, c.winfo_width())
        q = self.q
        name = (q.name if q else D.code_digits(self.code)) or self.code
        pad = int(18 * S)
        c.create_text(pad, int(14 * S), text=name, anchor="nw",
                      fill=pal["text"], font=F.ui(3, True))
        code_txt = D.code_digits(self.code)
        c.create_text(pad, int(42 * S), text=code_txt, anchor="nw",
                      fill=pal["muted"], font=F.num(-3, False))
        market = D.market_label(self.code, bool(q and q.is_index))
        bw = F.tiny().measure(market) + int(16 * S)
        draw_round_rect(c, pad + F.num(-3, False).measure(code_txt) + int(8 * S),
                        int(39 * S), pad + F.num(-3, False).measure(code_txt) + int(8 * S) + bw,
                        int(59 * S), int(9 * S), pal["panel_alt"], pal["border_soft"], 1)
        c.create_text(pad + F.num(-3, False).measure(code_txt) + int(8 * S) + bw / 2,
                      int(49 * S), text=market, fill=pal["text_dim"], font=F.tiny())

        if not q:
            c.create_text(pad, int(62 * S), text="行情加载中…", anchor="w",
                          fill=pal["faint"], font=F.label())
            return
        pct = D.today_pct(q)
        chg = q.change if D.quote_is_today(q) else 0.0
        color = D.change_color(pct, pal)
        price_txt = D.fmt_price(q.price, q.dp)
        c.create_text(pad, int(58 * S), text=price_txt, anchor="nw",
                      fill=color, font=F.num(14, True))
        pw = F.num(14, True).measure(price_txt)
        chip_x = pad + pw + int(12 * S)
        chip_w = int(136 * S)
        soft = pal["pos_soft"] if pct > 0 else (pal["neg_soft"] if pct < 0 else pal["panel_soft"])
        draw_round_rect(c, chip_x, int(66 * S), chip_x + chip_w, int(94 * S),
                        int(7 * S), soft, "", 1)
        c.create_text(chip_x + int(10 * S), int(80 * S), text=D.fmt_change(chg, q.dp),
                      anchor="w", fill=color, font=F.num(-1, True))
        c.create_text(chip_x + chip_w - int(10 * S), int(80 * S), text=D.fmt_pct(pct),
                      anchor="e", fill=color, font=F.num(-1, True))

        # 右侧数据格(4 列 2 行)
        x0 = int(w * 0.44)
        cw = max(110, (w - x0 - pad) / 4.0)
        stats = []
        if q.is_index:
            vol_txt = D.fmt_volume(q.volume).replace("手", "股")
            stats = [("今开", D.fmt_price(q.open, q.dp)), ("最高", D.fmt_price(q.high, q.dp)),
                     ("最低", D.fmt_price(q.low, q.dp)), ("昨收", D.fmt_price(q.prev_close, q.dp)),
                     ("成交量", vol_txt), ("成交额", D.fmt_amount(q.amount)),
                     ("振幅", "%.2f%%" % q.amplitude if q.amplitude else "-"),
                     ("时间", D.fmt_quote_time(q.time))]
        else:
            stats = [("今开", D.fmt_price(q.open, q.dp)), ("最高", D.fmt_price(q.high, q.dp)),
                     ("最低", D.fmt_price(q.low, q.dp)), ("昨收", D.fmt_price(q.prev_close, q.dp)),
                     ("成交量", D.fmt_volume(q.volume)), ("成交额", D.fmt_amount(q.amount)),
                     ("换手", "%.2f%%" % q.turnover if q.turnover else "-"),
                     ("量比", "%.2f" % q.volume_ratio if q.volume_ratio else "-")]
        for i, (k, v) in enumerate(stats):
            x = x0 + (i % 4) * cw
            y = int(16 * S) + (i // 4) * int(32 * S)
            c.create_text(x, y, text=k, anchor="nw", fill=pal["muted"], font=F.tiny())
            c.create_text(x, y + int(15 * S), text=v, anchor="nw",
                          fill=pal["text_dim"], font=F.num(-2, False))

    # ---------------- 侧栏 ----------------
    def _draw_side(self):
        if not self._side_visible:
            return
        c = self.side
        if not c.winfo_exists():
            return
        c.delete("all")
        pal, S = self.pal, self.S
        F = self.fonts
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        self._side_rows = []
        pad = int(12 * S)
        q = self.q
        if not q:
            c.create_text(w / 2, h / 2, text="加载中…", fill=pal["faint"], font=F.label())
            return
        if q.is_index:
            c.create_text(pad, int(10 * S), text="指数信息", anchor="nw",
                          fill=pal["text"], font=F.ui(0, True))
            avg = 0.0
            if self._series and self._series.avg_prices:
                avg = self._series.avg_prices[-1]
            vol_txt = D.fmt_volume(q.volume).replace("手", "股")
            rows = [("点位", D.fmt_price(q.price, q.dp)),
                    ("涨跌", D.fmt_change(q.change, q.dp) + "  " + D.fmt_pct(D.today_pct(q))),
                    ("今开", D.fmt_price(q.open, q.dp)),
                    ("最高", D.fmt_price(q.high, q.dp)),
                    ("最低", D.fmt_price(q.low, q.dp)),
                    ("昨收", D.fmt_price(q.prev_close, q.dp)),
                    ("均价", D.fmt_price(avg, q.dp) if avg else "-"),
                    ("成交额", D.fmt_amount(q.amount)),
                    ("成交量", vol_txt),
                    ("振幅", "%.2f%%" % q.amplitude if q.amplitude else "-"),
                    ("时间", D.fmt_quote_time(q.time))]
            y = int(36 * S)
            for k, v in rows:
                c.create_text(pad, y, text=k, anchor="w", fill=pal["muted"], font=F.tiny())
                c.create_text(w - pad, y, text=v, anchor="e", fill=pal["text_dim"],
                              font=F.num(-2, False))
                y += int(22 * S)
            bars = self._bars
            if len(bars) >= 6:
                y += int(8 * S)
                c.create_line(pad, y, w - pad, y, fill=pal["border"])
                y += int(20 * S)
                c.create_text(pad, y, text="区间表现", anchor="w",
                              fill=pal["text"], font=F.ui(-1, True))
                y += int(22 * S)
                for label, n in (("5日", 5), ("20日", 20)):
                    if len(bars) > n and bars[-n - 1].close:
                        pct = (bars[-1].close - bars[-n - 1].close) / bars[-n - 1].close * 100
                        seg = bars[-n:]
                        c.create_text(pad, y, text=label + "涨跌", anchor="w",
                                      fill=pal["muted"], font=F.tiny())
                        c.create_text(w - pad, y, text="%+.2f%%" % pct, anchor="e",
                                      fill=D.change_color(pct, pal), font=F.num(-3, False))
                        y += int(20 * S)
                        c.create_text(pad, y, text=label + "高/低", anchor="w",
                                      fill=pal["muted"], font=F.tiny())
                        c.create_text(w - pad, y,
                                      text="%s / %s" % (D.fmt_price(max(b.high for b in seg), q.dp),
                                                        D.fmt_price(min(b.low for b in seg), q.dp)),
                                      anchor="e", fill=pal["text_dim"], font=F.num(-3, False))
                        y += int(20 * S)
            c.create_text(pad, min(h - int(52 * S), y + int(14 * S)),
                          text="指数无五档盘口与逐笔明细。\n分时、K线支持滚轮缩放、拖动平移、双击复位。",
                          anchor="nw", fill=pal["faint"], font=F.tiny(),
                          width=max(100, w - 2 * pad))
            return

        c.create_text(pad, int(10 * S), text="五档盘口", anchor="nw",
                      fill=pal["text"], font=F.ui(0, True))
        bid_p = list(getattr(q, "bid_prices", []) or [])
        bid_v = list(getattr(q, "bid_vols", []) or [])
        ask_p = list(getattr(q, "ask_prices", []) or [])
        ask_v = list(getattr(q, "ask_vols", []) or [])
        if not bid_p and q.bid1:
            bid_p, bid_v = [q.bid1], [q.bid1_vol]
        if not ask_p and q.ask1:
            ask_p, ask_v = [q.ask1], [q.ask1_vol]
        y = int(30 * S)
        row_h = int(20 * S)
        for i in range(4, -1, -1):
            label = "卖" + "一二三四五"[i]
            price = ask_p[i] if i < len(ask_p) else 0.0
            vol = ask_v[i] if i < len(ask_v) else 0.0
            self._level_row(c, label, price, vol, y, w, pad, pal, F, S, "ask")
            y += row_h
        c.create_line(pad, y + int(2 * S), w - pad, y + int(2 * S), fill=pal["border"])
        y += int(8 * S)
        for i in range(5):
            label = "买" + "一二三四五"[i]
            price = bid_p[i] if i < len(bid_p) else 0.0
            vol = bid_v[i] if i < len(bid_v) else 0.0
            self._level_row(c, label, price, vol, y, w, pad, pal, F, S, "bid")
            y += row_h

        y += int(4 * S)
        c.create_line(pad, y, w - pad, y, fill=pal["border"])
        y += int(8 * S)
        c.create_text(pad, y, text="成交明细", anchor="nw",
                      fill=pal["text"], font=F.ui(-1, True))
        y += int(18 * S)
        # 明细表头
        c.create_text(pad, y, text="时间", anchor="nw", fill=pal["faint"], font=F.tiny())
        c.create_text(pad + int(62 * S), y, text="价格", anchor="nw", fill=pal["faint"], font=F.tiny())
        c.create_text(w - pad - int(22 * S), y, text="量", anchor="ne", fill=pal["faint"], font=F.tiny())
        c.create_text(w - pad, y, text="向", anchor="ne", fill=pal["faint"], font=F.tiny())
        y += int(15 * S)
        avail = max(1, int((h - y - int(22 * S)) / row_h))
        ticks = self._ticks[self._tick_scroll:self._tick_scroll + avail]
        if not ticks:
            c.create_text(pad, y + int(6 * S), text="逐笔加载中…" if self._tick_page < 0 else "暂无逐笔",
                          anchor="nw", fill=pal["faint"], font=F.tiny())
        for t in ticks:
            col = pal["pos"] if t.side == "B" else (pal["neg"] if t.side == "S" else pal["muted"])
            side_txt = {"B": "买", "S": "卖", "M": "中"}.get(t.side, t.side)
            c.create_text(pad, y, text=t.time[-8:], anchor="nw", fill=pal["muted"], font=F.tiny())
            c.create_text(pad + int(62 * S), y, text=D.fmt_price(t.price, q.dp),
                          anchor="nw", fill=col, font=F.num(-3, False))
            c.create_text(w - pad - int(22 * S), y, text=_fmt_lots(t.volume),
                          anchor="ne", fill=pal["text_dim"], font=F.num(-3, False))
            c.create_text(w - pad, y, text=side_txt, anchor="ne", fill=col, font=F.tiny())
            y += row_h
        if len(self._ticks) > self._tick_scroll + avail:
            c.create_text(w / 2, min(h - int(8 * S), y + int(2 * S)),
                          text="滚轮加载更早", fill=pal["faint"], font=F.tiny())

    def _level_row(self, c, label, price, vol, y, w, pad, pal, F, S, kind):
        col = pal["neg"] if kind == "ask" else pal["pos"]
        selected = self._price_line is not None and abs(price - self._price_line) < 1e-9
        if selected:
            draw_round_rect(c, pad - int(2 * S), y - int(2 * S), w - pad + int(2 * S),
                            y + int(16 * S), int(4 * S), pal["selected"], "", 1)
        c.create_text(pad, y, text=label, anchor="nw", fill=pal["muted"], font=F.tiny())
        c.create_text(pad + int(34 * S), y, text=D.fmt_price(price, self.q.dp) if price else "-",
                      anchor="nw", fill=col, font=F.num(-3, False))
        c.create_text(w - pad, y, text=_fmt_lots(vol) if vol else "-",
                      anchor="ne", fill=pal["text_dim"], font=F.num(-3, False))
        self._side_rows.append(("level", float(price), y - int(2 * S), y + int(16 * S)))

    def _on_side_click(self, event):
        for kind, price, y1, y2 in self._side_rows:
            if kind == "level" and y1 <= event.y <= y2 and price > 0:
                self._price_line = None if (self._price_line is not None
                                            and abs(price - self._price_line) < 1e-9) else price
                self._draw_all()
                return
        # 点「滚轮加载更早」区域: 直接加载
        if self._ticks and event.y > self.side.winfo_height() - 30 * self.S:
            self._load_older_ticks()

    def _on_side_wheel(self, event):
        if not self._ticks:
            return
        step = 3 if event.delta < 0 else -3
        max0 = max(0, len(self._ticks) - 8)
        self._tick_scroll = max(0, min(max0, self._tick_scroll + step))
        if self._tick_scroll >= max0 and event.delta < 0:
            self._load_older_ticks()
        self._draw_side()
        return "break"

    def _load_older_ticks(self):
        if self._tick_busy or self._tick_page <= 0:
            return
        self._tick_busy = True
        gen, page = self._gen, self._tick_page - 1

        def work():
            try:
                ticks = D.fetch_ticks(self.code, page)
            except Exception:
                ticks = []
            self._async_q.put(("tick_older", gen, page, ticks))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 图表 ----------------
    def _draw_all(self):
        try:
            if not self.chart.winfo_exists():
                return
        except tk.TclError:
            return
        self.chart.delete("all")
        if self.mode == "minute":
            self._draw_minute(False)
        elif self.mode == "5day":
            self._draw_minute(True)
        else:
            self._draw_kline()
        if self._price_line is not None:
            self._draw_price_line()
        if self._cursor:
            self._draw_cross(*self._cursor)

    def _draw_price_line(self):
        p = self._plot
        if not p or p.get("empty") or not p.get("y2"):
            return
        pal, S, F = self.pal, self.S, self.fonts
        try:
            yy = p["py"](self._price_line)
        except Exception:
            return
        if not (p["y1"] <= yy <= p["y2"]):
            return
        c = self.chart
        c.create_line(p["x1"], yy, p["x2"], yy, fill=pal["warn"],
                      dash=(4, 3), width=max(1, int(1.0 * S)), tags="price_line")
        c.create_text(p["x1"] + int(4 * S), yy - int(3 * S),
                      text="价位 " + D.fmt_price(self._price_line, self.q.dp if self.q else 2),
                      anchor="sw", fill=pal["warn"], font=F.tiny(), tags="price_line")

    def _plot_rect(self):
        w, h = self.chart.winfo_width(), self.chart.winfo_height()
        S = self.S
        left = int(54 * S)
        right = int(52 * S)
        top = int(8 * S)
        bottom = int(20 * S)
        return left, top, max(left + 20, w - right), max(top + 20, h - bottom)

    def _sub_rects(self):
        x1, y1, x2, y2 = self._plot_rect()
        total_h = y2 - y1
        if self.sub == "vol":
            return ((x1, y1, x2, y1 + int(total_h * 0.68)),
                    (x1, y1 + int(total_h * 0.72), x2, y2), None)
        return ((x1, y1, x2, y1 + int(total_h * 0.52)),
                (x1, y1 + int(total_h * 0.56), x2, y1 + int(total_h * 0.72)),
                (x1, y1 + int(total_h * 0.76), x2, y2))

    def _minute_arrays(self, five=False):
        days = self._five_days if five else ([self._series] if self._series else [])
        pts, avgs, vols, xs, day_marks, day_bases = [], [], [], [], [], []
        for di, s in enumerate(days):
            if not s:
                continue
            prev = s.prev_close or (self.q.prev_close if self.q else 0.0)
            day_bases.append(prev)
            for i, (t, p) in enumerate(s.points):
                pos = di * MINUTES + minute_pos(t)
                xs.append(pos)
                pts.append(p)
                avgs.append(s.avg_prices[i] if i < len(s.avg_prices) else p)
                vols.append(s.volumes[i] if i < len(s.volumes) else 0.0)
            if five:
                day_marks.append((di * MINUTES, s.date))
        return pts, avgs, vols, xs, day_bases, day_marks

    def _draw_minute(self, five=False):
        pal, S = self.pal, self.S
        F = self.fonts
        c = self.chart
        x1, y1, x2, y2 = self._plot_rect()
        main_r, vol_r, ind_r = self._sub_rects()
        x1, y1, x2, y2 = main_r
        # 背景/边框
        c.create_rectangle(x1, y1, x2, y2, outline=pal["border"], width=1)
        c.create_rectangle(vol_r[0], vol_r[1], vol_r[2], vol_r[3],
                           outline=pal["border_soft"], width=1)
        if ind_r:
            c.create_rectangle(ind_r[0], ind_r[1], ind_r[2], ind_r[3],
                               outline=pal["border_soft"], width=1)
        pts, avgs, vols, xs, day_bases, marks = self._minute_arrays(five)
        total = max(MINUTES * max(1, len(day_bases) if five else 1), 1)
        base_prev = day_bases[-1] if day_bases else 0.0
        if not pts:
            c.create_text((x1 + x2) / 2, (y1 + y2) / 2, text="分时数据加载中…" if self._series is None
                          else "暂无分时数据", fill=pal["faint"], font=F.label())
            self._plot = {"kind": "minute", "empty": True}
            return
        if self._view is None:
            self._view = (0.0, float(total))
        start, end = self._view
        span = max(1.0, end - start)
        visible = [(xs[i], pts[i], avgs[i], vols[i]) for i in range(len(pts))
                   if start - 1 <= xs[i] <= end + 1]
        if not visible:
            visible = [(xs[i], pts[i], avgs[i], vols[i]) for i in range(len(pts))]
        hi = max(max(v[1] for v in visible), base_prev or -1e18)
        lo = min(min(v[1] for v in visible), base_prev or 1e18)
        if hi <= lo:
            hi, lo = hi + 0.01, lo - 0.01
        pad_v = (hi - lo) * 0.08
        hi += pad_v
        lo -= pad_v

        def px(xv):
            return x1 + (xv - start) / span * (x2 - x1)

        def py(v):
            return y2 - (v - lo) / (hi - lo) * (y2 - y1)

        # 网格与坐标
        for i in range(1, 5):
            yy = y1 + (y2 - y1) * i / 5.0
            c.create_line(x1, yy, x2, yy, fill=pal["border_soft"])
            val = hi - (hi - lo) * i / 5.0
            c.create_text(x1 - int(4 * S), yy, text=D.fmt_price(val, self.q.dp if self.q else 2),
                          anchor="e", fill=pal["muted"], font=F.tiny())
            if base_prev and not five:
                c.create_text(x2 + int(4 * S), yy, text="%+.2f%%" % ((val - base_prev) / base_prev * 100),
                              anchor="w", fill=pal["faint"], font=F.tiny())
        # 上下端点价格，避免只标中间网格、看不出真实区间
        dp = self.q.dp if self.q else 2
        c.create_text(x1 - int(4 * S), y1 + int(7 * S), text=D.fmt_price(hi, dp),
                      anchor="e", fill=pal["muted"], font=F.tiny())
        c.create_text(x1 - int(4 * S), y2 - int(7 * S), text=D.fmt_price(lo, dp),
                      anchor="e", fill=pal["muted"], font=F.tiny())
        # 时间刻度
        ticks = [0, 30, 60, 90, 120, 150, 180, 210, 240]
        for m in ticks:
            if five and len(day_bases) > 1:
                continue
            if start <= m <= end:
                xx = px(m)
                c.create_line(xx, y1, xx, y2, fill=pal["border_soft"], dash=(2, 3))
                anchor = "nw" if m == 0 else ("ne" if m == MINUTES else "n")
                c.create_text(xx, y2 + int(6 * S), text=self._hm(m), anchor=anchor,
                              fill=pal["faint"], font=F.tiny())
        if five and len(day_bases) > 1:
            for m, date in marks:
                if start <= m <= end:
                    xx = px(m)
                    c.create_line(xx, y1, xx, y2, fill=pal["border"], dash=(3, 4))
                    c.create_text(xx + int(3 * S), y2 + int(6 * S),
                                  text=date[5:] if len(date) >= 10 else date,
                                  anchor="nw", fill=pal["muted"], font=F.tiny())
        # 昨收线(5日只画最新一天, 历史天各有昨收, 全画会糊)
        if base_prev:
            yy = py(base_prev)
            lx = px((len(day_bases) - 1) * MINUTES) if five else x1
            c.create_line(lx, yy, x2, yy, fill=pal["muted"], dash=(4, 3))
            c.create_text(lx + int(3 * S), yy - int(3 * S), text="昨收 " + D.fmt_price(base_prev, 2),
                          anchor="sw", fill=pal["muted"], font=F.tiny())

        # 分日画线(5日不跨日连接)
        groups = []
        if five:
            for di in range(len(self._five_days)):
                groups.append([v for v in visible if (di * MINUTES) <= v[0] < ((di + 1) * MINUTES)])
        else:
            groups = [visible]
        for gi, group in enumerate(groups):
            if not group:
                continue
            gb = day_bases[gi] if gi < len(day_bases) else base_prev
            lastp = group[-1][1]
            up = bool(gb) and lastp >= gb
            coords = []
            for xv, p, a, v in group:
                coords += [px(xv), py(p)]
            if len(coords) >= 4:
                if not five:
                    line_col = pal["pos"] if up else pal["neg"]
                    c.create_polygon(coords + [coords[-2], y2, coords[0], y2],
                                     fill=pal["pos_soft"] if up else pal["neg_soft"], outline="")
                else:
                    line_col = pal["text_dim"]
                c.create_line(*coords, fill=line_col, width=max(1, int(1.4 * S)))
            avg_coords = []
            for xv, p, a, v in group:
                avg_coords += [px(xv), py(a)]
            if len(avg_coords) >= 4:
                c.create_line(*avg_coords, fill=pal["warn"], width=max(1, int(1.0 * S)))
        if not five and pts:
            lx, ly = px(xs[-1]), py(pts[-1])
            col = pal["pos"] if base_prev and pts[-1] >= base_prev else pal["neg"]
            c.create_oval(lx - int(2.4 * S), ly - int(2.4 * S),
                          lx + int(2.4 * S), ly + int(2.4 * S), fill=col, outline="")

        # 量柱
        maxv = max([v[3] for v in visible] or [1.0]) or 1.0
        vh = vol_r[3] - vol_r[1]
        bw = max(1.0, (x2 - x1) / max(1.0, span) * 0.7)
        prev_p = None
        for xv, p, a, v in visible:
            xx = px(xv)
            if v <= 0:
                continue
            bh = max(1.0, v / maxv * (vh - 4 * S))
            col = pal["pos"] if (prev_p is None or p >= prev_p) else pal["neg"]
            c.create_rectangle(xx - bw / 2, vol_r[3] - bh, xx + bw / 2, vol_r[3],
                               fill=col, outline="")
            prev_p = p
        c.create_text(vol_r[0] + int(3 * S), vol_r[1] + int(2 * S), text="成交量",
                      anchor="nw", fill=pal["faint"], font=F.tiny())

        if ind_r:
            self._draw_indicator(ind_r, [v[1] for v in visible],
                                 [px(v[0]) for v in visible],
                                 visible_x=[px(v[0]) for v in visible], start=start, end=end,
                                 px=px, x1=x1, x2=x2)
        if five and self._tick_note:
            c.create_text(x2 - int(4 * S), y1 + int(3 * S), text=self._tick_note,
                          anchor="ne", fill=pal["warn"], font=F.tiny())
        self._plot = {"kind": "minute", "empty": False, "x1": x1, "x2": x2,
                      "y1": y1, "y2": y2, "start": start, "end": end,
                      "hi": hi, "lo": lo, "total": total, "base": base_prev,
                      "xs": xs, "pts": pts, "avgs": avgs, "vols": vols,
                      "five": five, "groups": groups, "day_bases": day_bases,
                      "cross_bottom": (ind_r[3] if ind_r else vol_r[3]),
                      "px": px, "py": py}

    def _hm(self, m):
        m = int(m)
        if m <= 120:
            mins = 9 * 60 + 30 + m
        else:
            mins = 13 * 60 + (m - 120)
        return "%02d:%02d" % (mins // 60, mins % 60)

    def _kline_label(self, date: str) -> str:
        date = str(date or "")
        if len(date) >= 10:
            if self.mode == "day":
                return date[5:]
            return date[:7]
        return date

    def _draw_kline(self):
        pal, S = self.pal, self.S
        F = self.fonts
        c = self.chart
        x1, y1, x2, y2 = self._plot_rect()
        main_r, vol_r, ind_r = self._sub_rects()
        x1, y1, x2, y2 = main_r
        c.create_rectangle(x1, y1, x2, y2, outline=pal["border"], width=1)
        c.create_rectangle(vol_r[0], vol_r[1], vol_r[2], vol_r[3],
                           outline=pal["border_soft"], width=1)
        if ind_r:
            c.create_rectangle(ind_r[0], ind_r[1], ind_r[2], ind_r[3],
                               outline=pal["border_soft"], width=1)
        bars = self._bars
        if not bars:
            c.create_text((x1 + x2) / 2, (y1 + y2) / 2, text="K线加载中…",
                          fill=pal["faint"], font=F.label())
            self._plot = {"kind": "kline", "empty": True}
            return
        n = len(bars)
        if self._view is None:
            self._view = (max(0, n - 120), float(n))
        start, end = self._view
        start = max(0, min(start, n - 1))
        end = max(start + 1, min(end, n))
        span = end - start
        i0, i1 = max(0, int(start) - 1), min(n, int(end) + 2)
        vis = bars[i0:i1]
        hi = max(b.high for b in vis)
        lo = min(b.low for b in vis)
        ma5 = self._ma(bars, 5)
        ma10 = self._ma(bars, 10)
        ma20 = self._ma(bars, 20)
        for arr in (ma5, ma10, ma20):
            for i in range(i0, i1):
                if i < len(arr) and arr[i] is not None:
                    hi = max(hi, arr[i]); lo = min(lo, arr[i])
        if hi <= lo:
            hi, lo = hi + 0.01, lo - 0.01
        pad_v = (hi - lo) * 0.06
        # 长期月线（前复权）早期价格可能只有个位数，6% 留白会把下沿压成负数
        pad_v = min(pad_v, max(0.0, lo) * 0.5)
        hi += pad_v
        lo -= pad_v

        def px(i):
            return x1 + (i - start) / span * (x2 - x1)

        def py(v):
            return y2 - (v - lo) / (hi - lo) * (y2 - y1)

        for i in range(1, 5):
            yy = y1 + (y2 - y1) * i / 5
            c.create_line(x1, yy, x2, yy, fill=pal["border_soft"])
            val = hi - (hi - lo) * i / 5
            c.create_text(x1 - int(4 * S), yy, text=D.fmt_price(val, 2),
                          anchor="e", fill=pal["muted"], font=F.tiny())
        c.create_text(x1 - int(4 * S), y1 + int(7 * S), text=D.fmt_price(hi, 2),
                      anchor="e", fill=pal["muted"], font=F.tiny())
        c.create_text(x1 - int(4 * S), y2 - int(7 * S), text=D.fmt_price(lo, 2),
                      anchor="e", fill=pal["muted"], font=F.tiny())
        step_labels = max(1, int(span / 6))
        last_i = int(end) - 1
        last_label_i = int(start) + step_labels * ((last_i - int(start)) // step_labels)
        for i in range(int(start), int(end), step_labels):
            xx = px(i)
            c.create_line(xx, y1, xx, y2, fill=pal["border_soft"], dash=(2, 3))
            anchor = "nw" if i == int(start) else "n"
            c.create_text(xx, y2 + int(6 * S), text=self._kline_label(bars[i].date),
                          anchor=anchor, fill=pal["faint"], font=F.tiny())
        if 0 <= last_i < len(bars) and last_i - last_label_i >= 2:
            c.create_text(px(last_i), y2 + int(6 * S), text=self._kline_label(bars[last_i].date),
                          anchor="ne", fill=pal["faint"], font=F.tiny())
        body_w = max(1.5, (x2 - x1) / span * 0.62)
        for i in range(i0, i1):
            b = bars[i]
            xx = px(i)
            col = pal["pos"] if b.close >= b.open else pal["neg"]
            c.create_line(xx, py(b.high), xx, py(b.low), fill=col, width=1)
            c.create_rectangle(xx - body_w / 2, py(max(b.open, b.close)),
                               xx + body_w / 2, py(min(b.open, b.close)),
                               fill=col if b.close < b.open else "", outline=col)
        for arr, col in ((ma5, pal["warn"]), (ma10, pal["accent"]), (ma20, pal["muted"])):
            coords = []
            for i in range(i0, i1):
                if i < len(arr) and arr[i] is not None:
                    coords += [px(i), py(arr[i])]
            if len(coords) >= 4:
                c.create_line(*coords, fill=col, width=1)
        self._chart_legend(c, x1 + int(7 * S), y1 + int(5 * S),
                           [("MA5", pal["warn"]), ("MA10", pal["accent"]), ("MA20", pal["muted"])])

        # 量柱
        maxv = max([b.volume for b in vis] or [1.0]) or 1.0
        vh = vol_r[3] - vol_r[1]
        bw = max(1.0, body_w)
        for i in range(i0, i1):
            b = bars[i]
            xx = px(i)
            bh = max(1.0, b.volume / maxv * (vh - 4 * S))
            col = pal["pos"] if b.close >= b.open else pal["neg"]
            c.create_rectangle(xx - bw / 2, vol_r[3] - bh, xx + bw / 2, vol_r[3],
                               fill=col, outline="")
        c.create_text(vol_r[0] + int(3 * S), vol_r[1] + int(2 * S), text="成交量",
                      anchor="nw", fill=pal["faint"], font=F.tiny())
        if ind_r:
            xs = [px(i) for i in range(i0, i1)]
            self._draw_indicator(ind_r, [b.close for b in bars], xs,
                                 visible_x=xs, start=0, end=1,
                                 px=lambda x: x, x1=x1, x2=x2, raw_x=True,
                                 highs=[b.high for b in bars],
                                 lows=[b.low for b in bars], start_i=i0)
        self._plot = {"kind": "kline", "empty": False, "x1": x1, "x2": x2,
                      "y1": y1, "y2": y2, "start": start, "end": end,
                      "hi": hi, "lo": lo, "total": n, "bars": bars,
                      "cross_bottom": (ind_r[3] if ind_r else vol_r[3]),
                      "px": px, "py": py, "i0": i0, "i1": i1}

    def _ma(self, bars, n):
        out = []
        s = 0.0
        for i, b in enumerate(bars):
            s += b.close
            if i >= n:
                s -= bars[i - n].close
            out.append(s / n if i >= n - 1 else None)
        return out

    def _chart_legend(self, c, x, y, items, bg=True):
        pal, S, F = self.pal, self.S, self.fonts
        font = F.tiny()
        widths = [font.measure(t) for t, _ in items]
        total = sum(widths) + int(10 * S) * (len(items) - 1) + int(12 * S)
        if bg:
            draw_round_rect(c, x, y, x + total, y + int(17 * S), int(4 * S),
                            pal["panel"], pal["border_soft"], 1)
        tx = x + int(6 * S)
        ty = y + int(8 * S)
        for (text, color), tw in zip(items, widths):
            c.create_text(tx, ty, text=text, anchor="w", fill=color, font=font)
            tx += tw + int(10 * S)

    def _draw_indicator(self, rect, closes, xs, visible_x=None, start=0, end=1,
                        px=None, x1=0, x2=0, raw_x=False, highs=None, lows=None,
                        start_i=0):
        pal, S = self.pal, self.S
        F = self.fonts
        c = self.chart
        x1r, y1r, x2r, y2r = rect
        if not closes:
            return
        if self.sub == "macd":
            dif, dea, hist = macd_values(closes)
            if not dif:
                return
            vals = []
            for arr in (dif, dea, hist):
                vals += arr[start_i:start_i + len(xs)]
            hi, lo = max(vals or [0.0]), min(vals or [0.0])
            if hi <= lo:
                hi, lo = hi + 0.01, lo - 0.01
            pad = (hi - lo) * 0.08
            hi += pad; lo -= pad

            def yy(v):
                return y2r - (v - lo) / (hi - lo) * (y2r - y1r)

            zero = yy(0)
            c.create_line(x1r, zero, x2r, zero, fill=pal["border_soft"])
            for arr, col in ((dif, pal["warn"]), (dea, pal["accent"])):
                coords = []
                for j in range(len(xs)):
                    i = start_i + j
                    if 0 <= i < len(arr):
                        coords += [xs[j], yy(arr[i])]
                if len(coords) >= 4:
                    c.create_line(*coords, fill=col, width=1)
            bw = max(1.0, (x2r - x1r) / max(1, len(xs)) * 0.5)
            for j in range(len(xs)):
                i = start_i + j
                if i >= len(hist):
                    break
                x = xs[j]
                v = hist[i]
                col = pal["pos"] if v >= 0 else pal["neg"]
                c.create_rectangle(x - bw / 2, zero, x + bw / 2, yy(v),
                                   fill=col, outline="")
            self._chart_legend(c, x1r + 3 * S, y1r + int(1 * S),
                               [("MACD", pal["faint"]), ("DIF", pal["warn"]),
                                ("DEA", pal["accent"])], bg=False)
        elif self.sub == "kdj":
            ks, ds, js = kdj_values(closes, highs, lows)
            if not ks:
                return

            def yy(v):
                return y2r - max(0, min(100, v)) / 100.0 * (y2r - y1r)

            for lv in (20, 50, 80):
                c.create_line(x1r, yy(lv), x2r, yy(lv), fill=pal["border_soft"], dash=(2, 3))
            for arr, col in ((ks, pal["warn"]), (ds, pal["accent"]), (js, pal["neg"])):
                coords = []
                for j in range(len(xs)):
                    i = start_i + j
                    if 0 <= i < len(arr):
                        coords += [xs[j], yy(arr[i])]
                if len(coords) >= 4:
                    c.create_line(*coords, fill=col, width=1)
            self._chart_legend(c, x1r + 3 * S, y1r + int(1 * S),
                               [("KDJ", pal["faint"]), ("K", pal["warn"]),
                                ("D", pal["accent"]), ("J", pal["neg"])], bg=False)

    # ---------------- 十字光标 ----------------
    def _on_chart_motion(self, event):
        if self._drag:
            return
        self._cursor = (event.x, event.y)
        if not self._cursor_lock:
            self._draw_cross(event.x, event.y)

    def _on_chart_leave(self, _event):
        if not self._cursor_lock:
            self._cursor = None
            self.chart.delete("cross")

    def _on_chart_press(self, event):
        if self._inner_plot(event.x, event.y):
            self._drag = [event.x, self._view, False]
            self._cursor = (event.x, event.y)
            self._draw_cross(event.x, event.y)

    def _on_chart_drag(self, event):
        if self._drag:
            dx = event.x - self._drag[0]
            if abs(dx) > 3:
                self._drag[2] = True
                start, end = self._drag[1]
                span = end - start
                total = self._plot.get("total") or 1
                shift = -dx / max(1, self._plot["x2"] - self._plot["x1"]) * span
                ns = max(0, min(total - span, start + shift))
                self._view = (ns, ns + span)
                self._draw_all()

    def _on_chart_release(self, event):
        if not self._drag:
            return
        dragged = self._drag[2]
        self._drag = None
        if not dragged:
            if self._cursor_lock:
                self._cursor_lock = False
                self._cursor = None
                self.chart.delete("cross")
            else:
                self._cursor_lock = True
                self._cursor = (event.x, event.y)
                self._draw_cross(event.x, event.y)

    def _on_chart_double(self, _event):
        self._view = None
        self._cursor_lock = False
        self._cursor = None
        self._draw_all()
        return "break"

    def _on_wheel(self, event):
        if not self._plot or self._plot.get("empty"):
            return "break"
        total = self._plot.get("total") or 1
        start, end = self._view or (0, total)
        span = end - start
        factor = 1 / 1.18 if event.delta > 0 else 1.18
        min_span = 15.0 if self._plot["kind"] == "kline" else 30.0
        new_span = max(min_span, min(float(total), span * factor))
        anchor = max(0.0, min(1.0, (event.x - self._plot["x1"]) /
                               max(1, self._plot["x2"] - self._plot["x1"])))
        idx = start + anchor * span
        ns = idx - anchor * new_span
        ns = max(0.0, min(float(total) - new_span, ns))
        self._view = (ns, ns + new_span)
        self._cursor = (event.x, event.y)
        self._draw_all()
        return "break"

    def _inner_plot(self, x, y):
        p = self._plot
        if not p or p.get("empty"):
            return False
        return p["x1"] <= x <= p["x2"] and p["y1"] <= y <= p["y2"] + (
            self.chart.winfo_height() - p["y2"])

    def _draw_cross(self, x, y):
        self.chart.delete("cross")
        p = self._plot
        if not p or p.get("empty"):
            return
        pal, S = self.pal, self.S
        F = self.fonts
        x1, x2, y1, y2 = p["x1"], p["x2"], p["y1"], p["y2"]
        if not (x1 <= x <= x2):
            return
        if not (y1 <= y <= self.chart.winfo_height()):
            return
        c = self.chart
        bottom = p.get("cross_bottom") or p["y2"]
        c.create_line(x, y1, x, bottom, fill=pal["muted"], dash=(3, 3), tags="cross")
        c.create_line(x1, y, x2, y, fill=pal["muted"], dash=(3, 3), tags="cross")
        info = ""
        if p["kind"] == "minute":
            start, end = p["start"], p["end"]
            span = max(1.0, end - start)
            idx_val = start + (x - x1) / max(1, x2 - x1) * span
            # 最近点
            best = None
            for i, xv in enumerate(p["xs"]):
                if best is None or abs(xv - idx_val) < abs(p["xs"][best] - idx_val):
                    best = i
            if best is None:
                return
            i = best
            price = p["pts"][i]
            avg = p["avgs"][i]
            vol = p["vols"][i]
            tt = self._time_of_index(p["xs"][i], p.get("five"))
            day = int(p["xs"][i]) // MINUTES
            bases = p.get("day_bases") or []
            base = bases[day] if day < len(bases) else (p.get("base") or 0)
            pct = (price - base) / base * 100 if base else 0
            info = "%s  价 %s  %+.2f%%  均 %s  量 %s" % (
                tt, D.fmt_price(price, self.q.dp if self.q else 2), pct,
                D.fmt_price(avg, self.q.dp if self.q else 2), _fmt_lots(vol))
        elif p["kind"] == "kline":
            bars = p["bars"]
            start, end = p["start"], p["end"]
            idx = int(start + (x - x1) / max(1, x2 - x1) * (end - start))
            idx = max(0, min(len(bars) - 1, idx))
            b = bars[idx]
            pct = (b.close - b.open) / b.open * 100 if b.open else 0
            info = "%s  开 %s  高 %s  低 %s  收 %s  涨 %.2f%%  量 %s" % (
                b.date, D.fmt_price(b.open, 2), D.fmt_price(b.high, 2),
                D.fmt_price(b.low, 2), D.fmt_price(b.close, 2), pct, _fmt_lots(b.volume))
        if not info:
            return
        tw = F.tiny().measure(info) + int(16 * S)
        tx = x + int(12 * S) if x + tw < x2 else x - tw - int(12 * S)
        ty = y1 + int(6 * S)
        draw_round_rect(c, tx, ty, tx + tw, ty + int(24 * S), int(5 * S),
                        pal["card_bg"], pal["card_border"], 1, tags="cross")
        c.create_text(tx + int(8 * S), ty + int(12 * S), text=info, anchor="w",
                      fill=pal["text_dim"], font=F.tiny(), tags="cross")

    def _time_of_index(self, xv, five=False):
        day = int(xv) // MINUTES
        m = int(xv) % MINUTES
        hm = self._hm(m)
        if five and day < len(self._five_days):
            return self._five_days[day].date[4:6] + "-" + self._five_days[day].date[6:] + " " + hm
        return hm

    # ---------------- 关闭 ----------------
    def close(self):
        self._save_geometry()
        for aid in self._after_ids:
            try:
                self.win.after_cancel(aid)
            except Exception:
                pass
        self._after_ids = []
        super().close()
