# -*- coding: utf-8 -*-
"""行情数据层: 腾讯行情接口、分时数据、代码规范化、搜索与格式化。

保持"只用标准库"的约束, 便于打包成免安装 exe。
"""
from __future__ import annotations

import datetime as _dt
import json
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# 兜底: urlopen 的 timeout 不覆盖 DNS 解析, 网络抖动时会让取数线程无限挂起
socket.setdefaulttimeout(10.0)

QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
SINA_URL = "https://hq.sinajs.cn/list={codes}"
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}&_var=min_data"
SEARCH_URL = "https://smartbox.gtimg.cn/s3/?q={kw}&t=all"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
    "Accept": "*/*",
}
# 新浪行情必须带财经站 Referer, 否则 403
SINA_HEADERS = dict(HEADERS, Referer="https://finance.sina.com.cn")

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


def _round2(v: float) -> float:
    return round(v + 0.0, 2)


# ---------------- 模拟盘: 交易费用与持仓推导 ----------------
DEFAULT_FEES = {
    "stock_rate": 0.00009,   # 股票佣金费率: 万0.9
    "stock_min": 5.0,        # 股票佣金起收(元)
    "fund_rate": 0.00006,    # ETF/场内基金佣金: 万0.6
    "fund_min": 0.1,         # ETF 起收(元)
    "stamp_rate": 0.0005,    # 卖出印花税: 万5(仅股票)
}


def is_fund(code: str) -> bool:
    """ETF / LOF 等场内基金(按代码段判断)。"""
    c = code[2:] if len(code) > 2 else code
    return c.startswith(("51", "56", "58", "159", "16"))


def trade_fee(code: str, side: str, price: float, lots: int, fees=None) -> float:
    """估算交易费用(元) = 佣金(按品种费率/起收) + 卖出印花税(仅股票)。

    fees 可覆盖默认费率: stock_rate/stock_min/fund_rate/fund_min/stamp_rate。
    """
    f = dict(DEFAULT_FEES)
    if isinstance(fees, dict):
        for k in DEFAULT_FEES:
            try:
                if k in fees:
                    f[k] = float(fees[k])
            except (TypeError, ValueError):
                pass
    try:
        amount = float(price) * int(lots) * 100.0
    except (TypeError, ValueError):
        return 0.0
    if amount <= 0:
        return 0.0
    if is_fund(code):
        fee = max(amount * f["fund_rate"], f["fund_min"])
    else:
        fee = max(amount * f["stock_rate"], f["stock_min"])
        if side == "sell":
            fee += amount * f["stamp_rate"]
    return round(fee, 2)


@dataclass(slots=True)
class Position:
    lots: int = 0            # 当前持仓(手)
    avg_cost: float = 0.0    # 平均成本(元), 0 表示成本未知
    realized: float = 0.0    # 已实现盈亏(元, 历史累计)
    buy_amount: float = 0.0  # 累计买入金额
    sell_amount: float = 0.0 # 累计卖出金额
    fees: float = 0.0        # 累计费用


def position_of(trades):
    """平均成本法推导持仓, 返回 (Position, rows)。

    买入: 费用计入成本; 卖出: 按当时均价结算已实现盈亏, 均价不变。
    rows 与 trades 等长, 每项 {"lots_after","avg_after","pnl"}。
    """
    pos = Position()
    rows = []
    for t in trades or []:
        rec = {"lots_after": pos.lots, "avg_after": pos.avg_cost, "pnl": None}
        if isinstance(t, dict):
            side = t.get("side")
            try:
                price = float(t.get("price") or 0)
                lots = int(t.get("lots") or 0)
                fee = float(t.get("fee") or 0)
            except (TypeError, ValueError):
                price, lots, fee = 0.0, 0, 0.0
            if lots > 0 and side == "buy":
                prev_shares = pos.lots * 100
                pos.lots += lots
                pos.fees += fee
                if price > 0:
                    pos.buy_amount += price * lots * 100.0
                    new_shares = pos.lots * 100
                    pos.avg_cost = ((prev_shares * pos.avg_cost
                                     + price * lots * 100.0 + fee) / new_shares
                                    if new_shares else 0.0)
            elif lots > 0 and side == "sell":
                sell_lots = min(lots, pos.lots)
                if sell_lots > 0:
                    pos.sell_amount += price * sell_lots * 100.0
                    pos.fees += fee
                    rec["pnl"] = ((price - pos.avg_cost) * sell_lots * 100.0 - fee
                                  if pos.avg_cost > 0 else 0.0)
                    pos.realized += rec["pnl"]
                    pos.lots -= sell_lots
        rec["lots_after"] = pos.lots
        rec["avg_after"] = pos.avg_cost
        rows.append(rec)
    return pos, rows


