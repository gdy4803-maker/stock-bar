# -*- coding: utf-8 -*-
"""AI 研判口径: 阶段判断、Prompt、结构化结果解析、数据质量。

只依赖标准库和 sb_data，不依赖 tkinter，方便单元测试与复用。
"""
from __future__ import annotations

import datetime as _dt
import re
import time
from dataclasses import dataclass, field
from typing import Any

import sb_data as D

# ---------------- 阶段 ----------------
PHASES = {
    "pre": {
        "label": "盘前",
        "task": "盘前等待竞价，不要假设开盘方向；给出需要关注的条件。",
        "next": "09:15",
    },
    "auction": {
        "label": "竞价",
        "task": "集合竞价进行中，重点看撮合价、买卖余量、撤单痕迹；不要当成已开盘。",
        "next": "09:25",
    },
    "matching": {
        "label": "撮合",
        "task": "竞价已撮合、等待开盘，重点判断缺口和量能能否延续。",
        "next": "09:30",
    },
    "open": {
        "label": "开盘",
        "task": "开盘 30 分钟，重点确认缺口回补、量价配合和第一波方向。",
        "next": "10:00",
    },
    "morning": {
        "label": "早盘",
        "task": "早盘趋势阶段，重点看分时均价、量能延续和关键价位。",
        "next": "11:30",
    },
    "noon": {
        "label": "午间",
        "task": "午间休市，复盘上午走势，等待下午开盘确认。",
        "next": "13:00",
    },
    "afternoon": {
        "label": "盘中",
        "task": "下午盘中，重点看趋势延续、量能变化和关键位得失。",
        "next": "14:30",
    },
    "tail": {
        "label": "尾盘",
        "task": "尾盘决策，重点看收盘位置、尾盘量能和次日风险。",
        "next": "15:00",
    },
    "after": {
        "label": "盘后",
        "task": "盘后复盘，基于收盘数据分析，不把盘后数据当成可交易信号。",
        "next": "次日 09:15",
    },
    "closed": {
        "label": "休市",
        "task": "休市复盘，只做观察计划，不给出即时买卖指令。",
        "next": "下一交易日 09:15",
    },
}

LIVE_PHASES = {"auction", "matching", "open", "morning", "afternoon", "tail"}

# ---------------- 评分 / 动作 ----------------
SCORE_BANDS = (
    (80, 100, "强烈买入", "buy"),
    (60, 79, "买入", "buy"),
    (40, 59, "观望", "watch"),
    (20, 39, "减仓", "reduce"),
    (0, 19, "卖出", "sell"),
)

ACTION_LABELS = {
    "buy": "买入",
    "add": "加仓",
    "hold": "持有",
    "reduce": "减仓",
    "sell": "卖出",
    "watch": "观望",
    "avoid": "回避",
    "alert": "预警",
}
ACTION_VALUES = tuple(ACTION_LABELS.values())

_ACTION_ALIASES = {
    "强烈买入": "买入",
    "建议买入": "买入",
    "建仓": "买入",
    "布局": "买入",
    "增持": "加仓",
    "持有观察": "持有",
    "减持": "减仓",
    "清仓": "卖出",
    "强烈卖出": "卖出",
    "等待": "观望",
    "暂不买入": "回避",
    "规避": "回避",
    "风险预警": "预警",
    "警惕": "预警",
}

DATA_LEVEL_LABEL = {
    "good": "正常",
    "usable": "可用",
    "limited": "受限",
    "poor": "不足",
    "unknown": "未知",
}


def now_dt() -> _dt.datetime:
    return _dt.datetime.now()


