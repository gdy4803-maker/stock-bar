# -*- coding: utf-8 -*-
"""窗口与卡片: 悬浮详情卡、设置、自选股管理、持仓设置。"""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import sb_data as D
from sb_theme import (KEY_COLOR, center_on_bar, draw_round_rect,
                      force_topmost)
from sb_widgets import (CanvasButton, Segmented, Slider, Switch, make_entry, setup_ttk)


def _text_run(canvas, x, y, parts, anchor="w"):
    """按顺序绘制多段不同颜色/字体的文字, 返回结束 x。"""
    if anchor == "e":
        total = sum(font.measure(text) for text, font, _color in parts)
        x -= total
    for text, font, color in parts:
        canvas.create_text(x, y, text=text, anchor="w", fill=color, font=font)
        x += font.measure(text)
    return x


def _load_support_photo(app, name="support.png", target=None):
    """加载赞赏码图片; 支持源码目录与 PyInstaller 的 _MEIPASS。"""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "assets", name))
    base = getattr(app, "base_dir", "")
    if base:
        candidates.append(os.path.join(base, "assets", name))
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            photo = tk.PhotoImage(file=path)
        except tk.TclError:
            continue
        if target:
            factor = max(1, int(round(photo.width() / float(target))))
            if factor > 1:
                photo = photo.subsample(factor, factor)
        return photo
    return None


