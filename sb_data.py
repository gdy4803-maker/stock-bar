# -*- coding: utf-8 -*-
"""行情数据层: 腾讯行情接口、分时数据、代码规范化、搜索与格式化。

保持"只用标准库"的约束, 便于打包成免安装 exe。
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}&_var=min_data"
SEARCH_URL = "https://smartbox.gtimg.cn/s3/?q={kw}&t=all"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
    "Accept": "*/*",
}

MARKETS = ("sh", "sz", "bj", "hk", "us")

TYPE_LABEL = {
    "GP-A": "A股", "GP-B": "B股", "ETF": "ETF", "ZS": "指数", "KCB": "科创板",
    "CYS": "创业板", "LOF": "LOF", "FJ": "分级", "QZ": "权证", "HK": "港股",
    "US": "美股", "KZZ": "可转债",
}


# ---------------- 数据结构 ----------------
@dataclass(slots=True)
class Quote:
    code: str
    name: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    change: float
    pct: float
    volume: float          # 手
    amount: float          # 万元
    time: str              # YYYYMMDDHHMMSS
    dp: int = 2            # 价格小数位
    is_index: bool = False


@dataclass(slots=True)
class MinuteSeries:
    code: str
    points: list = field(default_factory=list)   # [(HH:MM, price), ...]
    prev_close: float = 0.0
    date: str = ""


@dataclass(slots=True)
class Symbol:
    code: str
    name: str
    kind: str = ""

    @property
    def kind_label(self) -> str:
        return TYPE_LABEL.get(self.kind, self.kind or "")


# ---------------- 代码规范化 ----------------
def normalize_code(raw: str, as_index: bool = False) -> str:
    """纯数字代码自动补市场前缀; 已带 sh/sz/bj/hk/us 前缀则原样返回。

    规则:
      6/5 -> sh (沪市主板/科创板/ETF),  0/3/1 -> sz (深市/创业板/ETF),
      4/8 -> bj (北交所),  9 -> bj (北交所指数)
      as_index=True: 000xxx -> sh, 399xxx -> sz, 899xxx -> bj
    """
    raw = str(raw or "").strip().lower().replace(" ", "")
    if not raw:
        return ""
    if raw[:2] in MARKETS and len(raw) > 2:
        return raw
    if not raw.isdigit():
        return raw
    if as_index:
        if raw.startswith("000"):
            return "sh" + raw
        if raw.startswith(("399", "899")):
            return ("bj" if raw.startswith("899") else "sz") + raw
    if raw.startswith(("6", "5")):
        return "sh" + raw
    if raw.startswith(("0", "3", "1")):
        return "sz" + raw
    if raw.startswith(("4", "8", "9")):
        return "bj" + raw
    return raw


def is_index_code(code: str) -> bool:
    """粗判指数(仅用于缺少上下文时); 精确判断请用 Quote.is_index。"""
    code = (code or "").lower()
    num = code[2:]
    return (code.startswith("sh") and num.startswith("000")) or \
           (code.startswith("sz") and num.startswith("399")) or \
           (code.startswith("bj") and num.startswith("899"))


def market_label(code: str, is_index: bool = False) -> str:
    if is_index:
        return "指数"
    prefix = (code or "")[:2]
    return {"sh": "沪市", "sz": "深市", "bj": "北交所", "hk": "港股", "us": "美股"}.get(prefix, "")


def code_digits(code: str) -> str:
    code = code or ""
    return code[2:] if code[:2] in MARKETS else code


# ---------------- 拉取行情 ----------------
def _to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price_dp(raw: str) -> int:
    raw = (raw or "").strip()
    if "." in raw:
        return max(0, min(3, len(raw.split(".", 1)[1])))
    return 2


def fetch_quotes(codes, index_codes=(), timeout: float = 6.0) -> dict[str, Quote]:
    """批量拉取实时行情, 返回 {code: Quote}。网络/解析异常会向上抛出。"""
    codes = [c for c in codes if c]
    if not codes:
        return {}
    index_set = set(index_codes or ())
    url = QUOTE_URL.format(codes=",".join(codes))
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    text = raw.decode("gbk", errors="replace")

    result: dict[str, Quote] = {}
    for line in text.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        var, _, payload = line.partition("=")
        code = var.strip()
        if code.startswith("v_"):
            code = code[2:]
        payload = payload.strip().strip('"')
        fields = payload.split("~")
        if len(fields) < 38 or not fields[1]:
            continue
        try:
            price_raw = fields[3] or "0"
            result[code] = Quote(
                code=code,
                name=fields[1],
                price=_to_float(fields[3]),
                prev_close=_to_float(fields[4]),
                open=_to_float(fields[5]),
                high=_to_float(fields[33]),
                low=_to_float(fields[34]),
                change=_to_float(fields[31]),
                pct=_to_float(fields[32]),
                volume=_to_float(fields[36]),
                amount=_to_float(fields[37]),
                time=fields[30],
                dp=_price_dp(price_raw),
                is_index=code in index_set,
            )
        except (IndexError, ValueError):
            continue
    return result


# ---------------- 分时 ----------------
def fetch_minutes(code: str, timeout: float = 4.0) -> MinuteSeries | None:
    """拉取当日分时数据; 失败返回 None。"""
    if not code:
        return None
    url = MINUTE_URL.format(code=code)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    text = raw.decode("utf-8", errors="replace")
    start = text.find("{")
    if start < 0:
        return None
    obj = json.loads(text[start:])
    node = (obj.get("data") or {}).get(code)
    if not isinstance(node, dict):
        return None
    inner = node.get("data") or {}
    rows = inner.get("data") or []
    points = []
    for row in rows:
        parts = str(row).split()
        if len(parts) < 2:
            continue
        label = parts[0]
        try:
            price = float(parts[1])
        except ValueError:
            continue
        if len(label) == 4 and label.isdigit():
            label = label[:2] + ":" + label[2:]
        points.append((label, price))
    prev_close = 0.0
    try:
        qt = node.get("qt", {}).get(code) or []
        if len(qt) > 4:
            prev_close = _to_float(qt[4])
    except Exception:
        prev_close = 0.0
    if not points:
        return None
    return MinuteSeries(code=code, points=points, prev_close=prev_close,
                        date=str(inner.get("date") or ""))


# ---------------- 搜索 ----------------
def search_symbols(keyword: str, timeout: float = 8.0) -> list[Symbol]:
    """腾讯联想搜索, 返回 Symbol 列表(A股/ETF/指数/港股/美股)。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    url = SEARCH_URL.format(kw=urllib.parse.quote(keyword))
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    text = raw.decode("gbk", errors="replace")
    if '"' not in text:
        return []
    body = text.split('"', 2)[1]
    if "\\u" in body:
        try:  # 接口偶尔返回 \uXXXX 转义, 借 JSON 解码器还原
            body = json.loads('"' + body + '"')
        except Exception:
            pass
    out: list[Symbol] = []
    seen = set()
    for item in body.split("^"):
        parts = item.split("~")
        if len(parts) < 5:
            continue
        market, num, name, _pinyin, kind = parts[0], parts[1], parts[2], parts[3], parts[4]
        if market not in MARKETS or not num or not name:
            continue
        code = market + num
        if code in seen:
            continue
        seen.add(code)
        out.append(Symbol(code=code, name=name, kind=kind))
    return out