def phase_of(now: _dt.datetime | None = None) -> dict:
    """按本地时钟判断 A 股阶段。周末直接休市；法定节假日由行情日期在质量里提示。"""
    now = now or now_dt()
    if now.weekday() >= 5:
        key = "closed"
    else:
        hm = now.hour * 60 + now.minute
        if hm < 9 * 60 + 15:
            key = "pre"
        elif hm < 9 * 60 + 25:
            key = "auction"
        elif hm < 9 * 60 + 30:
            key = "matching"
        elif hm < 10 * 60:
            key = "open"
        elif hm < 11 * 60 + 30:
            key = "morning"
        elif hm < 13 * 60:
            key = "noon"
        elif hm < 14 * 60 + 30:
            key = "afternoon"
        elif hm < 15 * 60:
            key = "tail"
        else:
            key = "after"
    info = dict(PHASES[key])
    info.update({"key": key, "now": now, "clock": now.strftime("%H:%M"),
                 "date": now.strftime("%Y-%m-%d")})
    return info


def _q_date(q) -> str:
    raw = str(getattr(q, "time", "") or "")
    if len(raw) >= 8:
        return "%s-%s-%s" % (raw[:4], raw[4:6], raw[6:8])
    return ""


def _norm_date(raw) -> str:
    """把 20261123 / 2026-11-23 / 2026/11/23 统一成 YYYY-MM-DD。"""
    text = str(raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return "%s-%s-%s" % (digits[:4], digits[4:6], digits[6:8])
    return ""


def _q_age(q, now: _dt.datetime) -> int | None:
    raw = str(getattr(q, "time", "") or "")
    if len(raw) < 14:
        return None
    try:
        qt = _dt.datetime(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]),
                          int(raw[8:10]), int(raw[10:12]), int(raw[12:14]))
        return max(0, int((now - qt).total_seconds()))
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class DataQuality:
    level: str = "unknown"       # good / usable / limited / poor / unknown
    label: str = "未知"
    notes: list[str] = field(default_factory=list)
    quote_age: int | None = None

    @property
    def text(self) -> str:
        return "；".join(self.notes) if self.notes else "数据正常"


def _best_level(notes: list[str], severe: bool, missing: bool) -> str:
    if missing:
        return "poor"
    if severe:
        return "limited"
    if notes:
        return "usable"
    return "good"


def quality_of(ctx: "AIContext", phase: dict | None = None) -> DataQuality:
    phase = phase or phase_of(ctx.now)
    now = ctx.now
    notes: list[str] = []
    missing = False
    severe = False
    q = ctx.quote
    if q is None:
        missing = True
        notes.append("行情缺失")
    else:
        qd = _q_date(q)
        today = now.strftime("%Y-%m-%d")
        if qd and qd != today:
            notes.append("行情非今日(%s)" % qd[5:])
            if phase["key"] in LIVE_PHASES:
                severe = True
        age = _q_age(q, now)
        if age is not None and phase["key"] in LIVE_PHASES and age > 120:
            notes.append("行情延迟%d秒" % age)
            severe = True
    if ctx.series is None or not getattr(ctx.series, "points", None):
        notes.append("分时缺失")
        severe = True
    else:
        sd = _norm_date(getattr(ctx.series, "date", "") or "")
        if sd and sd != now.strftime("%Y-%m-%d"):
            notes.append("分时非今日(%s)" % sd[5:])
            severe = True
    if not ctx.auction_samples:
        notes.append("竞价样本缺失")
    else:
        ad = str(ctx.auction_samples[0][0])[:8] if ctx.auction_samples else ""
        if len(ad) >= 8 and ad != now.strftime("%Y%m%d"):
            notes.append("竞价非今日")
            severe = True
    if not ctx.is_index and not ctx.brief:
        notes.append("基本面摘要缺失")
    if not ctx.is_index:
        has_book = bool(getattr(q, "bid_prices", None) or getattr(q, "bid1", 0))
        if not has_book:
            notes.append("五档缺失")
        if ctx.ticks_count <= 0:
            notes.append("逐笔未加载")
    else:
        notes.append("指数无五档/逐笔")
    level = _best_level(notes, severe, missing)
    return DataQuality(level=level, label=DATA_LEVEL_LABEL.get(level, "未知"),
                       notes=notes, quote_age=_q_age(q, now) if q else None)


