# -*- coding: utf-8 -*-
"""行情数据层: 腾讯行情接口、分时数据、代码规范化、搜索与格式化。

保持"只用标准库"的约束, 便于打包成免安装 exe。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# 兜底: urlopen 的 timeout 不覆盖 DNS 解析, 网络抖动时会让取数线程无限挂起
socket.setdefaulttimeout(10.0)

QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
SINA_URL = "https://hq.sinajs.cn/list={codes}"
EAST_URL = ("https://push2.eastmoney.com/api/qt/ulist.np/get"
            "?fltt=2&invt=2&fields={fields}&secids={secids}")
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}&_var=min_data"
SEARCH_URL = "https://smartbox.gtimg.cn/s3/?q={kw}&t=all"
RELEASES_API = "https://api.github.com/repos/gdy4803-maker/stock-bar/releases?per_page=20"


def version_tuple(text: str) -> tuple[int, int, int]:
    """'v1.3.6' / '1.3.6' -> (1, 3, 6); 非数字段按 0 处理。"""
    parts = []
    for chunk in (text or "").lstrip("vV").split(".")[:3]:
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num or 0))
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def fetch_latest_release(timeout: float = 10.0):
    """查询 GitHub 最新 Release, 返回 {tag, url, size} 或 None(失败静默)。

    用 releases 列表而非 /latest —— 后者不返回"预发布"版本。
    """
    try:
        with urllib.request.urlopen(RELEASES_API, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        best = None
        for rel in (data if isinstance(data, list) else []):
            if rel.get("draft"):
                continue
            tag = str(rel.get("tag_name") or "")
            url = None
            size = 0
            for a in rel.get("assets") or []:
                if str(a.get("name") or "").endswith("-win64.zip"):
                    url = a.get("browser_download_url")
                    size = int(a.get("size") or 0)
                    break
            if tag and url and (best is None or version_tuple(tag) > version_tuple(best["tag"])):
                best = {"tag": tag, "url": url, "size": size}
        return best
    except Exception:
        pass
    return None


def download_file(url: str, dest: str, timeout: float = 60.0, progress=None) -> bool:
    """流式下载到 dest; progress(已下, 总量) 可选。失败返回 False。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        try:
                            progress(done, total)
                        except Exception:
                            pass
        return os.path.getsize(dest) > 0
    except Exception:
        return False

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
    bid1: float = 0.0      # 买一价
    ask1: float = 0.0      # 卖一价
    bid1_vol: float = 0.0  # 买一量(集合竞价时 = 虚拟撮合量)
    ask1_vol: float = 0.0  # 卖一量
    buy_rest: float = 0.0  # 买余量(集合竞价: 撮合后未成交的买单)
    sell_rest: float = 0.0 # 卖余量(集合竞价: 撮合后未成交的卖单)


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
    """ETF / LOF 等场内基金(按代码段判断)。

    上交所: 50(封基/LOF) 51(ETF/LOF) 52(ETF 新段, 如 520920) 56(ETF) 58(LOF)
    深交所: 15(基金/ETF) 16(LOF) 18(封基)
    """
    c = code[2:] if len(code) > 2 else code
    return c.startswith(("50", "51", "52", "56", "58", "15", "16", "18"))


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


def day_pnl_of(trades, price: float, prev_close: float, today: str = "",
               quote_time: str = "") -> float:
    """今日盈亏(元) —— 每一份股数都有"参考价", 离场/持有都结算:

    参考价: 昨日就有的股数 → 昨收; 今天买入的股数 → 买入价
    结算价: 今天卖出的股数 → 卖出价; 现在还持有的股数 → 现价

    今日成交按时间顺序 FIFO: 卖出先消耗最老的份额并按其参考价当场结算,
    所以"买入后卖出""清仓"当天同样有今日盈亏。超卖部分忽略(与持仓推导一致)。

    quote_time(YYYYMMDDHHMMSS): 行情时间戳; 若不是当天的行情(9 点前接口给的
    还是昨天的数据)则今日盈亏记 0 —— 否则昨天的涨跌会被算进今天。
    """
    today = today or _dt.date.today().isoformat()
    if quote_time and str(quote_time)[:8] != today.replace("-", ""):
        return 0.0
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


