# -*- coding: utf-8 -*-
"""规则候选点位: 只做确定性计算，不调用 AI。

输出 ideal/secondary/stop/tp 四组候选，AI 只能在这些候选里选编号；
最终价格仍由本模块的候选价格决定，AI 不能新增或改写价格。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

ROLE_LABELS = {
    "ideal": "理想买入",
    "secondary": "二次买入",
    "stop": "止损",
    "tp": "止盈候选",
}


@dataclass(slots=True)
class Candidate:
    cid: str
    role: str
    low: float
    high: float
    basis: str

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    def range_text(self, dp: int = 2) -> str:
        if abs(self.high - self.low) < 10 ** (-max(1, int(dp))) / 2.0:
            return _fmt_price(self.low, dp)
        return "%s–%s" % (_fmt_price(self.low, dp), _fmt_price(self.high, dp))

    def price_text(self, dp: int = 2) -> str:
        return _fmt_price(self.low, dp)


@dataclass(slots=True)
class LevelPlan:
    code: str
    current: float = 0.0
    dp: int = 2
    atr: float = 0.0
    bars_count: int = 0
    bars_date: str = ""
    candidates: dict[str, list[Candidate]] = field(default_factory=dict)
    chosen: dict[str, str] = field(default_factory=dict)
    trend: str = "unknown"
    quality: str = "poor"
    source: str = "rule"          # rule / ai / fallback
    note: str = ""
    asof: str = ""

    def selected(self, role: str) -> Candidate | None:
        key = "tp" if role in ("tp1", "tp2") else role
        cid = self.chosen.get(role, "")
        if not cid:
            return None
        for c in self.candidates.get(key, []):
            if c.cid == cid:
                return c
        return None

    def tp1(self) -> Candidate | None:
        return self.selected("tp1")

    def tp2(self) -> Candidate | None:
        return self.selected("tp2")

    def rr(self) -> float | None:
        e, s, t = self.selected("ideal"), self.selected("stop"), self.tp1()
        if not e or not s or not t:
            return None
        risk = e.mid - s.high
        reward = t.low - e.mid
        if risk <= 0 or reward <= 0:
            return None
        return reward / risk

    def risk_pct(self) -> float | None:
        e, s = self.selected("ideal"), self.selected("stop")
        if not e or not s or e.mid <= 0:
            return None
        return (e.mid - s.high) / e.mid * 100.0

    def status(self, role: str, price: float | None = None) -> str:
        p = self.current if price is None else float(price)
        if p <= 0:
            return ""
        e = self.selected(role if role in ("ideal", "secondary") else
                          ("stop" if role == "stop" else role))
        stop = self.selected("stop")
        if not e:
            return ""
        if stop and p <= stop.high:
            return "计划失效"
        if role in ("ideal", "secondary"):
            if p < e.low:
                return "低于区间"
            if p <= e.high:
                return "已进入区间"
            return ("等待二次回踩" if role == "secondary" else "等待回踩") + \
                " -%.2f%%" % ((p - e.high) / p * 100.0)
        if role == "stop":
            return "已跌破止损" if p <= e.high else \
                "距止损 -%.2f%%" % ((p - e.high) / p * 100.0)
        return "已达到" if p >= e.low else \
            "距止盈 +%.2f%%" % ((e.low - p) / p * 100.0)

    def prompt_lines(self) -> list[str]:
        lines = []
        for role in ("ideal", "secondary", "stop", "tp"):
            for c in self.candidates.get(role, []):
                lines.append("%s %s %s | %s" % (
                    c.cid, ROLE_LABELS.get(role, role),
                    c.range_text(self.dp), c.basis))
        return lines

    def apply_choices(self, choices: dict[str, str]) -> tuple[bool, str]:
        """AI 只返回编号；这里验证编号和价格顺序，失败不修改默认选择。"""
        if not choices:
            return False, "AI 未返回点位编号"
        key_roles = {
            "ideal": "ideal", "secondary": "secondary", "stop": "stop",
            "tp1": "tp", "tp2": "tp",
        }
        new_choice = dict(self.chosen)
        for key, role in key_roles.items():
            raw = str(choices.get(key) or "").strip().upper()
            if not raw:
                continue
            pool = self.candidates.get(role, [])
            if not any(c.cid == raw for c in pool):
                return False, "AI 选了不存在的候选 %s" % raw
            new_choice[key] = raw
        required = ["ideal", "stop", "tp1"]
        if self.candidates.get("secondary"):
            required.append("secondary")
        if any(not new_choice.get(k) for k in required):
            return False, "AI 点位不完整"
        if new_choice.get("tp1") and new_choice.get("tp2") and \
                new_choice["tp1"] == new_choice["tp2"]:
            return False, "止盈1/2 不能选同一个候选"

        old = dict(self.chosen)
        self.chosen = new_choice
        ok, reason = self.validate()
        if not ok:
            self.chosen = old
            return False, reason
        rr = self.rr()
        if rr is not None and rr < 1.5:
            self.chosen = old
            return False, "风报比不足 1.5"
        return True, ""

    def validate(self) -> tuple[bool, str]:
        ideal = self.selected("ideal")
        secondary = self.selected("secondary")
        stop = self.selected("stop")
        tp1 = self.selected("tp1")
        tp2 = self.selected("tp2")
        if not all((ideal, stop, tp1)):
            return False, "点位不完整"
        if secondary and secondary.high > ideal.low + 0.01:
            return False, "二次买入必须低于理想买入"
        if secondary and stop.high >= secondary.low:
            return False, "止损必须低于二次买入"
        if tp1.low <= ideal.high:
            return False, "止盈必须高于理想买入"
        if tp2 and tp2.low < tp1.high:
            return False, "止盈2 必须不低于止盈1"
        return True, ""


def _fmt_price(v: float, dp: int) -> str:
    try:
        return ("%%.%df" % max(0, min(4, int(dp)))) % float(v)
    except (TypeError, ValueError):
        return "-"


def _round_price(v: float, dp: int) -> float:
    step = 10 ** (-max(0, min(4, int(dp))))
    return round(round(float(v) / step) * step, max(0, min(4, int(dp))))


def _ma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / float(n)


def _atr(bars: list, n: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        b, prev = bars[i], bars[i - 1]
        trs.append(max(float(b.high) - float(b.low),
                       abs(float(b.high) - float(prev.close)),
                       abs(float(b.low) - float(prev.close))))
    if not trs:
        return 0.0
    if len(trs) < n:
        return sum(trs) / len(trs)
    atr = sum(trs[:n]) / float(n)
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / float(n)
    return atr


def _swing(bars: list, n: int) -> tuple[float, float]:
    part = bars[-n:] if len(bars) >= n else bars
    return max(float(b.high) for b in part), min(float(b.low) for b in part)


def _dedupe(points: list[tuple[float, str]], atr: float) -> list[tuple[float, str]]:
    """同价位候选合并；按离现价从近到远返回。"""
    out: list[tuple[float, str]] = []
    for price, basis in sorted(points, key=lambda x: -x[0]):
        if price <= 0:
            continue
        hit = None
        for i, (p, b) in enumerate(out):
            if abs(price - p) < max(0.01, 0.15 * atr):
                hit = i
                break
        if hit is None:
            out.append((price, basis))
        else:
            p, b = out[hit]
            if basis and basis not in b:
                out[hit] = (p, b + " / " + basis)
    return out


def _zone(price: float, atr: float, dp: int) -> tuple[float, float]:
    lo = _round_price(price - 0.15 * atr, dp)
    hi = _round_price(price + 0.15 * atr, dp)
    if hi <= lo:
        hi = _round_price(lo + 10 ** (-max(0, min(4, int(dp)))), dp)
    return lo, hi


def _trend(current: float, ma20: float | None, ma60: float | None) -> str:
    if ma20 is None or ma60 is None:
        return "unknown"
    if current > ma20 and ma20 > ma60:
        return "up"
    if current < ma20 and ma20 > ma60:
        return "pullback"
    if current < ma20 and ma20 <= ma60:
        return "weak"
    return "range"


def build_plan(code: str, bars: list, quote=None, is_index: bool = False) -> LevelPlan | None:
    if is_index:
        return None
    bars = list(bars or [])
    bars.sort(key=lambda b: str(getattr(b, "date", "")))
    now = _dt.datetime.now()
    dp = int(getattr(quote, "dp", 2) or 2) if quote is not None else 2
    dp = max(0, min(4, dp))
    if len(bars) < 30:
        return LevelPlan(code=code, current=float(getattr(quote, "price", 0) or 0),
                         dp=dp, bars_count=len(bars),
                         bars_date=str(getattr(bars[-1], "date", "")) if bars else "",
                         quality="poor", note="K线不足 30 根，暂不计算",
                         asof=now.strftime("%Y-%m-%d %H:%M"))
    closes = [float(b.close) for b in bars]
    current = float(getattr(quote, "price", 0) or 0)
    if current <= 0:
        current = closes[-1]
    atr = _atr(bars, 14)
    if atr <= 0:
        atr = max(current * 0.01, 0.01)
    ma5, ma10, ma20, ma60 = (_ma(closes, n) for n in (5, 10, 20, 60))
    high20, low20 = _swing(bars, 20)
    high60, low60 = _swing(bars, 60)
    platform_high = None
    if len(bars) >= 21:
        platform_high = max(float(b.high) for b in bars[-21:-1])

    supports: list[tuple[float, str]] = []
    for p, name in ((ma5, "MA5"), (ma10, "MA10"), (ma20, "MA20"),
                    (ma60, "MA60"), (low20, "近20日低点"),
                    (low60, "近60日低点"), (platform_high, "前平台高点")):
        if p is not None and 0 < p < current:
            supports.append((float(p), name))
    if high60 > low60:
        for ratio, name in ((0.382, "0.382回撤"), (0.5, "0.5回撤"),
                            (0.618, "0.618回撤")):
            p = high60 - ratio * (high60 - low60)
            if 0 < p < current:
                supports.append((float(p), name))
    supports = _dedupe(supports, atr)

    ideal_pool = supports[:1]
    if supports:
        # 同一支撑簇允许并列成理想候选；与最近支撑相差超过 0.18 ATR 的
        # 下一档留给二次买入，避免理想/二次大面积重叠。
        base = supports[0][0]
        for p, basis in supports[1:]:
            if base - p <= 0.18 * atr:
                ideal_pool.append((p, basis))
            else:
                break
    ideal: list[Candidate] = []
    for i, (p, basis) in enumerate(ideal_pool, 1):
        lo, hi = _zone(p, atr, dp)
        ideal.append(Candidate("I%d" % i, "ideal", lo, hi, basis))

    secondary_pool: list[tuple[float, str]] = []
    if ideal:
        # 二次买入必须低于全部理想候选；允许支撑区上沿与理想区轻微贴近，
        # 但绘制时会把上沿压到理想区下方，避免两档价格重叠。
        floor = min(c.low for c in ideal) - 0.2 * atr
        secondary_pool = [x for x in supports
                          if x[0] - 0.15 * atr < floor][:3]
    secondary: list[Candidate] = []
    for i, (p, basis) in enumerate(secondary_pool, 1):
        lo, hi = _zone(p, atr, dp)
        if ideal and hi >= ideal[0].low:
            hi = _round_price(ideal[0].low - 0.02 * atr, dp)
            if hi <= lo:
                continue
        secondary.append(Candidate("S%d" % i, "secondary", lo, hi, basis))

    # 止损候选：从二次支撑往下让 ATR，同时纳入 MA60/近低点和 6% 最大风险位。
    sec_low = secondary[0].low if secondary else (
        ideal[0].low - 0.3 * atr if ideal else current - 0.5 * atr)
    stop_points: list[tuple[float, str]] = [
        (sec_low - 0.3 * atr, "二次支撑-0.3ATR"),
        (sec_low - 0.5 * atr, "二次支撑-0.5ATR"),
    ]
    for p, name in ((ma60, "MA60"), (low20, "近20日低点"), (low60, "近60日低点")):
        if p is not None and p < sec_low:
            stop_points.append((float(p) - 0.3 * atr, name + "-0.3ATR"))
    if ideal:
        risk_cap = ideal[0].mid * 0.94
        if risk_cap < sec_low:
            stop_points.append((risk_cap, "最大风险6%"))
    stops = _dedupe(stop_points, atr)[:3]
    stop: list[Candidate] = []
    for i, (p, basis) in enumerate(stops, 1):
        p = _round_price(p, dp)
        if p > 0:
            stop.append(Candidate("L%d" % i, "stop", p, p, basis))

    # 止盈候选：优先用上方结构压力，没有结构才用 ATR 扩展。
    targets: list[tuple[float, str]] = []
    for p, name in ((high20, "近20日高点"), (high60, "近60日高点"),
                    (platform_high, "前平台高点")):
        if p is not None and p > current:
            targets.append((float(p), name))
    if high60 > low60:
        ext = low60 + 1.618 * (high60 - low60)
        if ext > current:
            targets.append((float(ext), "1.618扩展"))
    if not targets:
        targets = [(current + 2.0 * atr, "ATR 2倍扩展"),
                   (current + 3.0 * atr, "ATR 3倍扩展")]
    targets = _dedupe(targets, atr)[:3]
    # _dedupe 按价格从高到低，止盈需要从近到远。
    targets.sort(key=lambda x: x[0])
    tp: list[Candidate] = []
    for i, (p, basis) in enumerate(targets, 1):
        p = _round_price(p, dp)
        if p > current:
            tp.append(Candidate("T%d" % i, "tp", p, p, basis))

    chosen: dict[str, str] = {}
    if ideal:
        chosen["ideal"] = ideal[0].cid
    if secondary:
        chosen["secondary"] = secondary[0].cid
    if stop:
        chosen["stop"] = stop[0].cid
    if tp:
        # 默认止盈必须让风报比至少 1.5；否则选最远的止盈候选。
        base_ideal = ideal[0] if ideal else None
        base_stop = stop[0] if stop else None
        sel = tp[0]
        if base_ideal and base_stop:
            risk = base_ideal.mid - base_stop.high
            if risk > 0:
                for cand in tp:
                    if (cand.low - base_ideal.mid) / risk >= 1.5:
                        sel = cand
                        break
                else:
                    sel = tp[-1]
        chosen["tp1"] = sel.cid
        idx = tp.index(sel)
        if idx + 1 < len(tp):
            chosen["tp2"] = tp[idx + 1].cid

    quality = "good"
    if len(bars) < 60:
        quality = "limited"
    elif len(bars) < 120:
        quality = "limited"
    if not (ideal and secondary and stop and tp):
        quality = "limited" if quality == "good" else quality

    note = ""
    if not supports:
        note = "下方支撑不足"
    elif not secondary:
        note = "二次支撑不足"
    elif not tp:
        note = "上方压力不足"

    return LevelPlan(
        code=code, current=current, dp=dp, atr=atr,
        bars_count=len(bars), bars_date=str(getattr(bars[-1], "date", "")),
        candidates={"ideal": ideal, "secondary": secondary,
                    "stop": stop, "tp": tp},
        chosen=chosen, trend=_trend(current, ma20, ma60),
        quality=quality, note=note, asof=now.strftime("%Y-%m-%d %H:%M"),
    )


def trend_label(trend: str) -> str:
    return {"up": "多头", "pullback": "回踩", "weak": "偏弱",
            "range": "震荡"}.get(trend, "未知")