@dataclass(slots=True)
class AIContext:
    code: str
    name: str = ""
    quote: Any = None
    series: Any = None
    bars: list = field(default_factory=list)
    auction_samples: list = field(default_factory=list)
    auction_prev: float = 0.0
    auction_score: dict | None = None
    position: Any = None
    brief: str = ""
    strength: dict | None = None
    index_quote: Any = None
    ticks_count: int = 0
    sector_tags: list[str] = field(default_factory=list)
    level_candidates: list[str] = field(default_factory=list)
    now: _dt.datetime = field(default_factory=now_dt)

    @property
    def is_index(self) -> bool:
        return bool(getattr(self.quote, "is_index", False)) or str(self.code).startswith(("sh000", "sz399"))


@dataclass(slots=True)
class AIResult:
    code: str = ""
    phase_key: str = ""
    phase_label: str = ""
    clock: str = ""
    score: int | None = None
    action: str = ""
    action_label: str = ""
    conclusion: str = ""
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)
    next_check: str = ""
    sniper_choices: dict[str, str] = field(default_factory=dict)
    sniper_reason: str = ""
    quality: DataQuality = field(default_factory=DataQuality)
    conflict: bool = False
    conflict_reason: str = ""
    raw: str = ""
    error: str = ""
    generated_at: float = 0.0

    @property
    def score_band(self) -> str:
        band = score_band(self.score)
        return band[2] if band else ""

    @property
    def has_structured(self) -> bool:
        return bool(self.score is not None or self.action or self.conclusion
                    or self.reasons or self.risks or self.watch)

    def display_text(self) -> str:
        """给悬停卡/复制用的紧凑文本。"""
        if self.error:
            return self.error
        lines = []
        if self.phase_label:
            lines.append("阶段: %s %s" % (self.phase_label, self.clock))
        if self.score is not None:
            lines.append("评分: %d/100%s" % (self.score, (" " + self.score_band) if self.score_band else ""))
        if self.action_label:
            lines.append("动作: " + self.action_label)
        if self.conclusion:
            lines.append("结论: " + self.conclusion)
        if self.reasons:
            lines.append("理由: " + "；".join(self.reasons[:3]))
        if self.risks:
            lines.append("风险: " + "；".join(self.risks[:2]))
        if self.watch:
            lines.append("观察: " + "；".join(self.watch[:3]))
        if self.conflict:
            lines.append("口径: " + self.conflict_reason)
        if self.quality.notes:
            lines.append("数据: " + self.quality.text)
        if not lines:
            return self.raw or "AI 无返回"
        return "\n".join(lines)


def score_band(score: int | None):
    if score is None:
        return None
    for lo, hi, label, action in SCORE_BANDS:
        if lo <= score <= hi:
            return lo, hi, label, action
    return None


def normalize_action(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("：", ":").replace("，", ",")
    if text in ACTION_VALUES:
        return text
    if text in _ACTION_ALIASES:
        return _ACTION_ALIASES[text]
    for label in ACTION_VALUES:
        if label in text:
            return label
    return ""


def _split_items(block: str) -> list[str]:
    items = []
    for line in str(block or "").replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫]\s*", "", line)
        line = re.sub(r"^[-•·*]+\s*", "", line)
        line = re.sub(r"^\d+[.)、]\s*", "", line)
        if line:
            items.append(line)
    return items


_LABELS = "结论|评分|动作|理由|风险|观察|建议|点位|下次检查"


def _block(text: str, label: str) -> str:
    """取 label 到下一个标签之间的内容。"""
    pattern = (r"(?:^|\n)\s*(?:%s)\s*[:：]\s*(.*?)(?=\n\s*(?:%s)\s*[:：]|\Z)"
               % (label, _LABELS))
    m = re.search(pattern, text, re.S)
    return m.group(1).strip() if m else ""


def _line(text: str, label: str) -> str:
    m = re.search(r"(?:^|\n)\s*%s\s*[:：]\s*([^\n]*)" % label, text)
    return m.group(1).strip() if m else ""