def day_base_of(trades, prev_close: float, today: str = "") -> float:
    """今日盈亏的"基数"(元) —— 与 day_pnl_of 同一套分批口径:

    老仓份额按昨收、今日买入份额按买价; 卖出消耗掉的份额也按其参考价计入基数。
    今日涨幅 = day_pnl_of(...) / day_base_of(...) × 100 —— 这样"今日买入有亏损"时
    金额与涨幅两种显示才对得上(此前涨幅全按昨收算, 与金额口径不一致)。
    """
    today = today or _dt.date.today().isoformat()
    items = [t for t in (trades or []) if isinstance(t, dict)]
    prior = [t for t in items if str(t.get("time", ""))[:10] < today]
    prior_pos, _ = position_of(prior)
    tranches: list[list[float]] = []
    if prior_pos.lots > 0 and prev_close > 0:
        tranches.append([float(prior_pos.lots * 100), float(prev_close)])
    day = [t for t in items if str(t.get("time", ""))[:10] == today]
    day.sort(key=lambda t: str(t.get("time", "")))
    base = 0.0
    for t in day:
        try:
            tp = float(t.get("price") or 0)
            shares = float(int(t.get("lots") or 0) * 100)
        except (TypeError, ValueError):
            continue
        if shares <= 0 or tp <= 0:
            continue
        if t.get("side") == "buy":
            tranches.append([shares, tp])
        elif t.get("side") == "sell":
            remain = shares
            while remain > 0 and tranches:
                tr = tranches[0]
                take = min(remain, tr[0])
                base += tr[1] * take           # 卖出消耗份额的参考价基数
                tr[0] -= take
                remain -= take
                if tr[0] <= 1e-9:
                    tranches.pop(0)
    for shares, ref in tranches:
        base += ref * shares                   # 仍持有份额的参考价基数
    return base