class InfoCard:
    """鼠标悬停时显示的行情详情卡(含当日分时图)。"""

    WIDTH = 350

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=KEY_COLOR)
        try:
            self.win.attributes("-transparentcolor", KEY_COLOR)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(self.win, bg=KEY_COLOR, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.code = None
        self.series = None
        self.state = "idle"
        self.visible = False
        self.w = 0
        self.h = 0

    # ---------- 显示/隐藏 ----------
    def show(self, code):
        if not code or code not in self.app.quotes:
            self.hide()
            return
        if code != self.code:
            self.code = code
            self.series = None
            self.state = "loading"
            self.app.request_minutes(code, self._on_minutes)
        self.visible = True
        self._render()
        self._place()
        self.win.deiconify()
        force_topmost(self.win)
        self.win.lift()

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        self.code = None
        try:
            self.win.withdraw()
        except tk.TclError:
            pass

    def _on_minutes(self, code, series):
        if code != self.code:
            return
        self.series = series
        self.state = "ready" if series else "empty"
        if self.visible:
            self._render()
            self._place()

    def refresh(self):
        if self.visible:
            self._render()
            self._place()

    # ---------- 布局 ----------
    def _holding(self, q):
        if q.is_index:
            return None
        h = self.app.cfg.get("holdings", {}).get(q.code)
        if h and h.get("lots"):
            return h
        return None

    def _plan(self, q):
        S = self.app.scale * 0.875
        pad = int(16 * S)
        rows = 2 if q.is_index else 3
        simple = self.app.cfg.get("card_mode") == "simple"
        p = {"w": int(self.WIDTH * S), "pad": pad, "rows": rows, "S": S, "simple": simple}
        y = pad
        p["head"] = y
        y += int(24 * S)
        y += int(10 * S)
        p["price"] = y
        y += int(32 * S)
        if not simple:
            y += int(6 * S)
            p["legend"] = y
            y += int(14 * S)
            p["chart"] = y
            y += int(64 * S)
            p["time"] = y
            y += int(16 * S)
        y += int(6 * S)
        p["grid"] = y
        y += rows * int(20 * S)
        p["hold"] = bool(self._holding(q))
        if p["hold"]:
            y += int(10 * S)
            p["hdiv"] = y
            y += int(8 * S)
            p["hold1"] = y
            y += int(18 * S)
            p["hold2"] = y
            y += int(18 * S)
        y += int(10 * S)
        p["fdiv"] = y
        y += int(9 * S)
        p["foot"] = y
        y += int(14 * S)
        p["h"] = y + pad
        return p

    # ---------- 绘制 ----------
    def _render(self):
        q = self.app.quotes.get(self.code)
        if not q:
            self.hide()
            return
        pal = self.app.pal
        fonts = self.app.card_fonts
        S = self.app.scale * 0.875
        p = self._plan(q)
        w, h, pad = p["w"], p["h"], p["pad"]
        self.w, self.h = w, h
        c = self.canvas
        c.configure(width=w, height=h)
        c.delete("all")
        draw_round_rect(c, 0.5, 0.5, w - 0.5, h - 0.5, 14 * S,
                        pal["card_bg"], pal["card_border"], 1)

        # 头部: 名称 / 代码 / 市场
        y = p["head"]
        c.create_text(pad, y, text=q.name, anchor="nw", fill=pal["text"],
                      font=fonts.card_title())
        name_w = fonts.card_title().measure(q.name)
        c.create_text(pad + name_w + 7 * S, y + 4 * S, text=D.code_digits(q.code),
                      anchor="nw", fill=pal["muted"], font=fonts.num(-3, False))
        market = D.market_label(q.code, q.is_index)
        badge_w = fonts.tiny().measure(market) + 14 * S
        draw_round_rect(c, w - pad - badge_w, y + 1 * S, w - pad, y + 19 * S, 9 * S,
                        pal["panel_alt"], pal["border_soft"], 1)
        c.create_text(w - pad - badge_w / 2, y + 10 * S, text=market,
                      fill=pal["text_dim"], font=fonts.tiny())

        # 现价 + 涨跌 chip
        y = p["price"]
        color = D.change_color(q.pct, pal)
        price_txt = D.fmt_price(q.price, q.dp)
        c.create_text(pad, y, text=price_txt, anchor="nw", fill=pal["text"],
                      font=fonts.card_price())
        price_w = fonts.card_price().measure(price_txt)
        chip_x = pad + price_w + 12 * S
        chip_w = 118 * S
        chip_h = 26 * S
        soft = pal["pos_soft"] if q.pct > 0 else (pal["neg_soft"] if q.pct < 0 else pal["panel_alt"])
        draw_round_rect(c, chip_x, y + 2 * S, chip_x + chip_w, y + 2 * S + chip_h, 7 * S,
                        soft, "", 1)
        c.create_text(chip_x + 10 * S, y + 2 * S + chip_h / 2, text=D.fmt_change(q.change, q.dp),
                      anchor="w", fill=color, font=fonts.num(-2, True))
        c.create_text(chip_x + chip_w - 10 * S, y + 2 * S + chip_h / 2, text=D.fmt_pct(q.pct),
                      anchor="e", fill=color, font=fonts.num(-2, True))

        # 分时图(简洁模式不显示)
        if not p["simple"]:
            self._draw_chart(c, p, q, color, soft)
            c.create_text(pad, p["time"] + 4 * S, text=self._first_time(), anchor="w",
                          fill=pal["faint"], font=fonts.tiny())
            c.create_text(w - pad, p["time"] + 4 * S, text=self._last_time(), anchor="e",
                          fill=pal["faint"], font=fonts.tiny())

        # 数据网格
        rows = [
            ("今开", D.fmt_price(q.open, q.dp), "最高", D.fmt_price(q.high, q.dp)),
            ("昨收", D.fmt_price(q.prev_close, q.dp), "最低", D.fmt_price(q.low, q.dp)),
        ]
        if not q.is_index:
            rows.append(("成交量", D.fmt_volume(q.volume), "成交额", D.fmt_amount(q.amount)))
        col_w = (w - 2 * pad - int(16 * S)) / 2
        for i, (k1, v1, k2, v2) in enumerate(rows):
            yy = p["grid"] + i * 20 * S
            c.create_text(pad, yy, text=k1, anchor="nw", fill=pal["muted"], font=fonts.label())
            c.create_text(pad + col_w, yy, text=v1, anchor="ne", fill=pal["text_dim"],
                          font=fonts.num(-2, False))
            x2 = pad + col_w + 16 * S
            c.create_text(x2, yy, text=k2, anchor="nw", fill=pal["muted"], font=fonts.label())
            c.create_text(w - pad, yy, text=v2, anchor="ne", fill=pal["text_dim"],
                          font=fonts.num(-2, False))

        # 持仓
        hold = self._holding(q)
        if hold:
            c.create_line(pad, p["hdiv"], w - pad, p["hdiv"], fill=pal["border_soft"])
            lots = int(hold.get("lots", 0))
            shares = lots * 100
            mv = q.price * shares
            dpnl = (q.price - q.prev_close) * shares
            left = [("持仓 ", fonts.label(), pal["muted"]),
                    (f"{lots}手", fonts.label(), pal["text_dim"])]
            if hold.get("cost"):
                left += [("    成本 ", fonts.label(), pal["muted"]),
                         (D.fmt_price(float(hold["cost"]), q.dp), fonts.num(-2, False),
                          pal["text_dim"])]
            _text_run(c, pad, p["hold1"], left)
            _text_run(c, w - pad, p["hold1"],
                      [("市值 ", fonts.label(), pal["muted"]),
                       (D.fmt_money(mv), fonts.num(-2, False), pal["text_dim"])],
                      anchor="e")
            _text_run(c, pad, p["hold2"],
                      [("当日 ", fonts.label(), pal["muted"]),
                       (D.fmt_money_signed(dpnl), fonts.num(-2, True), D.change_color(dpnl, pal))])
            if hold.get("cost"):
                cp = (q.price - float(hold["cost"])) * shares
                _text_run(c, w - pad, p["hold2"],
                          [("累计 ", fonts.label(), pal["muted"]),
                           (D.fmt_money_signed(cp), fonts.num(-2, True),
                            D.change_color(cp, pal))],
                          anchor="e")

        # 页脚
        c.create_line(pad, p["fdiv"], w - pad, p["fdiv"], fill=pal["border_soft"])
        state = D.session_state(q.time)
        y = p["foot"] + 7 * S
        c.create_text(pad, y, text=D.fmt_quote_time(q.time), anchor="w",
                      fill=pal["faint"], font=fonts.tiny())
        if state:
            tx = pad + fonts.tiny().measure(D.fmt_quote_time(q.time)) + 8 * S
            c.create_oval(tx, y - 2.5 * S, tx + 5 * S, y + 2.5 * S,
                          fill=pal["accent"] if state == "交易中" else pal["faint"], outline="")
            c.create_text(tx + 8 * S, y, text=state, anchor="w", fill=pal["faint"],
                          font=fonts.tiny())
        c.create_text(w - pad, y, text="腾讯行情 · 仅供参考", anchor="e",
                      fill=pal["faint"], font=fonts.tiny())

    def _first_time(self):
        if self.series and self.series.points:
            return self.series.points[0][0]
        return ""

    def _last_time(self):
        if self.series and self.series.points:
            return self.series.points[-1][0]
        return ""

    def _draw_chart(self, c, p, q, color, soft):
        pal = self.app.pal
        fonts = self.app.card_fonts
        S = self.app.scale * 0.875
        pad = p["pad"]
        x1, x2 = pad, p["w"] - pad
        y1, y2 = p["chart"], p["chart"] + 64 * S
        # 图例
        c.create_text(x1, p["legend"], text="分时", anchor="nw", fill=pal["faint"],
                      font=fonts.tiny())
        if q.prev_close:
            c.create_text(x2, p["legend"], text="昨收 " + D.fmt_price(q.prev_close, q.dp),
                          anchor="ne", fill=pal["faint"], font=fonts.tiny())
        c.create_line(x1, y2, x2, y2, fill=pal["border_soft"])
        c.create_line(x1, y1, x1, y2, fill=pal["border_soft"])

        series = self.series
        if not series or not series.points:
            msg = "分时数据加载中…" if self.state == "loading" else "暂无分时数据"
            c.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=msg, fill=pal["faint"],
                          font=fonts.label())
            return

        pts = series.points
        if len(pts) > 180:
            step = len(pts) / 180.0
            pts = [pts[min(len(pts) - 1, int(i * step))] for i in range(180)]
        prices = [v for _t, v in pts]
        prev = q.prev_close or series.prev_close or prices[0]
        lo = min(min(prices), prev)
        hi = max(max(prices), prev)
        if hi - lo < 1e-9:
            hi = lo + 0.01
            lo -= 0.01
        span = (hi - lo) * 1.12
        mid = (hi + lo) / 2
        lo, hi = mid - span / 2, mid + span / 2
        n = len(pts)
        coords = []
        for i, (_t, v) in enumerate(pts):
            px = x1 + (x2 - x1) * (i / max(1, n - 1))
            py = y2 - (v - lo) / (hi - lo) * (y2 - y1)
            coords += [px, py]
        area = coords + [x2, y2, x1, y2]
        c.create_polygon(*area, fill=soft, outline="")
        if q.prev_close:
            py = y2 - (q.prev_close - lo) / (hi - lo) * (y2 - y1)
            c.create_line(x1, py, x2, py, fill=pal["border"], dash=(3, 3))
        c.create_line(*coords, fill=color, width=max(1, int(1.6 * S)))
        c.create_oval(coords[-2] - 2.6 * S, coords[-1] - 2.6 * S,
                      coords[-2] + 2.6 * S, coords[-1] + 2.6 * S,
                      fill=color, outline=pal["card_bg"])

    def _place(self):
        if not self.w:
            return
        bx, by, bw, bh = self.app.bar_rect()
        scr = self.app.screen
        w, h = self.w, self.h
        x = bx + (bw - w) // 2
        y = by + bh + 8
        if y + h > scr.vy + scr.vh - 8:
            y = by - h - 8
        x, y = scr.clamp(x, y, w, h, pad=8)
        self.win.geometry(f"{w}x{h}+{x}+{y}")