def _parse_sniper(text: str) -> tuple[dict[str, str], str]:
    """只接受候选编号，例如 I1/S1/L1/T1；模型写价格会被忽略。"""
    block = _block(text, "点位")
    if not block:
        return {}, ""
    choices: dict[str, str] = {}
    for key, label in (("ideal", "理想买入"), ("secondary", "二次买入"),
                       ("stop", "止损"), ("tp1", "止盈1"), ("tp2", "止盈2")):
        m = re.search(r"(?:%s)\s*[:：]\s*([A-Za-z]\d+)" % label, block)
        if m:
            choices[key] = m.group(1).upper()
    reason = _line(block, "理由")
    if not reason:
        for line in _split_items(block):
            if "理由" in line:
                reason = re.sub(r"^.*?[:：]\s*", "", line).strip()
                break
    return choices, reason


def parse_result(text: str, code: str = "") -> AIResult:
    raw = str(text or "").strip()
    r = AIResult(code=code, raw=raw, generated_at=time.time())
    if not raw:
        r.error = "AI 无返回"
        return r
    r.conclusion = _line(raw, "结论")
    score_m = re.search(r"评分\s*[:：]\s*(\d{1,3})", raw)
    if score_m:
        try:
            s = int(score_m.group(1))
            r.score = max(0, min(100, s))
        except ValueError:
            r.score = None
    r.action_label = normalize_action(_line(raw, "动作"))
    r.action = next((k for k, v in ACTION_LABELS.items() if v == r.action_label), "")
    reasons = _block(raw, "理由")
    risks = _block(raw, "风险")
    watch = _block(raw, "观察") or _block(raw, "建议")
    r.reasons = _split_items(reasons)[:4]
    r.risks = _split_items(risks)[:3]
    r.watch = _split_items(watch)[:4]
    r.sniper_choices, r.sniper_reason = _parse_sniper(raw)
    r.next_check = _line(raw, "下次检查")
    if not r.next_check:
        for item in r.watch:
            if "下次检查" in item or "下次" in item:
                r.next_check = re.sub(r"^.*?[:：]\s*", "", item).strip()
                break
    if not r.conclusion:
        # 兼容模型只输出一段话: 取第一句，不丢原文
        first = next((l.strip() for l in raw.split("\n") if l.strip()), "")
        r.conclusion = first[:80]
    if r.score is not None and r.action_label:
        band = score_band(r.score)
        if band:
            canonical = band[3]
            neutral = r.action in ("watch", "hold", "avoid", "alert")
            directional = r.action in ("buy", "add", "reduce", "sell")
            if (neutral and canonical in ("buy", "reduce", "sell")) or \
                    (directional and r.action != canonical and r.action not in ("add", "reduce")):
                r.conflict = True
                r.conflict_reason = "评分%d属「%s」，动作为「%s」，请结合理由复核" % (
                    r.score, band[2], r.action_label)
    if not r.action_label and not r.score and not r.reasons and not r.risks:
        r.error = ""
    return r


def _ma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / float(n)