def sold_today(trades, today: str = "") -> bool:
    """今天是否有卖出成交(用于识别"今日清仓", 今天清仓的仍计入今日盈亏)。"""
    today = today or _dt.date.today().isoformat()
    for t in (trades or []):
        if not isinstance(t, dict) or t.get("side") != "sell":
            continue
        if str(t.get("time", ""))[:10] == today:
            try:
                if int(t.get("lots") or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def quote_is_today(q, today: str = "") -> bool:
    """行情是否为当天数据(9 点前接口给的还是昨天的行情)。"""
    today = (today or _dt.date.today().isoformat()).replace("-", "")
    return str(getattr(q, "time", ""))[:8] == today


def today_pct(q):
    """今日涨跌幅(%): 行情不是当天的(9 点前)按 0 计 —— 与今日盈亏口径一致,
    避免"今日盈亏 0 但涨幅是昨天的"这种不一致。"""
    if q is None:
        return None
    if not quote_is_today(q):
        return 0.0
    return q.pct


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
            price = _to_float(fields[3])
            prev = _to_float(fields[4])
            if price <= 0 or prev <= 0:
                # 集合竞价/停牌时常返回 0 价或空价: 丢弃, 保留上一次有效行情
                # (否则会出现 (0-昨收)/昨收 = -100% 的假跌幅)
                continue
            result[code] = Quote(
                code=code,
                name=fields[1],
                price=price,
                prev_close=prev,
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
                bid1=_to_float(fields[9]) if len(fields) > 9 else 0.0,
                ask1=_to_float(fields[19]) if len(fields) > 19 else 0.0,
                bid1_vol=_to_float(fields[10]) if len(fields) > 20 else 0.0,
                ask1_vol=_to_float(fields[20]) if len(fields) > 20 else 0.0,
                buy_rest=_to_float(fields[12]) if len(fields) > 22 else 0.0,
                sell_rest=_to_float(fields[22]) if len(fields) > 22 else 0.0,
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
            price = _to_float(f[3])
            prev = _to_float(f[2])
            if price <= 0 or prev <= 0:
                # 集合竞价/停牌时常返回 0 价或空价: 丢弃, 保留上一次有效行情
                # (否则 (0-昨收)/昨收 会算出 -100% 的假跌幅)
                continue
            stamp = (f[30].replace("-", "") + f[31].replace(":", ""))[:14]
            result[code] = Quote(
                code=code,
                name=f[0],
                price=price,
                prev_close=prev,
                open=_to_float(f[1]),
                high=_to_float(f[4]),
                low=_to_float(f[5]),
                change=_round2(price - prev),
                pct=_round2((price / prev - 1) * 100),
                volume=_to_float(f[8]) / 100.0,        # 股 -> 手(对齐腾讯)
                amount=_to_float(f[9]) / 10000.0,      # 元 -> 万元(对齐腾讯)
                time=stamp,
                dp=_price_dp(price_raw),
                is_index=code in index_set,
            )
        except (IndexError, ValueError, ZeroDivisionError):
            continue
    return result


def _fetch_eastmoney(codes, index_set, timeout: float) -> dict[str, Quote]:
    """东方财富源(第三源)——腾讯/新浪全挂时兜底接管。

    字段(实测校准): f2=现价 f3=涨跌% f4=涨跌额 f5=量(手) f6=额(元)
    f12=代码 f13=市场 f14=名称 f15=高 f16=低 f17=开 f18=昨收
    """
    if not codes:
        return {}
    secids = ",".join(("1." + c[2:] if c.startswith("sh") else "0." + c[2:]) for c in codes)
    fields = "f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18"
    url = EAST_URL.format(fields=fields, secids=secids)
    req = urllib.request.Request(url, headers={
        "User-Agent": HEADERS["User-Agent"], "Referer": "https://quote.eastmoney.com/"})
    raw = None
    last_exc: Exception | None = None
    for attempt in range(3):                 # 东财偶发拒连/限流: 退避重试
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            break
        except Exception as exc:
            last_exc = exc
            import time as _t
            _t.sleep(0.6 * (attempt + 1))
    if raw is None:
        raise last_exc if last_exc else RuntimeError("东财无响应")
    payload = json.loads(raw.decode("utf-8", "replace"))
    rows = ((payload.get("data") or {}).get("diff")) or []
    by_num = {c[2:]: c for c in codes}
    now = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
    out: dict[str, Quote] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        num = str(r.get("f12") or "")
        code = by_num.get(num)
        if not code:
            continue
        price_raw = str(r.get("f2") or "")
        price = _to_float(price_raw)
        prev = _to_float(r.get("f18"))
        if price <= 0 or prev <= 0:
            continue
        out[code] = Quote(
            code=code,
            name=str(r.get("f14") or code),
            price=price,
            prev_close=prev,
            open=_to_float(r.get("f17")),
            high=_to_float(r.get("f15")),
            low=_to_float(r.get("f16")),
            change=_to_float(r.get("f4")),
            pct=_to_float(r.get("f3")),
            volume=_to_float(r.get("f5")),          # 手
            amount=_to_float(r.get("f6")) / 10000.0,  # 元 -> 万元
            time=now,
            dp=_price_dp(price_raw),
            is_index=code in index_set,
        )
    return out


def fetch_quotes(codes, index_codes=(), timeout: float = 6.0) -> dict[str, Quote]:
    """批量拉取实时行情, 返回 {code: Quote}。网络/解析异常会向上抛出。

    腾讯 + 新浪双源并发, 按行情时间戳取更新的那条 ——
    交易所 L1 快照约 3s 一拍, 两源刷新相位不同, 合并后比任一单源更快看到新价。
    两源都失败时用东方财富兜底(第三源)。
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
        # 第三源兜底: 东财(主源全挂时接管, 保证行情不断)
        try:
            east = _fetch_eastmoney(codes, index_set, timeout)
        except Exception as exc:
            errors.append(exc if isinstance(exc, Exception) else RuntimeError("东财"))
            raise errors[0] if errors else RuntimeError("行情源无数据")
        if not east:
            raise errors[0] if errors else RuntimeError("行情源无数据")
        return east

    merged = dict(tencent)
    for code, q in sina.items():
        cur = merged.get(code)
        # 时间戳(YYYYMMDDHHMMSS)字典序即时间序; 相同则保留腾讯
        if cur is None or str(q.time) > str(cur.time):
            if cur is not None:
                # 新浪源不带五档盘口: 沿用腾讯的买一/卖一(否则盘口会变 0)
                if q.bid1 <= 0 and cur.bid1 > 0:
                    q.bid1, q.ask1 = cur.bid1, cur.ask1
                if q.bid1_vol <= 0 and cur.bid1_vol > 0:
                    q.bid1_vol, q.ask1_vol = cur.bid1_vol, cur.ask1_vol
                    q.buy_rest, q.sell_rest = cur.buy_rest, cur.sell_rest
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
def _trim_zeros(text: str, min_frac: int) -> str:
    """去掉无意义的尾随 0, 但至少保留 min_frac 位小数(不同行情源小数位不一致时统一观感)。"""
    if "." not in text:
        return text
    int_part, frac = text.rsplit(".", 1)
    frac = frac.rstrip("0")
    if len(frac) < min_frac:
        frac = frac.ljust(min_frac, "0")
    return (int_part + "." + frac) if frac else int_part


def fmt_price(value: float, dp: int = 2) -> str:
    try:
        v = float(value)
        s = "{:,.{dp}f}".format(v, dp=dp)
        return _trim_zeros(s, 2 if abs(v) >= 1 else 3)
    except (TypeError, ValueError):
        return "--"


def fmt_change(value: float, dp: int = 2) -> str:
    try:
        v = float(value)
        s = "{:+,.{dp}f}".format(v, dp=dp)
        sign = s[0] if s[:1] in "+-" else ""
        return sign + _trim_zeros(s.lstrip("+-"), 2 if abs(v) >= 1 else 3)
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


def fmt_yi_wan(value: float) -> str:
    """金额(元)按 亿/万 显示, 最多 2 位小数: 12.35亿 / 72.39万 / 8,600。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "--"
    if abs(v) >= 1e8:
        return ("%.2f" % (v / 1e8)).rstrip("0").rstrip(".") + "亿"
    if abs(v) >= 1e4:
        return ("%.2f" % (v / 1e4)).rstrip("0").rstrip(".") + "万"
    return "{:,.0f}".format(v)


# ---------------- 集合竞价评分 ----------------
def auction_score(samples, prev_close: float, weights=None):
    """集合竞价评分(-10 ~ +10): 返回 {"total","items","verdict","tone"}。

    samples: [(时间戳, 虚拟撮合价, 虚拟撮合量, 买余量, 卖余量), ...]
    weights: 可选 [w1..w5, b] —— 用历史数据训练出的权重(逻辑回归);
    不传则用内置启发式权重。每项 -2 ~ +2 并附依据文字, 评分透明可核对。
    """
    if not samples or prev_close <= 0:
        return None
    prices = [float(s[1]) for s in samples if s[1] > 0]
    vols = [float(s[2]) for s in samples]
    if len(prices) < 2:
        return None
    buy = float(samples[-1][3])
    sell = float(samples[-1][4])
    last = prices[-1]
    gap = (last - prev_close) / prev_close * 100.0
    items = []

    # 1) 高开幅度: 温和高开最有利, 过度反应不加分
    if gap > 4.0:
        s1, d1 = 1, f"高开 {gap:.2f}% 偏大, 过度反应不加分"
    elif gap >= 1.0:
        s1, d1 = 2, f"高开 {gap:.2f}%, 温和高开偏强"
    elif gap >= 0.3:
        s1, d1 = 1, f"高开 {gap:.2f}%, 略偏强"
    elif gap > -0.3:
        s1, d1 = 0, f"平开 {gap:.2f}%, 多空均衡"
    elif gap > -1.0:
        s1, d1 = -1, f"低开 {abs(gap):.2f}%, 略偏弱"
    elif gap > -4.0:
        s1, d1 = -2, f"低开 {abs(gap):.2f}%, 抛压偏大"
    else:
        s1, d1 = -1, f"低开 {abs(gap):.2f}% 偏大, 过度反应或有修复"
    items.append(("高开幅度", s1, d1))

    # 2) 撮合价趋势(后 1/3 时段的边际变化)
    k = max(2, len(prices) // 3)
    slope = (last - prices[-k]) / prev_close * 100.0
    if slope > 0.3:
        s2, d2 = 2, f"撮合价稳步上移 {slope:+.2f}%, 买盘持续"
    elif slope > 0.05:
        s2, d2 = 1, f"撮合价小幅上移 {slope:+.2f}%"
    elif slope >= -0.05:
        s2, d2 = 0, f"撮合价横盘 {slope:+.2f}%"
    elif slope >= -0.3:
        s2, d2 = -1, f"撮合价小幅走低 {slope:+.2f}%"
    else:
        s2, d2 = -2, f"撮合价持续走低 {slope:+.2f}%, 卖盘持续"
    items.append(("撮合趋势", s2, d2))

    # 3) 买卖余量(撮合后未成交的买/卖单 = 直接的供需失衡)
    total_rest = buy + sell
    if total_rest <= 0:
        s3, d3 = 0, "无余量(撮合价上买卖充分匹配)"
    else:
        ratio = buy / total_rest
        if ratio >= 0.75:
            s3, d3 = 2, f"买压 {ratio * 100:.0f}%, 买方主导"
        elif ratio >= 0.6:
            s3, d3 = 1, f"买压 {ratio * 100:.0f}%, 略偏买方"
        elif ratio > 0.4:
            s3, d3 = 0, f"买压 {ratio * 100:.0f}%, 多空均衡"
        elif ratio > 0.25:
            s3, d3 = -1, f"卖压 {(1 - ratio) * 100:.0f}%, 略偏卖方"
        else:
            s3, d3 = -2, f"卖压 {(1 - ratio) * 100:.0f}%, 卖方主导"
    items.append(("买卖力量", s3, d3))

    # 4) 量价配合(末段量能 vs 首段)
    m = max(2, len(vols) // 3)
    v_head = sum(vols[:m]) / m
    v_tail = sum(vols[-m:]) / m
    price_up = last >= prices[0]
    vol_up = v_tail > v_head * 1.3 if v_head > 0 else v_tail > 0
    if vol_up and price_up:
        s4, d4 = 2, "价升量增, 抢筹配合"
    elif vol_up and not price_up:
        s4, d4 = -2, "价跌量增, 抛压释放"
    elif not vol_up and price_up:
        s4, d4 = 0, "价升量缩, 上涨缺量"
    else:
        s4, d4 = -1, "价跌量缩, 观望为主"
    items.append(("量价配合", s4, d4))

    # 5) 价格稳定(后半段波动 + 9:20 前后是否"画图")
    half = prices[len(prices) // 2:]
    hi2, lo2 = max(half), min(half)
    wave = (hi2 - lo2) / prev_close * 100.0
    early_peak = max(prices[:max(2, len(prices) // 2)])
    drop = (early_peak - last) / prev_close * 100.0
    if wave <= 0.3 and drop < 0.5:
        s5, d5 = 1, f"价格稳定(波动 {wave:.2f}%), 意图真实"
    elif drop >= 1.0:
        s5, d5 = -1, f"前期冲高回落 {drop:.2f}%, 疑似虚挂撤单"
    elif wave > 1.5:
        s5, d5 = -1, f"波动较大(振幅 {wave:.2f}%), 分歧明显"
    else:
        s5, d5 = 0, f"波动 {wave:.2f}%, 常规"
    items.append(("价格稳定", s5, d5))

    scores = [x[1] for x in items]
    if weights and len(weights) >= 6:
        # 训练权重(逻辑回归): total = Σ w_i * score_i + b, 映射到 -10..+10
        z = sum(w * s for w, s in zip(weights[:5], scores)) + weights[5]
        prob = 1.0 / (1.0 + __import__("math").exp(-max(-30.0, min(30.0, z))))
        total = int(round((prob - 0.5) * 20))
    else:
        total = sum(scores)                     # 内置启发式: 各项直接相加
    total = max(-10, min(10, total))
    if total >= 4:
        verdict, tone = "偏多：可买入/持有", "pos"
    elif total <= -4:
        verdict, tone = "偏空：回避/卖出", "neg"
    else:
        verdict, tone = "中性：观望为宜", "flat"
    return {"total": total, "items": items, "verdict": verdict, "tone": tone,
            "gap": gap, "buy_ratio": (buy / (buy + sell)) if (buy + sell) > 0 else 0.5}


def train_logistic(rows, lr: float = 0.25, epochs: int = 400):
    """纯 Python 逻辑回归训练 —— 样本小(几百条×5特征), 无需 numpy/sklearn。

    rows: [([s1..s5], label 0/1), ...]  其中 s_i = 各项得分(-2..2),
    label = 当日涨幅>0(1) 或 <=0(0)。
    返回 [w1..w5, b] 或 None(样本不足/无区分度)。
    """
    if not rows or len(rows) < 60:
        return None
    labels = {y for _f, y in rows}
    if len(labels) < 2:
        return None                      # 全是同向样本, 训不出来
    w = [0.0] * 5
    b = 0.0
    n = float(len(rows))
    math_exp = __import__("math").exp
    for _ in range(epochs):
        gw = [0.0] * 5
        gb = 0.0
        for feats, y in rows:
            z = sum(w[i] * feats[i] for i in range(5)) + b
            p = 1.0 / (1.0 + math_exp(-max(-30.0, min(30.0, z))))
            e = p - y
            for i in range(5):
                gw[i] += e * feats[i]
            gb += e
        w = [w[i] - lr * gw[i] / n for i in range(5)]
        b -= lr * gb / n
    return [round(v, 6) for v in w] + [round(b, 6)]


def model_accuracy(rows, weights) -> float | None:
    """模型在样本上的准确率(阈值 0.5); weights=None 时评估内置启发式。"""
    if not rows:
        return None
    ok = 0
    for feats, y in rows:
        if weights and len(weights) >= 6:
            z = sum(weights[i] * feats[i] for i in range(5)) + weights[5]
            prob = 1.0 / (1.0 + __import__("math").exp(-max(-30.0, min(30.0, z))))
            pred = 1 if prob >= 0.5 else 0
        else:
            pred = 1 if sum(feats) > 0 else 0     # 启发式: 各项得分和>0 看多
        ok += (1 if pred == y else 0)
    return ok / float(len(rows))


# ---------------- 大模型研判 ----------------
LLM_PRESETS = {
    "deepseek": {"base": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "mimo": {"base": "https://api.xiaomimimo.com/v1", "model": "mimo-v2.6-pro"},
    "custom": {"base": "", "model": ""},
}

MCP_STOCK_URL = "http://82.156.17.205/cnstock/mcp"
_mcp_cache: dict[str, tuple[float, str]] = {}      # code -> (时间, 文本)


def mcp_stock_brief(code: str, timeout: float = 20.0, cache_sec: float = 1800.0,
                    level: str = "brief"):
    """调用 mcp-cn-a-stock 工具, 返回该股数据文本; 失败返回 None。

    level: brief=基本面+历史统计 | full=再加技术分析(信号摘要+最近3日指标明细)。
    带 30 分钟缓存, 避免重复请求触发限流。
    """
    sym = (code or "").upper()
    if not sym:
        return None
    now = __import__("time").time()
    hit = _mcp_cache.get((sym, level))
    if hit and now - hit[0] < cache_sec:
        return hit[1]

    def _rpc(method, params=None, rid=1):
        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params
        req = urllib.request.Request(MCP_STOCK_URL,
                                     data=json.dumps(body).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        if not raw.strip():
            return {}                    # notification 只回 202 无正文
        # streamable-http: SSE("event: message\ndata: {...}") 或纯 JSON
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(raw)

    try:
        _rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                            "clientInfo": {"name": "stockbar", "version": "1.0"}})
        _rpc("notifications/initialized", {}, rid=None)
        data = _rpc("tools/call", {"name": (level if level in ("brief", "medium", "full")
                                            else "brief"),
                                   "arguments": {"symbol": sym}}, rid=2)
        text = ""
        for item in (data.get("result", {}).get("content") or []):
            if isinstance(item, dict) and item.get("type") == "text":
                text += item.get("text", "")
        text = text.strip()
        if level == "full" and text:
            # 裁剪: 只留"信号摘要"+ 指标表头 + 最近 3 行(避免 prompt 过长)
            keep = []
            lines = text.splitlines()
            table_seen = 0
            for ln in lines:
                if ln.startswith("|") and "---" not in ln:
                    table_seen += 1
                    if table_seen > 4:
                        continue
                elif ln.startswith("|") and "---" in ln:
                    keep.append(ln)
                    continue
                if table_seen > 4 and ln.startswith("|"):
                    continue
                keep.append(ln)
            text = "\n".join(keep)[:2200]
        if text:
            _mcp_cache[(sym, level)] = (now, text)
        return text or None
    except Exception:
        return None


def mcp_strength(code: str):
    """从 MCP 数据提取相对强度(RPS 近似)与形态提示(给评分卡/AI 用)。

    返回 {"alpha": {"5"/"20"/"60": x}, "high20":.., "price":.., "flags":[...]} 或 None。
    """
    t = mcp_stock_brief(code)
    if not t:
        return None
    out = {"alpha": {}, "high20": None, "low20": None, "price": None, "flags": []}
    for ln in t.splitlines():
        s = ln.strip()
        if "跑赢" in s or "Alpha" in s:
            nums = []
            for c in s.split("|"):
                c = c.strip().replace("%", "").replace("+", "")
                try:
                    nums.append(float(c))
                except (TypeError, ValueError):
                    continue
            for key, i in (("1", 0), ("5", 1), ("20", 2), ("60", 3)):
                if len(nums) > i:
                    out["alpha"][key] = nums[i]
        if "20日" in s and "最高" in s:
            try:
                out["high20"] = float(s.split("最高:")[1].split()[0])
            except Exception:
                pass
            try:
                out["low20"] = float(s.split("最低:")[1].split()[0])
            except Exception:
                pass
        if s.startswith("- 今日:") or s.startswith("- 当日:"):
            try:
                out["price"] = float(s.split(":")[1].split()[0])
            except Exception:
                pass
    a60 = out["alpha"].get("60")
    a20 = out["alpha"].get("20")
    if a60 is not None:
        if a60 >= 15:
            out["flags"].append("RPS强势(60日跑赢基准 %+.1f%%)" % a60)
        elif a60 <= -10:
            out["flags"].append("RPS弱势(60日跑输基准 %+.1f%%)" % a60)
    if out["price"] and out["high20"] and out["price"] >= out["high20"] * 0.995:
        out["flags"].append("接近/突破20日新高")
    if a20 is not None and a20 >= 10:
        out["flags"].append("中期上升趋势(20日跑赢 %+.1f%%)" % a20)
    return out


def scan_market(limit: int = 40, timeout: float = 15.0):
    """当日异动扫描: 全市场 A 股快照(新浪主源/东财兜底) -> 筛放量大涨。

    返回 [{"code","name","price","pct","amount"(元),"volume"(手)}, ...] 按涨幅降序。
    规则: 涨幅>=4% 且 成交额>=2亿, 取前 limit 只。
    """
    out = market_snapshot(min_pct=4.0, min_amount=2e8, limit=limit, timeout=timeout)
    return out


STRATEGIES = ("海龟突破", "RPS突破", "放量突破", "高窄旗形", "涨停洗盘", "跌停反包", "强势回调")


def _strategy_hits(r, st):
    """单只股票命中哪些策略 -> {策略名: 依据}; 未命中的不出现。"""
    a = st.get("alpha") or {}
    a20, a60 = a.get("20"), a.get("60")
    p, h20, l20 = st.get("price"), st.get("high20"), st.get("low20")
    hits = {}
    # 海龟突破: 20日新高 + 成交额过亿 + 阳线(防诱多)
    if p and h20 and p >= h20 * 0.998 and r["amount"] >= 1e8 and r["pct"] > 0:
        hits["海龟突破"] = "20日新高+成交额%.1f亿" % (r["amount"] / 1e8)
    # RPS 突破: 60日跑赢基准>=20% + 20日趋势向上
    if a60 is not None and a60 >= 20 and (a20 or 0) > 0:
        hits["RPS突破"] = "60日跑赢%+.0f%%/20日%+.0f%%" % (a60, a20 or 0)
    # 放量突破: 涨幅>=4% + 成交额>=2亿 + 逼近/突破20日高点
    if r["pct"] >= 4 and r["amount"] >= 2e8 and p and h20 and p >= h20 * 0.97:
        hits["放量突破"] = "涨幅%+.1f%%+成交额%.1f亿" % (r["pct"], r["amount"] / 1e8)
    # 高窄旗形: 20日振幅<25% + 逼近高点 + 60日强势
    if p and h20 and l20 and h20 > l20 and (h20 - l20) / max(p, 0.01) < 0.25 \
            and p >= h20 * 0.97 and (a60 or 0) >= 15:
        hits["高窄旗形"] = "振幅%.0f%%+逼近高点" % ((h20 - l20) / p * 100)
    # 涨停洗盘(近似): 短期横盘整理 + 60日强势 + 逼近高点
    if -2.0 <= r["pct"] <= 2.0 and p and h20 and p >= h20 * 0.93 and (a60 or 0) >= 15:
        hits["涨停洗盘"] = "整理中+60日跑赢%+.0f%%" % (a60 or 0)
    # 跌停反包(近似): 大涨收复 + 中期趋势向上
    if r["pct"] >= 5 and (a20 or 0) > 0 and (a60 or 0) >= 10:
        hits["跌停反包"] = "强反弹%+.1f%%" % r["pct"]
    # 强势回调: 60日强 + 20日回调
    if (a60 or 0) >= 15 and (a20 or 0) < -2:
        hits["强势回调"] = "60日%+.0f%%/20日%+.0f%%" % (a60 or 0, a20 or 0)
    return hits


def select_stocks(check: int = 60, strategies=None, on_progress=None):
    """收盘后选股: 全市场快照初筛 -> 逐只取历史(MCP) -> 策略命中打分 -> 排序。

    strategies: 使用哪些策略(默认全部); 结果含 hits(命中策略及依据)。
    """
    use = set(strategies) if strategies else set(STRATEGIES)
    snap = market_snapshot(min_pct=1.5, min_amount=3e8, limit=max(check * 3, 200))
    if not snap:
        return []
    out = []
    for i, r in enumerate(snap[:check]):
        if on_progress:
            try:
                on_progress(i + 1, min(check, len(snap)), r["name"])
            except Exception:
                pass
        st = mcp_strength(r["code"]) or {}
        all_hits = _strategy_hits(r, st)
        hits = {k: v for k, v in all_hits.items() if k in use}
        score = len(hits) * 2           # 命中越多越可靠
        r = dict(r)
        r["score"] = score
        r["hits"] = hits
        r["reasons"] = "、".join("%s(%s)" % (k, v) for k, v in hits.items()) or "无命中"
        out.append(r)
    out = [r for r in out if r["score"] > 0]
    out.sort(key=lambda x: (-x["score"], -x["pct"]))
    return out[:20]


def market_snapshot(min_pct: float = 4.0, min_amount: float = 2e8,
                    limit: int = 400, timeout: float = 15.0):
    """全市场 A 股快照(按涨幅降序): 主用新浪排行接口, 失败退回东财。

    新浪: Market_Center.getHQNodeData(node=hs_a, sort=changepercent) 80条/页。
    """
    out = []
    # ---- 主源: 新浪全市场排行 ----
    try:
        for page in (1, 2, 3):
            url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   "Market_Center.getHQNodeData?page=%d&num=80&sort=changepercent&asc=0"
                   "&node=hs_a&symbol=&_s_r_a=page" % page)
            req = urllib.request.Request(url, headers={
                "User-Agent": HEADERS["User-Agent"],
                "Referer": "https://finance.sina.com.cn/"})
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            rows = json.loads(raw.decode("utf-8", "replace"))
            if not rows:
                break
            for r in rows:
                if not isinstance(r, dict):
                    continue
                try:
                    pct = float(r.get("changepercent") or 0)
                    amount = float(r.get("amount") or 0)      # 元
                    price = float(r.get("trade") or 0)
                except (TypeError, ValueError):
                    continue
                if price <= 0 or pct < min_pct or amount < min_amount:
                    continue
                out.append({"code": str(r.get("symbol") or ""),
                            "name": str(r.get("name") or ""),
                            "price": price, "pct": pct, "amount": amount,
                            "volume": float(r.get("volume") or 0) / 100.0})
            if len(out) >= limit:
                break
    except Exception:
        out = []
    if out:
        out.sort(key=lambda x: x["pct"], reverse=True)
        return out[:limit]
    # ---- 兜底: 东财 ----
    return _snapshot_eastmoney(min_pct, min_amount, limit, timeout)


def _snapshot_eastmoney(min_pct, min_amount, limit, timeout):
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=600&po=1&np=1"
           "&fltt=2&invt=2&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fid=f3"
           "&fields=f2,f3,f5,f6,f12,f13,f14")
    req = urllib.request.Request(url, headers={
        "User-Agent": HEADERS["User-Agent"], "Referer": "https://quote.eastmoney.com/"})
    raw = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            break
        except Exception:
            import time as _t
            _t.sleep(0.8 * (attempt + 1))
    if raw is None:
        return []
    try:
        rows = ((json.loads(raw.decode("utf-8", "replace")).get("data") or {}).get("diff")) or []
    except Exception:
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            pct = float(r.get("f3") or 0)
            amount = float(r.get("f6") or 0)
            price = float(r.get("f2") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or pct < min_pct or amount < min_amount:
            continue
        num = str(r.get("f12") or "")
        mkt = str(r.get("f13") or "0")
        code = ("sh" if mkt == "1" else "sz") + num
        out.append({"code": code, "name": str(r.get("f14") or num),
                    "price": price, "pct": pct, "amount": amount,
                    "volume": float(r.get("f5") or 0)})
    out.sort(key=lambda x: x["pct"], reverse=True)
    return out[:limit]


def llm_key(cfg=None) -> str:
    """API Key: 优先 config.json 的 llm_key, 没有则读环境变量 STOCKBAR_LLM_KEY。"""
    k = ""
    if isinstance(cfg, dict):
        k = str(cfg.get("llm_key") or "").strip()
    return k or os.environ.get("STOCKBAR_LLM_KEY", "").strip()


def llm_ask(cfg, prompt: str, system: str = "", timeout: float = 60.0):
    """调 OpenAI 兼容接口 /chat/completions, 返回文本; 失败返回 None。

    配置: llm_enabled / llm_base / llm_model / llm_key(或环境变量)。
    """
    if not isinstance(cfg, dict) or not cfg.get("llm_enabled"):
        return None, "未启用 AI 研判(设置里开启)"
    key = llm_key(cfg)
    if not key:
        return None, "未配置 API Key(设置里填写, 或设置环境变量 STOCKBAR_LLM_KEY)"
    base = str(cfg.get("llm_base") or "").strip().rstrip("/")
    model = str(cfg.get("llm_model") or "").strip()
    if not base:
        preset = LLM_PRESETS.get(str(cfg.get("llm_provider") or "deepseek"))
        base = (preset or {}).get("base", "")
        model = model or (preset or {}).get("model", "")
    if not base or not model:
        return None, "接口地址/模型名未配置"
    if not base.endswith("/chat/completions"):
        base = base + "/chat/completions"
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = json.dumps({"model": model, "messages": msgs,
                       "temperature": 0.4, "max_tokens": 1600}).encode("utf-8")
    try:
        req = urllib.request.Request(base, data=body, method="POST")
        req.add_header("Authorization", "Bearer " + key)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", HEADERS["User-Agent"])
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data["choices"][0]["message"]["content"].strip(), None
    except Exception as exc:
        return None, "AI 调用失败: %s" % (str(exc)[:120],)


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