def day_pnl_of(trades, price: float, prev_close: float, today: str = "") -> float:
    """今日盈亏(元) —— 每一份股数都有"参考价", 离场/持有都结算:

    参考价: 昨日就有的股数 → 昨收; 今天买入的股数 → 买入价
    结算价: 今天卖出的股数 → 卖出价; 现在还持有的股数 → 现价

    今日成交按时间顺序 FIFO: 卖出先消耗最老的份额并按其参考价当场结算,
    所以"买入后卖出""清仓"当天同样有今日盈亏。超卖部分忽略(与持仓推导一致)。
    """
    today = today or _dt.date.today().isoformat()
    items = [t for t in (trades or []) if isinstance(t, dict)]
    # 1) 今日之前的成交 -> 昨收时的持仓, 作为"老仓"份额(参考价=昨收)
    prior = [t for t in items if str(t.get("time", ""))[:10] < today]
    prior_pos, _ = position_of(prior)
    tranches: list[list[float]] = []
    if prior_pos.lots > 0 and prev_close > 0:
        tranches.append([float(prior_pos.lots * 100), float(prev_close)])
    # 2) 今日成交: 买=追加份额(参考价=买价), 卖=FIFO 消耗并当场结算
    day = [t for t in items if str(t.get("time", ""))[:10] == today]
    day.sort(key=lambda t: str(t.get("time", "")))
    pnl = 0.0
    for t in day:
        try:
            tp = float(t.get("price") or 0)
            shares = float(int(t.get("lots") or 0) * 100)
        except (TypeError, ValueError):
            continue
        if shares <= 0 or tp <= 0:
            continue
        side = t.get("side")
        if side == "buy":
            tranches.append([shares, tp])
        elif side == "sell":
            remain = shares
            while remain > 0 and tranches:
                tr = tranches[0]
                take = min(remain, tr[0])
                pnl += (tp - tr[1]) * take      # 卖出价 - 该份额的参考价
                tr[0] -= take
                remain -= take
                if tr[0] <= 1e-9:
                    tranches.pop(0)
    # 3) 剩余份额用现价结算
    for shares, ref in tranches:
        if shares > 0 and price > 0:
            pnl += (price - ref) * shares
    return pnl


def _price_dp(raw: str) -> int:
    raw = (raw or "").strip()
    if "." in raw:
        return max(0, min(3, len(raw.split(".", 1)[1])))
    return 2


def _fetch_tencent(codes, index_set, timeout: float) -> dict[str, Quote]:
    """腾讯行情(主源): 批量、稳定, 更新节奏约 1~3s。"""
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


def _fetch_sina(codes, index_set, timeout: float) -> dict[str, Quote]:
    """新浪行情(副源): A股全覆盖, 与腾讯互有先后 —— 合并后能比任一单源更快看到新价。

    字段: 0名称 1今开 2昨收 3最新 4最高 5最低 ... 8成交量(股) 9成交额(元) 30日期 31时间
    """
    codes = [c for c in codes if c[:2] in ("sh", "sz", "bj")]
    if not codes:
        return {}
    url = SINA_URL.format(codes=",".join(codes))
    req = urllib.request.Request(url, headers=SINA_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("gbk", errors="replace")

    result: dict[str, Quote] = {}
    for line in text.strip().splitlines():
        line = line.strip().rstrip(";")
        if not line or "=" not in line:
            continue
        var, _, payload = line.partition("=")
        code = var.strip()
        if code.startswith("var "):
            code = code[4:].strip()
        if code.startswith("hq_str_"):
            code = code[len("hq_str_"):]
        f = payload.strip().strip('"').split(",")
        if len(f) < 32 or not f[0]:
            continue
        try:
            price_raw = f[3] or "0"
            stamp = (f[30].replace("-", "") + f[31].replace(":", ""))[:14]
            result[code] = Quote(
                code=code,
                name=f[0],
                price=_to_float(f[3]),
                prev_close=_to_float(f[2]),
                open=_to_float(f[1]),
                high=_to_float(f[4]),
                low=_to_float(f[5]),
                change=_round2(_to_float(f[3]) - _to_float(f[2])),
                pct=_round2((_to_float(f[3]) / _to_float(f[2]) - 1) * 100 if _to_float(f[2]) else 0.0),
                volume=_to_float(f[8]) / 100.0,        # 股 -> 手(对齐腾讯)
                amount=_to_float(f[9]) / 10000.0,      # 元 -> 万元(对齐腾讯)
                time=stamp,
                dp=_price_dp(price_raw),
                is_index=code in index_set,
            )
        except (IndexError, ValueError, ZeroDivisionError):
            continue
    return result


def fetch_quotes(codes, index_codes=(), timeout: float = 6.0) -> dict[str, Quote]:
    """批量拉取实时行情, 返回 {code: Quote}。网络/解析异常会向上抛出。

    腾讯 + 新浪双源并发, 按行情时间戳取更新的那条 ——
    交易所 L1 快照约 3s 一拍, 两源刷新相位不同, 合并后比任一单源更快看到新价。
    """
    codes = [c for c in codes if c]
    if not codes:
        return {}
    index_set = set(index_codes or ())

    from concurrent.futures import ThreadPoolExecutor
    tencent: dict[str, Quote] = {}
    sina: dict[str, Quote] = {}
    errors: list[Exception] = []
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        f_qq = pool.submit(_fetch_tencent, codes, index_set, timeout)
        f_sn = pool.submit(_fetch_sina, codes, index_set, timeout)
        for fut, name, key in ((f_qq, "腾讯", "tencent"), (f_sn, "新浪", "sina")):
            try:
                data = fut.result(timeout=timeout + 2.0)   # 有界等待: 单源卡死不拖垮整体
            except Exception as exc:      # 单源失败/超时不影响另一源
                errors.append(exc if isinstance(exc, Exception) else RuntimeError(name))
                continue
            if key == "tencent":
                tencent = data
            else:
                sina = data
    finally:
        # 不等挂住的线程: 立刻返回, 让上层继续用已有数据
        pool.shutdown(wait=False, cancel_futures=True)
    if not tencent and not sina:
        raise errors[0] if errors else RuntimeError("行情源无数据")

    merged = dict(tencent)
    for code, q in sina.items():
        cur = merged.get(code)
        # 时间戳(YYYYMMDDHHMMSS)字典序即时间序; 相同则保留腾讯
        if cur is None or str(q.time) > str(cur.time):
            merged[code] = q
    return merged


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
    # 接口可能带 var 前缀 / 尾随分号, 用 JSONDecoder 只取第一个对象
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
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
