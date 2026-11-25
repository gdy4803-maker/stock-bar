# -*- coding: utf-8 -*-
"""标签内选股: 规则初筛 + AI 大模型复核，只输出观察候选，不生成买卖指令。

独立于 tkinter，便于离线测试。依赖 sb_sector.BoardMember 与 sb_data.llm_ask。
"""
from __future__ import annotations

import bisect
import re

import sb_data as D
import sb_sector as SEC

AI_SYSTEM = (
    "你是 A 股板块观察名单筛选助手。只从用户给出的候选编号中选择，"
    "不得新增股票、不得编造价格、不得给出买卖指令或收益承诺。"
    "选股结果只用于观察，不构成投资建议。"
)


def _metric_rank(values: list[float], value: float) -> float:
    """返回 value 在 values 中的分位(0~1)；全部相同或空时返回 0.5。"""
    if not values:
        return 0.5
    lo, hi = min(values), max(values)
    if hi <= lo:
        return 0.5
    pos = bisect.bisect_right(values, value)
    return max(0.0, min(1.0, pos / len(values)))


def score_member(member, pct_values=None, amount_values=None,
                 turnover_values=None, vol_values=None) -> float:
    """按涨幅/成交额/换手/量比的分位打分；0~100，越高越靠前。"""
    pct_values = pct_values or []
    amount_values = amount_values or []
    turnover_values = turnover_values or []
    vol_values = vol_values or []
    pct = float(getattr(member, "pct", 0.0) or 0.0)
    score = 50.0
    score += 20.0 * _metric_rank(pct_values, pct)
    score += 15.0 * _metric_rank(amount_values, float(getattr(member, "amount", 0.0) or 0.0))
    score += 8.0 * _metric_rank(turnover_values, float(getattr(member, "turnover", 0.0) or 0.0))
    score += 7.0 * _metric_rank(vol_values, float(getattr(member, "vol_ratio", 0.0) or 0.0))
    if pct > 0:
        score += 2.0
    return round(score, 1)


def rank_members(members, limit: int = 24):
    """过滤 ST/无效代码，按综合分排序；返回 [(member, score)]。"""
    valid = []
    for m in list(members or []):
        code = str(getattr(m, "code", "") or "")
        name = str(getattr(m, "name", "") or "")
        if not code or not name:
            continue
        if "ST" in name.upper():
            continue
        valid.append(m)
    if not valid:
        return []

    pct_values = sorted(float(getattr(m, "pct", 0.0) or 0.0) for m in valid)
    amount_values = sorted(float(getattr(m, "amount", 0.0) or 0.0) for m in valid)
    turnover_values = sorted(float(getattr(m, "turnover", 0.0) or 0.0) for m in valid)
    vol_values = sorted(float(getattr(m, "vol_ratio", 0.0) or 0.0) for m in valid)
    scored = [(m, score_member(m, pct_values, amount_values,
                               turnover_values, vol_values)) for m in valid]
    scored.sort(key=lambda x: (-x[1], -float(getattr(x[0], "pct", 0.0) or 0.0),
                               -float(getattr(x[0], "amount", 0.0) or 0.0),
                               str(getattr(x[0], "code", ""))))
    return scored[:max(1, int(limit))]


def rule_picks(members, limit: int = 8):
    """没有 AI 时的规则候选。返回 [(member, score, reason)]。"""
    return [(m, score, "规则初筛：涨幅/成交额/换手/量比综合排名")
            for m, score in rank_members(members, limit=limit)]


def build_prompt(tag_name: str, candidates) -> str:
    """候选按 P1/P2... 编号；AI 只能选编号，避免自由报价。"""
    lines = [
        "板块/标签：%s" % (tag_name or "当前标签"),
        "候选（按规则分排序，金额单位亿元）：",
    ]
    for i, (m, score) in enumerate(candidates, 1):
        lines.append(
            "P%d|%s|%s|涨幅%+.2f%%|成交额%.2f亿|换手%.2f%%|量比%.2f|规则分%.1f"
            % (i, getattr(m, "name", ""), D.code_digits(getattr(m, "code", "")),
               float(getattr(m, "pct", 0.0) or 0.0),
               float(getattr(m, "amount", 0.0) or 0.0) / 1e8,
               float(getattr(m, "turnover", 0.0) or 0.0),
               float(getattr(m, "vol_ratio", 0.0) or 0.0), score)
        )
    lines.extend([
        "任务：结合涨幅、成交额、换手、量比与板块属性，从候选中选 3-6 只加入观察名单。",
        "输出格式：每行“P编号|不超过20字的观察理由”。",
        "只输出这些行，不要表格、不要价格、不要买卖建议。",
    ])
    return "\n".join(lines)


def parse_reply(text: str, candidates):
    """把 AI 回复解析成 [(member, reason)]；只接受候选编号。"""
    by_id = {}
    by_code = {}
    for i, (m, _score) in enumerate(candidates, 1):
        by_id["P%d" % i] = m
        by_code[str(getattr(m, "code", ""))] = m

    text = str(text or "")
    seen = set()
    picks = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.search(r"(?:^|\s)(P\s*\d+)\s*[|｜:：\-—]\s*(.*)$", line, re.I)
        if not m:
            m = re.search(r"(?:^|\s)(P\s*\d+)", line, re.I)
        if not m:
            continue
        pid = re.sub(r"\s+", "", m.group(1).upper())
        if pid in seen or pid not in by_id:
            continue
        reason = (m.group(2) if m.lastindex and m.lastindex >= 2 else "").strip()
        reason = re.sub(r"\s+", " ", reason)[:80]
        seen.add(pid)
        picks.append((by_id[pid], reason or "AI 观察候选"))
    if picks:
        return picks[:8]

    # 兼容模型只返回代码/JSON 的情况；仍只映射已有候选。
    for raw in re.findall(r"(?:P\s*(\d+)|(\d{6}))", text, re.I):
        pid = "P" + raw[0] if raw[0] else ""
        code = raw[1] if raw[1] else ""
        member = by_id.get(pid) if pid else by_code.get(code)
        if not member:
            continue
        if member.code in {getattr(x[0], "code", "") for x in picks}:
            continue
        picks.append((member, "AI 观察候选"))
        if len(picks) >= 8:
            break
    return picks


def select_with_ai(cfg, tag_name: str, candidates, timeout: float = 45.0):
    """调用大模型复核候选；返回 (picks, error)。picks: [(member, reason)]。"""
    if not candidates:
        return [], "无可选候选"
    prompt = build_prompt(tag_name, candidates)
    text, err = D.llm_ask(cfg, prompt, system=AI_SYSTEM, timeout=timeout)
    if err:
        return [], err
    picks = parse_reply(text or "", candidates)
    if not picks:
        return [], "AI 未返回可识别的候选编号"
    return picks, ""
