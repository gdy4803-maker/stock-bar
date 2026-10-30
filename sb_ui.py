# -*- coding: utf-8 -*-
"""窗口与卡片: 悬浮详情卡、设置、自选股管理、持仓设置。"""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import sb_data as D
from sb_theme import (KEY_COLOR, apply_dark_titlebar, center_on_bar,
                      draw_round_rect, focus_window, force_topmost)
from sb_widgets import (CanvasButton, Segmented, SizeHandle, Slider, Switch,
                        make_entry, setup_ttk)


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
        self.kind = "stock"          # stock=个股/指数详情卡 | holdings=持仓总览卡
        self.series = None
        self.state = "idle"
        self.visible = False
        self.w = 0
        self.h = 0

    def _card_alpha(self) -> float:
        """卡片透明度(10% ~ 100%), 可在设置里调节。"""
        try:
            v = float(self.app.cfg.get("card_opacity", 1.0))
        except (TypeError, ValueError):
            v = 1.0
        return max(0.10, min(1.0, v))

    def apply_alpha(self):
        """应用卡片透明度(设置里拖动滑杆时即时生效)。"""
        try:
            self.win.attributes("-alpha", self._card_alpha())
        except tk.TclError:
            pass

    # ---------- 显示/隐藏 ----------
    def show(self, code):
        if not code or code not in self.app.quotes:
            self.hide()
            return
        if self.kind != "stock" or code != self.code:
            self.kind = "stock"
            self.code = code
            self.series = None
            self.state = "loading"
            self.app.request_minutes(code, self._on_minutes)
        self.visible = True
        self._render()
        self._place()
        self.apply_alpha()
        self.win.deiconify()
        force_topmost(self.win)
        self.win.lift()

    def show_holdings(self):
        """显示持仓总览卡(悬停行情条的持仓段时)。"""
        self.kind = "holdings"
        self.code = None
        self.visible = True
        self._render()
        self._place()
        self.apply_alpha()
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
            self.apply_alpha()

    # ---------- 布局 ----------
    def _holding(self, q):
        """返回该股持仓(模拟盘推导的 Position)或 None。"""
        if q.is_index:
            return None
        h = self.app.cfg.get("holdings", {}).get(q.code)
        if not h:
            return None
        pos, _ = D.position_of(h.get("trades"))
        return pos if pos.lots > 0 else None

    def _render_holdings(self):
        """持仓总览卡: 真实持仓每只两行 + 底部汇总(悬停行情条持仓段时显示)。"""
        pal = self.app.pal
        fonts = self.app.card_fonts
        S = self.app.scale * 0.875
        pad = int(16 * S)
        w = int(self.WIDTH * S)
        MAX_ROWS = 8

        codes = [c for c in self.app.pick_codes if c]
        for c in (self.app.cfg.get("holdings") or {}):
            if c and c not in codes:
                codes.append(c)
        rows = []
        mv = day = fl = real = 0.0
        for code in codes:
            h = self.app.cfg.get("holdings", {}).get(code)
            if not h:
                continue
            pos, _ = D.position_of(h.get("trades"))
            q = self.app.quotes.get(code)
            real += pos.realized        # 汇总口径含清仓股的历史盈亏
            closed_today = pos.lots <= 0 and D.sold_today(h.get("trades"))
            if pos.lots <= 0 and not closed_today:
                continue                 # 只列当前持仓 + 今日清仓; 更早的清仓不列
            name = q.name if q else code
            price = q.price if q else 0.0
            fv = ((price - pos.avg_cost) * pos.lots * 100
                  if (q and pos.lots > 0 and pos.avg_cost > 0) else None)
            dv = D.day_pnl_of(h.get("trades"), price, q.prev_close) if q else None
            mv += price * pos.lots * 100
            day += dv or 0.0
            fl += fv or 0.0
            rows.append((name, pos.lots, pos.avg_cost, fv, dv, pos.realized))

        hidden = max(0, len(rows) - MAX_ROWS)
        rows = rows[:MAX_ROWS]
        total = real + fl

        # 布局
        y = pad
        y += int(24 * S)                     # 标题
        y += int(8 * S)
        row_y = []
        for _ in rows:
            row_y.append(y)
            y += int(44 * S)                 # 每只两行: 行1 名称, 行2 浮动(两股之间偏上)
        if hidden:
            y += int(16 * S)
        y += int(8 * S)
        div_y = y
        y += int(12 * S)
        sum_y = y
        y += int(20 * S) * 3
        h = int(y + pad)

        self.w, self.h = w, h
        c = self.canvas
        c.configure(width=w, height=h)
        c.delete("all")
        draw_round_rect(c, 0.5, 0.5, w - 0.5, h - 0.5, 14 * S,
                        pal["card_bg"], pal["card_border"], 1)

        # 标题 + 只数徽标
        c.create_text(pad, pad, text="持仓总览", anchor="nw", fill=pal["text"],
                      font=fonts.card_title())
        badge = "%d 只" % (len(rows) + hidden)
        bw = fonts.tiny().measure(badge) + 14 * S
        draw_round_rect(c, w - pad - bw, pad + 1 * S, w - pad, pad + 19 * S, 9 * S,
                        pal["panel_alt"], pal["border_soft"], 1)
        c.create_text(w - pad - bw / 2, pad + 10 * S, text=badge,
                      fill=pal["text_dim"], font=fonts.tiny())

        # 每只持仓: 行1(顶对齐) 名称+持仓+成本, 行2(中线对齐) 浮动+今日
        for i, (name, lots, cost, fv, dv, rl) in enumerate(rows):
            y = row_y[i]
            c.create_text(pad, y, text=name[:7], anchor="nw", fill=pal["text"],
                          font=fonts.label())
            cost_txt = D.fmt_price(cost, 3 if cost < 5 else 2) if cost > 0 else "-"
            c.create_text(w - pad, y, anchor="ne", fill=pal["muted"],
                          font=fonts.num(-3, False),
                          text="%d手  成本 %s" % (lots, cost_txt))
            runs = [("浮动 ", fonts.tiny(), pal["muted"])]
            if fv is None:                    # 今日清仓: 无浮动持仓
                runs.append(("-", fonts.num(-3, False), pal["faint"]))
            else:
                runs.append((D.fmt_money_signed(fv), fonts.num(-3, True),
                             D.change_color(fv, pal)))
            runs += [("    今日 ", fonts.tiny(), pal["muted"]),
                     (D.fmt_money_signed(dv or 0.0), fonts.num(-3, True),
                      D.change_color(dv or 0.0, pal))]
            _text_run(c, pad, y + int(28 * S), runs)

        if hidden:
            c.create_text(pad, row_y[-1] + int(46 * S), anchor="nw",
                          text="… 等 %d 只" % hidden, fill=pal["faint"], font=fonts.tiny())

        # 汇总(有持仓才画)
        if rows:
            c.create_line(pad, div_y, w - pad, div_y, fill=pal["border_soft"])
            col_w = (w - 2 * pad - int(16 * S)) / 2
            cells = (("持仓市值", D.fmt_money(mv), pal["text_dim"], False),
                     ("今日盈亏", D.fmt_money_signed(day), D.change_color(day, pal), False),
                     ("浮动盈亏", D.fmt_money_signed(fl), D.change_color(fl, pal), False),
                     ("已实现", D.fmt_money_signed(real), D.change_color(real, pal), False),
                     ("总盈亏", D.fmt_money_signed(total), D.change_color(total, pal), True))
            for i, (k, v, color, bold) in enumerate(cells):
                cx = pad if i % 2 == 0 else pad + col_w + int(16 * S)
                cy = sum_y + (i // 2) * int(20 * S)
                c.create_text(cx, cy, text=k, anchor="w", fill=pal["muted"], font=fonts.tiny())
                c.create_text(cx + col_w, cy, text=v, anchor="e", fill=color,
                              font=fonts.num(-3, bold))
        else:
            c.create_text(w / 2, sum_y + int(2 * S), text="暂无持仓",
                          anchor="center", fill=pal["faint"], font=fonts.label())
            c.create_text(w / 2, sum_y + int(22 * S),
                          text="去「持仓设置」开启模拟盘", anchor="center",
                          fill=pal["muted"], font=fonts.tiny())

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
        if self.kind == "holdings":
            self._render_holdings()
            return
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
            lots = hold.lots
            shares = lots * 100
            mv = q.price * shares
            dpnl = D.day_pnl_of(
                (self.app.cfg.get("holdings", {}).get(q.code) or {}).get("trades"),
                q.price, q.prev_close)
            left = [("持仓 ", fonts.label(), pal["muted"]),
                    (f"{lots}手", fonts.label(), pal["text_dim"])]
            if hold.avg_cost > 0:
                left += [("    成本 ", fonts.label(), pal["muted"]),
                         (D.fmt_price(hold.avg_cost, q.dp), fonts.num(-2, False),
                          pal["text_dim"])]
            if abs(hold.realized) > 0.004:
                left += [("    已实现 ", fonts.label(), pal["muted"]),
                         (D.fmt_money_signed(hold.realized), fonts.num(-2, True),
                          D.change_color(hold.realized, pal))]
            _text_run(c, pad, p["hold1"], left)
            _text_run(c, w - pad, p["hold1"],
                      [("市值 ", fonts.label(), pal["muted"]),
                       (D.fmt_money(mv), fonts.num(-2, False), pal["text_dim"])],
                      anchor="e")
            _text_run(c, pad, p["hold2"],
                      [("当日 ", fonts.label(), pal["muted"]),
                       (D.fmt_money_signed(dpnl), fonts.num(-2, True), D.change_color(dpnl, pal))])
            if hold.avg_cost > 0:
                fp = (q.price - hold.avg_cost) * shares
                _text_run(c, w - pad, p["hold2"],
                          [("浮动 ", fonts.label(), pal["muted"]),
                           (D.fmt_money_signed(fp), fonts.num(-2, True),
                            D.change_color(fp, pal))],
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
        if len(pts) < 2:
            # 集合竞价/刚开盘只有 1 个点: 画一个点即可, 直接 create_line 会因坐标不足抛异常
            if pts:
                v = pts[0][1]
                prev1 = q.prev_close or series.prev_close or v
                lo1, hi1 = min(v, prev1), max(v, prev1)
                if hi1 - lo1 < 1e-9:
                    hi1, lo1 = lo1 + 0.01, lo1 - 0.01
                py = y2 - (v - lo1) / (hi1 - lo1) * (y2 - y1)
                px = (x1 + x2) / 2
                c.create_oval(px - 3 * S, py - 3 * S, px + 3 * S, py + 3 * S,
                              fill=color, outline="")
            return
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

    def __init__(self, app, title, width, height, min_size=(620, 460)):
        self.app = app
        self.pal = app.pal
        self.fonts = app.ui_fonts
        self.S = app.scale
        self.title_text = title
        self._on_resize_cb = None
        self._on_resize_release_cb = None
        self._resize_job = None
        self._last_size = None
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.title(title)
        self.win.configure(bg=self.pal["panel"])
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        w, h = int(width * self.S), int(height * self.S)
        bx, by, bw, bh = app.bar_rect()
        center_on_bar(self.win, bx, by, bw, bh, w, h, app.screen)
        self.win.bind("<Escape>", lambda e: self.close())
        # 正宗 Windows 窗口: 系统标题栏 + 原生 8 向缩放(拖动/贴边/动画由系统提供)
        self.win.resizable(True, True)
        try:
            self.win.wm_minsize(int(min_size[0] * self.S), int(min_size[1] * self.S))
        except tk.TclError:
            pass
        self.win.bind("<Configure>", self._on_configure)

        self.shell = tk.Frame(self.win, bg=self.pal["panel"])
        self.shell.pack(fill="both", expand=True, padx=1, pady=1)
        self.body = tk.Frame(self.shell, bg=self.pal["panel"])
        self.body.pack(fill="both", expand=True)

        self.win.deiconify()
        self.win.update_idletasks()
        apply_dark_titlebar(self.win, self.pal.get("name") == "dark")
        force_topmost(self.win)
        self.win.lift()
        self.win.focus_force()
        focus_window(self.win)      # OS 层焦点, 输入框/快捷键才可用
        if self.key:
            app._dialogs[self.key] = self

    # ---- 窗口缩放: 由系统原生边框处理, 这里只做回调分发 ----
    def set_resize_handler(self, on_resize=None, on_release=None):
        """注册窗口尺寸变化回调: on_resize(w, h) 在缩放过程中触发(防抖),
        on_release() 在缩放结束后触发(适合保存布局)。"""
        self._on_resize_cb = on_resize
        self._on_resize_release_cb = on_release

    def _on_configure(self, event):
        if event.widget is not self.win:
            return
        if self._resize_job is not None:
            try:
                self.win.after_cancel(self._resize_job)
            except tk.TclError:
                pass
        try:
            self._resize_job = self.win.after(120, self._fire_resize)
        except tk.TclError:
            self._resize_job = None

    def _fire_resize(self):
        self._resize_job = None
        try:
            w, h = self.win.winfo_width(), self.win.winfo_height()
        except tk.TclError:
            return
        if (w, h) != self._last_size:
            self._last_size = (w, h)
            if self._on_resize_cb:
                self._on_resize_cb(w, h)
        if self._on_resize_release_cb:
            self._on_resize_release_cb()

    def raise_(self):
        try:
            self.win.deiconify()
            force_topmost(self.win)
            self.win.lift()
            self.win.focus_force()
            focus_window(self.win)
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
        """副标题行(标题已由系统标题栏展示, 不再重复)。"""
        head = tk.Frame(self.body, bg=self.pal["panel"])
        head.pack(fill="x", padx=22, pady=(14, 8))
        tk.Label(head, text=subtitle, bg=self.pal["panel"], fg=self.pal["muted"],
                 font=self.fonts.label(), anchor="w").pack(side="left")
        ver = tk.Label(head, text="v" + self.app.version, bg=self.pal["panel"],
                       fg=self.pal["faint"], font=self.fonts.tiny())
        ver.pack(side="right", pady=(6, 0))
        tk.Frame(self.body, bg=self.pal["border"], height=1).pack(fill="x")

    def section(self, parent, title):
        pal, S = self.pal, self.S
        outer = tk.Frame(parent, bg=pal["panel_alt"],
                         highlightbackground=pal["border"], highlightthickness=1)
        outer.pack(fill="x", pady=(0, 12))
        head = tk.Frame(outer, bg=pal["panel_alt"])
        head.pack(fill="x", padx=14, pady=(10, 4))
        bar = tk.Frame(head, bg=pal["accent"], width=max(2, int(3 * S)),
                       height=int(13 * S))
        bar.pack(side="left", padx=(0, 8))
        bar.pack_propagate(False)
        tk.Label(head, text=title, bg=pal["panel_alt"], fg=pal["text_dim"],
                 font=self.fonts.label(), anchor="w").pack(side="left")
        inner = tk.Frame(outer, bg=pal["panel_alt"])
        inner.pack(fill="x", padx=14, pady=(0, 12))
        return inner

    def row(self, parent, label, label_width=92):
        """一行设置项: 左侧固定宽标签 + 右侧控件(垂直居中)。"""
        pal = self.pal
        r = tk.Frame(parent, bg=pal["panel_alt"])
        r.pack(fill="x", pady=4)
        holder = tk.Frame(r, bg=pal["panel_alt"], width=int(label_width * self.S))
        holder.pack(side="left", fill="y")
        holder.pack_propagate(False)
        tk.Label(holder, text=label, bg=pal["panel_alt"], fg=pal["text_dim"],
                 font=self.fonts.label(), anchor="w").pack(side="left", pady=6)
        return r


# ---------------- 设置 ----------------
class SettingsWindow(BaseDialog):
    key = "settings"

    def __init__(self, app):
        super().__init__(app, "设置", 840, 750)
        self._build_footer()
        self.header("设置", "调整外观与行情，改动立即生效")
        body = self._build_scroll_body()
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
        self._build_repo(left)
        self._build_market(right)
        self._build_fees(right)
        self._build_shortcuts(right)
        self._build_tips(right)
        # 等子控件排布后再同步滚动区域
        self.win.after_idle(self._sync_scroll_region)

    def _build_scroll_body(self):
        """设置内容可滚动: 小屏或高缩放下不会裁切底部区块。"""
        pal = self.pal
        setup_ttk(self.win, pal, self.fonts, self.S)
        wrap = tk.Frame(self.body, bg=pal["panel"])
        wrap.pack(fill="both", expand=True, padx=18, pady=(12, 6))
        self._sb_canvas = tk.Canvas(wrap, bg=pal["panel"], highlightthickness=0, bd=0)
        self._sb_scroll = ttk.Scrollbar(wrap, orient="vertical",
                                        command=self._sb_canvas.yview,
                                        style="SB.Vertical.TScrollbar")
        self._sb_canvas.configure(yscrollcommand=self._sb_scroll.set)
        self._sb_canvas.pack(side="left", fill="both", expand=True)
        self._sb_scroll.pack(side="right", fill="y")
        inner = tk.Frame(self._sb_canvas, bg=pal["panel"])
        self._sb_inner = inner
        self._sb_item = self._sb_canvas.create_window(
            (0, 0), window=inner, anchor="nw", width=int(800 * self.S))
        inner.bind("<Configure>", lambda e: self._sync_scroll_region())
        self._sb_canvas.bind("<Configure>", self._on_scroll_canvas_resize)
        # 滚轮
        self._sb_canvas.bind("<Enter>", self._bind_mousewheel)
        self._sb_canvas.bind("<Leave>", self._unbind_mousewheel)
        return inner

    def _on_scroll_canvas_resize(self, event):
        try:
            self._sb_canvas.itemconfigure(self._sb_item, width=event.width)
        except (tk.TclError, AttributeError):
            pass
        self._sync_scroll_region()

    def _sync_scroll_region(self):
        try:
            self._sb_canvas.configure(scrollregion=self._sb_canvas.bbox("all"))
        except (tk.TclError, AttributeError):
            pass

    def _bind_mousewheel(self, _event=None):
        self.win.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event=None):
        try:
            self.win.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass

    def _on_mousewheel(self, event):
        try:
            if self._sb_canvas.winfo_exists():
                self._sb_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except (tk.TclError, AttributeError, TypeError):
            pass

    def close(self):
        self._unbind_mousewheel()
        super().close()

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
                  min_item=44).pack(side="left")
        r = self.row(sec, "显示名称")
        self.show_name_sw = Switch(r, bool(self.app.cfg["show_name"]),
                                   self._set_show_name, pal, S)
        self.show_name_sw.pack(side="left", pady=2)
        tk.Label(r, text="行情条里显示股票名称", bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(side="left", padx=(10, 0))
        r = self.row(sec, "文字不透明度")
        Slider(r, int(round(float(self.app.cfg["opacity"]) * 100)), 10, 100,
               self._set_opacity, pal, self.fonts, scale=S, width=264).pack(side="left", pady=2)
        r = self.row(sec, "背景不透明度")
        Slider(r, int(round(float(self.app.cfg.get("bg_opacity", 1.0)) * 100)), 0, 100,
               self._set_bg_opacity, pal, self.fonts, scale=S, width=264).pack(side="left", pady=2)
        r = self.row(sec, "卡片透明度")
        Slider(r, int(round(float(self.app.cfg.get("card_opacity", 1.0)) * 100)), 10, 100,
               self._set_card_opacity, pal, self.fonts, scale=S, width=264).pack(side="left", pady=2)

    def _build_window(self, parent):
        pal = self.pal
        sec = self.section(parent, "窗口")
        r = self.row(sec, "窗口置顶")
        Switch(r, bool(self.app.cfg["topmost"]), self._set_topmost, pal, self.S).pack(side="left", pady=2)
        tk.Label(r, text="行情条始终浮在其他窗口之上", bg=pal["panel_alt"],
                 fg=pal["faint"], font=self.fonts.tiny()).pack(side="left", padx=(10, 0))
        r = self.row(sec, "嵌入任务栏")
        self.dock_sw = Switch(r, bool(self.app.cfg.get("dock_taskbar")), self._set_dock,
                              pal, self.S)
        self.dock_sw.pack(side="left", pady=2)
        tk.Label(r, text="长在任务栏上，可左右拖动", bg=pal["panel_alt"],
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

    # ---- 源码仓库 ----
    def _build_repo(self, parent):
        pal = self.pal
        sec = self.section(parent, "源码仓库")
        r = tk.Frame(sec, bg=pal["panel_alt"])
        r.pack(fill="x", pady=2)
        url = getattr(self.app, "repo_url", "") or "https://github.com/gdy4803-maker/stock-bar"
        # 显示去掉协议前缀的短地址, 点击用完整 URL 打开
        short = url.split("://", 1)[-1]
        lab = tk.Label(r, text=short, bg=pal["panel_alt"], fg=pal["accent"],
                       font=self.fonts.label(), cursor="hand2", anchor="w")
        lab.pack(side="left", fill="x", expand=True)
        lab.bind("<Button-1>", lambda e: self._open_repo())
        CanvasButton(r, "打开仓库", self._open_repo, pal, self.fonts,
                     kind="ghost", width=84, height=30, scale=self.S,
                     bg=pal["panel_alt"]).pack(side="right")

    def _open_repo(self):
        url = getattr(self.app, "repo_url", "") or "https://github.com/gdy4803-maker/stock-bar"
        # 打包成 exe 后 webbrowser 模块常失效, 优先用系统 ShellExecute 打开默认浏览器
        opened = False
        try:
            os.startfile(url)          # type: ignore[attr-defined]
            opened = True
        except Exception:
            try:
                import webbrowser
                opened = bool(webbrowser.open(url))
            except Exception:
                opened = False
        if not opened:
            try:
                self.app.input.clipboard_clear()
                self.app.input.clipboard_append(url)
            except Exception:
                pass
            messagebox.showinfo(
                "打开仓库",
                "无法自动打开浏览器，链接已复制到剪贴板：\n\n" + url,
                parent=self.win)

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
        r = self.row(sec, "持仓显示")
        Segmented(r, [("money", "金额"), ("pct", "涨幅")],
                  self.app.cfg.get("pnl_display", "money"), self._set_pnl_display,
                  pal, self.fonts).pack(side="left")
        tk.Label(r, text="行情条中间那段", bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(side="left", padx=(10, 0))
        r = self.row(sec, "详情卡")
        Segmented(r, [("simple", "简洁"), ("detail", "详细")],
                  self.app.cfg.get("card_mode", "detail"), self._set_card_mode,
                  pal, self.fonts).pack(side="left")
        tk.Label(r, text="简洁 = 不含分时图", bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(side="left", padx=(10, 0))

    # ---- 交易手续费 ----
    _FEE_WAN = ("stock_rate", "fund_rate", "stamp_rate")

    def _fmt_fee(self, key):
        try:
            v = float(self.app.cfg.get("fees", {}).get(key, 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if key in self._FEE_WAN:
            v *= 10000.0
        return ("%.4f" % v).rstrip("0").rstrip(".") or "0"

    def _set_fee(self, key, var):
        try:
            v = float(var.get() or 0)
        except ValueError:
            return
        if key in self._FEE_WAN:
            v = v / 10000.0
        if v < 0:
            v = 0.0
        self.app.cfg.setdefault("fees", {})[key] = round(v, 8)
        self._apply()

    def _build_fees(self, parent):
        pal = self.pal
        sec = self.section(parent, "交易手续费")
        self.fee_vars = {}

        def fee_row(label, fields):
            r = self.row(sec, label)
            for key, cap, width in fields:
                var = tk.StringVar(value=self._fmt_fee(key))
                ent = make_entry(r, pal, self.fonts, width=width, textvariable=var,
                                 numeric=True)
                ent.pack(side="left", ipady=4)
                ent.bind("<Return>", lambda e, k=key, v=var: self._set_fee(k, v))
                ent.bind("<FocusOut>", lambda e, k=key, v=var: self._set_fee(k, v))
                self.fee_vars[key] = var
                tk.Label(r, text=cap, bg=pal["panel_alt"], fg=pal["faint"],
                         font=self.fonts.tiny()).pack(side="left", padx=(6, 12))

        fee_row("股票佣金", [("stock_rate", "万分之", 6), ("stock_min", "元起收", 6)])
        fee_row("ETF 佣金", [("fund_rate", "万分之", 6), ("fund_min", "元起收", 6)])
        fee_row("卖出印花税", [("stamp_rate", "万分之（仅股票卖出）", 8)])
        tk.Label(sec, text="费用 = 佣金（不足起收按起收）＋ 卖出印花税（仅股票）",
                 bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(anchor="w", padx=12, pady=(2, 10))

    def _build_shortcuts(self, parent):
        pal = self.pal
        sec = self.section(parent, "快捷键")
        items = [("悬停", "查看分时与持仓详情"), ("左键", "切换下一只自选"),
                 ("→ / ←", "切换自选"), ("右键", "打开菜单"),
                 ("Ctrl + ,", "打开设置"), ("F5", "重新加载配置"),
                 ("Ctrl + M", "管理自选股"), ("Ctrl + H", "持仓设置"),
                 ("Esc", "关闭窗口/卡片")]
        for k, v in items:
            r = self.row(sec, k)
            tk.Label(r, text=v, anchor="w", bg=pal["panel_alt"],
                     fg=pal["text_dim"], font=self.fonts.label()).pack(side="left")

    def _build_tips(self, parent):
        pal = self.pal
        sec = self.section(parent, "小提示")
        for text in ("按住左键拖动，可放到任务栏上悬浮",
                     "悬停片刻显示分时与持仓详情",
                     "背景不透明度调到 0% 只显示文字",
                     "卡片透明度可让悬停详情卡半透明",
                     "嵌入任务栏后可左右拖动换位置"):
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
        # 走 close() 以解绑全局滚轮并摘掉对话框引用
        self.close()
        self.app.open_settings()

    def _set_font(self, value):
        self.app.cfg["font_size"] = int(value)
        self._apply()

    def _set_show_name(self, value):
        self.app.cfg["show_name"] = bool(value)
        self._apply()

    def _set_opacity(self, value):
        self.app.cfg["opacity"] = max(0.10, min(1.0, value / 100.0))
        self._apply()

    def _set_bg_opacity(self, value):
        self.app.cfg["bg_opacity"] = max(0.0, min(1.0, value / 100.0))
        self._apply()

    def _set_card_opacity(self, value):
        self.app.cfg["card_opacity"] = max(0.10, min(1.0, value / 100.0))
        self._apply()          # 卡片开着时 refresh() 内即时应用新透明度

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
                self.autostart_hint.config(text="写入失败：被安全软件拦截启动项", fg=self.pal["warn"])
                self._autostart_help()
            else:
                self.autostart_hint.config(text="取消失败：请检查启动项权限", fg=self.pal["warn"])

    def _autostart_help(self):
        """三种注册方式都失败时给出可操作的解决办法。"""
        try:
            from tkinter import messagebox
            messagebox.showwarning(
                "开机自启未能写入",
                "注册表 / 启动文件夹 / 计划任务 三种方式都被系统拒绝。\n\n"
                "通常是安全软件的「启动项防护」在拦截（如：火绒安全）。\n\n"
                "解决办法：\n"
                "1. 打开安全软件 → 信任区/白名单，添加本程序所在文件夹；\n"
                "2. 或临时关闭「启动项防护」，打开本开关后再恢复；\n"
                "3. 也可以手动把本程序的快捷方式放进「启动」文件夹。",
                parent=self.win)
        except Exception:
            pass

    def _set_dock(self, value):
        self.app.set_dock(bool(value))
        if hasattr(self, "dock_sw"):
            self.dock_sw.set(bool(self.app.cfg.get("dock_taskbar")))

    def _set_interval(self, value):
        self.app.cfg["interval_seconds"] = float(value)
        self._apply()

    def _set_pnl_display(self, value):
        self.app.cfg["pnl_display"] = "pct" if value == "pct" else "money"
        self._apply()

    def _set_cn_colors(self, value):
        self.app.cfg["cn_colors"] = bool(value)
        self._apply()

    def _apply_index(self):
        if not self.win.winfo_exists():
            return
        raw = self.index_var.get().strip()
        code = D.normalize_code(raw, as_index=True)
        if not code or code[:2] not in ("sh", "sz", "bj") or not code[2:].isdigit():
            code = "sh000001"
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
    """持仓设置 · 模拟盘: 记录买卖操作, 自动推导持仓/浮动/已实现盈亏。

    窗口可拉伸; 两个列表各有分割把手可上下调节(带最小值), 布局记忆。
    """

    key = "holdings"
    MIN_LIST = 150          # 列表最小高度(px, 未缩放)

    def __init__(self, app):
        lay = app.cfg.get("holdings_layout")
        self._lay = lay if isinstance(lay, dict) else {}
        super().__init__(app, "持仓设置 · 模拟盘", 880, 816)
        self.header("持仓设置 · 模拟盘",
                    "买入填成本价、卖出填卖价, 费用按费率规则自动估算, 历史盈亏自动累计")
        self.selected = None
        self.holdings = {c: {"trades": list(h.get("trades") or [])}
                         for c, h in (app.cfg.get("holdings") or {}).items()
                         if isinstance(h, dict)}
        self._fee_dirty = False
        self.min_list = int(self.MIN_LIST * self.S)
        lay_top = int(self._lay.get("h_top") or 0)
        lay_log = int(self._lay.get("h_log") or 0)
        self.h_top = max(self.min_list, lay_top) if lay_top > 0 else int(236 * self.S)
        self.h_log = max(self.min_list, lay_log) if lay_log > 0 else int(236 * self.S)
        self._build()
        self._init_layout()
        self._refresh()
        self._tick()

    # ---------- 构建 ----------
    def _card(self, parent, title):
        pal = self.pal
        card = tk.Frame(parent, bg=pal["panel_alt"], highlightbackground=pal["border"],
                        highlightthickness=1)
        tk.Label(card, text=title, bg=pal["panel_alt"], fg=pal["muted"],
                 font=self.fonts.label()).pack(anchor="w", padx=12, pady=(8, 4))
        return card

    def _build(self):
        pal = self.pal
        S = self.S
        setup_ttk(self.win, pal, self.fonts, S)

        foot = tk.Frame(self.body, bg=pal["panel"])
        foot.pack(fill="x", side="bottom", padx=18, pady=(2, 12))
        self.status = tk.Label(foot, text="", bg=pal["panel"], fg=pal["muted"],
                               font=self.fonts.label())
        self.status.pack(side="left")
        CanvasButton(foot, "完成", self.close, pal, self.fonts, kind="primary",
                     width=82, height=34, scale=S).pack(side="right")

        body = tk.Frame(self.body, bg=pal["panel"])
        body.pack(fill="both", expand=True, padx=18, pady=(8, 4))

        # ---- 持仓明细(高度可调) ----
        self.top_box = tk.Frame(body, bg=pal["panel"])
        self.top_box.pack(fill="x")
        self.top_box.pack_propagate(False)
        card = self._card(self.top_box, "持仓明细")
        card.pack(fill="both", expand=True)
        cols = [("name", "名称", 150, "w"), ("code", "代码", 74, "w"),
                ("price", "现价", 84, "e"), ("lots", "持仓(手)", 72, "e"),
                ("cost", "成本", 84, "e"), ("fl", "浮动盈亏", 98, "e"),
                ("day", "今日盈亏", 98, "e"), ("real", "已实现", 92, "e")]
        self.tree = ttk.Treeview(card, columns=[c[0] for c in cols], show="headings",
                                 style="SB.Treeview", height=4)
        for cid, label, width, anchor in cols:
            self.tree.heading(cid, text=label, anchor=anchor)
            self.tree.column(cid, width=int(width * S), anchor=anchor, stretch=True)
        for tag, col in (("up", pal["pos"]), ("down", pal["neg"]), ("flat", pal["text"])):
            self.tree.tag_configure(tag, foreground=col)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        # 分割把手: 上下拖动改变持仓明细高度, 下方组件随之移动(有最小值)
        SizeHandle(body, lambda dx, dy: self._drag_list("top", dy), pal, S, "ns",
                   thick=int(7 * S), on_release=self._save_layout).pack(fill="x", pady=(3, 3))

        # ---- 交易区 ----
        trade = tk.Frame(body, bg=pal["panel_alt"], highlightbackground=pal["border"],
                         highlightthickness=1)
        trade.pack(fill="x", pady=(8, 0))
        self.trade = trade

        head = tk.Frame(trade, bg=pal["panel_alt"])
        head.pack(fill="x", padx=14, pady=(10, 2))
        self.sel_label = tk.Label(head, text="在上方选择一只股票后交易", bg=pal["panel_alt"],
                                  fg=pal["text_dim"], font=self.fonts.label())
        self.sel_label.pack(side="left")
        self.hint_label = tk.Label(head, text="", bg=pal["panel_alt"], fg=pal["faint"],
                                   font=self.fonts.tiny())
        self.hint_label.pack(side="right")

        form = tk.Frame(trade, bg=pal["panel_alt"])
        form.pack(fill="x", padx=14, pady=(4, 2))
        for i in range(4):
            form.columnconfigure(i, weight=1, uniform="form")   # 四列等宽, 窗口越宽输入框越长

        def field(col, label, numeric=True):
            box = tk.Frame(form, bg=pal["panel_alt"])
            box.grid(row=0, column=col, sticky="ew",
                     padx=(0, 18 if col < 3 else 0))
            tk.Label(box, text=label, bg=pal["panel_alt"], fg=pal["muted"],
                     font=self.fonts.label()).pack(side="left", padx=(0, 10))
            var = tk.StringVar()
            ent = make_entry(box, pal, self.fonts, width=10, textvariable=var,
                             numeric=numeric)
            ent.pack(side="left", fill="x", expand=True, ipady=5)
            return var, ent

        self.price_var, self.price_entry = field(0, "价格")
        self.lots_var, self.lots_entry = field(1, "数量(手)")
        self.fee_var, self.fee_entry = field(2, "费用")
        self.note_var, self.note_entry = field(3, "备注", numeric=False)

        btns = tk.Frame(trade, bg=pal["panel_alt"])
        btns.pack(fill="x", padx=14, pady=(6, 12))
        CanvasButton(btns, "买入", lambda: self._trade("buy"), pal, self.fonts,
                     kind="primary", width=92, height=34, scale=S,
                     bg=pal["panel_alt"]).pack(side="left")
        CanvasButton(btns, "卖出", lambda: self._trade("sell"), pal, self.fonts,
                     width=92, height=34, scale=S, bg=pal["panel_alt"]).pack(side="left", padx=(12, 0))
        CanvasButton(btns, "全部卖出", self._sell_all, pal, self.fonts, kind="quiet",
                     width=104, height=34, scale=S, bg=pal["panel_alt"]).pack(side="left", padx=(12, 0))
        tk.Label(btns, text="回车 = 买入", bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(side="right", padx=(0, 2))

        for w in (self.price_entry, self.lots_entry, self.fee_entry, self.note_entry):
            w.bind("<Return>", lambda e: self._trade("buy"))
        for w in (self.price_entry, self.lots_entry):
            w.bind("<KeyRelease>", lambda e: self._auto_fee())
        self.fee_entry.bind("<Key>", lambda e: setattr(self, "_fee_dirty", True))

        # ---- 成交记录(高度可调) ----
        self.log_box = tk.Frame(body, bg=pal["panel"])
        self.log_box.pack(fill="x", pady=(8, 0))
        self.log_box.pack_propagate(False)
        log = self._card(self.log_box, "成交记录")
        log.pack(fill="both", expand=True)
        lcols = [("time", "时间", 118, "w"), ("name", "名称", 118, "w"),
                 ("side", "方向", 50, "c"), ("price", "价格", 78, "e"),
                 ("lots", "数量(手)", 68, "e"), ("fee", "费用", 68, "e"),
                 ("pnl", "本笔盈亏", 94, "e"), ("note", "备注", 130, "w")]
        self.log_tree = ttk.Treeview(log, columns=[c[0] for c in lcols], show="headings",
                                     style="SB.Treeview", height=5)
        for cid, label, width, anchor in lcols:
            self.log_tree.heading(cid, text=label, anchor=anchor)
            self.log_tree.column(cid, width=int(width * S), anchor=anchor, stretch=True)
        for tag, col in (("up", pal["pos"]), ("down", pal["neg"]), ("flat", pal["text"])):
            self.log_tree.tag_configure(tag, foreground=col)
        self.log_tree.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        CanvasButton(log, "删除所选记录", self._del_trade, pal, self.fonts, kind="quiet",
                     width=118, height=28, scale=S,
                     bg=pal["panel_alt"]).pack(anchor="w", padx=10, pady=(0, 8))
        # 分割把手: 下拉变高成交记录, 下方汇总下移(有最小值)
        SizeHandle(body, lambda dx, dy: self._drag_list("log", dy), pal, S, "ns",
                   thick=int(7 * S), on_release=self._save_layout).pack(fill="x", pady=(3, 3))

        # ---- 汇总 ----
        totals = tk.Frame(body, bg=pal["panel_alt"], highlightbackground=pal["border"],
                          highlightthickness=1)
        totals.pack(fill="x", pady=(8, 0))
        self.totals = totals
        self.stat_vars = {}
        for key, label in (("buy", "累计买入"), ("sell", "累计卖出"),
                           ("real", "已实现盈亏"), ("fl", "浮动盈亏"), ("total", "总盈亏")):
            cell = tk.Frame(totals, bg=pal["panel_alt"])
            cell.pack(side="left", expand=True, fill="x", padx=10, pady=7)
            tk.Label(cell, text=label, bg=pal["panel_alt"], fg=pal["muted"],
                     font=self.fonts.tiny()).pack(anchor="w")
            var = tk.Label(cell, text="--", bg=pal["panel_alt"], fg=pal["text"],
                           font=self.fonts.num(-1, True))
            var.pack(anchor="w")
            self.stat_vars[key] = var

        self._set_state(False)

    # ---------- 布局(可拉伸 + 分割把手) ----------
    def _init_layout(self):
        S = self.S
        if self.h_top <= 0:
            self.h_top = int(236 * S)
        if self.h_log <= 0:
            self.h_log = int(236 * S)
        lay = self._lay
        try:
            if lay.get("w") and lay.get("h"):
                w = max(int(lay["w"]), int(820 * S))
                h = max(int(lay["h"]), int(640 * S))
                self.win.geometry(f"{w}x{h}+{self.win.winfo_x()}+{self.win.winfo_y()}")
        except (TypeError, ValueError):
            pass
        self._apply_lists()
        self.win.update_idletasks()
        # 固定区高度用"实际被挤压的量"修正: 列表定高后若固定部件被压缩, 补足窗口高度
        deficit = 0
        for wdg in (getattr(self, "trade", None), getattr(self, "totals", None)):
            if wdg is not None:
                deficit = max(deficit, max(0, wdg.winfo_reqheight() - wdg.winfo_height()))
        if deficit:
            self.win.geometry(f"{self.win.winfo_width()}x{self.win.winfo_height() + deficit}")
            self.win.update_idletasks()
        # 固定区 = 窗口高 - 两个列表高(标题/表单/汇总/把手/边距)
        self._fixed_px = self.win.winfo_height() - self.h_top - self.h_log
        avail = self.win.winfo_height() - self._fixed_px
        cur = self.h_top + self.h_log
        if avail > 2 * self.min_list and cur > 0 and cur != avail:
            self.h_top = int(round(avail * self.h_top / cur))
            self.h_log = avail - self.h_top
            self._apply_lists()
        self.set_resize_handler(self._on_win_resize, self._save_layout)
        self._save_layout()

    def _apply_lists(self):
        self.top_box.configure(height=self.h_top)
        self.log_box.configure(height=self.h_log)

    def _drag_list(self, which, dy):
        if which == "top":
            self._set_lists(self.h_top + dy, self.h_log, resize_win=True)
        else:
            self._set_lists(self.h_top, self.h_log + dy, resize_win=True)

    def _set_lists(self, h_top, h_log, resize_win=False):
        h_top = max(self.min_list, int(h_top))
        h_log = max(self.min_list, int(h_log))
        if (h_top, h_log) == (self.h_top, self.h_log):
            return
        self.h_top, self.h_log = h_top, h_log
        self._apply_lists()
        if resize_win:
            # 窗口跟随内容变高/变矮, 下方组件随之移动;
            # 贴近屏幕底部放不下时把窗口整体上移腾出空间
            want = self._fixed_px + h_top + h_log
            min_h = self._fixed_px + 2 * self.min_list
            x, y = self.win.winfo_x(), self.win.winfo_y()
            bottom_limit = self.app.screen.vy + self.app.screen.vh - 20
            h = min(want, max(min_h, bottom_limit - y))
            if h < want:
                y = max(self.app.screen.vy + 10, bottom_limit - want)
                h = want
            if (h, y) != (self.win.winfo_height(), self.win.winfo_y()):
                # 只改几何(轻量): 逐次强制重排是拖动卡顿的来源
                self.win.geometry(f"{self.win.winfo_width()}x{h}+{x}+{y}")

    def _on_win_resize(self, w, h):
        """窗口拉大/缩小时, 多出的高度按当前比例分给两个列表(各有最小值)。"""
        total = h - self._fixed_px
        if total < 2 * self.min_list:
            total = 2 * self.min_list
        cur = self.h_top + self.h_log
        h_top = int(round(total * self.h_top / cur)) if cur > 0 else total // 2
        h_top = min(max(self.min_list, h_top), total - self.min_list)
        self.h_top, self.h_log = h_top, total - h_top
        self._apply_lists()

    def _save_layout(self):
        try:
            self.app.cfg["holdings_layout"] = {
                "w": self.win.winfo_width(), "h": self.win.winfo_height(),
                "h_top": int(self.h_top), "h_log": int(self.h_log)}
            self.app.save_config()
        except Exception:
            pass

    def _codes(self):
        """自选 + 有持仓的代码(去重保序), 持仓不因移出自选而消失。"""
        codes = [c for c in self.app.pick_codes if c]
        for c in (self.app.cfg.get("holdings") or {}):
            if c and c not in codes:
                codes.append(c)
        return codes

    def _pos(self, code):
        pos, _ = D.position_of(self.holdings.get(code, {}).get("trades"))
        return pos

    def _set_state(self, enabled):
        state = "normal" if enabled else "disabled"
        for w in (self.price_entry, self.lots_entry, self.fee_entry, self.note_entry):
            w.config(state=state)
        if not enabled:
            self.sel_label.config(text="在上方选择一只股票后交易")
            self.hint_label.config(text="")

    # ---------- 表格 ----------
    def _row_values(self, code):
        q = self.app.quotes.get(code)
        pos = self._pos(code)
        trades = (self.app.cfg.get("holdings", {}).get(code) or {}).get("trades")
        name = q.name if q else code
        price = D.fmt_price(q.price, q.dp) if q else "--"
        realized = D.fmt_money_signed(pos.realized) if abs(pos.realized) > 0.004 else "-"
        if q and pos.lots > 0:
            shares = pos.lots * 100
            fv = (q.price - pos.avg_cost) * shares if pos.avg_cost > 0 else None
            day = D.day_pnl_of(trades, q.price, q.prev_close)
            ref = fv if fv is not None else day
            tag = "up" if ref > 0 else ("down" if ref < 0 else "flat")
            values = (name, D.code_digits(code), price, str(pos.lots),
                      D.fmt_price(pos.avg_cost, q.dp) if pos.avg_cost > 0 else "-",
                      D.fmt_money_signed(fv) if fv is not None else "-",
                      D.fmt_money_signed(day), realized)
        elif abs(pos.realized) > 0.004 or D.sold_today(trades):
            # 已清仓: 显示历史已实现盈亏; 今日清仓的还要显示今天的盈亏
            day = D.day_pnl_of(trades, q.price, q.prev_close) if q else None
            day_txt = D.fmt_money_signed(day) if (day is not None and abs(day) > 0.004) else "-"
            values = (name, D.code_digits(code), price, "0", "-", "-", day_txt, realized)
            tag = "up" if pos.realized > 0 else "down"
        else:
            values = (name, D.code_digits(code), price, "-", "-", "-", "-", "-")
            tag = "flat"
        return values, tag

    def _refresh(self):
        selected = self.selected
        self.tree.delete(*self.tree.get_children())
        for code in self._codes():
            values, tag = self._row_values(code)
            self.tree.insert("", "end", iid=code, values=values, tags=(tag,))
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        self._refresh_log()
        self._update_totals()

    def _refresh_values(self):
        """行情刷新只改单元格, 不重建行, 避免打断输入。"""
        codes = self._codes()
        existing = list(self.tree.get_children())
        if existing != codes:
            self._refresh()
            return
        for code in codes:
            values, tag = self._row_values(code)
            if tuple(str(v) for v in self.tree.item(code, "values")) != tuple(str(v) for v in values):
                self.tree.item(code, values=values, tags=(tag,))
        self._update_totals()

    def _refresh_log(self):
        tree = self.log_tree
        tree.delete(*tree.get_children())
        rows = []
        for code, h in self.holdings.items():
            trades = h.get("trades") or []
            _, recs = D.position_of(trades)
            for i, t in enumerate(trades):
                rows.append((str(t.get("time") or ""), code, i, t, recs[i]))
        rows.sort(key=lambda r: r[0], reverse=True)
        for tstr, code, i, t, rec in rows:
            q = self.app.quotes.get(code)
            name = q.name if q else code
            pnl = rec.get("pnl")
            side = t.get("side")
            values = (tstr, name, "买入" if side == "buy" else "卖出",
                      D.fmt_price(float(t.get("price") or 0), q.dp if q else 2),
                      str(int(t.get("lots") or 0)),
                      D.fmt_money(float(t.get("fee") or 0)),
                      D.fmt_money_signed(pnl) if pnl is not None else "-",
                      str(t.get("note") or ""))
            if pnl is None:
                tag = "flat"
            else:
                tag = "up" if pnl > 0 else ("down" if pnl < 0 else "flat")
            tree.insert("", "end", iid=f"{code}#{i}", values=values, tags=(tag,))

    def _update_totals(self):
        pal = self.pal
        buy = sell = real = fl = 0.0
        for code in self._codes():
            pos = self._pos(code)
            buy += pos.buy_amount
            sell += pos.sell_amount
            real += pos.realized
            q = self.app.quotes.get(code)
            if q and pos.lots > 0 and pos.avg_cost > 0:
                fl += (q.price - pos.avg_cost) * pos.lots * 100
        total = real + fl
        for key, val, color in (("buy", buy, pal["text_dim"]), ("sell", sell, pal["text_dim"]),
                                ("real", real, D.change_color(real, pal)),
                                ("fl", fl, D.change_color(fl, pal)),
                                ("total", total, D.change_color(total, pal))):
            self.stat_vars[key].config(text=D.fmt_money_signed(val), fg=color)

    # ---------- 交易 ----------
    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            self.selected = None
            self._set_state(False)
            return
        code = sel[0]
        self.selected = code
        q = self.app.quotes.get(code)
        pos = self._pos(code)
        name = q.name if q else code
        self.sel_label.config(text=f"正在交易：{name}  {D.code_digits(code)}")
        if not self.price_var.get().strip():
            self.price_var.set(("%.3f" % q.price).rstrip("0").rstrip(".") if q else "")
        if not self.lots_var.get().strip():
            self.lots_var.set("1")
        self.hint_label.config(
            text=(f"现价 {D.fmt_price(q.price, q.dp)}   可卖 {pos.lots} 手   "
                  f"{'成本 ' + D.fmt_price(pos.avg_cost, q.dp) if pos.avg_cost > 0 else ''}"))
        self._set_state(True)
        self._auto_fee()

    def _auto_fee(self):
        if self._fee_dirty or not self.selected:
            return
        try:
            price = float(self.price_var.get() or 0)
            lots = int(float(self.lots_var.get() or 0))
        except ValueError:
            return
        fee = D.trade_fee(self.selected, "buy", price, lots, self.app.cfg.get("fees"))
        self.fee_var.set(("%.2f" % fee).rstrip("0").rstrip(".") if fee else "")

    def _read_form(self):
        code = self.selected
        if not code:
            self.status.config(text="请先在持仓明细里选择一只股票")
            return None
        try:
            price = float(self.price_var.get() or 0)
            lots = int(float(self.lots_var.get() or 0))
        except ValueError:
            self.status.config(text="价格/数量格式不正确")
            return None
        if price <= 0 or lots <= 0:
            self.status.config(text="价格与数量必须大于 0")
            return None
        try:
            fee = float(self.fee_var.get() or 0)
        except ValueError:
            fee = D.trade_fee(code, "buy", price, lots, self.app.cfg.get("fees"))
        return code, price, lots, max(0.0, fee), self.note_var.get().strip()[:40]

    def _trade(self, side):
        data = self._read_form()
        if not data:
            return
        code, price, lots, fee, note = data
        pos = self._pos(code)
        if side == "sell" and lots > pos.lots:
            self.status.config(text=f"可卖不足: 当前持仓 {pos.lots} 手")
            return
        if not self._fee_dirty and side == "sell":
            fee = D.trade_fee(code, "sell", price, lots, self.app.cfg.get("fees"))
            self.fee_var.set("%.2f" % fee)
        trade = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "side": side,
                 "price": round(price, 4), "lots": lots,
                 "fee": round(fee, 2), "note": note}
        self.holdings.setdefault(code, {"trades": []})["trades"].append(trade)
        self._commit()
        self.status.config(
            text=("已买入 %d 手 @ %s, 费用 %s" if side == "buy"
                  else "已卖出 %d 手 @ %s, 费用 %s") % (lots, D.fmt_price(price, 2),
                                                        D.fmt_money(fee)))

    def _sell_all(self):
        if not self.selected:
            self.status.config(text="请先选择一只股票")
            return
        pos = self._pos(self.selected)
        if pos.lots <= 0:
            self.status.config(text="当前无持仓可卖")
            return
        self.lots_var.set(str(pos.lots))
        self._fee_dirty = False
        self._trade("sell")

    def _del_trade(self):
        sel = self.log_tree.selection()
        if not sel:
            self.status.config(text="请先在成交记录里选择一条")
            return
        code, _, idx = sel[0].partition("#")
        trades = self.holdings.get(code, {}).get("trades") or []
        try:
            i = int(idx)
            removed = trades.pop(i)
        except (ValueError, IndexError):
            return
        if not trades:
            self.holdings.pop(code, None)
        self._commit()
        self.status.config(
            text="已删除: %s %s %s手 @ %s" % (
                removed.get("time", ""),
                "买入" if removed.get("side") == "buy" else "卖出",
                removed.get("lots"), D.fmt_price(float(removed.get("price") or 0), 2)))

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
