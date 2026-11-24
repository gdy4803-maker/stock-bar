# -*- coding: utf-8 -*-
"""窗口与卡片: 悬浮详情卡、设置、自选股管理、持仓设置。"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import re
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


def auction_phase(now=None) -> str:
    """集合竞价阶段: auction=竞价中(9:15–9:25) | matched=已撮合待开盘(9:25–9:30) | ''=正常时段。"""
    t = time.localtime(now or time.time())
    mins = t.tm_hour * 60 + t.tm_min
    if 9 * 60 + 15 <= mins < 9 * 60 + 25:
        return "auction"
    if 9 * 60 + 25 <= mins < 9 * 60 + 30:
        return "matched"
    return ""


def _fit_text(font, text, max_px):
    """按像素宽度截断文字, 末尾加省略号(标题等固定区域用)。"""
    text = text or ""
    if max_px <= 0:
        return ""
    if font.measure(text) <= max_px:
        return text
    ell = "…"
    ell_w = font.measure(ell)
    if ell_w >= max_px:
        return ell
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid]) + ell_w <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ell


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
    """加载赞赏码图片: 优先用内嵌 base64(不依赖外部文件), 找不到才退回 assets/。

    深色主题自动用反色版(白色码点), 跟随界面背景。
    """
    photo = None
    try:
        import sb_support_img
        inverted = (getattr(app, "pal", {}).get("name", "") == "dark")
        photo = sb_support_img.get_photo(inverted=inverted)
    except Exception:
        photo = None
    if photo is None:                     # 兼容: 退回外部图片文件
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
            break
    if photo is None:
        return None
    if target:
        factor = max(1, int(round(photo.width() / float(target))))
        if factor > 1:
            photo = photo.subsample(factor, factor)
    return photo


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
        self.canvas.bind("<Button-1>", self._on_card_click)   # AI 按钮 / 分时图 / 持仓卡
        self.canvas.bind("<Motion>", self._on_card_motion)
        self.canvas.pack()
        self.code = None
        self.kind = "stock"          # stock=个股/指数详情卡 | holdings=持仓总览卡
        self._chart_hit = None       # 分时图命中矩形: 点击打开大窗口
        self._ai_btn = None
        self._ai_cache = {}          # {code: AI研判文字} —— 按股票分开, 防串号
        self._ai_busy = set()        # 正在研判的 code 集合
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

    def show_auction(self, code: str | None = None):
        """显示竞价评分卡(快捷键 3; 竞价时段实时, 其它时间=当日最近一次)。"""
        self.kind = "auction"
        if code:
            self.code = code
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

    def _render_auction(self):
        """竞价评分卡(快捷键 3): 五项得分 + 依据 + 结论, 评分透明可核对。"""
        pal = self.app.pal
        fonts = self.app.card_fonts
        S = self.app.scale * 0.875
        code = self.code or getattr(self.app, "_current_pick_code", lambda: "")()
        q = self.app.quotes.get(code)
        S_ = self.app.scale * 0.875
        w = int(self.WIDTH * S_ * 1.12)
        pad = int(16 * S_)
        today = time.strftime("%Y%m%d")
        raw = (getattr(self.app, "_auction", {}) or {}).get(code, [])
        samples = [s for s in raw if str(s[0])[:8] == today]
        day_label = "今日"
        if not samples:
            days = sorted({str(s[0])[:8] for s in raw})
            if days:
                d = days[-1]
                samples = [s for s in raw if str(s[0])[:8] == d]
                day_label = "%s/%s/%s" % (d[:4], d[4:6], d[6:])
        # 昨收: 优先行情, 行情为空(如收盘后不请求)时用存档里的昨收兜底
        prev = q.prev_close if (q and q.prev_close) else \
            float((getattr(self.app, "_auction_prev", {}) or {}).get(code) or 0)
        sc = D.auction_score(samples, prev,
                             getattr(self.app, "_score_weights", None))
        rows = sc["items"] if sc else []
        row_h = int(26 * S_)
        ai_on = bool(self.app.cfg.get("llm_enabled"))
        ai_text = self._ai_cache.get(code or "")
        ai_busy = (code or "") in self._ai_busy
        # AI 文字按宽度估算行数, 动态给高度(不再截断, 全文显示; 仅防极端长度)
        if ai_text:
            ai_text = ai_text if len(ai_text) <= 1200 else ai_text[:1200] + "…"
            per_line = max(16, int((w - 2 * pad) / max(6, int(7 * S_))))
            ai_lines = sum(max(1, (len(seg) + per_line - 1) // per_line)
                           for seg in ai_text.split("\n"))
            ai_h = int(64 * S_) + ai_lines * int(13 * S_)
        else:
            ai_h = int(70 * S_) if ai_on else 0
        h = int(96 * S_) + int(42 * S_) + row_h * max(5, len(rows)) + int(64 * S_) + ai_h
        self.w, self.h = w, h
        c = self.canvas
        c.configure(width=w, height=h)
        c.delete("all")
        draw_round_rect(c, 0.5, 0.5, w - 0.5, h - 0.5, 14 * S_, pal["card_bg"],
                        pal["card_border"], 1)

        # 头部
        y = pad
        name = q.name if q else (code or "未选中")
        _text_run(c, pad, y, [
            (name, fonts.label(), pal["text"]),
            ("  " + (D.code_digits(code) if code else ""), fonts.tiny(), pal["faint"]),
            ("   竞价评分" + ("" if day_label == "今日" else " · " + day_label),
             fonts.tiny(), pal["muted"]),
        ])
        y += int(30 * S_)

        if not sc:
            loaded = len(getattr(self.app, "_auction", {}) or {})
            c.create_text(w / 2, y + int(40 * S_),
                          text="暂无竞价数据(需 9:15–9:25 采集)\n当前已载入 %d 只股票的竞价数据" % loaded,
                          fill=pal["faint"], font=fonts.label(), justify="center")
            return

        tone = {"pos": pal["pos"], "neg": pal["neg"]}.get(sc["tone"], pal["text_dim"])
        # 总分(左=规则评分, 右=规则结论)
        c.create_text(pad, y, text=("%+d" % sc["total"]), anchor="w",
                      fill=tone, font=fonts.num(16, True))
        c.create_text(pad + int(52 * S_), y + int(3 * S_), text="/ 10  规则评分",
                      anchor="w", fill=pal["faint"], font=fonts.tiny())
        c.create_text(w - pad, y, text=sc["verdict"], anchor="e",
                      fill=tone, font=fonts.label())
        y += int(30 * S_)
        # AI 结论对照行(与规则评分并列; 未跑时给出提示)
        ai_text = self._ai_cache.get(code or "")
        if ai_text:
            ai_view = ""
            for seg in ai_text.split("\n"):
                if "偏多" in seg or "中性" in seg or "偏空" in seg:
                    ai_view = seg.strip()
                    break
            ai_tone = (pal["pos"] if "偏多" in ai_view
                       else pal["neg"] if "偏空" in ai_view else pal["text_dim"])
            same = (("偏多" in ai_view and sc["tone"] == "pos")
                    or ("偏空" in ai_view and sc["tone"] == "neg")
                    or ("中性" in ai_view and sc["tone"] == "flat"))
            c.create_text(pad, y, text="AI 研判", anchor="w", fill=pal["faint"],
                          font=fonts.tiny())
            c.create_text(pad + int(64 * S_), y,
                          text=(ai_view[:42] if ai_view else "已研判(详见下方)"),
                          anchor="w", fill=ai_tone, font=fonts.tiny())
            if not same:
                c.create_text(w - pad, y, text="⚠ 与规则不一致", anchor="e",
                              fill=pal["warn"], font=fonts.tiny())
        else:
            c.create_text(pad, y,
                          text="AI 研判: 未运行 —— 悬浮行情条按 T 开始",
                          anchor="w", fill=pal["faint"], font=fonts.tiny())
        y += int(22 * S_)

        # 相对强度(RPS 近似) + 形态提示(来自 MCP)
        st = D.mcp_strength(code) if code else None
        if st and (st.get("alpha") or st.get("flags")):
            a60 = st["alpha"].get("60")
            txt2 = "强度 "
            if a60 is not None:
                txt2 += ("60日跑赢基准 %+.1f%%" % a60)
            if st.get("flags"):
                txt2 += " ｜ " + "、".join(st["flags"][:2])
            tone2 = (pal["pos"] if (a60 or 0) >= 0 else pal["neg"])
            c.create_text(pad, y, text=txt2, anchor="w", fill=tone2,
                          font=fonts.tiny())
            y += int(20 * S_)

        # 五项明细
        c.create_line(pad, y, w - pad, y, fill=pal["border_soft"])
        y += int(10 * S_)
        for name2, score, desc in rows:
            col = pal["pos"] if score > 0 else (pal["neg"] if score < 0 else pal["faint"])
            c.create_text(pad, y, text=name2, anchor="w", fill=pal["text_dim"],
                          font=fonts.tiny())
            c.create_text(pad + int(78 * S_), y, text=("%+d" % score), anchor="w",
                          fill=col, font=fonts.num(-1, True))
            c.create_text(pad + int(104 * S_), y, text=desc, anchor="w",
                          fill=pal["faint"], font=fonts.tiny())
            y += row_h

        y += int(4 * S_)
        c.create_line(pad, y, w - pad, y, fill=pal["border_soft"])
        y += int(14 * S_)
        # 页脚小字(与个股卡"腾讯行情 · 仅供参考"同位置同风格)
        c.create_text(w - pad, y, text="评分仅供参考，不构成投资建议", anchor="e",
                      fill=pal["faint"], font=fonts.tiny())
        # AI 研判(启用后显示按钮; 结果按股票分开展示)
        self._ai_btn = None
        if sc and self.app.cfg.get("llm_enabled"):
            bh = int(26 * S_)
            bx2 = pad + int(96 * S_)
            by1 = y - int(18 * S_)
            by2 = by1 + bh
            draw_round_rect(c, pad, by1, bx2, by2, 6 * S_, pal["accent"], "", 1)
            c.create_text((pad + bx2) / 2, (by1 + by2) / 2, text="AI 研判 [T]",
                          fill="#FFFFFF", font=fonts.tiny())
            self._ai_btn = (pad, by1, bx2, by2)
            if ai_text:
                c.create_text(pad, y + int(22 * S_), text=ai_text, anchor="nw",
                              fill=pal["text_dim"], font=fonts.tiny(),
                              width=int(w - 2 * pad))
            elif ai_busy:
                c.create_text(pad, y + int(22 * S_), text="AI 研判生成中…", anchor="nw",
                              fill=pal["faint"], font=fonts.tiny())

    def _on_card_click(self, event):
        rect = getattr(self, "_ai_btn", None)
        if rect and rect[0] <= event.x <= rect[2] and rect[1] <= event.y <= rect[3]:
            self._ask_ai()
            return "break"
        if self.kind == "holdings":
            self.app.open_holdings()
            return "break"
        hit = getattr(self, "_chart_hit", None)
        if self.kind == "stock" and hit and (
                hit[0] <= event.x <= hit[2] and hit[1] <= event.y <= hit[3]):
            code = self.code
            if code:
                self.hide()
                self.app.open_stock_detail(code)
            return "break"

    def _on_card_motion(self, event):
        """分时图/持仓卡显示手型光标; 其余区域保持默认。"""
        hand = False
        if self.kind == "holdings":
            hand = True
        else:
            hit = getattr(self, "_chart_hit", None)
            hand = bool(hit and hit[0] <= event.x <= hit[2]
                        and hit[1] <= event.y <= hit[3])
        try:
            self.canvas.configure(cursor="hand2" if hand else "")
        except tk.TclError:
            pass

    def _ask_ai(self):
        """点击「AI 研判」: 后台线程调用大模型, 结果回到卡片展示(按股票分存)。"""
        code = self.code or getattr(self.app, "_current_pick_code", lambda: "")()
        if not code or code in self._ai_busy:
            return
        q = self.app.quotes.get(code)
        today = time.strftime("%Y%m%d")
        raw = (getattr(self.app, "_auction", {}) or {}).get(code, [])
        samples = [s for s in raw if str(s[0])[:8] == today]
        day_label = "今日"
        if not samples:
            days = sorted({str(s[0])[:8] for s in raw})
            if days:
                d = days[-1]
                samples = [s for s in raw if str(s[0])[:8] == d]
                day_label = "%s/%s/%s" % (d[:4], d[4:6], d[6:])
        # 昨收: 优先行情, 行情为空(如收盘后不请求)时用存档里的昨收兜底
        prev = q.prev_close if (q and q.prev_close) else \
            float((getattr(self.app, "_auction_prev", {}) or {}).get(code) or 0)
        sc = D.auction_score(samples, prev,
                             getattr(self.app, "_score_weights", None))
        if not sc or not q:
            self._ai_cache[code] = "暂无竞价数据, 无法研判"
            self._render()
            return
        buy = samples[-1][3] if samples else 0
        sell = samples[-1][4] if samples else 0
        vol = samples[-1][2] if samples else 0
        p_start, p_end = samples[0][1], samples[-1][1]
        prices = [s[1] for s in samples]
        p_hi, p_lo = max(prices), min(prices)
        amp = (p_hi - p_lo) / q.prev_close * 100 if q.prev_close else 0.0
        # 量能时序: 首段 vs 末段平均撮合量(放量/缩量)
        k = max(2, len(samples) // 3)
        v_head = sum(s[2] for s in samples[:k]) / k
        v_tail = sum(s[2] for s in samples[-k:]) / k
        # 9:20 前后对比(撤单痕迹: 前段冲高、后段回落 = 疑似虚挂)
        half = max(2, len(samples) // 2)
        p_early_peak = max(prices[:half])
        p_after_min = min(prices[half:])
        fake = (p_early_peak - p_end) / q.prev_close * 100 if q.prev_close else 0.0

        # ---- 补充上下文: 大盘 / 历史竞价 / 开盘后表现 ----
        extra = []
        idx = getattr(self.app, "index_code", "")
        isamp = [s for s in (getattr(self.app, "_auction", {}) or {}).get(idx, [])
                 if str(s[0])[:8] == today]
        iq = self.app.quotes.get(idx)
        if isamp and iq and iq.prev_close > 0:
            igap = (isamp[-1][1] - iq.prev_close) / iq.prev_close * 100
            ib, iis = isamp[-1][3], isamp[-1][4]
            extra.append(f"- 大盘({iq.name})竞价缺口 {igap:+.2f}%, 买卖余量 {ib:.0f}/{iis:.0f}手")
        hist = self._recent_auction(code)
        if hist:
            extra.append("- 该股近%d次竞价: %s" % (len(hist), "; ".join(hist)))
        # 开盘后表现(9:30 后复盘时很有用)
        if q.open > 0:
            extra.append(f"- 开盘后实况: 今开 {q.open:.2f}, 最新 {q.price:.2f}"
                         f"({q.pct:+.2f}%), 成交 {D.fmt_volume(q.volume)}手,"
                         f"成交额 {D.fmt_yi_wan(q.amount * 10000)}")
        # 历史统计 + 基本面 + 技术分析(来自开源 MCP 数据服务 full 级别)
        brief = D.mcp_stock_brief(code, level="full")
        if brief:
            extra.append("- 该股基本面/历史统计/技术分析(数据源 mcp-cn-a-stock):\n" + brief)
        st = D.mcp_strength(code)
        if st and st.get("flags"):
            extra.append("- 相对强度/形态提示: " + "、".join(st["flags"]))
        prompt = (
            "你是A股盘前研判助手。以下是【真实行情数据】, 请只依据这些数据研判:\n"
            f"1) 股票: {q.name}({code})\n"
            f"2) 昨收价: {q.prev_close:.2f}\n"
            f"3) 竞价起始价: {p_start:.2f}  竞价结束价(撮合价): {p_end:.2f}"
            f"  期间变动 {(p_end - p_start) / q.prev_close * 100:+.2f}%\n"
            f"4) 开盘缺口(撮合价vs昨收): {sc['gap']:+.2f}%\n"
            f"5) 竞价区间: 最高 {p_hi:.2f} / 最低 {p_lo:.2f}, 振幅 {amp:.2f}%\n"
            f"6) 竞价虚拟成交量: {vol:.0f}手"
            f" (首段均量 {v_head:.0f}手 → 末段均量 {v_tail:.0f}手,"
            f" {'放量' if v_tail > v_head * 1.2 else '缩量' if v_tail < v_head * 0.8 else '平稳'})\n"
            f"7) 买卖余量: 买 {buy:.0f}手 / 卖 {sell:.0f}手 (买压 {sc['buy_ratio'] * 100:.0f}%)\n"
            f"8) 撤单痕迹: 前段峰值 {p_early_peak:.2f} 到结束价回落 {fake:.2f}%"
            f"({'疑似虚挂撤单' if fake >= 1.0 else '价格真实'}), 9:20后最低 {p_after_min:.2f}\n"
            + ("\n".join(extra) + "\n" if extra else "")
            + "9) 规则评分: " + ", ".join(f"{n}{s:+d}" for n, s, _ in sc["items"]) +
            f"  合计 {sc['total']:+d}/10\n\n"
            "【铁律】所有数字必须直接引用上面给出的数值, 禁止编造、改写或推算任何数字;"
            "提到价格时只用'昨收价/起始价/结束价/今开/最新'的原文数值。\n"
            "【输出格式】严格按下面四行写, 总长100~180字, 不要省略任何一行:\n"
            "①综合研判: 偏多/中性/偏空\n"
            "②理由: 2-3条, 每条引用具体数据; 【至少一条为非方向性因素】"
            "(估值分位/技术超买超卖/风险收益比), 不要只说'因为涨了所以强'\n"
            "③风险: 1-2条\n"
            "④操作倾向: 买入/持有/观望/卖出\n"
            "最后一行加: 不构成投资建议")
        self._ai_cache[code] = ""
        self._ai_busy.add(code)
        self._render()

        def work(c=code):
            text, err = D.llm_ask(
                self.app.cfg, prompt,
                system=("你是谨慎的A股盘前分析助手, 观点要明确但不武断。\n"
                        "【重要原则】\n"
                        "1) 不得简单地'涨了就持有、跌了就卖出' —— 当日方向只是参考之一。\n"
                        "2) 必须综合考虑: 估值分位(PE/PB 高低)、技术超买超卖(KDJ/RSI 高低位)、"
                        "趋势与估值是否背离、风险收益比。\n"
                        "3) 当价格方向与估值/技术面矛盾时, 敢于给出与方向相反的建议"
                        "(例如高开+严重超买 → 即便涨也建议观望/减仓; 低位+超卖+估值合理 → "
                        "即便跌也可建议持有/低吸)。\n"
                        "4) 理由中至少一条引用非方向性因素(估值/技术位置/风险收益比)。"))
            result = err or text or "AI 无返回"
            try:
                self.win.after(0, lambda: self._ai_done(c, result))
            except Exception:
                self._ai_busy.discard(c)   # 窗口已关/主循环未运行: 静默结束

        threading.Thread(target=work, daemon=True).start()

    def _recent_auction(self, code: str, days: int = 5):
        """读最近若干天的竞价存档, 汇总该股的缺口/买压(给 AI 当历史上下文)。

        存档目录与程序分离(%LOCALAPPDATA%\\StockBar\\auction_data), 打包/升级不丢。
        """
        import glob as _glob
        out = []
        app = self.app
        folder = (app._auction_dir() if hasattr(app, "_auction_dir")
                  else os.path.join(getattr(app, "base_dir", ""), "auction_data"))
        for path in sorted(_glob.glob(os.path.join(folder, "*.json")), reverse=True)[:days]:
            try:
                d = json.load(open(path, "r", encoding="utf-8"))
                st = (d.get("stocks") or {}).get(code)
                if not st or not st.get("samples"):
                    continue
                pc = float(st.get("prev_close") or 0)
                smp = st["samples"]
                if pc <= 0:
                    continue
                gap = (float(smp[-1][1]) - pc) / pc * 100
                b, s2 = float(smp[-1][3]), float(smp[-1][4])
                ratio = b / (b + s2) * 100 if (b + s2) > 0 else 50.0
                out.append("%s 缺口%+.2f%% 买压%.0f%%" % (d.get("date", "?")[5:], gap, ratio))
            except Exception:
                continue
        return out

    def _ai_done(self, code, text):
        self._ai_busy.discard(code)
        self._ai_cache[code] = text
        self._render()
        self._place()

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
            dv = (D.day_pnl_of(h.get("trades"), price, q.prev_close, quote_time=q.time)
                  if q else None)
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
        self._chart_hit = None
        self._ai_btn = None
        if self.kind == "holdings":
            self._render_holdings()
            return
        if self.kind == "auction":
            self._render_auction()
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
        market = D.market_label(q.code, q.is_index)
        badge_w = fonts.tiny().measure(market) + 14 * S
        code_txt = D.code_digits(q.code)
        code_w = fonts.num(-3, False).measure(code_txt)
        # 名称给代码和市场徽标让位: 超长名称截断, 不挤在一起
        name_max = (w - pad - badge_w) - 10 * S - code_w - 7 * S - pad
        name_txt = _fit_text(fonts.card_title(), q.name, name_max)
        c.create_text(pad, y, text=name_txt, anchor="nw", fill=pal["text"],
                      font=fonts.card_title())
        name_w = fonts.card_title().measure(name_txt)
        c.create_text(pad + name_w + 7 * S, y + 4 * S, text=code_txt,
                      anchor="nw", fill=pal["muted"], font=fonts.num(-3, False))
        draw_round_rect(c, w - pad - badge_w, y + 1 * S, w - pad, y + 19 * S, 9 * S,
                        pal["panel_alt"], pal["border_soft"], 1)
        c.create_text(w - pad - badge_w / 2, y + 10 * S, text=market,
                      fill=pal["text_dim"], font=fonts.tiny())

        # 现价 + 涨跌 chip(行情非当日时, 涨跌按 0 显示, 与今日盈亏口径一致)
        y = p["price"]
        pct = D.today_pct(q)
        chg = q.change if D.quote_is_today(q) else 0.0
        color = D.change_color(pct, pal)
        price_txt = D.fmt_price(q.price, q.dp)
        c.create_text(pad, y, text=price_txt, anchor="nw", fill=pal["text"],
                      font=fonts.card_price())
        price_w = fonts.card_price().measure(price_txt)
        chip_x = pad + price_w + 12 * S
        chip_w = 118 * S
        chip_h = 26 * S
        soft = pal["pos_soft"] if pct > 0 else (pal["neg_soft"] if pct < 0 else pal["panel_alt"])
        draw_round_rect(c, chip_x, y + 2 * S, chip_x + chip_w, y + 2 * S + chip_h, 7 * S,
                        soft, "", 1)
        c.create_text(chip_x + 10 * S, y + 2 * S + chip_h / 2, text=D.fmt_change(chg, q.dp),
                      anchor="w", fill=color, font=fonts.num(-2, True))
        c.create_text(chip_x + chip_w - 10 * S, y + 2 * S + chip_h / 2, text=D.fmt_pct(pct),
                      anchor="e", fill=color, font=fonts.num(-2, True))

        # 分时图(简洁模式不显示)
        if not p["simple"]:
            self._draw_chart(c, p, q, color, soft)
            self._chart_hit = (pad, p["chart"] - int(4 * S),
                               w - pad, p["chart"] + int(68 * S))
            phase = auction_phase()
            if phase == "auction":
                # 竞价进行中: 右端是实时时间(而不是固定的 09:25)
                t1, t2 = "09:15", time.strftime("%H:%M")
            elif phase:
                t1, t2 = "09:15", "09:25"
            else:
                t1, t2 = "09:30", "15:00"      # 分时横轴固定交易时段(同花顺式)
            c.create_text(pad, p["time"] + 8 * S, text=t1, anchor="w",
                          fill=pal["faint"], font=fonts.tiny())
            c.create_text(w - pad, p["time"] + 8 * S, text=t2, anchor="e",
                          fill=pal["faint"], font=fonts.tiny())

        # 数据网格
        rows = [
            ("今开", D.fmt_price(q.open, q.dp), "最高", D.fmt_price(q.high, q.dp)),
            ("昨收", D.fmt_price(q.prev_close, q.dp), "最低", D.fmt_price(q.low, q.dp)),
        ]
        if not q.is_index:
            rows.append(("成交量", D.fmt_volume(q.volume), "成交额", D.fmt_amount(q.amount)))
        col_w = (w - 2 * pad - int(22 * S)) / 2
        for i, (k1, v1, k2, v2) in enumerate(rows):
            yy = p["grid"] + i * 20 * S
            c.create_text(pad, yy, text=k1, anchor="nw", fill=pal["muted"], font=fonts.label())
            c.create_text(pad + col_w, yy, text=v1, anchor="ne", fill=pal["text_dim"],
                          font=fonts.num(-2, False))
            x2 = pad + col_w + 22 * S
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
                q.price, q.prev_close, quote_time=q.time)
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

    def _draw_auction(self, c, p, q, color, soft):
        """集合竞价走势图(数据驱动):
        - 上方: 虚拟撮合价曲线 + 昨收参考线
        - 下方: 量柱 = 虚拟撮合量, 颜色 = 买卖余量对比(红=买压, 绿=卖压)
        9:25 撮合后补上真实的竞价成交量柱。"""
        S = self.app.scale * 0.875
        pal = self.app.pal
        fonts = self.app.card_fonts
        pad = p["pad"]
        w = p["w"]
        x1, x2 = pad + 4 * S, w - pad - 4 * S
        y1 = p["chart"]
        y2 = p["chart"] + int(64 * S)
        phase = auction_phase()
        today = time.strftime("%Y%m%d")
        raw = (getattr(self.app, "_auction", {}) or {}).get(q.code, [])
        samples = [s for s in raw if str(s[0])[:8] == today and s[1] > 0]
        prev = q.prev_close or (samples[0][1] if samples else 0.0)

        # 无采样: 提示等待(附诊断: 已载入多少只, 便于排查)
        if not samples:
            loaded = len(getattr(self.app, "_auction", {}) or {})
            c.create_text((x1 + x2) / 2, (y1 + y2) / 2,
                          text="暂无竞价数据(需 9:15–9:25 采集)\n当前已载入 %d 只" % loaded,
                          fill=pal["faint"], font=fonts.label(), justify="center")
            return

        prices = [s[1] for s in samples]
        lo = min(min(prices), prev)
        hi = max(max(prices), prev)
        if hi - lo < 1e-9:
            hi, lo = lo + 0.01, lo - 0.01
        span = (hi - lo) * 1.12
        mid = (hi + lo) / 2
        lo, hi = mid - span / 2, mid + span / 2

        # 布局: 底部是量柱带, 上方是撮合价曲线
        band_h = int((y2 - y1) * 0.36)
        band_top = y2 - band_h

        # 昨收参考虚线
        py_prev = band_top - (prev - lo) / (hi - lo) * (band_top - y1)
        c.create_line(x1, py_prev, x2, py_prev, fill=pal["border"], dash=(3, 3))

        # 量柱: 高度 = 该段虚拟撮合量, 颜色 = 买卖余量对比
        n_seg = 24
        buckets = []
        for i in range(n_seg):
            a = int(i * len(samples) / n_seg)
            b = max(a + 1, int((i + 1) * len(samples) / n_seg))
            b = min(b, len(samples))
            buckets.append(samples[a:b])
        vols = [max((s[2] for s in seg), default=0.0) for seg in buckets]
        max_v = max(vols) if max(vols) > 0 else 1.0
        bar_w = max(3, int((x2 - x1) / n_seg * 0.62))
        for i, seg in enumerate(buckets):
            if not seg:
                continue
            vol = vols[i]
            buy = max((s[3] for s in seg), default=0.0)
            sell = max((s[4] for s in seg), default=0.0)
            cx = x1 + (x2 - x1) * (i + 0.5) / n_seg
            hgt = max(2.0, vol / max_v * band_h) if vol > 0 else 2.0
            hgt = min(hgt, band_h)
            if buy > sell:
                col = pal["pos"]                 # 买压: 红
            elif sell > buy:
                col = pal["neg"]                 # 卖压: 绿
            else:
                col = pal["muted"]               # 均衡
            c.create_rectangle(cx - bar_w / 2, y2 - hgt, cx + bar_w / 2, y2,
                               fill=col, outline="")

        # 撮合价曲线(画在量柱带上方)
        n = len(prices)
        coords = []
        for i, v in enumerate(prices):
            px = x1 + (x2 - x1) * (i / max(1, n - 1))
            py = band_top - (v - lo) / (hi - lo) * (band_top - y1)
            coords += [px, py]
        if len(coords) >= 4:
            c.create_line(*coords, fill=color, width=max(1, int(1.6 * S)))
        c.create_oval(coords[-2] - 2.6 * S, coords[-1] - 2.6 * S,
                      coords[-2] + 2.6 * S, coords[-1] + 2.6 * S,
                      fill=color, outline=pal["card_bg"])

        # 标注: 左=标题+评分徽章, 右=撮合价与买卖余量(紧凑; 时间刻度在轴上已有)
        buy_now = samples[-1][3]
        sell_now = samples[-1][4]
        sc = D.auction_score(samples, prev)
        parts = [("集合竞价", fonts.tiny(), pal["muted"])]
        if sc:
            tone = (pal["pos"] if sc["tone"] == "pos"
                    else pal["neg"] if sc["tone"] == "neg" else pal["faint"])
            label = ("偏多" if sc["tone"] == "pos"
                     else "偏空" if sc["tone"] == "neg" else "中性")
            parts += [("  [%+d " % sc["total"], fonts.tiny(), tone),
                      (label, fonts.tiny(), tone),
                      ("]", fonts.tiny(), tone)]
        _text_run(c, x1, y1 - int(13 * S), parts)
        c.create_text(x2, y1 - int(13 * S),
                      text=("撮合价 %s · 买%s 卖%s"
                            % (D.fmt_price(prices[-1], q.dp),
                               D.fmt_yi_wan(prices[-1] * buy_now * 100),
                               D.fmt_yi_wan(prices[-1] * sell_now * 100))),
                      anchor="e", fill=color, font=fonts.tiny())

        # 9:25 撮合后: 补真实的竞价成交量柱
        if phase == "matched" and q.volume > 0:
            bw = int((x2 - x1) * 0.10)
            cx = (x1 + x2) / 2
            hgt = int((y2 - y1) * 0.5)
            col = pal["pos"] if prices[-1] >= prev else pal["neg"]
            c.create_rectangle(cx - bw / 2, y2 - hgt, cx + bw / 2, y2,
                               fill=col, outline="")
            c.create_text(cx, y2 - hgt - int(9 * S),
                          text="竞价成交 %s手" % D.fmt_volume(q.volume),
                          anchor="s", fill=pal["text_dim"], font=fonts.tiny())

    def _draw_chart(self, c, p, q, color, soft):
        if auction_phase():
            self._draw_auction(c, p, q, color, soft)      # 竞价时段: 显示竞价走势图
            return
        pal = self.app.pal
        fonts = self.app.card_fonts
        S = self.app.scale * 0.875
        pad = p["pad"]
        x1, x2 = pad, p["w"] - pad
        y1, y2 = p["chart"], p["chart"] + 64 * S
        # 图例: 左=分时, 右=买一/卖一报价(价×手); 同一基线, 避免左右文字高低不齐
        legend_y = p["legend"] + int(7 * S)
        c.create_text(x1, legend_y, text="分时", anchor="w", fill=pal["faint"],
                      font=fonts.tiny())
        if q.bid1 > 0 or q.ask1 > 0:
            _text_run(c, x2, legend_y, [
                ("买一 ", fonts.tiny(), pal["faint"]),
                ("%s×%s" % (D.fmt_price(q.bid1, q.dp),
                            D.fmt_yi_wan(q.bid1 * q.bid1_vol * 100)),
                 fonts.tiny(), pal["pos"]),
                ("  卖一 ", fonts.tiny(), pal["faint"]),
                ("%s×%s" % (D.fmt_price(q.ask1, q.dp),
                            D.fmt_yi_wan(q.ask1 * q.ask1_vol * 100)),
                 fonts.tiny(), pal["neg"]),
            ], anchor="e")
        elif q.prev_close:
            c.create_text(x2, legend_y, text="昨收 " + D.fmt_price(q.prev_close, q.dp),
                          anchor="e", fill=pal["faint"], font=fonts.tiny())
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

        def _xmin(t):
            """HH:MM -> 交易分钟序(9:30=0, 11:30=120, 13:00=120, 15:00=240)。"""
            try:
                hh, mm = int(t[:2]), int(t[3:5])
            except Exception:
                return 0
            mins = hh * 60 + mm
            if mins <= 11 * 60 + 30:
                return max(0, mins - (9 * 60 + 30))
            if mins < 13 * 60:
                return 120                        # 午休压缩
            return min(240, 120 + mins - (13 * 60))

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
        hi_v, lo_v = max(prices), min(prices)
        # 纵轴(同花顺式): 以昨收为中心对称, 范围随最高/最低点变化
        dev = max(abs(hi_v - prev), abs(lo_v - prev))
        if dev < 1e-9:
            dev = max(abs(prev) * 0.002, 0.01)
        dev *= 1.18
        lo, hi = prev - dev, prev + dev
        # X 按时间映射到固定 9:30–15:00(未到的时间留白)
        coords = []
        for t, v in pts:
            px = x1 + (x2 - x1) * (_xmin(t) / 240.0)
            py = y2 - (v - lo) / (hi - lo) * (y2 - y1)
            coords += [px, py]
        last_x = coords[-2]
        area = coords + [last_x, y2, x1, y2]
        c.create_polygon(*area, fill=soft, outline="")
        if q.prev_close:
            py = y2 - (q.prev_close - lo) / (hi - lo) * (y2 - y1)
            # 昨收虚线(同花顺式): 明显可见 + 标注价格
            c.create_line(x1, py, x2, py, fill=pal["muted"], dash=(4, 3),
                          width=max(1, int(1.1 * S)))
            c.create_text(x1 + int(3 * S), py - int(3 * S),
                          text="昨收 " + D.fmt_price(q.prev_close, q.dp),
                          anchor="sw", fill=pal["muted"], font=fonts.tiny())
        c.create_line(*coords, fill=color, width=max(1, int(1.6 * S)))
        c.create_oval(coords[-2] - 2.6 * S, coords[-1] - 2.6 * S,
                      coords[-2] + 2.6 * S, coords[-1] + 2.6 * S,
                      fill=color, outline=pal["card_bg"])
        # 最高/最低点标注(点 + 价格, 同花顺式)
        i_hi = prices.index(hi_v)
        i_lo = prices.index(lo_v)
        for idx, val, col, dy in ((i_hi, hi_v, pal["pos"], -int(13 * S)),
                                  (i_lo, lo_v, pal["neg"], int(4 * S))):
            px = coords[idx * 2]
            py = coords[idx * 2 + 1]
            c.create_oval(px - 2.2 * S, py - 2.2 * S, px + 2.2 * S, py + 2.2 * S,
                          fill=col, outline="")
            c.create_text(px, py + dy, text=D.fmt_price(val, q.dp),
                          anchor="s" if dy < 0 else "n", fill=col,
                          font=fonts.tiny())

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

    def on_app_settings(self):
        """外观设置变更后的统一入口；子类可覆盖并重绘自定义画布。"""
        self.pal = self.app.pal

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
        self._build_update(left)
        self._build_support(left)
        self._build_repo(left)
        self._build_market(right)
        self._build_fees(right)
        self._build_ai(right)
        self._build_shortcuts(right)
        self._build_tips(left)           # 小提示放左列: 平衡两列高度, 视觉更舒服
        self._bind_autosave()            # 输入框打字即自动保存(不用切焦点)
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
        tk.Label(r, text="关闭后只显示涨跌幅", bg=pal["panel_alt"], fg=pal["faint"],
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
        tk.Label(r, text="始终显示在最前", bg=pal["panel_alt"],
                 fg=pal["faint"], font=self.fonts.tiny()).pack(side="left", padx=(10, 0))
        r = self.row(sec, "嵌入任务栏")
        self.dock_sw = Switch(r, bool(self.app.cfg.get("dock_taskbar")), self._set_dock,
                              pal, self.S)
        self.dock_sw.pack(side="left", pady=2)
        tk.Label(r, text="固定在任务栏，可左右拖动", bg=pal["panel_alt"],
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
                     kind="ghost", width=92, height=32, scale=self.S,
                     bg=pal["panel_alt"]).pack(side="right", padx=(8, 0))
        CanvasButton(r, "管理自选股…", self.app.open_manage, pal, self.fonts,
                     kind="primary", width=104, height=32, scale=self.S,
                     bg=pal["panel_alt"]).pack(side="right")

    # ---- 更新 ----
    def _build_update(self, parent):
        pal, F, S = self.pal, self.fonts, self.S
        sec = self.section(parent, "更新")
        # 状态行: 版本 + 状态徽章(紧挨着)
        r = tk.Frame(sec, bg=pal["panel_alt"])
        r.pack(fill="x", pady=(2, 4))
        holder = tk.Frame(r, bg=pal["panel_alt"], width=int(92 * S))
        holder.pack(side="left", fill="y")
        holder.pack_propagate(False)
        tk.Label(holder, text="当前版本", bg=pal["panel_alt"], fg=pal["text_dim"],
                 font=F.label(), anchor="w").pack(side="left", pady=6)
        tk.Label(r, text="v" + self.app.version, bg=pal["panel_alt"], fg=pal["text"],
                 font=F.num(0, True)).pack(side="left")
        self._update_chip = tk.Label(r, text="", bg=pal["panel_alt"],
                                     fg=pal["muted"], font=F.tiny())
        self._update_chip.pack(side="left", padx=(10, 0))
        r = self.row(sec, "检查更新")
        CanvasButton(r, "立即检查", self._check_update_now, pal, F,
                     kind="ghost", width=92, height=32, scale=S,
                     bg=pal["panel_alt"]).pack(side="left")
        self._update_btn = CanvasButton(r, "更新", self._do_update, pal, F,
                                        kind="primary", width=92, height=32, scale=S,
                                        bg=pal["panel_alt"])
        self._btn_shown = False        # 「更新」按钮: 仅有新版本时出现
        tk.Label(sec, text="自动重启到新版，配置不变", bg=pal["panel_alt"],
                 fg=pal["faint"], font=F.tiny()).pack(
            anchor="w", padx=int(104 * S), pady=(2, 10))
        self.refresh_update_status()

    def _update_status_text(self):
        app = self.app
        if getattr(app, "_updating", False):
            return app._update_stage or "正在更新…"
        info = getattr(app, "_update_info", None)
        if info:
            return "发现新版本 %s" % info.get("tag", "")
        if getattr(app, "_update_check_state", "") == "fail":
            return "检查失败（限流/网络），稍后再试"
        return "已是最新版本"

    def refresh_update_status(self):
        try:
            text = self._update_status_text()
            new = bool(getattr(self.app, "_update_info", None)) and not self.app._updating
            self._update_chip.config(
                text="  " + text + "  ",
                fg=self.pal["accent"] if new else self.pal["muted"],
                bg=self.pal["panel_alt"] if not new else self.pal["menu_hover"])
            # 「更新」按钮: 仅有新版本时出现
            if new and not self._btn_shown:
                self._update_btn.pack(side="left", padx=(10, 0))
                self._btn_shown = True
            elif not new and self._btn_shown:
                self._update_btn.pack_forget()
                self._btn_shown = False
        except Exception:
            pass

    def _check_update_now(self):
        self._update_chip.config(text="  正在检查…  ")
        import threading
        threading.Thread(target=self._check_now_worker, daemon=True).start()

    def _check_now_worker(self):
        # 手动检查: 不受缓存限制, 结果也会写入缓存
        try:
            self.app._check_update_worker()
        except Exception:
            pass
        try:
            self.win.after(0, self.refresh_update_status)
        except Exception:
            pass

    def _do_update(self):
        if not getattr(self.app, "_update_info", None):
            self.refresh_update_status()
            return
        self.app.start_update()
        self.refresh_update_status()

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
        qr.create_rectangle(0, 0, size, size, fill=pal["card_bg"],
                            outline=pal["border_soft"], width=1)
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
        short = url.split("://", 1)[-1]
        self._repo_lab = tk.Label(r, text=short, bg=pal["panel_alt"], fg=pal["accent"],
                                  font=self.fonts.label(), cursor="hand2", anchor="w")
        self._repo_lab.pack(side="left", fill="x", expand=True)
        self._repo_lab.bind("<Button-1>", lambda e: self._copy_repo())
        CanvasButton(r, "复制仓库", self._copy_repo, pal, self.fonts,
                     kind="ghost", width=84, height=32, scale=self.S,
                     bg=pal["panel_alt"]).pack(side="right")

    def _copy_repo(self):
        """复制仓库地址到剪贴板(不再跳转浏览器)。"""
        url = getattr(self.app, "repo_url", "") or "https://github.com/gdy4803-maker/stock-bar"
        ok = False
        for clip in (getattr(self.app, "input", None), self.win):
            try:
                clip.clipboard_clear()
                clip.clipboard_append(url)
                ok = True
                break
            except Exception:
                continue
        try:
            self._repo_lab.config(text=("已复制到剪贴板 ✓" if ok else "复制失败, 请手动复制"),
                                  fg=(self.pal["pos"] if ok else self.pal["warn"]))
            self.win.after(1500, self._restore_repo_label)
        except Exception:
            pass

    def _restore_repo_label(self):
        try:
            url = getattr(self.app, "repo_url", "") or "https://github.com/gdy4803-maker/stock-bar"
            self._repo_lab.config(text=url.split("://", 1)[-1], fg=self.pal["accent"])
        except Exception:
            pass

    # ---- 行情 ----
    def _build_market(self, parent):
        pal = self.pal
        sec = self.section(parent, "行情")
        r = self.row(sec, "刷新间隔")
        Segmented(r, [(v, f"{v:g}s") for v in self.app.INTERVALS],
                  float(self.app.cfg["interval_seconds"]), self._set_interval,
                  pal, self.fonts, item_pad=9, min_item=38).pack(side="left")
        r = self.row(sec, "请求时段")
        Segmented(r, [("trading", "交易时段 9-16点"), ("all", "全天请求")],
                  self.app.cfg.get("fetch_mode", "trading"), self._set_fetch_mode,
                  pal, self.fonts).pack(side="left")
        r = self.row(sec, "右侧指数")
        self.index_var = tk.StringVar(value=self.app.cfg["index"])
        entry = make_entry(r, pal, self.fonts, width=10, textvariable=self.index_var,
                           numeric=True)
        entry.pack(side="left", ipady=4)
        entry.bind("<Return>", lambda e: self._apply_index())
        entry.bind("<FocusOut>", lambda e: self._apply_index())
        CanvasButton(r, "应用", self._apply_index, pal, self.fonts, kind="ghost",
                     width=52, height=32, scale=self.S,
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
        r = self.row(sec, "详情卡")
        Segmented(r, [("simple", "简洁"), ("detail", "详细")],
                  self.app.cfg.get("card_mode", "detail"), self._set_card_mode,
                  pal, self.fonts).pack(side="left")
        tk.Label(r, text="简洁版不含分时图", bg=pal["panel_alt"], fg=pal["faint"],
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

    def _set_cfg(self, key, value):
        self.app.cfg[key] = value
        self._apply()

    def _set_fetch_mode(self, value):
        self.app.cfg["fetch_mode"] = value
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

    def _build_ai(self, parent):
        """AI 研判(大模型)设置: 启用开关 / 服务商 / 接口 / 模型 / Key。"""
        pal = self.pal
        sec = self.section(parent, "AI 研判")
        r0 = self.row(sec, "自动研判")
        sw = Switch(r0, bool(self.app.cfg.get("llm_enabled")),
                    lambda v: self._set_cfg("llm_enabled", bool(v)), pal, self.S)
        sw.pack(side="left")
        tk.Label(r0, text="开启后 9:25 自动研判一次",
                 anchor="w", bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(side="left", padx=(10, 0))

        prov = str(self.app.cfg.get("llm_provider") or "deepseek")
        r1 = self.row(sec, "服务商")
        seg = Segmented(r1, [("deepseek", "DeepSeek"), ("mimo", "MIMO"), ("custom", "自定义")],
                        prov, self._set_llm_provider, pal, self.fonts, scale=self.S)
        seg.pack(side="left")

        def field(label, key, placeholder=""):
            r = self.row(sec, label)
            var = tk.StringVar(value=str(self.app.cfg.get(key) or ""))
            ent = make_entry(r, pal, self.fonts, width=20, textvariable=var)
            ent.pack(side="left", fill="x", expand=True, ipady=4)
            if placeholder:
                tk.Label(r, text=placeholder, bg=pal["panel_alt"], fg=pal["faint"],
                         font=self.fonts.tiny()).pack(side="left", padx=(8, 0))
            ent.bind("<Return>", lambda e, k=key, v=var: self._set_cfg(k, v.get().strip()))
            ent.bind("<FocusOut>", lambda e, k=key, v=var: self._set_cfg(k, v.get().strip()))
            return var

        self._llm_base_var = field("接口地址", "llm_base")
        self._llm_model_var = field("模型名", "llm_model")
        self._llm_key_var = field("API Key", "llm_key", "可留空")

    def _set_llm_provider(self, name: str):
        preset = D.LLM_PRESETS.get(name) or {}
        self.app.cfg["llm_provider"] = name
        if preset.get("base"):
            self.app.cfg["llm_base"] = preset["base"]
        if preset.get("model"):
            self.app.cfg["llm_model"] = preset["model"]
        self._llm_base_var.set(self.app.cfg.get("llm_base") or "")
        self._llm_model_var.set(self.app.cfg.get("llm_model") or "")
        self._apply()

    def _build_shortcuts(self, parent):
        pal = self.pal
        sec = self.section(parent, "快捷键")
        items = [("悬停", "查看分时与持仓详情"), ("左键", "切换下一只自选"),
                 ("→ / ←", "切换自选"), ("右键", "打开菜单"),
                 ("1 / 2 / 3", "换卡片：个股 / 持仓总览 / 竞价评分"),
                 ("Ctrl+Shift+S", "打开设置"), ("F5", "重新加载配置"),
                 ("Ctrl+M", "管理自选股"), ("Ctrl+H", "持仓设置"),
                 ("Esc", "关闭窗口/卡片")]
        for k, v in items:
            r = self.row(sec, k, label_width=108)
            tk.Label(r, text=v, anchor="w", bg=pal["panel_alt"],
                     fg=pal["text_dim"], font=self.fonts.label()).pack(side="left")
        tk.Label(sec, text=("按 1 / 2 / 3 换卡片时，鼠标需停在行情条上；移开即关闭。"),
                 anchor="w", justify="left", bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny(), wraplength=int(320 * self.S)).pack(
            fill="x", padx=14, pady=(2, 12))

    def _build_tips(self, parent):
        pal = self.pal
        sec = self.section(parent, "小提示")
        for text in ("按住左键拖动，可放到任务栏上悬浮",
                     "悬停片刻显示分时与持仓详情",
                     "背景不透明度 0% = 纯文字模式"):
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
                     width=84, height=32, scale=self.S).pack(side="left")
        tk.Label(foot, text="配置保存在程序同目录 config.json", bg=pal["panel"],
                 fg=pal["faint"], font=self.fonts.tiny()).pack(side="left", padx=(12, 0))
        CanvasButton(foot, "完成", self.close, pal, self.fonts, kind="primary",
                     width=84, height=32, scale=self.S).pack(side="right")

    # ---- 回调 ----
    def _bind_autosave(self):
        """给设置里所有输入框绑定"打字即保存"(防抖 0.6 秒), 不必切焦点。"""
        def walk(w):
            for ch in w.winfo_children():
                if isinstance(ch, tk.Entry):
                    ch.bind("<KeyRelease>", self._on_entry_edit, add="+")
                walk(ch)
        walk(self.win)
        self._autosave_job = None

    def _on_entry_edit(self, _event=None):
        if getattr(self, "_autosave_job", None):
            try:
                self.win.after_cancel(self._autosave_job)
            except tk.TclError:
                pass
        try:
            self._autosave_job = self.win.after(600, self._flush_inputs)
        except tk.TclError:
            self._autosave_job = None

    def _flush_inputs(self):
        """把所有输入框的当前值写回配置并应用。"""
        self._autosave_job = None
        try:
            for name in ("index_var", "_llm_base_var", "_llm_model_var", "_llm_key_var"):
                var = getattr(self, name, None)
                if var is not None:
                    key = {"index_var": "index", "_llm_base_var": "llm_base",
                           "_llm_model_var": "llm_model", "_llm_key_var": "llm_key"}[name]
                    if key == "index":
                        continue     # 指数走 _apply_index(有校验)
                    self.app.cfg[key] = var.get().strip()
            for key, var in (getattr(self, "fee_vars", {}) or {}).items():
                try:
                    v = float(var.get() or 0)
                except ValueError:
                    continue
                if key in self._FEE_WAN:
                    v = v / 10000.0
                self.app.cfg.setdefault("fees", {})[key] = max(0.0, round(v, 8))
            self._apply()
        except Exception:
            pass

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
        super().__init__(app, "支持开发者", 300, 416, min_size=(240, 300))
        self.header("感谢支持", "欢迎扫码")
        pal = self.pal
        # 先放底部按钮栏, 再让内容区填充剩余空间(后 pack 的底部栏会被挤出窗口)
        foot = tk.Frame(self.body, bg=pal["panel"])
        foot.pack(fill="x", side="bottom", padx=18, pady=(2, 10))
        CanvasButton(foot, "关闭", self.close, pal, self.fonts, kind="primary",
                     width=84, height=32, scale=self.S).pack(side="right")
        body = tk.Frame(self.body, bg=pal["panel"])
        body.pack(fill="both", expand=True, pady=(4, 0))
        self._qr_card = tk.Frame(body, bg=pal["card_bg"],
                                 highlightbackground=pal["border_soft"],
                                 highlightthickness=1)
        self._qr_card.pack(pady=(6, 8))
        photo = _load_support_photo(app, "support.png")
        if photo is not None:
            self._qr_base = photo
            self.qr_label = tk.Label(self._qr_card, image=photo, bg=pal["card_bg"], bd=0)
            self.qr_label.image = photo
            self.qr_label.pack(padx=10, pady=10)
        else:
            self._qr_base = None
            self.qr_label = tk.Label(self._qr_card, text="赞赏码图片缺失",
                                     bg=pal["card_bg"], fg=pal["muted"],
                                     font=self.fonts.label(), justify="center")
            self.qr_label.pack(padx=28, pady=50)
        tk.Label(body, text="微信扫码 · 韩的赞赏码", bg=pal["panel"], fg=pal["text_dim"],
                 font=self.fonts.label()).pack()
        tk.Label(body, text="你的支持是持续更新的动力 ♥", bg=pal["panel"],
                 fg=pal["muted"], font=self.fonts.tiny()).pack(pady=(3, 0))

        # 二维码随窗口缩放(防抖)
        self._qr_job = None
        self.win.bind("<Configure>", self._on_qr_resize)
        self.win.after(120, self._apply_qr_size)

    def _on_qr_resize(self, event):
        if event.widget is not self.win:
            return
        if self._qr_job is not None:
            try:
                self.win.after_cancel(self._qr_job)
            except tk.TclError:
                pass
        try:
            self._qr_job = self.win.after(120, self._apply_qr_size)
        except tk.TclError:
            self._qr_job = None

    def _apply_qr_size(self):
        """按窗口大小调整二维码尺寸: 略小于窗口, 拉大窗口二维码跟着变大。"""
        self._qr_job = None
        if self._qr_base is None:
            return
        try:
            w = self.win.winfo_width()
            h = self.win.winfo_height()
        except tk.TclError:
            return
        size = int(min(w * 0.72, h * 0.62))
        size = max(180, min(size, 520))
        import sb_support_img
        photo = sb_support_img.scaled_to(self._qr_base, size)
        try:
            self.qr_label.config(image=photo)
            self.qr_label.image = photo
        except tk.TclError:
            pass


# ---------------- 使用帮助 ----------------
class HelpWindow(BaseDialog):
    """详细使用教程: 分组卡片 + 可滚动正文, 支持 F1 / 右键菜单打开。"""

    key = "help"

    def __init__(self, app):
        super().__init__(app, "使用帮助", 880, 800, min_size=(660, 540))
        self._wrap_items = []
        self._build_footer()
        self.header("使用帮助", "从快速上手到进阶功能，一页看懂")
        body = self._build_scroll_body()
        self._build_quick_start(body)
        self._build_bar(body)
        self._build_menu(body)
        self._build_settings(body)
        self._build_watchlist(body)
        self._build_holdings(body)
        self._build_cards(body)
        self._build_tools(body)
        self._build_data(body)
        self._build_detail(body)
        self._build_shortcuts(body)
        self._build_faq(body)
        self.win.bind("<MouseWheel>", self._on_mousewheel)
        self.win.after_idle(self._sync_scroll_region)
        self.win.after_idle(self._refresh_wrap)

    # ---- 滚动与排版 ----
    def _build_footer(self):
        pal = self.pal
        foot = tk.Frame(self.body, bg=pal["panel"])
        foot.pack(fill="x", side="bottom", padx=18, pady=(4, 12))
        CanvasButton(foot, "打开程序目录", self._open_dir, pal, self.fonts,
                     kind="ghost", width=112, height=32, scale=self.S).pack(side="left")
        self._dir_hint = tk.Label(foot, text="配置与数据都在程序目录", bg=pal["panel"],
                                  fg=pal["faint"], font=self.fonts.tiny())
        self._dir_hint.pack(side="left", padx=(12, 0))
        CanvasButton(foot, "关闭", self.close, pal, self.fonts,
                     kind="primary", width=84, height=32, scale=self.S).pack(side="right")

    def _build_scroll_body(self):
        pal = self.pal
        setup_ttk(self.win, pal, self.fonts, self.S)
        wrap = tk.Frame(self.body, bg=pal["panel"])
        wrap.pack(fill="both", expand=True, padx=18, pady=(12, 6))
        self._canvas = tk.Canvas(wrap, bg=pal["panel"], highlightthickness=0, bd=0)
        self._scroll = ttk.Scrollbar(wrap, orient="vertical",
                                     command=self._canvas.yview,
                                     style="SB.Vertical.TScrollbar")
        self._canvas.configure(yscrollcommand=self._scroll.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._scroll.pack(side="right", fill="y")
        inner = tk.Frame(self._canvas, bg=pal["panel"])
        self._inner = inner
        self._item = self._canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: self._sync_scroll_region())
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        return inner

    def _on_canvas_resize(self, event):
        try:
            self._canvas.itemconfigure(self._item, width=event.width)
        except (tk.TclError, AttributeError):
            pass
        self._refresh_wrap(event.width)
        self._sync_scroll_region()

    def _sync_scroll_region(self):
        try:
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        except (tk.TclError, AttributeError):
            pass

    def _refresh_wrap(self, width=None):
        if width is None:
            try:
                width = self._canvas.winfo_width()
            except tk.TclError:
                return
        for lab, text, font, indent in self._wrap_items:
            try:
                lab.configure(text=self._wrap_px(text, font,
                                                max(180, width - 72 * self.S - indent)),
                              wraplength=0)
            except tk.TclError:
                pass

    @staticmethod
    def _wrap_px(text, font, max_px):
        """按像素宽度逐字换行: Tk 的 wraplength 只在空格处断行, 中文长句会溢出。"""
        if max_px <= 24:
            return text
        out = []
        for para in str(text).split("\n"):
            line = ""
            for token in re.findall(r"[A-Za-z0-9_./:%~+\-]+|[\u4e00-\u9fff]|.", para):
                cand = line + token
                if font.measure(cand) <= max_px:
                    line = cand
                    continue
                if line.strip():
                    out.append(line.rstrip())
                if font.measure(token) > max_px:
                    chunk = ""
                    for ch in token:
                        if font.measure(chunk + ch) <= max_px:
                            chunk += ch
                        else:
                            if chunk:
                                out.append(chunk)
                            chunk = ch
                    line = chunk
                else:
                    line = token.lstrip()
            out.append(line.rstrip())
        return "\n".join(out)

    def _on_mousewheel(self, event):
        try:
            if self._canvas.winfo_exists():
                self._canvas.yview_scroll(int(-1 * (event.delta / 120) * 3), "units")
        except (tk.TclError, AttributeError, TypeError):
            pass

    def _card(self, parent, number, title):
        pal, F = self.pal, self.fonts
        card = tk.Frame(parent, bg=pal["panel_alt"],
                        highlightbackground=pal["border"], highlightthickness=1)
        card.pack(fill="x", pady=(0, 12))
        head = tk.Frame(card, bg=pal["panel_alt"])
        head.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(head, text=number, bg=pal["panel_alt"], fg=pal["accent"],
                 font=F.num(-1, True), width=3, anchor="w").pack(side="left")
        tk.Label(head, text=title, bg=pal["panel_alt"], fg=pal["text"],
                 font=F.ui(1, True), anchor="w").pack(side="left")
        tk.Frame(card, bg=pal["border_soft"], height=1).pack(fill="x", padx=14, pady=(3, 7))
        inner = tk.Frame(card, bg=pal["panel_alt"])
        inner.pack(fill="x", padx=(42, 18), pady=(0, 11))
        return inner

    def _p(self, parent, text, color=None, indent=0, pady=(0, 6)):
        font = self.fonts.ui(-1)
        lab = tk.Label(parent, text=text, bg=self.pal["panel_alt"],
                       fg=color or self.pal["text_dim"], font=font,
                       anchor="w", justify="left",
                       wraplength=max(220, int(650 * self.S)))
        lab.pack(fill="x", pady=pady)
        self._wrap_items.append((lab, text, font, int(indent * self.S)))
        return lab

    def _sub(self, parent, text):
        lab = tk.Label(parent, text=text, bg=self.pal["panel_alt"],
                       fg=self.pal["muted"], font=self.fonts.ui(0, True),
                       anchor="w", justify="left")
        lab.pack(fill="x", pady=(9, 3))
        return lab

    def _bullet(self, parent, text):
        row = tk.Frame(parent, bg=self.pal["panel_alt"])
        row.pack(fill="x", pady=1)
        tk.Label(row, text="·", bg=self.pal["panel_alt"], fg=self.pal["accent"],
                 font=self.fonts.ui(-1, True)).pack(side="left", anchor="n")
        font = self.fonts.ui(-1)
        lab = tk.Label(row, text=text, bg=self.pal["panel_alt"],
                       fg=self.pal["text_dim"], font=font,
                       anchor="w", justify="left",
                       wraplength=max(220, int(650 * self.S)))
        lab.pack(side="left", fill="x", expand=True, padx=(7, 0))
        self._wrap_items.append((lab, text, font, int(20 * self.S)))

    def _step(self, parent, num, text):
        row = tk.Frame(parent, bg=self.pal["panel_alt"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=f"{num}.", bg=self.pal["panel_alt"], fg=self.pal["accent"],
                 font=self.fonts.num(-1, True), width=2, anchor="w").pack(side="left", anchor="n")
        font = self.fonts.ui(-1)
        lab = tk.Label(row, text=text, bg=self.pal["panel_alt"],
                       fg=self.pal["text_dim"], font=font,
                       anchor="w", justify="left",
                       wraplength=max(220, int(650 * self.S)))
        lab.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self._wrap_items.append((lab, text, font, int(26 * self.S)))

    def _kv(self, parent, key, value):
        row = tk.Frame(parent, bg=self.pal["panel_alt"])
        row.pack(fill="x", pady=2)
        holder = tk.Frame(row, bg=self.pal["panel_alt"], width=int(170 * self.S))
        holder.pack(side="left", fill="y")
        holder.pack_propagate(False)
        tk.Label(holder, text=key, bg=self.pal["panel_alt"], fg=self.pal["text"],
                 font=self.fonts.ui(-1, True), anchor="w").pack(side="left")
        font = self.fonts.ui(-1)
        lab = tk.Label(row, text=value, bg=self.pal["panel_alt"],
                       fg=self.pal["text_dim"], font=font,
                       anchor="w", justify="left",
                       wraplength=max(220, int(650 * self.S)))
        lab.pack(side="left", fill="x", expand=True)
        self._wrap_items.append((lab, value, font, int(170 * self.S)))

    def _open_dir(self):
        try:
            os.startfile(getattr(self.app, "base_dir", "") or ".")
            self._dir_hint.config(text="已打开程序目录", fg=self.pal["text_dim"])
        except Exception:
            self._dir_hint.config(text="打开失败，请手动定位程序目录", fg=self.pal["warn"])

    # ---- 01 快速开始 ----
    def _build_quick_start(self, parent):
        sec = self._card(parent, "01", "快速开始")
        self._p(sec, f"双击 看股插件V{self.app.version}.exe 即可运行，免安装。"
                     "首次启动会在程序目录生成 config.json，自选、持仓和所有设置都存在这里。")
        self._step(sec, 1, "启动后行情条默认位于屏幕右下角。按住左键可拖到任意位置；"
                           "拖到任务栏会自动吸附。")
        self._step(sec, 2, "行情条分三段：左边是当前自选股，中间是持仓当日盈亏，"
                           "右边是自选指数。没有持仓时中间段自动隐藏。")
        self._step(sec, 3, "鼠标悬停分区看详情；单击左边个股段切换下一只；"
                           "在详情卡的分时图上左键单击可打开详细大窗口；右键打开功能菜单。")
        self._p(sec, "建议第一次使用按这个顺序设置：外观与字号 → 管理自选股 → 持仓设置 → "
                     "需要时再配置 AI 研判。", color=self.pal["muted"], pady=(6, 0))

    # ---- 02 行情条 ----
    def _build_bar(self, parent):
        sec = self._card(parent, "02", "行情条怎么用")
        self._sub(sec, "悬停查看详情")
        self._bullet(sec, "个股段：分时图、现价/涨跌、买一卖一；简洁模式下不显示分时图。")
        self._bullet(sec, "持仓段：持仓总览，列出每只持仓的成本、浮动盈亏、今日盈亏和汇总。")
        self._bullet(sec, "指数段：指数分时与涨跌；指数代码可在设置里改。")
        self._sub(sec, "点击与拖动")
        self._bullet(sec, "单击个股段：切换到下一只自选股；单击持仓段：在“金额 / 涨幅”之间切换。")
        self._bullet(sec, "按住左键拖动：移动行情条；拖到任务栏会自动吸附并记住位置。")
        self._bullet(sec, "指数段不响应单击，避免误触。")
        self._sub(sec, "常用键盘操作")
        self._bullet(sec, "← / → / ↑ / ↓：切换自选股。")
        self._bullet(sec, "1 / 2 / 3：切换个股 / 持仓总览 / 竞价评分卡；鼠标需停在行情条上。")
        self._bullet(sec, "Esc：关闭当前详情卡；T：打开竞价卡并立即 AI 研判。")

    # ---- 03 菜单 ----
    def _build_menu(self, parent):
        sec = self._card(parent, "03", "右键菜单逐项说明")
        self._p(sec, "在行情条上点右键打开。带勾选的项（窗口置顶、嵌入任务栏等）点击后就地翻转，"
                     "菜单不会立刻关闭，方便连续调整。")
        for key, value in (
                ("设置…", "外观、窗口、行情、手续费、AI、更新，改动立即生效。"),
                ("管理自选股…", "搜索、添加、移除、上下排序；保存后立即生效。"),
                ("自选强弱排序…", "按 60 日相对强度与竞价评分给自选排序。"),
                ("收盘选股…", "勾选策略，扫描全市场，结果双击可加入自选。"),
                ("当日异动扫描…", "筛选涨幅 ≥4% 且成交额 ≥2 亿的股票，双击加自选。"),
                ("持仓设置…", "模拟盘买卖记录，自动计算持仓、浮动与今日盈亏。"),
                ("切换股票", "子菜单直接选择当前显示的股票。"),
                ("窗口置顶", "勾选后始终显示在其他窗口之上。"),
                ("嵌入任务栏（左）", "勾选后固定在任务栏，可左右拖动；开启时自动找空位。"),
                ("回到右下角", "位置跑偏或换显示器后，一键回到右下角。"),
                ("使用帮助", "打开本教程；也可直接按 F1。"),
                ("退出", "保存配置并退出程序。"),
        ):
            self._kv(sec, key, value)
        self._p(sec, "刷新间隔在「设置 → 行情」里调整；复制行情按 Ctrl+C；"
                     "重新加载配置按 F5。", color=self.pal["muted"], pady=(6, 0))

    # ---- 04 设置 ----
    def _build_settings(self, parent):
        sec = self._card(parent, "04", "设置页：每个选项做什么")
        self._sub(sec, "外观")
        self._bullet(sec, "显示样式：胶囊 = 名称 + 涨跌幅 + 持仓 + 指数；紧凑 = 更短，只保留涨跌幅与盈亏。")
        self._bullet(sec, "界面配色：深色 / 浅色；字号 10 / 12 / 14 / 16。")
        self._bullet(sec, "显示名称：关闭后自选段只显示涨跌幅，适合极简桌面。")
        self._bullet(sec, "文字 / 背景 / 卡片不透明度：背景调到 0% 就是纯文字模式。")
        self._sub(sec, "窗口")
        self._bullet(sec, "窗口置顶：始终显示在最前。")
        self._bullet(sec, "嵌入任务栏：固定在任务栏；开启时自动避开已有图标，之后跟随宽度不搬家。")
        self._bullet(sec, "开机启动：登录 Windows 后自动运行；被安全软件拦截时会弹出处理办法。")
        self._sub(sec, "行情")
        self._bullet(sec, "刷新间隔：0.5–10 秒；网络一般时建议 2–3 秒。")
        self._bullet(sec, "请求时段：交易时段 9–16 点（收盘和周末不请求，省资源）/ 全天请求。")
        self._bullet(sec, "右侧指数：填指数代码，如 sh000001（上证）或 sz399001（深证成指）。")
        self._bullet(sec, "涨跌配色：红涨绿跌 / 绿涨红跌。")
        self._bullet(sec, "持仓显示：金额 / 涨幅；也可以单击行情条中间段快速切换。")
        self._bullet(sec, "详情卡：简洁（不含分时图）/ 详细（分时、买卖一、OHLC、量额）。")
        self._sub(sec, "交易手续费")
        self._bullet(sec, "股票佣金、ETF 佣金、卖出印花税；持仓窗口按这里的费率自动估算费用。")
        self._sub(sec, "AI 研判")
        self._bullet(sec, "自动研判：开启后 9:25 对当前股票自动跑一次，每天一次。")
        self._bullet(sec, "服务商：DeepSeek / MIMO / 自定义（OpenAI 兼容接口）。")
        self._bullet(sec, "接口地址、模型名、API Key；Key 可留空，程序会读环境变量 STOCKBAR_LLM_KEY。")
        self._sub(sec, "更新与支持")
        self._bullet(sec, "更新：显示当前版本，可立即检查；发现新版本时出现“更新”按钮。")
        self._bullet(sec, "支持开发者：扫码窗；源码仓库：一键复制地址。")

    # ---- 05 自选 ----
    def _build_watchlist(self, parent):
        sec = self._card(parent, "05", "管理自选股")
        self._step(sec, 1, "右键菜单「管理自选股…」，或按 Ctrl+M 打开。")
        self._step(sec, 2, "在搜索框输入名称、代码或拼音首字母，如 茅台、510300、gzmt；"
                           "稍等片刻出现结果。")
        self._step(sec, 3, "选中结果后点「添加 →」加入右侧列表；选中已有项可「← 移除」。")
        self._step(sec, 4, "用「↑ 上移 / ↓ 下移」调整顺序；列表顺序就是行情条切换顺序。")
        self._step(sec, 5, "点「保存并应用」立即生效，无需重启。")
        self._p(sec, "建议自选控制在 5–15 只：太多会拉长切换路径，也会增加每次刷新请求量。",
                color=self.pal["muted"], pady=(6, 0))

    # ---- 06 持仓 ----
    def _build_holdings(self, parent):
        sec = self._card(parent, "06", "持仓设置（模拟盘）")
        self._p(sec, "这里只是本地模拟记账，不连接券商、不会下单。交易对象来自自选股列表与已有持仓；"
                     "新股票请先加入自选。")
        self._step(sec, 1, "右键菜单「持仓设置…」，或按 Ctrl+H 打开。")
        self._step(sec, 2, "在上方持仓明细中选中一只股票；选中后价格会自动填入当前价。")
        self._step(sec, 3, "填写成交价与数量（单位：手，1 手 = 100 股），备注可留空。")
        self._step(sec, 4, "点「买入 / 卖出」记录成交；在输入框按回车等于买入。卖出可用「全部卖出」。")
        self._step(sec, 5, "费用按设置里的佣金、起收、印花税自动计算，不需要手工填。")
        self._p(sec, "盈亏口径：老仓按昨收、今日买入按买价计算今日盈亏；卖出按 FIFO 先消耗最早份额。"
                     "清仓后仍保留已实现盈亏，今日清仓的股票也会显示在列表里。",
                color=self.pal["muted"])
        self._bullet(sec, "「删除所选记录」可撤销误录的成交，持仓与盈亏会立即重算。")
        self._bullet(sec, "两个列表之间的横条可上下拖动，窗口大小与布局会自动记住。")
        self._bullet(sec, "空表会显示「暂无持仓」或「暂无成交记录」，不是加载失败。")

    # ---- 07 卡片与 AI ----
    def _build_cards(self, parent):
        sec = self._card(parent, "07", "详情卡、竞价评分与 AI")
        self._sub(sec, "三种卡片")
        self._bullet(sec, "个股卡：悬停个股段，或用 1；显示分时、买卖一、现价与涨跌。")
        self._bullet(sec, "持仓总览：悬停持仓段，或用 2；每只持仓 + 底部汇总。")
        self._bullet(sec, "竞价评分：按 3；9:15–9:25 显示实时竞价，其他时段显示当日或最近一次评分。")
        self._p(sec, "按 1 / 2 / 3 时鼠标必须停在行情条上；移开卡片会自动关闭。"
                     "按 Esc 可立即关闭。", color=self.pal["muted"])
        self._sub(sec, "竞价评分")
        self._bullet(sec, "5 项可解释评分：高开幅度、撮合趋势、买卖力量、量价配合、价格稳定。")
        self._bullet(sec, "顶部徽章给出偏多 / 中性 / 偏空；红色偏多、绿色偏空，跟随涨跌配色设置。")
        self._bullet(sec, "规则评分 = 五项得分直接相加，不依赖大模型；没有配置 AI 也能使用。"
                          "训练权重只作模型参考，不覆盖规则总分。")
        self._sub(sec, "AI 研判")
        self._bullet(sec, "在设置里填服务商、接口、模型、API Key；开启“自动研判”后 9:25 自动跑一次。")
        self._bullet(sec, "也可在竞价卡上点「AI 研判」，或悬停时按 T 立即研判当前股票。")
        self._bullet(sec, "输出包含综合研判、核心理由、风险、操作倾向；需要联网，通常几秒到几十秒。")
        self._p(sec, "AI 与评分都只作为辅助信息，不构成投资建议。", color=self.pal["warn"])

    # ---- 08 选股工具 ----
    def _build_tools(self, parent):
        sec = self._card(parent, "08", "收盘选股、强弱排序、异动扫描")
        self._sub(sec, "收盘选股")
        self._bullet(sec, "7 个策略可勾选：海龟突破、RPS 突破、放量突破、高窄旗形、涨停洗盘、跌停反包、强势回调。")
        self._bullet(sec, "先全市场初筛，再对候选做深度分析；候选数量可设 5–500，越大越慢。")
        self._bullet(sec, "命中策略越多越可靠；结果会列出每条策略的依据，双击一行可加入自选。")
        self._bullet(sec, "勾选策略与候选数量会自动记住；运行期间请耐心等待，可随时关闭窗口。")
        self._sub(sec, "自选强弱排序")
        self._bullet(sec, "按 60 日跑赢沪深 300 的幅度（RPS 近似）与竞价评分排序。")
        self._bullet(sec, "同时给出 20 日跑赢、形态/强度提示；数据缓存 30 分钟，点「刷新」可重算。")
        self._sub(sec, "当日异动扫描")
        self._bullet(sec, "全市场筛选涨幅 ≥4% 且成交额 ≥2 亿，按涨幅降序，最多显示 40 只。")
        self._bullet(sec, "双击一行直接加入自选；已在自选中的会提示，不会重复添加。")
        self._p(sec, "选股与扫描结果仅供筛选参考，不构成买卖建议；行情接口免费，可能存在延迟。",
                color=self.pal["warn"])

    # ---- 09 数据 ----
    def _build_data(self, parent):
        sec = self._card(parent, "09", "更新、配置与数据")
        self._sub(sec, "更新")
        self._bullet(sec, "启动时静默检查新版本，结果缓存 6 小时；发现新版本时右键菜单顶部会出现更新入口。")
        self._bullet(sec, "点「更新」会自动下载、校验、解压并重启到新版；配置文件不变，失败自动回滚。")
        self._bullet(sec, "也可以手动下载新包：退出程序，只替换 exe 与 _internal，保留自己的 config.json。")
        self._sub(sec, "程序目录里的文件")
        self._kv(sec, "config.json", "自选、持仓、外观、AI、手续费等全部设置；首次运行自动生成。")
        self._kv(sec, "config.json.bak", "配置损坏时的自动备份；可用它恢复。")
        self._kv(sec, "stock_bar.log", "运行日志；行情异常、窗口报错时先看这里。")
        self._kv(sec, "auction_data\\", "每日竞价存档与 weights.json（模型参考权重，不覆盖规则总分）；用于复盘和自动训练。")
        self._kv(sec, "update_cache.json", "更新检查缓存；删除后会重新检查。")
        self._kv(sec, "stock_bar.lock", "单实例锁；程序运行期间存在，退出后自动删除。")
        self._sub(sec, "手动改配置")
        self._p(sec, "高级用户仍可直接编辑 config.json：先退出程序并备份，保存为 UTF-8，"
                     "再启动；已运行时按 F5 可重新加载。JSON 写错会被忽略并回退默认值。",
                color=self.pal["muted"])
        self._p(sec, "整个文件夹可直接复制到另一台电脑，配置与持仓一起带走。", color=self.pal["muted"])

    # ---- 10 详细窗口 ----
    def _build_detail(self, parent):
        sec = self._card(parent, "10", "股票详细大窗口")
        self._p(sec, "左侧分时图左键单击，或按 Enter，即可打开大窗口。窗口为单例："
                     "重复打开会复用同一个窗口并切换到新股票，不会叠出一堆窗口。")
        self._sub(sec, "三个入口")
        self._bullet(sec, "个股卡：悬停自选段后，左键单击卡片里的分时图；或按 Enter。")
        self._bullet(sec, "指数卡：左键单击指数分时图；或鼠标停在指数段按 Enter。")
        self._bullet(sec, "持仓卡：左键单击整张持仓总览卡，进入「持仓设置」；"
                          "持仓卡不打开分时大窗口。")
        self._sub(sec, "1.8.1 修复")
        self._bullet(sec, "详细窗口的涨跌颜色会跟随「红涨绿跌 / 绿涨红跌」设置即时重绘；"
                          "切换深浅主题时详细窗口也会自动重建，不再沿用旧配色。")
        self._bullet(sec, "港股/美股行情时间格式已兼容，涨跌幅不再恒为 0%；"
                          "A 股开盘前的昨日行情仍按 0% 处理。")
        self._sub(sec, "窗口结构（1.8.0）")
        self._bullet(sec, "左栏：全高自选股列表，与右侧图表分开；顶部搜索框宽度跟随左栏，"
                          "下面列表独立滚动。搜索支持代码/名称/拼音；点结果切股，"
                          "点 + 加入自选，已在自选显示 ✓。")
        self._bullet(sec, "分隔线：左栏和右侧之间的竖线可左右拖动调宽（约 180–360），"
                          "双击恢复默认；宽度会自动记住。")
        self._bullet(sec, "右栏顶部：名称、代码、市场、现价与涨跌；右侧八格为今开/最高/最低/昨收、"
                          "成交量/额/换手/量比（指数为成交量/额/振幅/时间）。")
        self._bullet(sec, "右栏下方整页纵向滚动，顺序为「图表 + 五档/逐笔」→「周期/指标」→"
                          "「板块 / 概念」→「狙击点位」→「AI 研判」。"
                          "五档/逐笔与 K 线严格等高；指数没有五档与逐笔，右栏自动隐藏，"
                          "图表占满右栏，板块/点位也隐藏。")
        self._bullet(sec, "周期：分时 / 5日 / 日K / 周K / 月K；指标：量 / MACD / KDJ。"
                          "周期和指标不再固定底部，会随整页一起上下滚动。")
        self._sub(sec, "滚轮规则（互不打扰）")
        self._bullet(sec, "图表内滚轮：仍是缩放横轴；分时 30–240 分钟，K 线 15 根到全部。")
        self._bullet(sec, "五档/逐笔内滚轮：仍是逐笔翻页；左栏内滚轮：滚动自选列表；"
                          "搜索框内滚轮不劫持。")
        self._bullet(sec, "右侧滚动条、周期/指标行、板块/点位等中立位置滚轮：整页上下滚动，"
                          "用来翻到下方 AI。点周期行右侧「AI ↓」可直接滚到 AI；"
                          "AI 标题旁「↑ 回到图表」回到顶部。")
        self._sub(sec, "板块 / 概念（1.8.0）")
        self._bullet(sec, "数据来自东方财富个股所属板块接口；按「行业 → 地域 → 概念」排序，"
                          "普通模式下最多显示行业 2 个、地域 1 个、概念 6 个，其余用总数提示。")
        self._bullet(sec, "标签带当日板块涨幅；红涨绿跌跟随主题。指数/风格/资金属性标签"
                          "（沪深300、上证50、融资融券、基金重仓、大盘/中盘/小盘等）会被过滤。")
        self._bullet(sec, "按股票代码缓存 30 分钟；网络失败静默降级为空标签，不影响行情和 AI。")
        self._sub(sec, "狙击点位（1.8.0）")
        self._bullet(sec, "点位由程序按 180 根前复权日线计算，AI 不自由报价；"
                          "AI 只能从带编号的候选里选，价格不能新增或改写。")
        self._bullet(sec, "理想买入：现价下方最近的支撑区（MA5/MA10/MA20/MA60、"
                          "近 20/60 日低点、前平台高点、0.382/0.5/0.618 回撤）。")
        self._bullet(sec, "二次买入：理想区下方的下一档支撑；没有合适下一档时显示“—”，不硬凑；"
                          "止损放在二次支撑下方 0.3–0.5 ATR，并参考 MA60/近低点与 6% 最大风险位；"
                          "止盈用近 20/60 日高点、前平台高点或 1.618 扩展。")
        self._bullet(sec, "AI 选点后由程序复核：二次买入必须低于理想买入、止损必须低于二次买入、"
                          "止盈必须高于理想买入、风报比不足 1.5 或编号不存在时自动回退规则默认，"
                          "右上角标「AI 选择 / 规则回退」。")
        self._bullet(sec, "点位状态随 2 秒行情刷新：等待回踩、已进入区间、距止损、距止盈、计划失效；"
                          "只做辅助参考，不构成投资建议。")
        self._sub(sec, "AI 研判（详细窗口内）")
        self._bullet(sec, "分模块：01 评分（0–100 + 区间）、02 推荐（动作 + 结论）、03 建议/理由、"
                          "04 风险、05 观察计划（触发/失效/下次检查）、06 数据质量 + 免责声明。")
        self._bullet(sec, "阶段按本地时钟判断：盘前 / 竞价 / 撮合 / 开盘 / 早盘 / 午间 / 盘中 / 尾盘 / 盘后 / 休市；"
                          "Prompt 会带上当前阶段、持仓、行情/分时/K线/竞价和 MCP 摘要。")
        self._bullet(sec, "评分口径：80–100 强烈买入、60–79 买入、40–59 观望、20–39 减仓、0–19 卖出；"
                          "动作支持买入/加仓/持有/减仓/卖出/观望/回避/预警。评分与动作冲突时显示「口径待复核」。")
        self._bullet(sec, "数据质量由程序本地计算：行情延迟/非今日、分时/竞价日期、基本面摘要缺失、"
                          "五档/逐笔缺失、指数无盘口等；不让模型编造数据。")
        self._bullet(sec, "启用狙击点位后，Prompt 追加规则候选编号；AI 只能返回 I/S/L/T 编号，"
                          "不能写价格。程序负责复核，失败自动回退规则默认。")
        self._bullet(sec, "第一次滚动到 AI 且当前阶段无缓存时自动研判；按代码+日期+阶段缓存；"
                          "右上角「重新研判」可强制刷新。未启用或没填 API Key 时只显示设置入口，不请求接口。")
        self._sub(sec, "鼠标操作")
        self._bullet(sec, "拖动：按住左键左右拖动平移；双击复位到全览。")
        self._bullet(sec, "单击：锁定十字光标，再单击解锁；竖线联动主图、成交量与指标区。")
        self._bullet(sec, "点右侧五档价格：在主图画一条黄色价位线，再点同一价格取消。")
        self._bullet(sec, "成交明细：滚轮上下翻；翻到最早一页会继续加载更早的逐笔。")
        self._sub(sec, "刷新与数据")
        self._bullet(sec, "行情 2 秒、分时 30 秒、逐笔 2 秒、K 线 60 秒；窗口不可见时暂停，"
                          "不会在后台空跑。")
        self._bullet(sec, "5日分时没有稳定的公开接口，程序从首次使用起按日本地累积；"
                          "首日只显示 1 天，之后逐日补到 5 天。")
        self._bullet(sec, "逐笔来自腾讯最近成交分页，不是 Level-2；免费接口可能有延迟。")
        self._sub(sec, "窗口")
        self._bullet(sec, "位置、大小与左栏宽度自动记忆；右上角「置顶」可切换是否总在最前；Esc 关闭。")
        self._p(sec, "K 线中的 MA5/MA10/MA20 颜色与图例一致；MACD 的 DIF/DEA、"
                     "KDJ 的 K/D/J 也在指标区左上角标注。",
                color=self.pal["muted"], pady=(6, 0))

    # ---- 11 快捷键 ----
    def _build_shortcuts(self, parent):
        sec = self._card(parent, "11", "快捷键一览")
        for key, value in (
                ("F1", "打开本使用帮助"),
                ("Ctrl+Shift+S", "打开设置"),
                ("Ctrl+M", "管理自选股"),
                ("Ctrl+H", "持仓设置"),
                ("Ctrl+C", "复制当前行情（需先点一下行情条）"),
                ("F5", "重新加载 config.json"),
                ("← / → / ↑ / ↓", "切换上一只 / 下一只自选"),
                ("1 / 2 / 3", "个股卡 / 持仓总览 / 竞价评分卡"),
                ("Enter", "打开当前悬停分区的大窗口（个股/指数）或持仓设置"),
                ("T", "竞价卡 + 立即 AI 研判"),
                ("Esc", "关闭详情卡或当前窗口"),
        ):
            self._kv(sec, key, value)
        self._p(sec, "1 / 2 / 3 / T / Enter 需要鼠标停在行情条上；Ctrl 系列快捷键需要先让行情条获得焦点"
                     "（单击一下即可）。", color=self.pal["muted"], pady=(6, 0))

    # ---- 11 FAQ ----
    def _build_faq(self, parent):
        sec = self._card(parent, "12", "常见问题")
        for q, a in (
                ("行情不刷新 / 收盘后不动",
                 "设置 → 行情 → 请求时段改成「全天请求」；交易时段模式下 16 点后和周末不请求。"),
                ("行情条找不到了",
                 "右键菜单「回到右下角」；换显示器后也用它复位。"),
                ("文字看不清 / 背景太透",
                 "设置 → 外观：提高文字不透明度或字号；背景不透明度调到 0% 可得纯文字模式。"),
                ("任务栏位置被图标挡住",
                 "关闭再开启「嵌入任务栏」，让它重新找空位；或直接拖到空位。"),
                ("开机自启失败",
                 "多为安全软件启动项防护。把程序目录加入信任区，或手动把快捷方式放进启动文件夹。"),
                ("更新失败",
                 "多为网络或 GitHub 限流。稍后重试，或手动下载 zip，只替换 exe 与 _internal。"),
                ("持仓盈亏显示 -- 或 0",
                 "没有持仓、非交易时段或行情尚未就绪时会这样；开盘后会自动更新。"),
                ("被杀毒软件报毒",
                 "打包 exe 常被误报。确认来自本项目发布页后，把程序目录加入信任区。"),
                ("能否多开",
                 "不建议。程序用 stock_bar.lock 限制单实例，多开会争抢配置与日志。"),
        ):
            self._p(sec, "Q： " + q, color=self.pal["text"], pady=(6, 1))
            self._p(sec, "A： " + a, color=self.pal["text_dim"], pady=(0, 2))
        self._p(sec, "数据来自免费公开行情接口，可能有延迟；所有评分、选股与 AI 结论仅供参考，"
                     "不构成投资建议。", color=self.pal["warn"], pady=(10, 0))


# ---------------- 自选股管理 ----------------
def _section_card(parent, title, pal, fonts, scale):
    """通用卡片容器(标题 + 内容区), 供各窗口复用; 自动铺满 parent。"""
    box = tk.Frame(parent, bg=pal["panel"])
    box.pack(fill="both", expand=True)          # 关键: 容器自身要铺开, 否则内容不显示
    inner = tk.Frame(box, bg=pal["panel_alt"], highlightbackground=pal["border_soft"],
                     highlightthickness=1)
    inner.pack(fill="both", expand=True)
    tk.Label(inner, text=title, anchor="w", bg=pal["panel_alt"], fg=pal["text_dim"],
             font=fonts.label()).pack(anchor="w", padx=12, pady=(8, 2))
    return inner


def _tree_hint(tree, text, pal, fonts):
    """空表格占位提示: 叠在表格中心并置顶。"""
    lab = tk.Label(tree, text=text, bg=pal["panel_soft"], fg=pal["faint"],
                   font=fonts.label())
    lab.place(relx=0.5, rely=0.5, anchor="center")
    lab.lift()
    return lab


def _hint_show(lab, show):
    try:
        if show:
            lab.place(relx=0.5, rely=0.5, anchor="center")
            lab.lift()
        else:
            lab.place_forget()
    except tk.TclError:
        pass


class SelectWindow(BaseDialog):
    """收盘选股: 策略可勾选(海龟/RPS/放量/旗形等), 双击加自选。"""
    key = "select"

    def __init__(self, app):
        super().__init__(app, "收盘选股", 900, 720)
        setup_ttk(self.win, self.pal, self.fonts, self.S)     # 表格样式跟随主题
        self.header("收盘选股", "全市场初筛＋策略命中，收盘后运行效果最好；双击一行加入自选")
        pal = self.pal
        # 先占住底部按钮栏, 再让内容区填充(否则按钮会被挤出可视区)
        foot = tk.Frame(self.body, bg=pal["panel"])
        foot.pack(fill="x", side="bottom", padx=18, pady=(2, 10))
        CanvasButton(foot, "关闭", self.close, pal, self.fonts, kind="primary",
                     width=84, height=32, scale=self.S).pack(side="right")
        body = tk.Frame(self.body, bg=pal["panel"])
        body.pack(fill="both", expand=True, pady=(6, 0))
        card = _section_card(body, "策略与筛选", pal, self.fonts, self.S)
        row = tk.Frame(card, bg=pal["panel_alt"])
        row.pack(fill="x", padx=12, pady=(2, 0))
        self._checks = {}
        saved = set(self.app.cfg.get("select_strategies") or D.STRATEGIES)
        for name in D.STRATEGIES:
            var = tk.BooleanVar(value=(name in saved))
            self._checks[name] = var
            tk.Checkbutton(row, text=name, variable=var, bg=pal["panel_alt"],
                           fg=pal["text_dim"], selectcolor=pal["panel"],
                           activebackground=pal["panel_alt"],
                           activeforeground=pal["text"],
                           font=self.fonts.tiny()).pack(side="left", padx=(0, 8))
        row2 = tk.Frame(card, bg=pal["panel_alt"])
        row2.pack(fill="x", padx=12, pady=(4, 8))
        tk.Label(row2, text="筛选深度", bg=pal["panel_alt"], fg=pal["muted"],
                 font=self.fonts.tiny()).pack(side="left")
        self.depth_var = tk.StringVar(value=str(self.app.cfg.get("select_depth") or 60))
        ent = make_entry(row2, pal, self.fonts, width=6, textvariable=self.depth_var,
                         numeric=True)
        ent.pack(side="left", ipady=3, padx=(6, 6))
        tk.Label(row2, text="候选 5~500，越大越慢",
                 bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(side="left")
        CanvasButton(row2, "开始选股", self._start, pal, self.fonts, kind="primary",
                     width=104, height=32, scale=self.S).pack(side="left", padx=(14, 0))
        self.status = tk.Label(row2, text="", bg=pal["panel_alt"],
                               fg=pal["faint"], font=self.fonts.tiny())
        self.status.pack(side="left", padx=(12, 0))
        cols = [("idx", "#", 34, "c"), ("name", "名称", 118, "w"),
                ("code", "代码", 80, "w"), ("price", "现价", 76, "e"),
                ("pct", "涨幅", 70, "e"), ("amount", "成交额", 84, "e"),
                ("hits", "命中数", 62, "c"), ("reasons", "命中策略(依据)", 240, "w")]
        self.tree = ttk.Treeview(card, columns=[c[0] for c in cols], show="headings",
                                 style="SB.Treeview", height=14)
        for cid, label, width, anchor in cols:
            self.tree.heading(cid, text=label, anchor=anchor)
            self.tree.column(cid, width=int(width * self.S), anchor=anchor, stretch=True)
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree.bind("<Double-Button-1>", self._add)
        self.empty_hint = _tree_hint(self.tree, "点击「开始选股」，扫描全市场",
                                     pal, self.fonts)
        tk.Label(card, text="命中策略越多越可靠；数据每 30 分钟缓存",
                 bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(anchor="w", padx=12, pady=(0, 8))
        self._busy = False

    def _start(self):
        if getattr(self, "_busy", False):
            return
        use = [n for n, v in self._checks.items() if v.get()]
        if not use:
            self.status.config(text="请至少勾选一个策略")
            return
        try:
            depth = max(5, min(500, int(self.depth_var.get() or 60)))
        except ValueError:
            depth = 60
        self.app.cfg["select_strategies"] = use
        self.app.cfg["select_depth"] = depth
        self.app.save_config()
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.empty_hint.config(text="选股中…")
        _hint_show(self.empty_hint, True)
        self._busy = True
        self.status.config(text="选股中… 0/%d" % depth)

        def prog(i, n, name):
            self._prog_text = "选股中… %d/%d %s" % (i, n, name)

        def work():
            err = None
            rows = []
            try:
                rows = D.select_stocks(check=depth, strategies=use, on_progress=prog)
            except Exception as exc:
                err = str(exc)[:80]
            self._result = (rows, err)      # 主线程轮询取走(线程里不能调 Tk)

        self._prog_text = "选股中… 0/%d" % depth
        self._result = None
        threading.Thread(target=work, daemon=True).start()
        self._poll_result()

    def _poll_result(self):
        """主线程轮询后台结果(Tk 的 after 不能在子线程调用)。"""
        if self._result is not None:
            rows, err = self._result
            self._result = None
            self._done(rows, err)
            return
        if getattr(self, "_prog_text", None):
            self.status.config(text=self._prog_text)
        try:
            self.win.after(150, self._poll_result)
        except Exception:
            self._busy = False

    def _done(self, rows, err):
        self._busy = False
        if err:
            self.status.config(text="选股失败: %s" % err)
            self.empty_hint.config(text="选股失败")
            _hint_show(self.empty_hint, True)
            return
        if not rows:
            self.status.config(text="无命中(可加大筛选深度或勾选更多策略)")
            self.empty_hint.config(text="无命中结果")
            _hint_show(self.empty_hint, True)
            return
        for i, r in enumerate(rows, 1):
            self.tree.insert("", "end", iid=r["code"], values=(
                i, r["name"], D.code_digits(r["code"]),
                D.fmt_price(r["price"], 2), D.fmt_pct(r["pct"]),
                D.fmt_yi_wan(r["amount"]), len(r.get("hits") or {}),
                r.get("reasons", "")))
        _hint_show(self.empty_hint, False)
        self.status.config(text="共 %d 只命中(按命中策略数排序)" % len(rows))

    def _add(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        code = sel[0]
        vals = self.tree.item(sel[0], "values")
        app = self.app
        picks = [D.normalize_code(c) for c in (app.cfg.get("stocks") or []) if c]
        if code in picks:
            self.status.config(text="%s 已在自选中" % vals[1])
            return
        picks.append(code)
        app.cfg["stocks"] = picks
        app.save_config()
        try:
            app._load_lists()
            app._restart_worker()
        except Exception:
            pass
        self.status.config(text="已加入自选: %s" % vals[1])


class StrengthWindow(BaseDialog):
    """自选强弱排序: 按 RPS 近似(60日跑赢基准) + 竞价评分排序。"""
    key = "strength"

    def __init__(self, app):
        super().__init__(app, "自选强弱排序", 780, 620)
        setup_ttk(self.win, self.pal, self.fonts, self.S)     # 表格样式跟随主题
        self.header("自选强弱排序", "按 60 日相对强度（RPS 近似）与竞价评分排序")
        pal = self.pal
        foot = tk.Frame(self.body, bg=pal["panel"])
        foot.pack(fill="x", side="bottom", padx=18, pady=(2, 10))
        self.status = tk.Label(foot, text="加载中…", bg=pal["panel"], fg=pal["faint"],
                               font=self.fonts.tiny())
        self.status.pack(side="left")
        CanvasButton(foot, "刷新", self._load, pal, self.fonts, kind="ghost",
                     width=84, height=32, scale=self.S).pack(side="right")
        CanvasButton(foot, "关闭", self.close, pal, self.fonts, kind="primary",
                     width=84, height=32, scale=self.S).pack(side="right", padx=(0, 8))
        body = tk.Frame(self.body, bg=pal["panel"])
        body.pack(fill="both", expand=True, pady=(6, 0))
        card = _section_card(body, "强弱排序", pal, self.fonts, self.S)
        cols = [("idx", "#", 34, "c"), ("name", "名称", 120, "w"),
                ("code", "代码", 84, "w"), ("alpha", "60日跑赢基准", 110, "e"),
                ("a20", "20日跑赢", 96, "e"), ("score", "竞价评分", 84, "e"),
                ("flags", "形态/强度", 180, "w")]
        self.tree = ttk.Treeview(card, columns=[c[0] for c in cols], show="headings",
                                 style="SB.Treeview", height=14)
        for cid, label, width, anchor in cols:
            self.tree.heading(cid, text=label, anchor=anchor)
            self.tree.column(cid, width=int(width * self.S), anchor=anchor, stretch=True)
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)
        self.empty_hint = _tree_hint(self.tree, "正在计算…", pal, self.fonts)
        tk.Label(card, text="RPS 近似 = 该股涨幅 - 沪深300 同期涨幅；数据每 30 分钟缓存",
                 bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(anchor="w", padx=12, pady=(0, 8))
        self.win.after(60, self._load)

    def _load(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.status.config(text="加载中…")
        self.empty_hint.config(text="正在计算…")
        _hint_show(self.empty_hint, True)
        app = self.app
        try:
            rows = []
            for code in app.pick_codes:
                if not code:
                    continue
                q = app.quotes.get(code)
                st = D.mcp_strength(code) or {}
                a60 = (st.get("alpha") or {}).get("60")
                a20 = (st.get("alpha") or {}).get("20")
                samples = (getattr(app, "_auction", {}) or {}).get(code, [])
                sc = D.auction_score(samples, q.prev_close if q else
                                     (getattr(app, "_auction_prev", {}) or {}).get(code, 0),
                                     getattr(app, "_score_weights", None)) if samples else None
                rows.append((code, q.name if q else code, a60, a20,
                             sc["total"] if sc else None,
                             "、".join((st.get("flags") or [])[:2])))
        except Exception as exc:
            self.status.config(text="加载失败: %s" % str(exc)[:80])
            self.empty_hint.config(text="加载失败")
            return
        if not rows:
            self.status.config(text="无自选或数据未就绪(需联网; 数据 30 分钟缓存)")
            self.empty_hint.config(text="暂无数据")
            return
        rows.sort(key=lambda r: (-(r[2] if r[2] is not None else -999),
                                 -(r[4] if r[4] is not None else -999)))
        for i, (code, name, a60, a20, score, flags) in enumerate(rows, 1):
            self.tree.insert("", "end", values=(
                i, name, D.code_digits(code),
                ("%.1f%%" % a60) if a60 is not None else "-",
                ("%.1f%%" % a20) if a20 is not None else "-",
                ("%+d" % score) if score is not None else "-", flags))
        _hint_show(self.empty_hint, False)
        self.status.config(text="共 %d 只(按 60 日相对强度排序)" % len(rows))


class ScanWindow(BaseDialog):
    """当日异动扫描: 全市场放量大涨股(1 个请求)。"""
    key = "scan"

    def __init__(self, app):
        super().__init__(app, "当日异动扫描", 780, 640)
        setup_ttk(self.win, self.pal, self.fonts, self.S)     # 表格样式跟随主题
        self.header("当日异动扫描", "全市场筛选：涨幅 ≥4% 且成交额 ≥2 亿，按涨幅降序")
        pal = self.pal
        foot = tk.Frame(self.body, bg=pal["panel"])
        foot.pack(fill="x", side="bottom", padx=18, pady=(2, 10))
        self.status = tk.Label(foot, text="加载中…", bg=pal["panel"], fg=pal["faint"],
                               font=self.fonts.tiny())
        self.status.pack(side="left")
        CanvasButton(foot, "刷新", self._load, pal, self.fonts, kind="ghost",
                     width=84, height=32, scale=self.S).pack(side="right")
        CanvasButton(foot, "关闭", self.close, pal, self.fonts, kind="primary",
                     width=84, height=32, scale=self.S).pack(side="right", padx=(0, 8))
        body = tk.Frame(self.body, bg=pal["panel"])
        body.pack(fill="both", expand=True, pady=(6, 0))
        card = _section_card(body, "异动列表", pal, self.fonts, self.S)
        cols = [("idx", "#", 34, "c"), ("name", "名称", 120, "w"),
                ("code", "代码", 84, "w"), ("price", "现价", 78, "e"),
                ("pct", "涨幅", 72, "e"), ("amount", "成交额", 88, "e")]
        self.tree = ttk.Treeview(card, columns=[c[0] for c in cols], show="headings",
                                 style="SB.Treeview", height=16)
        for cid, label, width, anchor in cols:
            self.tree.heading(cid, text=label, anchor=anchor)
            self.tree.column(cid, width=int(width * self.S), anchor=anchor, stretch=True)
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)
        self.tree.bind("<Double-Button-1>", self._add)
        self.empty_hint = _tree_hint(self.tree, "正在扫描全市场…", pal, self.fonts)
        tk.Label(card, text="双击一行加入自选；数据为全市场实时快照",
                 bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(anchor="w", padx=12, pady=(0, 8))
        self.win.after(60, self._load)

    def _load(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.status.config(text="加载中…")
        self.empty_hint.config(text="正在扫描全市场…")
        _hint_show(self.empty_hint, True)
        try:
            rows = D.scan_market(limit=40)
        except Exception as exc:
            self.status.config(text="加载失败: %s" % str(exc)[:80])
            self.empty_hint.config(text="加载失败")
            return
        if not rows:
            self.status.config(text="无异动股(或接口暂不可用, 请稍后刷新)")
            self.empty_hint.config(text="暂无数据")
            return
        for i, r in enumerate(rows, 1):
            self.tree.insert("", "end", iid=r["code"], values=(
                i, r["name"], D.code_digits(r["code"]),
                D.fmt_price(r["price"], 2), D.fmt_pct(r["pct"]),
                D.fmt_yi_wan(r["amount"])))
        _hint_show(self.empty_hint, False)
        self.status.config(text="共 %d 只异动(涨幅≥4%% 且成交额≥2亿)" % len(rows))

    def _add(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        code = sel[0]                      # 行的 iid 就是完整代码(sh600519)
        vals = self.tree.item(sel[0], "values")
        app = self.app
        picks = [D.normalize_code(c) for c in (app.cfg.get("stocks") or []) if c]
        if code in picks:
            self.status.config(text="%s 已在自选中" % vals[1])
            return
        picks.append(code)
        app.cfg["stocks"] = picks
        app.save_config()
        try:
            app._load_lists()
            app._restart_worker()          # 让新自选立即开始抓行情
        except Exception:
            pass
        self.status.config(text="已加入自选: %s" % vals[1])


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
                     width=104, height=32, scale=self.S).pack(side="right")
        CanvasButton(foot, "取消", self.close, pal, self.fonts, kind="ghost",
                     width=84, height=32, scale=self.S).pack(side="right", padx=(0, 8))

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
                     width=66, height=32, scale=self.S, bg=pal["panel_alt"]).pack(
            side="left", padx=8, pady=7)
        tk.Label(search, text="支持名称、代码、拼音首字母，如 茅台、510300", bg=pal["panel_alt"],
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
        tk.Label(middle, text="列表顺序\n即切换顺序", bg=pal["panel"], fg=pal["faint"],
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
                    "买入、卖出填成交价，费用按费率自动估算，盈亏自动累计")
        self.selected = None
        self.holdings = {c: {"trades": list(h.get("trades") or [])}
                         for c, h in (app.cfg.get("holdings") or {}).items()
                         if isinstance(h, dict)}
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
    def _tree_hint(self, tree, text):
        """空表格占位提示: 叠在表格中心, 有数据时隐藏。"""
        lab = tk.Label(tree, text=text, bg=self.pal["panel_soft"],
                       fg=self.pal["faint"], font=self.fonts.label())
        lab.place(relx=0.5, rely=0.55, anchor="center")
        lab.lift()
        return lab

    @staticmethod
    def _hint_show(lab, show):
        try:
            if show:
                lab.place(relx=0.5, rely=0.55, anchor="center")
                lab.lift()
            else:
                lab.place_forget()
        except tk.TclError:
            pass

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
                     width=84, height=32, scale=S).pack(side="right")

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
        self.top_hint = self._tree_hint(self.tree, "暂无持仓，下方交易后自动记录")
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
        self.sel_label = tk.Label(head, text="在上方选中一只股票后交易", bg=pal["panel_alt"],
                                  fg=pal["text_dim"], font=self.fonts.label())
        self.sel_label.pack(side="left")
        self.hint_label = tk.Label(head, text="", bg=pal["panel_alt"], fg=pal["faint"],
                                   font=self.fonts.tiny())
        self.hint_label.pack(side="right")

        form = tk.Frame(trade, bg=pal["panel_alt"])
        form.pack(fill="x", padx=14, pady=(4, 2))
        for i in range(3):
            form.columnconfigure(i, weight=1, uniform="form")   # 三列等宽, 输入框占满整行

        def field(col, label, numeric=True):
            box = tk.Frame(form, bg=pal["panel_alt"])
            box.grid(row=0, column=col, sticky="ew",
                     padx=(0, 18 if col < 2 else 0))
            holder = tk.Frame(box, bg=pal["panel_alt"], width=int(58 * S))
            holder.pack(side="left", fill="y")
            holder.pack_propagate(False)
            tk.Label(holder, text=label, bg=pal["panel_alt"], fg=pal["muted"],
                     font=self.fonts.label(), anchor="w").pack(side="left")
            var = tk.StringVar()
            ent = make_entry(box, pal, self.fonts, width=10, textvariable=var,
                             numeric=numeric)
            ent.pack(side="left", fill="x", expand=True, ipady=5)
            return var, ent

        self.price_var, self.price_entry = field(0, "价格")
        self.lots_var, self.lots_entry = field(1, "数量(手)")
        self.note_var, self.note_entry = field(2, "备注", numeric=False)
        self.fee_var = None            # 费用按设置费率自动计算, 不再手工填写

        btns = tk.Frame(trade, bg=pal["panel_alt"])
        btns.pack(fill="x", padx=14, pady=(6, 12))
        CanvasButton(btns, "买入", lambda: self._trade("buy"), pal, self.fonts,
                     kind="primary", width=92, height=32, scale=S,
                     bg=pal["panel_alt"]).pack(side="left")
        CanvasButton(btns, "卖出", lambda: self._trade("sell"), pal, self.fonts,
                     width=92, height=32, scale=S, bg=pal["panel_alt"]).pack(side="left", padx=(12, 0))
        CanvasButton(btns, "全部卖出", self._sell_all, pal, self.fonts, kind="quiet",
                     width=104, height=32, scale=S, bg=pal["panel_alt"]).pack(side="left", padx=(12, 0))
        tk.Label(btns, text="回车 = 买入", bg=pal["panel_alt"], fg=pal["faint"],
                 font=self.fonts.tiny()).pack(side="right", padx=(0, 2))

        for w in (self.price_entry, self.lots_entry, self.note_entry):
            w.bind("<Return>", lambda e: self._trade("buy"))

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
        # 先占住底部按钮行, 再让列表填充剩余高度(否则列表会把按钮挤出框外)
        btnrow = tk.Frame(log, bg=pal["panel_alt"])
        btnrow.pack(side="bottom", fill="x", padx=10, pady=(0, 8))
        CanvasButton(btnrow, "删除所选记录", self._del_trade, pal, self.fonts, kind="quiet",
                     width=104, height=32, scale=S,
                     bg=pal["panel_alt"]).pack(anchor="w")
        self.log_tree.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.log_hint = self._tree_hint(self.log_tree, "暂无成交记录")
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
        for w in (self.price_entry, self.lots_entry, self.note_entry):
            w.config(state=state)
        if not enabled:
            self.sel_label.config(text="在上方选中一只股票后交易")
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
            day = D.day_pnl_of(trades, q.price, q.prev_close, quote_time=q.time)
            ref = fv if fv is not None else day
            tag = "up" if ref > 0 else ("down" if ref < 0 else "flat")
            values = (name, D.code_digits(code), price, str(pos.lots),
                      D.fmt_price(pos.avg_cost, q.dp) if pos.avg_cost > 0 else "-",
                      D.fmt_money_signed(fv) if fv is not None else "-",
                      D.fmt_money_signed(day), realized)
        elif abs(pos.realized) > 0.004 or D.sold_today(trades):
            # 已清仓: 显示历史已实现盈亏; 今日清仓的还要显示今天的盈亏
            day = (D.day_pnl_of(trades, q.price, q.prev_close, quote_time=q.time)
                   if q else None)
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
        self._hint_show(self.top_hint, not self.tree.get_children())
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
        self._hint_show(self.log_hint, not tree.get_children())

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
            # 买入/卖出是累计金额, 不带正号; 盈亏才带 +/-
            text = D.fmt_money(val) if key in ("buy", "sell") else D.fmt_money_signed(val)
            self.stat_vars[key].config(text=text, fg=color)

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
        return code, price, lots, self.note_var.get().strip()[:40]

    def _trade(self, side):
        data = self._read_form()
        if not data:
            return
        code, price, lots, note = data
        pos = self._pos(code)
        if side == "sell" and lots > pos.lots:
            self.status.config(text=f"可卖不足: 当前持仓 {pos.lots} 手")
            return
        # 费用按设置费率自动计算: 股票用股票费率、ETF 用 ETF 费率(含卖出印花税)
        fee = D.trade_fee(code, side, price, lots, self.app.cfg.get("fees"))
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
