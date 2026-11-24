# -*- coding: utf-8 -*-
"""板块归属标签: 东方财富 slist 接口 + 本地缓存/分类。

只做“这只股票属于哪些行业/概念/地域”，不参与个股评分。
接口有风控，按代码缓存，失败静默返回空列表，不阻塞详细窗口。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

import sb_data as D

SECTOR_URL = "https://push2.eastmoney.com/api/qt/slist/get"
_CACHE_TTL = 1800.0
_FAIL_TTL = 900.0
_CACHE: dict[str, tuple[float, list["SectorTag"]]] = {}
_LOCK = threading.Lock()

# 指数/风格/资金属性标签：不是用户想看的行业或概念，默认不展示。
_STYLE_PREFIXES = (
    "HS300_", "上证50_", "上证180_", "上证380_", "深证100_", "深证成指_",
    "创业板50_", "科创50_", "中证", "MSCI", "富时", "标普", "道琼斯",
    "央视50_", "国证", "巨潮", "同花顺", "东方财富",
    "大盘", "中盘", "小盘", "微盘",
)
_STYLE_NAMES = {
    "融资融券", "沪股通", "深股通", "机构重仓", "基金重仓", "社保重仓",
    "QFII重仓", "券商重仓", "保险重仓", "信托重仓", "百元股", "低价股",
    "破净股", "次新股", "大盘股", "中盘股", "小盘股", "微盘股", "权重股",
    "行业龙头", "茅指数", "宁组合", "高股息", "红利股", "ST股", "预盈预增",
    "预亏预减", "昨日涨停", "昨日连板", "昨日触板", "东方财富热股",
    "MSCI中国", "富时罗素", "标准普尔", "转债标的", "同花顺漂亮100",
    "深成500", "创业成份", "深证100R", "上证380", "中证500",
    "基金增仓", "基金减仓", "机构增仓", "机构减仓", "社保增仓", "社保减仓",
    "QFII增仓", "QFII减仓", "券商增仓", "券商减仓", "保险增仓", "保险减仓",
}
# 常见一级行业名。接口没有 type 字段，名称规则只能做到“够用”。
_INDUSTRY_HINTS = {
    "食品饮料", "酿酒行业", "银行", "证券", "保险", "多元金融", "半导体",
    "软件服务", "互联网服务", "汽车整车", "汽车零部件", "电池", "光伏设备",
    "风电设备", "医疗器械", "化学制药", "中药", "生物制品", "医疗服务",
    "房地产开发", "工程建设", "水泥建材", "钢铁行业", "有色金属", "煤炭行业",
    "石油行业", "电力行业", "燃气", "航运港口", "航空机场", "物流行业",
    "旅游酒店", "商业百货", "家用电器", "纺织服装", "农牧饲渔", "化肥行业",
    "农药兽药", "包装材料", "造纸印刷", "塑胶制品", "橡胶制品", "玻璃玻纤",
    "装修建材", "专用设备", "通用设备", "仪器仪表", "电机", "电源设备",
    "电网设备", "通信设备", "计算机设备", "消费电子", "电子元件",
    "光学光电子", "游戏", "文化传媒", "教育", "环保行业", "综合行业",
}


@dataclass(slots=True)
class SectorTag:
    code: str
    name: str
    pct: float
    lead: str = ""
    kind: str = "concept"  # industry / concept / region

    @property
    def kind_label(self) -> str:
        return {"industry": "行业", "region": "地域", "concept": "概念"}.get(
            self.kind, "概念")


def _is_style(name: str) -> bool:
    name = str(name or "").strip()
    if not name:
        return True
    if name in _STYLE_NAMES:
        return True
    if name.endswith("风格"):
        return True
    return any(name.startswith(p) for p in _STYLE_PREFIXES)


def classify(name: str) -> str:
    """按名称做保守分类，无法确定时归入概念。"""
    name = str(name or "").strip()
    if not name:
        return "concept"
    if name.endswith("板块") and "概念" not in name:
        return "region"
    if "Ⅱ" in name or "Ⅲ" in name or name in _INDUSTRY_HINTS:
        return "industry"
    return "concept"


def _secid(code: str) -> str:
    code = D.normalize_code(code)
    if code.startswith("sh") and len(code) >= 8:
        return "1." + code[2:]
    if code.startswith(("sz", "bj")) and len(code) >= 8:
        return "0." + code[2:]
    return ""


def fetch_tags(code: str, timeout: float = 5.0) -> list[SectorTag]:
    """返回股票所属板块/概念；网络失败或非 A 股返回空列表。"""
    code = D.normalize_code(code)
    if not code:
        return []
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(code)
    if cached:
        ttl = _CACHE_TTL if cached[1] else _FAIL_TTL
        if now - cached[0] < ttl:
            return list(cached[1])

    secid = _secid(code)
    if not secid:
        return []
    params = {
        "fltt": "2", "invt": "2", "secid": secid, "spt": "3",
        "pi": "0", "pz": "100", "po": "1",
        "fields": "f12,f14,f3,f128",
    }
    url = SECTOR_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": D.HEADERS["User-Agent"],
        "Referer": "https://quote.eastmoney.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        obj = json.loads(raw)
        data = obj.get("data") or {}
        rows = data.get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        tags: list[SectorTag] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("f14") or "").strip()
            if not name or _is_style(name):
                continue
            try:
                pct = float(row.get("f3") or 0.0)
            except (TypeError, ValueError):
                pct = 0.0
            tags.append(SectorTag(
                code=str(row.get("f12") or ""), name=name, pct=pct,
                lead=str(row.get("f128") or "").strip(), kind=classify(name)))
        order = {"industry": 0, "region": 1, "concept": 2}
        tags.sort(key=lambda t: (order.get(t.kind, 3), -abs(t.pct), t.name))
        with _LOCK:
            _CACHE[code] = (now, list(tags))
        return tags
    except Exception:
        # 空结果也缓存，避免同一只股票反复触发风控。
        with _LOCK:
            _CACHE[code] = (now, [])
        return []


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