# ---------------- 格式化 ----------------
def fmt_price(value: float, dp: int = 2) -> str:
    try:
        return "{:,.{dp}f}".format(float(value), dp=dp)
    except (TypeError, ValueError):
        return "--"


def fmt_change(value: float, dp: int = 2) -> str:
    try:
        return "{:+,.{dp}f}".format(float(value), dp=dp)
    except (TypeError, ValueError):
        return "--"


def fmt_pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "--.--%"
    try:
        return ("{:+.2f}%" if signed else "{:.2f}%").format(float(value))
    except (TypeError, ValueError):
        return "--.--%"


def fmt_volume(volume: float) -> str:
    """成交量单位: 手。"""
    v = float(volume or 0)
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿手"
    if v >= 1e4:
        return f"{v / 1e4:.2f}万手"
    return f"{v:,.0f}手"


def fmt_amount(amount: float) -> str:
    """成交额单位: 万元(接口原始单位)。"""
    a = float(amount or 0)
    if a >= 1e4:
        return f"{a / 1e4:.2f}亿"
    return f"{a:,.0f}万"


def fmt_money(value: float) -> str:
    try:
        return "{:,.2f}".format(float(value))
    except (TypeError, ValueError):
        return "--"


def fmt_money_signed(value: float) -> str:
    try:
        return "{:+,.2f}".format(float(value))
    except (TypeError, ValueError):
        return "--"


def change_color(pct, pal: dict) -> str:
    """按涨跌返回颜色(涨=pos, 跌=neg, 平/无=flat)。"""
    try:
        if pct is None:
            return pal["flat"]
        if pct > 0:
            return pal["pos"]
        if pct < 0:
            return pal["neg"]
    except (TypeError, AttributeError):
        pass
    return pal["flat"]


def fmt_quote_time(raw: str) -> str:
    raw = str(raw or "")
    if len(raw) >= 14:
        return f"{raw[8:10]}:{raw[10:12]}:{raw[12:14]}"
    return "--:--:--"


def fmt_date(raw: str) -> str:
    raw = str(raw or "")
    if len(raw) >= 8:
        return f"{raw[4:6]}-{raw[6:8]}"
    return ""


def session_state(raw_time: str) -> str:
    """根据行情时间推断交易状态: 交易中 / 已收盘 / 休市 / 未开盘。"""
    raw = str(raw_time or "")
    if len(raw) < 12:
        return ""
    try:
        day = _dt.date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
        hh, mm = int(raw[8:10]), int(raw[10:12])
    except ValueError:
        return ""
    if day.weekday() >= 5:
        return "休市"
    if day != _dt.date.today():
        return "休市"
    minutes = hh * 60 + mm
    if minutes < 9 * 60 + 15:
        return "未开盘"
    if minutes <= 11 * 60 + 30 or 13 * 60 <= minutes <= 15 * 60:
        return "交易中"
    if 11 * 60 + 30 < minutes < 13 * 60:
        return "午间休市"
    return "已收盘"