def _series_summary(series) -> list[str]:
    if series is None or not getattr(series, "points", None):
        return ["- 分时: 缺失"]
    pts = [float(p) for _, p in series.points]
    vols = [float(v) for v in (getattr(series, "volumes", None) or [])]
    prev = float(getattr(series, "prev_close", 0) or 0)
    hi, lo = max(pts), min(pts)
    last = pts[-1]
    avg = (getattr(series, "avg_prices", None) or [last])[-1]
    out = ["- 分时: %s, %d 点, 高 %.2f / 低 %.2f, 最新 %.2f, 均价 %.2f" % (
        _norm_date(getattr(series, "date", "")) or "-", len(pts), hi, lo, last, float(avg))]
    if prev > 0:
        out.append("- 分时相对昨收: %+.2f%%" % ((last - prev) / prev * 100.0))
    if vols:
        k = max(2, len(vols) // 5)
        v1, v2 = sum(vols[:k]) / k, sum(vols[-k:]) / k
        out.append("- 分时量能: 前段均量 %.0f 手, 末段均量 %.0f 手, %s" % (
            v1, v2, "放量" if v2 > v1 * 1.2 else ("缩量" if v2 < v1 * 0.8 else "平稳")))
    return out


def _bars_summary(bars: list) -> list[str]:
    if not bars:
        return ["- K线: 未加载"]
    closes = [float(b.close) for b in bars]
    last = bars[-1]
    out = ["- 最近K线: %s 开 %.2f 高 %.2f 低 %.2f 收 %.2f" % (
        getattr(last, "date", ""), last.open, last.high, last.low, last.close)]
    for n in (5, 10, 20):
        ma = _ma(closes, n)
        if ma is not None:
            out.append("- MA%d: %.2f" % (n, ma))
    if len(bars) >= 6 and bars[-6].close:
        out.append("- 近5根涨跌: %+.2f%%" % ((bars[-1].close - bars[-6].close) / bars[-6].close * 100.0))
    return out


def build_prompt(ctx: AIContext, quality: DataQuality, phase: dict | None = None) -> str:
    phase = phase or phase_of(ctx.now)
    q = ctx.quote
    lines = [
        "你是谨慎的 A 股分析助手。当前阶段: %s %s。阶段任务: %s" % (
            phase["label"], phase["clock"], phase["task"]),
        "只依据下面的真实数据研判，禁止编造、改写或推算任何数字；数据缺失就明确说缺失。",
        "",
        "【标的信息】",
        "- 代码: %s  名称: %s%s" % (ctx.code, ctx.name or "-",
                                    "  指数" if ctx.is_index else ""),
    ]
    if q is not None:
        pct = D.today_pct(q)
        lines += [
            "- 最新: %.2f, 昨收 %.2f, 今开 %.2f, 高 %.2f, 低 %.2f, 涨跌 %s"
            % (q.price, q.prev_close, q.open, q.high, q.low,
               D.fmt_pct(pct) if pct is not None else "-"),
            "- 成交: %s 手, 成交额 %s, 换手 %s, 量比 %s" % (
                D.fmt_volume(q.volume), D.fmt_amount(q.amount),
                ("%.2f%%" % q.turnover) if q.turnover else "-",
                ("%.2f" % q.volume_ratio) if q.volume_ratio else "-"),
        ]
        if not ctx.is_index:
            lines.append("- 五档: 买一 %s/%s 手, 卖一 %s/%s 手" % (
                D.fmt_price(q.bid1, q.dp), D.fmt_volume(q.bid1_vol),
                D.fmt_price(q.ask1, q.dp), D.fmt_volume(q.ask1_vol)))
    else:
        lines.append("- 行情: 缺失")
    if ctx.auction_samples:
        last = ctx.auction_samples[-1]
        gap = ((float(last[1]) - ctx.auction_prev) / ctx.auction_prev * 100.0
               if ctx.auction_prev else 0.0)
        lines.append("- 竞价: 样本 %d, 撮合价 %.2f, 缺口 %+.2f%%, 买余量 %.0f / 卖余量 %.0f" % (
            len(ctx.auction_samples), float(last[1]), gap, float(last[3]), float(last[4])))
        if ctx.auction_score:
            lines.append("- 竞价评分: %+d/10, 结论 %s" % (
                int(ctx.auction_score.get("total") or 0),
                ctx.auction_score.get("verdict") or "-"))
    else:
        lines.append("- 竞价: 无样本")
    lines += _series_summary(ctx.series)
    lines += _bars_summary(ctx.bars)
    if ctx.position and getattr(ctx.position, "lots", 0) > 0:
        avg = float(getattr(ctx.position, "avg_cost", 0) or 0)
        pnl = ""
        if q is not None and avg > 0:
            pnl = ", 浮盈 %+.2f%%" % ((q.price - avg) / avg * 100.0)
        lines.append("- 持仓: %d 手, 成本 %.2f%s" % (ctx.position.lots, avg, pnl))
    else:
        lines.append("- 持仓: 无")
    if ctx.index_quote is not None:
        iq = ctx.index_quote
        lines.append("- 大盘: %s %.2f (%s), 成交额 %s" % (
            iq.name, iq.price, D.fmt_pct(D.today_pct(iq)), D.fmt_amount(iq.amount)))
    if ctx.strength and ctx.strength.get("flags"):
        lines.append("- 强度/形态: " + "、".join(ctx.strength["flags"][:5]))
    if ctx.brief:
        brief = str(ctx.brief).strip()
        lines.append("- 基本面/技术摘要:\n" + brief[:900])
    if ctx.sector_tags:
        lines.append("- 板块/概念: " + "、".join(ctx.sector_tags[:10]))
    if ctx.level_candidates:
        lines += [
            "",
            "【规则候选点位】以下价格由程序按日线计算，禁止修改、禁止新增价格：",
        ]
        lines += ["- " + x for x in ctx.level_candidates[:16]]
        lines += [
            "【点位选择】只能从上面的候选编号里选，不要输出价格。",
            "理想买入选 I 开头；二次买入选 S 开头；止损选 L 开头；止盈选 T 开头。",
            "止损必须低于二次买入，止盈必须高于理想买入；无法满足就不选，写“不适用”。",
            "候选里没有对应编号的档位写“不适用”，不要硬选其他档位。",
        ]
    lines += [
        "",
        "【数据质量】%s: %s" % (quality.label, quality.text),
        "【评分口径】0-100。80-100 强烈买入; 60-79 买入; 40-59 观望; "
        "20-39 减仓; 0-19 卖出。评分必须与动作同一口径。",
        "【动作】只能从 买入/加仓/持有/减仓/卖出/观望/回避/预警 中选一个。"
        "无持仓优先买入/观望/回避，有持仓优先加仓/持有/减仓/卖出。",
        "【格式】严格输出以下 %d 段，不要加免责声明、不要输出 JSON：" %
        (7 if ctx.level_candidates else 6),
        "结论：一句话，40 字以内",
        "评分：0-100 的整数",
        "动作：一个动作词",
        "理由：",
        "- 2-3 条，每条引用具体数据",
        "风险：",
        "- 1-2 条",
        "观察：",
        "- 触发条件/失效条件",
    ]
    if ctx.level_candidates:
        lines += [
            "点位：",
            "- 理想买入：只能选 I 开头（如 I1）",
            "- 二次买入：只能选 S 开头（如 S1）",
            "- 止损：只能选 L 开头（如 L1）",
            "- 止盈1：只能选 T 开头（如 T1）",
            "- 止盈2：只能选 T 开头（如 T2）",
            "- 理由：一句话，说明为什么选这组",
        ]
    lines += ["- 下次检查：%s" % phase["next"]]
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "你是谨慎、克制的 A 股分析助手。不得把当日涨跌直接等同于买卖理由；"
    "必须综合估值/技术位置/量价/风险收益比。所有数字只能引用用户提供的数值，"
    "数据不足时明确说明，不构成投资建议。"
)


def analyze(cfg, ctx: AIContext) -> AIResult:
    phase = phase_of(ctx.now)
    quality = quality_of(ctx, phase)
    prompt = build_prompt(ctx, quality, phase)
    text, err = D.llm_ask(cfg, prompt, system=SYSTEM_PROMPT, timeout=75.0)
    if err:
        return AIResult(code=ctx.code, phase_key=phase["key"],
                        phase_label=phase["label"], clock=phase["clock"],
                        quality=quality, error=err, generated_at=time.time())
    result = parse_result(text or "", ctx.code)
    result.phase_key = phase["key"]
    result.phase_label = phase["label"]
    result.clock = phase["clock"]
    result.quality = quality
    result.generated_at = time.time()
    if not result.raw:
        result.error = "AI 无返回"
    return result