# ---------------- 对话框基类 ----------------
class BaseDialog:
    key = ""

    def __init__(self, app, title, width, height):
        self.app = app
        self.pal = app.pal
        self.fonts = app.ui_fonts
        self.S = app.scale
        self.title_text = title
        self._move_origin = None
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.title(title)
        self.win.overrideredirect(True)
        self.win.configure(bg=self.pal["border"])
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        w, h = int(width * self.S), int(height * self.S)
        bx, by, bw, bh = app.bar_rect()
        center_on_bar(self.win, bx, by, bw, bh, w, h, app.screen)
        self.win.bind("<Escape>", lambda e: self.close())

        self.shell = tk.Frame(self.win, bg=self.pal["panel"])
        self.shell.pack(fill="both", expand=True, padx=1, pady=1)
        self._build_titlebar()
        self.body = tk.Frame(self.shell, bg=self.pal["panel"])
        self.body.pack(fill="both", expand=True)

        self.win.deiconify()
        self.win.update_idletasks()
        self._round_corners()
        force_topmost(self.win)
        self.win.lift()
        self.win.focus_force()
        if self.key:
            app._dialogs[self.key] = self

    def _round_corners(self):
        try:
            w = self.win.winfo_width()
            h = self.win.winfo_height()
            r = int(14 * self.S)
            gdi = ctypes.windll.gdi32
            gdi.CreateRoundRectRgn.restype = ctypes.c_void_p
            gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 4 + [ctypes.c_int, ctypes.c_int]
            rgn = gdi.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r)
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            hwnd = user32.GetAncestor(self.win.winfo_id(), 2) or self.win.winfo_id()
            user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
            user32.SetWindowRgn(ctypes.c_void_p(hwnd), ctypes.c_void_p(rgn), True)
        except Exception:
            pass

    def _build_titlebar(self):
        pal, F, S = self.pal, self.fonts, self.S
        tb = tk.Frame(self.shell, bg=pal["panel_alt"], height=int(38 * S))
        tb.pack(fill="x")
        tb.pack_propagate(False)
        self.titlebar = tb
        dot = tk.Label(tb, text="●", bg=pal["panel_alt"], fg=pal["accent"],
                       font=F.ui(-4))
        dot.pack(side="left", padx=(14, 6))
        label = tk.Label(tb, text=self.title_text, bg=pal["panel_alt"], fg=pal["text_dim"],
                         font=F.ui(-2, True))
        label.pack(side="left")
        close = tk.Label(tb, text="✕", bg=pal["panel_alt"], fg=pal["faint"],
                         font=F.ui(0), width=3, cursor="hand2")
        close.pack(side="right", fill="y")
        close.bind("<Enter>", lambda e: close.config(bg=pal["pos_soft"], fg=pal["pos"]))
        close.bind("<Leave>", lambda e: close.config(bg=pal["panel_alt"], fg=pal["faint"]))
        close.bind("<Button-1>", lambda e: self.close())
        for w in (tb, dot, label):
            w.bind("<Button-1>", self._tb_press)
            w.bind("<B1-Motion>", self._tb_drag)
        tk.Frame(self.shell, bg=pal["border"], height=1).pack(fill="x")

    def _tb_press(self, event):
        self._move_origin = (event.x_root, event.y_root, self.win.winfo_x(), self.win.winfo_y())

    def _tb_drag(self, event):
        if not self._move_origin:
            return
        dx = event.x_root - self._move_origin[0]
        dy = event.y_root - self._move_origin[1]
        x = self._move_origin[2] + dx
        y = self._move_origin[3] + dy
        x, y = self.app.screen.clamp(x, y, self.win.winfo_width(), self.win.winfo_height(), 4)
        self.win.geometry(f"+{x}+{y}")

    def raise_(self):
        try:
            self.win.deiconify()
            force_topmost(self.win)
            self.win.lift()
            self.win.focus_force()
        except tk.TclError:
            pass

    def close(self):
        if self.key:
            self.app._dialogs.pop(self.key, None)
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    # ---- 通用部件 ----
    def header(self, title, subtitle):
        head = tk.Frame(self.body, bg=self.pal["panel"])
        head.pack(fill="x", padx=22, pady=(16, 10))
        tk.Label(head, text=title, bg=self.pal["panel"], fg=self.pal["text"],
                 font=self.fonts.dialog_title(), anchor="w").pack(side="left")
        tk.Label(head, text=subtitle, bg=self.pal["panel"], fg=self.pal["muted"],
                 font=self.fonts.label(), anchor="w").pack(side="left", padx=(12, 0), pady=(6, 0))
        ver = tk.Label(head, text="v" + self.app.version, bg=self.pal["panel"],
                       fg=self.pal["faint"], font=self.fonts.tiny())
        ver.pack(side="right", pady=(6, 0))
        tk.Frame(self.body, bg=self.pal["border"], height=1).pack(fill="x")

    def section(self, parent, title):
        outer = tk.Frame(parent, bg=self.pal["panel_alt"],
                         highlightbackground=self.pal["border"], highlightthickness=1)
        outer.pack(fill="x", pady=(0, 10))
        tk.Label(outer, text=title, bg=self.pal["panel_alt"], fg=self.pal["muted"],
                 font=self.fonts.label(), anchor="w").pack(fill="x", padx=14, pady=(9, 2))
        inner = tk.Frame(outer, bg=self.pal["panel_alt"])
        inner.pack(fill="x", padx=14, pady=(0, 10))
        return inner

    def row(self, parent, label, label_width=84):
        r = tk.Frame(parent, bg=self.pal["panel_alt"])
        r.pack(fill="x", pady=3)
        holder = tk.Frame(r, bg=self.pal["panel_alt"], width=int(label_width * self.S))
        holder.pack(side="left", fill="y")
        holder.pack_propagate(False)
        tk.Label(holder, text=label, bg=self.pal["panel_alt"], fg=self.pal["text_dim"],
                 font=self.fonts.label(), anchor="w").pack(side="left", pady=5)
        return r


# ---------------- 设置 ----------------
class SettingsWindow(BaseDialog):
    key = "settings"

    def __init__(self, app):
        super().__init__(app, "设置", 840, 750)
        self._build_footer()
        self.header("设置", "调整外观与行情，改动立即生效")
        body = tk.Frame(self.body, bg=self.pal["panel"])
        body.pack(fill="both", expand=True, padx=18, pady=(12, 6))
        body.columnconfigure(0, weight=1, uniform="col")
        body.columnconfigure(1, weight=1, uniform="col")
        left = tk.Frame(body, bg=self.pal["panel"])
        left.grid(row=0, column=0, sticky="new", padx=(0, 7))
        right = tk.Frame(body, bg=self.pal["panel"])
        right.grid(row=0, column=1, sticky="new", padx=(7, 0))

        self._build_appearance(left)
        self._build_window(left)
        self._build_stocks(left)
        self._build_support(left)
        self._build_market(right)
        self._build_shortcuts(right)
        self._build_tips(right)

    # ---- 外观 ----
    def _build_appearance(self, parent):
        pal, S = self.pal, self.S
        sec = self.section(parent, "外观")
        r = self.row(sec, "显示样式")
        Segmented(r, [("pill", "胶囊"), ("compact", "紧凑")], self.app.cfg["appearance"],
                  self._set_appearance, pal, self.fonts).pack(side="left")
        r = self.row(sec, "界面配色")
        Segmented(r, [("dark", "深色"), ("light", "浅色")], self.app.cfg["theme"],
                  self._set_theme, pal, self.fonts).pack(side="left")
        r = self.row(sec, "字号")
        Segmented(r, [(10, "10"), (12, "12"), (14, "14"), (16, "16")],
                  int(self.app.cfg["font_size"]), self._set_font, pal, self.fonts,
                  min_item=40).pack(side="left")
        self.show_name_sw = Switch(r, bool(self.app.cfg["show_name"]),
                                   self._set_show_name, pal, S)
        self.show_name_sw.pack(side="right", padx=(0, 2))
        tk.Label(r, text="显示名称", bg=pal["panel_alt"], fg=pal["text_dim"],
                 font=self.fonts.label()).pack(side="right", padx=(0, 8))
        r = self.row(sec, "文字不透明度")
        Slider(r, int(round(float(self.app.cfg["opacity"]) * 100)), 55, 100,
               self._set_opacity, pal, self.fonts, scale=S, width=236).pack(side="left", pady=2)
        r = self.row(sec, "背景不透明度")
        Slider(r, int(round(float(self.app.cfg.get("bg_opacity", 1.0)) * 100)), 0, 100,
               self._set_bg_opacity, pal, self.fonts, scale=S, width=236).pack(side="left", pady=2)

    def _build_window(self, parent):
        pal = self.pal
        sec = self.section(parent, "窗口")
        r = self.row(sec, "窗口置顶")
        Switch(r, bool(self.app.cfg["topmost"]), self._set_topmost, pal, self.S).pack(side="left", pady=2)
        tk.Label(r, text="让行情条始终浮在其他窗口之上", bg=pal["panel_alt"],
                 fg=pal["faint"], font=self.fonts.tiny()).pack(side="left", padx=(10, 0))
        r = self.row(sec, "开机启动")
        self.autostart_sw = Switch(r, bool(self.app.is_autostart()), self._set_autostart,
                                   pal, self.S)
        self.autostart_sw.pack(side="left", pady=2)
        self.autostart_hint = tk.Label(r, text="登录 Windows 后自动运行", bg=pal["panel_alt"],
                                       fg=pal["faint"], font=self.fonts.tiny())
        self.autostart_hint.pack(side="left", padx=(10, 0))

    def _build_stocks(self, parent):
        pal = self.pal
        sec = self.section(parent, "自选与持仓")
        r = tk.Frame(sec, bg=pal["panel_alt"])
        r.pack(fill="x", pady=2)
        n = len(self.app.cfg.get("stocks", []))
        tk.Label(r, text=f"当前 {n} 只自选", bg=pal["panel_alt"], fg=pal["text_dim"],
                 font=self.fonts.label()).pack(side="left")
        CanvasButton(r, "持仓设置…", self.app.open_holdings, pal, self.fonts,
                     kind="ghost", width=92, height=30, scale=self.S,
                     bg=pal["panel_alt"]).pack(side="right", padx=(8, 0))
        CanvasButton(r, "管理自选股…", self.app.open_manage, pal, self.fonts,
                     kind="primary", width=104, height=30, scale=self.S,
                     bg=pal["panel_alt"]).pack(side="right")

    def _build_support(self, parent):
        pal, S = self.pal, self.S
        sec = self.section(parent, "支持开发者")
        row = tk.Frame(sec, bg=pal["panel_alt"])
        row.pack(fill="x", pady=1)
        size = int(84 * S)
        photo = _load_support_photo(self.app, "support.png", int(78 * S))
        qr = tk.Canvas(row, width=size, height=size, bg=pal["panel_alt"],
                       highlightthickness=0, bd=0, cursor="hand2")
        qr.pack(side="left")
        qr.create_rectangle(0, 0, size, size, fill="#FFFFFF",
                            outline=pal["border"], width=1)
        if photo is not None:
            qr.create_image(size / 2, size / 2, image=photo)
            qr.image = photo
        else:
            qr.create_text(size / 2, size / 2, text="♥", fill=pal["warn"],
                           font=self.fonts.label())
        txt = tk.Frame(row, bg=pal["panel_alt"])
        txt.pack(side="left", padx=(12, 0), fill="x", expand=True)
        for text, delta, color in (
                ("如果这个小工具帮到了你", -3, pal["muted"]),
                ("欢迎扫码请我喝咖啡 ♥", -2, pal["text_dim"]),
                ("点击二维码放大", -3, pal["accent"])):
            lab = tk.Label(txt, text=text, bg=pal["panel_alt"], fg=color,
                           font=self.fonts.ui(delta), anchor="w", cursor="hand2")
            lab.pack(anchor="w", pady=(1, 0))
            lab.bind("<Button-1>", lambda e: self._open_support())
        qr.bind("<Button-1>", lambda e: self._open_support())
        txt.bind("<Button-1>", lambda e: self._open_support())

    def _open_support(self):
        existing = self.app._dialogs.get("support")
        if existing is not None:
            try:
                existing.raise_()
                return
            except Exception:
                self.app._dialogs.pop("support", None)
        SupportDialog(self.app)

    # ---- 行情 ----
    def _build_market(self, parent):
        pal = self.pal
        sec = self.section(parent, "行情")
        r = self.row(sec, "刷新间隔")
        Segmented(r, [(v, f"{v:g}s") for v in self.app.INTERVALS],
                  float(self.app.cfg["interval_seconds"]), self._set_interval,
                  pal, self.fonts, item_pad=9, min_item=38).pack(side="left")
        r = self.row(sec, "右侧指数")
        self.index_var = tk.StringVar(value=self.app.cfg["index"])
        entry = make_entry(r, pal, self.fonts, width=10, textvariable=self.index_var,
                           numeric=True)
        entry.pack(side="left", ipady=4)
        entry.bind("<Return>", lambda e: self._apply_index())
        entry.bind("<FocusOut>", lambda e: self._apply_index())
        CanvasButton(r, "应用", self._apply_index, pal, self.fonts, kind="ghost",
                     width=52, height=30, scale=self.S,
                     bg=pal["panel_alt"]).pack(side="left", padx=(8, 0))
        self.index_hint = tk.Label(r, text="", bg=pal["panel_alt"], fg=pal["faint"],
                                   font=self.fonts.tiny())
        self.index_hint.pack(side="left", padx=(10, 0))
        self._refresh_index_hint()
        r = self.row(sec, "涨跌配色")
        Segmented(r, [(True, "红涨绿跌"), (False, "绿涨红跌")],
                  bool(self.app.cfg["cn_colors"]), self._set_cn_colors,
                  pal, self.fonts).pack(side="left")
        r = self.row(sec, "详情卡")
        Segmented(r, [("simple", "简洁"), ("detail", "详细")],
                  self.app.cfg.get("card_mode", "detail"), self._set_card_mode,
                  pal, self.fonts).pack(side="left")
        tk.Label(r, text="简洁 = 不含分时图", bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(side="left", padx=(10, 0))

    def _build_shortcuts(self, parent):
        pal = self.pal
        sec = self.section(parent, "快捷键")
        items = [("悬停", "查看详情与分时"), ("左键", "切换下一只自选"),
                 ("→ / ←", "切换自选"), ("右键", "打开菜单"),
                 ("Ctrl + ,", "设置"), ("F5", "重新加载配置")]
        for k, v in items:
            r = tk.Frame(sec, bg=pal["panel_alt"])
            r.pack(fill="x", pady=2)
            tk.Label(r, text=k, width=10, anchor="w", bg=pal["panel_alt"],
                     fg=pal["muted"], font=self.fonts.num(-3, False)).pack(side="left")
            tk.Label(r, text=v, anchor="w", bg=pal["panel_alt"],
                     fg=pal["text_dim"], font=self.fonts.label()).pack(side="left")

    def _build_tips(self, parent):
        pal = self.pal
        sec = self.section(parent, "小提示")
        for text in ("按住左键拖动，可放到任务栏上悬浮",
                     "悬停片刻显示分时与持仓详情",
                     "背景不透明度调到 0% 只显示文字"):
            r = tk.Frame(sec, bg=pal["panel_alt"])
            r.pack(fill="x", pady=2)
            tk.Label(r, text="·", bg=pal["panel_alt"], fg=pal["accent"],
                     font=self.fonts.label()).pack(side="left")
            tk.Label(r, text=text, bg=pal["panel_alt"], fg=pal["text_dim"],
                     font=self.fonts.label()).pack(side="left", padx=(6, 0))

    def _build_footer(self):
        pal = self.pal
        foot = tk.Frame(self.body, bg=pal["panel"])
        foot.pack(fill="x", side="bottom", padx=18, pady=(4, 12))
        CanvasButton(foot, "恢复默认", self._reset, pal, self.fonts, kind="quiet",
                     width=82, height=32, scale=self.S).pack(side="left")
        tk.Label(foot, text="配置保存在程序同目录 config.json", bg=pal["panel"],
                 fg=pal["faint"], font=self.fonts.tiny()).pack(side="left", padx=(12, 0))
        CanvasButton(foot, "完成", self.close, pal, self.fonts, kind="primary",
                     width=82, height=32, scale=self.S).pack(side="right")

    # ---- 回调 ----
    def _apply(self):
        self.app.apply_settings()

    def _set_appearance(self, value):
        self.app.cfg["appearance"] = value
        self._apply()

    def _set_theme(self, value):
        self.app.cfg["theme"] = value
        self._apply()
        self.win.after(30, self._reopen)

    def _reopen(self):
        try:
            self.app._dialogs.pop(self.key, None)
            self.win.destroy()
        except tk.TclError:
            pass
        self.app.open_settings()

    def _set_font(self, value):
        self.app.cfg["font_size"] = int(value)
        self._apply()

    def _set_show_name(self, value):
        self.app.cfg["show_name"] = bool(value)
        self._apply()

    def _set_opacity(self, value):
        self.app.cfg["opacity"] = max(0.55, min(1.0, value / 100.0))
        self._apply()

    def _set_bg_opacity(self, value):
        self.app.cfg["bg_opacity"] = max(0.0, min(1.0, value / 100.0))
        self._apply()

    def _set_card_mode(self, value):
        self.app.cfg["card_mode"] = value
        self._apply()

    def _set_topmost(self, value):
        self.app.cfg["topmost"] = bool(value)
        self._apply()

    def _set_autostart(self, value):
        ok = self.app.set_autostart(bool(value))
        actual = self.app.is_autostart()
        if hasattr(self, "autostart_sw"):
            self.autostart_sw.set(actual)
        if hasattr(self, "autostart_hint"):
            if ok and actual:
                self.autostart_hint.config(text="已开启", fg=self.pal["text_dim"])
            elif not value and not actual:
                self.autostart_hint.config(text="已关闭", fg=self.pal["faint"])
            elif value and not actual:
                self.autostart_hint.config(text="写入失败：系统权限限制", fg=self.pal["warn"])
            else:
                self.autostart_hint.config(text="取消失败：请检查启动项权限", fg=self.pal["warn"])

    def _set_interval(self, value):
        self.app.cfg["interval_seconds"] = float(value)
        self._apply()

    def _set_cn_colors(self, value):
        self.app.cfg["cn_colors"] = bool(value)
        self._apply()

    def _apply_index(self):
        if not self.win.winfo_exists():
            return
        raw = self.index_var.get().strip()
        code = D.normalize_code(raw, as_index=True) or "sh000001"
        if code != self.app.cfg["index"]:
            self.app.cfg["index"] = code
            self._apply()
            self.index_var.set(code)
            self.win.after(700, self._refresh_index_hint)
        else:
            self._refresh_index_hint()

    def _refresh_index_hint(self):
        q = self.app.quotes.get(self.app.cfg["index"])
        self.index_hint.config(text=(q.name if q else "等待行情…"))

    def _reset(self):
        if not messagebox.askokcancel("恢复默认", "将重置外观与行情设置（自选股与持仓保留），继续？",
                                      parent=self.win):
            return
        self.app.reset_appearance()
        self._reopen()


# ---------------- 支持开发者 ----------------
class SupportDialog(BaseDialog):
    key = "support"

    def __init__(self, app):
        super().__init__(app, "支持开发者", 430, 600)
        self.header("感谢支持", "如果这个小工具帮到了你，欢迎扫码")
        pal = self.pal
        body = tk.Frame(self.body, bg=pal["panel"])
        body.pack(fill="both", expand=True, pady=(6, 0))
        card = tk.Frame(body, bg="#FFFFFF", highlightbackground=pal["border"],
                        highlightthickness=1)
        card.pack(pady=(8, 10))
        photo = _load_support_photo(app, "support.png")
        if photo is not None:
            label = tk.Label(card, image=photo, bg="#FFFFFF", bd=0)
            label.image = photo
            label.pack(padx=12, pady=12)
        else:
            tk.Label(card, text="赞赏码图片缺失\n请将 assets 文件夹放在程序旁边",
                     bg="#FFFFFF", fg="#666666", font=self.fonts.label(),
                     justify="center").pack(padx=28, pady=60)
        tk.Label(body, text="微信扫码 · 韩 的赞赏码", bg=pal["panel"], fg=pal["text_dim"],
                 font=self.fonts.label()).pack()
        tk.Label(body, text="你的支持是持续更新的动力 ♥", bg=pal["panel"],
                 fg=pal["muted"], font=self.fonts.tiny()).pack(pady=(4, 0))

        foot = tk.Frame(self.body, bg=pal["panel"])
        foot.pack(fill="x", side="bottom", padx=18, pady=(2, 12))
        CanvasButton(foot, "关闭", self.close, pal, self.fonts, kind="primary",
                     width=90, height=34, scale=self.S).pack(side="right")


# ---------------- 自选股管理 ----------------
class ManageStocksWindow(BaseDialog):
    key = "manage"

    def __init__(self, app):
        super().__init__(app, "管理自选股", 860, 650)
        self.header("管理自选股", "搜索添加、排序与移除，保存后立即生效")
        self.results: list[D.Symbol] = []
        self.pending: list[D.Symbol] = []
        self._query_id = 0
        self._debounce = None
        self._q: queue.Queue = queue.Queue()
        self._load_pending()
        self._build()
        self._poll_queue()
        self._tick()
        self.entry.focus_set()

    def _load_pending(self):
        for code in self.app.cfg.get("stocks", []):
            q = self.app.quotes.get(code)
            name = q.name if q else ""
            self.pending.append(D.Symbol(code=code, name=name, kind=""))

    def _build(self):
        pal = self.pal
        foot = tk.Frame(self.body, bg=pal["panel"])
        foot.pack(fill="x", side="bottom", padx=18, pady=(2, 12))
        self.status = tk.Label(foot, text="", bg=pal["panel"], fg=pal["muted"],
                               font=self.fonts.label(), anchor="w")
        self.status.pack(side="left")
        CanvasButton(foot, "保存并应用", self._save, pal, self.fonts, kind="primary",
                     width=108, height=34, scale=self.S).pack(side="right")
        CanvasButton(foot, "取消", self.close, pal, self.fonts, kind="ghost",
                     width=76, height=34, scale=self.S).pack(side="right", padx=(0, 8))

        body = tk.Frame(self.body, bg=pal["panel"])
        body.pack(fill="both", expand=True, padx=18, pady=(10, 6))

        search = tk.Frame(body, bg=pal["panel_alt"], highlightbackground=pal["border"],
                          highlightthickness=1)
        search.pack(fill="x")
        tk.Label(search, text="搜索", bg=pal["panel_alt"], fg=pal["muted"],
                 font=self.fonts.label()).pack(side="left", padx=(14, 8), pady=10)
        self.entry = make_entry(search, pal, self.fonts, width=34)
        self.entry.pack(side="left", ipady=5, pady=8)
        self.entry.bind("<Return>", lambda e: self._search_now())
        self.entry.bind("<KeyRelease>", self._on_key)
        CanvasButton(search, "搜索", self._search_now, pal, self.fonts, kind="primary",
                     width=66, height=30, scale=self.S, bg=pal["panel_alt"]).pack(
            side="left", padx=8, pady=7)
        tk.Label(search, text="支持名称 / 代码 / 拼音首字母，如 茅台、510300", bg=pal["panel_alt"],
                 fg=pal["faint"], font=self.fonts.tiny()).pack(side="left", padx=(2, 12))

        lists = tk.Frame(body, bg=pal["panel"])
        lists.pack(fill="both", expand=True, pady=(10, 0))
        lists.columnconfigure(0, weight=1, uniform="list")
        lists.columnconfigure(2, weight=1, uniform="list")
        lists.rowconfigure(0, weight=1)

        left = tk.Frame(lists, bg=pal["panel_alt"], highlightbackground=pal["border"],
                        highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew")
        tk.Label(left, text="搜索结果", bg=pal["panel_alt"], fg=pal["muted"],
                 font=self.fonts.label()).pack(anchor="w", padx=12, pady=(9, 4))
        self.result_tree = self._make_tree(
            left, [("name", "名称", 146, "w"), ("code", "代码", 84, "w"),
                   ("kind", "类型", 62, "w")])
        self.result_tree.bind("<Double-Button-1>", lambda e: self._add_selected())
        self.result_hint = tk.Label(
            left, text="输入名称或代码后回车搜索\n例如：茅台、510300、科创50",
            bg=self.pal["panel_soft"], fg=self.pal["faint"], justify="center",
            font=self.fonts.label())
        self.result_hint.place(relx=0.5, rely=0.62, anchor="center")

        middle = tk.Frame(lists, bg=pal["panel"], width=int(96 * self.S))
        middle.grid(row=0, column=1, sticky="ns")
        middle.pack_propagate(False)
        CanvasButton(middle, "添加 →", self._add_selected, pal, self.fonts, kind="primary",
                     width=88, height=32, scale=self.S).pack(pady=(40, 6))
        CanvasButton(middle, "← 移除", self._remove_selected, pal, self.fonts,
                     width=88, height=32, scale=self.S).pack(pady=6)
        CanvasButton(middle, "↑ 上移", lambda: self._move(-1), pal, self.fonts,
                     width=88, height=32, scale=self.S).pack(pady=6)
        CanvasButton(middle, "↓ 下移", lambda: self._move(1), pal, self.fonts,
                     width=88, height=32, scale=self.S).pack(pady=6)
        tk.Label(middle, text="顺序即\n切换顺序", bg=pal["panel"], fg=pal["faint"],
                 font=self.fonts.tiny(), justify="center").pack(pady=(10, 0))

        right = tk.Frame(lists, bg=pal["panel_alt"], highlightbackground=pal["border"],
                         highlightthickness=1)
        right.grid(row=0, column=2, sticky="nsew")
        self._count_label = tk.Label(right, text="", bg=pal["panel_alt"], fg=pal["muted"],
                                     font=self.fonts.label())
        self._count_label.pack(anchor="w", padx=12, pady=(9, 4))
        self.current_tree = self._make_tree(
            right, [("idx", "#", 34, "center"), ("name", "名称", 128, "w"),
                    ("code", "代码", 74, "w"), ("pct", "涨跌幅", 76, "e")])
        self.current_tree.bind("<Double-Button-1>", lambda e: self._remove_selected())
        self._refresh_pending()

    def _make_tree(self, parent, columns):
        pal = self.pal
        setup_ttk(self.win, pal, self.fonts, self.S)
        tree = ttk.Treeview(parent, columns=[c[0] for c in columns], show="headings",
                            style="SB.Treeview", height=10)
        for cid, label, width, anchor in columns:
            tree.heading(cid, text=label, anchor=anchor)
            tree.column(cid, width=int(width * self.S), anchor=anchor, stretch=True)
        tree.tag_configure("up", foreground=pal["pos"])
        tree.tag_configure("down", foreground=pal["neg"])
        tree.tag_configure("flat", foreground=pal["text"])
        tree.tag_configure("muted", foreground=pal["faint"])
        tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        return tree

    # ---- 搜索 ----
    def _on_key(self, _event):
        if self._debounce:
            try:
                self.win.after_cancel(self._debounce)
            except tk.TclError:
                pass
        self._debounce = self.win.after(420, self._search_now)

    def _search_now(self):
        kw = self.entry.get().strip()
        if self._debounce:
            try:
                self.win.after_cancel(self._debounce)
            except tk.TclError:
                pass
            self._debounce = None
        if not kw:
            self.results = []
            self._refresh_results()
            return
        self._query_id += 1
        qid = self._query_id
        self.status.config(text="搜索中…")
        threading.Thread(target=self._search_worker, args=(qid, kw), daemon=True).start()

    def _search_worker(self, qid, kw):
        try:
            self._q.put(("ok", qid, D.search_symbols(kw), kw))
        except Exception as exc:
            self._q.put(("err", qid, str(exc), kw))

    def _poll_queue(self):
        try:
            while True:
                kind, qid, payload, kw = self._q.get_nowait()
                if qid != self._query_id:
                    continue
                if kind == "ok":
                    self.results = payload
                    self._refresh_results()
                    self.status.config(text=("找到 %d 个结果" % len(payload)) if payload
                                       else "没有找到「%s」" % kw)
                else:
                    self.results = []
                    self._refresh_results()
                    self.status.config(text="搜索失败，请检查网络")
        except queue.Empty:
            pass
        try:
            self.win.after(120, self._poll_queue)
        except tk.TclError:
            pass

    def _refresh_results(self):
        tree = self.result_tree
        tree.delete(*tree.get_children())
        for i, sym in enumerate(self.results):
            tree.insert("", "end", iid=str(i),
                        values=(sym.name, D.code_digits(sym.code), sym.kind_label))
        if self.results:
            self.result_hint.place_forget()
        else:
            self.result_hint.place(relx=0.5, rely=0.62, anchor="center")

    def _refresh_pending(self):
        tree = self.current_tree
        tree.delete(*tree.get_children())
        for i, sym in enumerate(self.pending):
            q = self.app.quotes.get(sym.code)
            name = (q.name if q else sym.name) or sym.code
            pct = q.pct if q else None
            tag = "flat"
            if pct is not None and pct > 0:
                tag = "up"
            elif pct is not None and pct < 0:
                tag = "down"
            tree.insert("", "end", iid=sym.code,
                        values=(i + 1, name, D.code_digits(sym.code), D.fmt_pct(pct)),
                        tags=(tag,))
        self._count_label.config(text="当前自选 · %d 只" % len(self.pending))
        if not self.pending:
            self.status.config(text="还没有自选股，搜索后点「添加 →」")

    def _selected_result(self):
        sel = self.result_tree.selection()
        if not sel:
            return None
        try:
            return self.results[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def _selected_pending_index(self):
        sel = self.current_tree.selection()
        if not sel:
            return None
        for i, sym in enumerate(self.pending):
            if sym.code == sel[0]:
                return i
        return None

    def _add_selected(self):
        sym = self._selected_result()
        if not sym:
            return
        if any(s.code == sym.code for s in self.pending):
            self.status.config(text="%s 已在自选中" % sym.name)
            return
        self.pending.append(sym)
        self._refresh_pending()
        self.current_tree.selection_set(sym.code)
        self.current_tree.see(sym.code)
        self.status.config(text="已添加 %s" % sym.name)

    def _remove_selected(self):
        idx = self._selected_pending_index()
        if idx is None:
            return
        sym = self.pending.pop(idx)
        self._refresh_pending()
        if self.pending:
            nxt = self.pending[min(idx, len(self.pending) - 1)].code
            self.current_tree.selection_set(nxt)
        self.status.config(text="已移除 %s" % (sym.name or sym.code))

    def _move(self, delta):
        idx = self._selected_pending_index()
        if idx is None:
            return
        new = idx + delta
        if not (0 <= new < len(self.pending)):
            return
        self.pending[idx], self.pending[new] = self.pending[new], self.pending[idx]
        code = self.pending[new].code
        self._refresh_pending()
        self.current_tree.selection_set(code)
        self.current_tree.see(code)

    def _save(self):
        if not self.pending:
            self.status.config(text="至少保留一只自选股")
            return
        self.app.set_stocks([s.code for s in self.pending])
        self.close()

    def _tick(self):
        try:
            if self.win.winfo_exists():
                sel = self.current_tree.selection()
                self._refresh_pending()
                if sel and self.current_tree.exists(sel[0]):
                    self.current_tree.selection_set(sel[0])
                self.win.after(1500, self._tick)
        except tk.TclError:
            pass


# ---------------- 持仓 ----------------
class HoldingsWindow(BaseDialog):
    key = "holdings"

    def __init__(self, app):
        super().__init__(app, "持仓设置", 880, 645)
        self.header("持仓设置", "填写手数与成本价，实时统计盈亏")
        self.selected = None
        self.holdings = dict(app.cfg.get("holdings", {}))
        self._build()
        self._refresh()
        self._tick()

    def _build(self):
        pal = self.pal
        foot = tk.Frame(self.body, bg=pal["panel"])
        foot.pack(fill="x", side="bottom", padx=18, pady=(2, 12))
        self.status = tk.Label(foot, text="", bg=pal["panel"], fg=pal["muted"],
                               font=self.fonts.label())
        self.status.pack(side="left")
        CanvasButton(foot, "完成", self.close, pal, self.fonts, kind="primary",
                     width=82, height=34, scale=self.S).pack(side="right")

        body = tk.Frame(self.body, bg=pal["panel"])
        body.pack(fill="both", expand=True, padx=18, pady=(10, 4))

        card = tk.Frame(body, bg=pal["panel_alt"], highlightbackground=pal["border"],
                        highlightthickness=1)
        card.pack(fill="both", expand=True)
        tk.Label(card, text="持仓明细", bg=pal["panel_alt"], fg=pal["muted"],
                 font=self.fonts.label()).pack(anchor="w", padx=12, pady=(9, 4))

        setup_ttk(self.win, pal, self.fonts, self.S)
        cols = [("name", "名称", 168, "w"), ("code", "代码", 76, "w"),
                ("price", "现价", 86, "e"), ("pct", "涨跌幅", 80, "e"),
                ("lots", "手数", 58, "e"), ("cost", "成本价", 86, "e"),
                ("day", "当日盈亏", 96, "e"), ("total", "累计盈亏", 102, "e")]
        self.tree = ttk.Treeview(card, columns=[c[0] for c in cols], show="headings",
                                 style="SB.Treeview", height=8)
        for cid, label, width, anchor in cols:
            self.tree.heading(cid, text=label, anchor=anchor)
            self.tree.column(cid, width=int(width * self.S), anchor=anchor, stretch=True)
        self.tree.tag_configure("up", foreground=pal["pos"])
        self.tree.tag_configure("down", foreground=pal["neg"])
        self.tree.tag_configure("flat", foreground=pal["text"])
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        edit = tk.Frame(body, bg=pal["panel_alt"], highlightbackground=pal["border"],
                        highlightthickness=1)
        edit.pack(fill="x", pady=(8, 0))
        self.sel_label = tk.Label(edit, text="未选择股票", bg=pal["panel_alt"],
                                  fg=pal["text_dim"], font=self.fonts.label())
        self.sel_label.grid(row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(8, 4))
        tk.Label(edit, text="持有手数", bg=pal["panel_alt"], fg=pal["muted"],
                 font=self.fonts.label()).grid(row=1, column=0, sticky="w", padx=(12, 6), pady=(2, 10))
        self.lots_var = tk.StringVar()
        self.lots_entry = make_entry(edit, pal, self.fonts, width=8, textvariable=self.lots_var,
                                     numeric=True)
        self.lots_entry.grid(row=1, column=1, sticky="w", ipady=4, pady=(2, 10))
        tk.Label(edit, text="手（1手=100股）", bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).grid(row=1, column=2, sticky="w", padx=6, pady=(2, 10))
        tk.Label(edit, text="成本价", bg=pal["panel_alt"], fg=pal["muted"],
                 font=self.fonts.label()).grid(row=1, column=3, sticky="e", padx=(18, 6), pady=(2, 10))
        self.cost_var = tk.StringVar()
        self.cost_entry = make_entry(edit, pal, self.fonts, width=9, textvariable=self.cost_var,
                                     numeric=True)
        self.cost_entry.grid(row=1, column=4, sticky="w", ipady=4, pady=(2, 10))
        tk.Label(edit, text="选填，用于累计盈亏", bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).grid(row=1, column=5, sticky="w", padx=(6, 0), pady=(2, 10))
        btns = tk.Frame(edit, bg=pal["panel_alt"])
        btns.grid(row=1, column=6, sticky="e", padx=12, pady=(2, 10))
        CanvasButton(btns, "保存", self._save_one, pal, self.fonts, kind="primary",
                     width=64, height=30, scale=self.S, bg=pal["panel_alt"]).pack(side="left")
        CanvasButton(btns, "清除", self._clear_one, pal, self.fonts, kind="quiet",
                     width=56, height=30, scale=self.S, bg=pal["panel_alt"]).pack(side="left", padx=(6, 0))
        edit.columnconfigure(6, weight=1)
        self.lots_entry.bind("<Return>", lambda e: self._save_one())
        self.cost_entry.bind("<Return>", lambda e: self._save_one())

        totals = tk.Frame(body, bg=pal["panel_alt"], highlightbackground=pal["border"],
                          highlightthickness=1)
        totals.pack(fill="x", pady=(8, 0))
        self.stat_vars = {}
        for i, (key, label) in enumerate([("mv", "持仓市值"), ("day", "当日盈亏"),
                                          ("total", "累计盈亏")]):
            cell = tk.Frame(totals, bg=pal["panel_alt"])
            cell.pack(side="left", expand=True, fill="x", padx=14, pady=8)
            tk.Label(cell, text=label, bg=pal["panel_alt"], fg=pal["muted"],
                     font=self.fonts.tiny()).pack(anchor="w")
            var = tk.Label(cell, text="--", bg=pal["panel_alt"], fg=pal["text"],
                           font=self.fonts.num(0, True))
            var.pack(anchor="w")
            self.stat_vars[key] = var

        self._set_editor_state(False)

    def _codes(self):
        return [c for c in self.app.pick_codes if c]

    def _set_editor_state(self, enabled):
        state = "normal" if enabled else "disabled"
        for w in (self.lots_entry, self.cost_entry):
            w.config(state=state)
        if not enabled:
            self.sel_label.config(text="在上方选择一只股票后填写持仓")

    def _row_values(self, code):
        q = self.app.quotes.get(code)
        h = self.holdings.get(code, {})
        lots = int(h.get("lots", 0) or 0)
        cost = h.get("cost")
        name = q.name if q else code
        if q:
            day = (q.price - q.prev_close) * lots * 100 if lots else 0.0
            total = (q.price - float(cost)) * lots * 100 if (lots and cost) else None
            values = (name, D.code_digits(code), D.fmt_price(q.price, q.dp),
                      D.fmt_pct(q.pct), str(lots) if lots else "-",
                      D.fmt_price(float(cost), q.dp) if cost else "-",
                      D.fmt_money_signed(day) if lots else "-",
                      D.fmt_money_signed(total) if total is not None else "-")
            tag = "up" if q.pct > 0 else ("down" if q.pct < 0 else "flat")
        else:
            values = (name, D.code_digits(code), "--", "--", str(lots) if lots else "-",
                      D.fmt_price(float(cost)) if cost else "-", "--", "--")
            tag = "flat"
        return values, tag

    def _refresh(self):
        tree = self.tree
        selected = self.selected
        tree.delete(*tree.get_children())
        for code in self._codes():
            values, tag = self._row_values(code)
            tree.insert("", "end", iid=code, values=values, tags=(tag,))
        if selected and tree.exists(selected):
            tree.selection_set(selected)
        self._update_totals()

    def _refresh_values(self):
        """只更新单元格, 不重建行, 避免打断正在编辑的输入框。"""
        tree = self.tree
        codes = self._codes()
        existing = list(tree.get_children())
        if len(existing) != len(codes) or any(a != b for a, b in zip(existing, codes)):
            self._refresh()
            return
        for code in codes:
            values, tag = self._row_values(code)
            if tuple(str(v) for v in tree.item(code, "values")) != tuple(str(v) for v in values):
                tree.item(code, values=values, tags=(tag,))
        self._update_totals()

    def _update_totals(self):
        pal = self.pal
        mv = day = total = 0.0
        has_total = False
        for code in self._codes():
            q = self.app.quotes.get(code)
            h = self.holdings.get(code, {})
            lots = int(h.get("lots", 0) or 0)
            if not q or not lots:
                continue
            shares = lots * 100
            mv += q.price * shares
            day += (q.price - q.prev_close) * shares
            if h.get("cost"):
                total += (q.price - float(h["cost"])) * shares
                has_total = True
        self.stat_vars["mv"].config(text=D.fmt_money(mv))
        self.stat_vars["day"].config(text=D.fmt_money_signed(day),
                                     fg=D.change_color(day, pal) if day else pal["text"])
        self.stat_vars["total"].config(
            text=D.fmt_money_signed(total) if has_total else "--",
            fg=(D.change_color(total, pal) if has_total else pal["muted"]))

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            self.selected = None
            self._set_editor_state(False)
            return
        code = sel[0]
        self.selected = code
        q = self.app.quotes.get(code)
        h = self.holdings.get(code, {})
        name = q.name if q else code
        self.sel_label.config(text=f"正在编辑：{name}  {D.code_digits(code)}")
        self.lots_var.set(str(h.get("lots", "")) if h.get("lots") else "")
        self.cost_var.set(("%.3f" % float(h["cost"])).rstrip("0").rstrip(".")
                          if h.get("cost") else "")
        self._set_editor_state(True)

    def _save_one(self):
        if not self.selected:
            return
        lots_s = self.lots_var.get().strip()
        cost_s = self.cost_var.get().strip()
        try:
            lots = int(float(lots_s)) if lots_s else 0
        except ValueError:
            self.status.config(text="手数请填写整数")
            return
        if lots < 0:
            lots = 0
        cost = None
        if cost_s:
            try:
                cost = float(cost_s)
            except ValueError:
                self.status.config(text="成本价格式不正确")
                return
            if cost <= 0:
                cost = None
        if lots > 0:
            entry = {"lots": lots}
            if cost:
                entry["cost"] = round(cost, 4)
            self.holdings[self.selected] = entry
            self.status.config(text="已保存")
        else:
            self.holdings.pop(self.selected, None)
            self.status.config(text="已清空该股票持仓")
        self._commit()

    def _clear_one(self):
        if not self.selected:
            return
        self.holdings.pop(self.selected, None)
        self.lots_var.set("")
        self.cost_var.set("")
        self.status.config(text="已清空该股票持仓")
        self._commit()

    def _commit(self):
        self.app.cfg["holdings"] = self.holdings
        self.app.save_config()
        self.app.refresh_pnl()
        self._refresh()

    def _tick(self):
        try:
            if self.win.winfo_exists():
                self._refresh_values()
                self.win.after(1200, self._tick)
        except tk.TclError:
            pass
