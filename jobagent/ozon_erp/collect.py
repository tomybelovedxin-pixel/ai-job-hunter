"""采集：从来源拿商品数据 → 清洗 → 去重 → 落库（等上架）。

来源是可插拔的：
- `MockSupplier`：本地假货源（自测用，带重复项和缺字段的脏数据）；
- `HttpSource`：公开接口/页面，标准库抓取，解析函数由调用方给（不瞎猜别人的页面结构）；
- `BrowserSource`：需要登录或 JS 渲染的页面，用本项目的 CDP 客户端只读取数
  （只读公开页，不绕验证码、不注入他人凭据）。

清洗规则是硬门槛：标题太短、价格 ≤ 0、没有图片的，直接标 skipped 并写原因，
不让脏数据流到上架那一步。
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from .store import OzonStore

MIN_TITLE_LEN = 5
CURRENCY = "RUB"


class Source(Protocol):
    name: str

    def items(self) -> Iterable[dict[str, Any]]: ...


@dataclass
class MockSupplier:
    """本地假货源：8 条里带 1 条重复、1 条缺价、1 条标题过短，用来看清洗是否生效。"""

    name: str = "mock-supplier"
    rows: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def demo(cls) -> "MockSupplier":
        base = [
            {"source_id": "A1001", "title": "Термос 保温壶 1.5L 不锈钢", "price": 1290,
             "images": ["https://img/a1.jpg", "https://img/a1b.jpg"],
             "attributes": {"brand": "NoName", "color": "银"}},
            {"source_id": "A1002", "title": "无线蓝牙耳机 降噪 TWS", "price": 2199,
             "images": ["https://img/a2.jpg"], "attributes": {"brand": "SoundX"}},
            {"source_id": "A1003", "title": "车载手机支架 出风口", "price": 549,
             "images": ["https://img/a3.jpg"], "attributes": {}},
            {"source_id": "A1004", "title": "LED 台灯 护眼 三档调光", "price": 899,
             "images": ["https://img/a4.jpg"], "attributes": {"power": "5W"}},
            {"source_id": "A1005", "title": "硅胶厨房刷 耐高温", "price": 199,
             "images": ["https://img/a5.jpg"], "attributes": {}},
            {"source_id": "A1002", "title": "无线蓝牙耳机 降噪 TWS", "price": 2199,
             "images": ["https://img/a2.jpg"], "attributes": {"brand": "SoundX"}},  # 重复
            {"source_id": "A1006", "title": "无价商品 测试", "price": 0,
             "images": ["https://img/a6.jpg"], "attributes": {}},                     # 缺价
            {"source_id": "A1007", "title": "壶", "price": 99,
             "images": [], "attributes": {}},                                        # 标题太短 + 无图
        ]
        return cls(rows=base)

    def items(self) -> Iterable[dict[str, Any]]:
        for row in self.rows:
            yield {**row, "source": self.name,
                   "source_url": f"mock://{self.name}/{row['source_id']}"}


@dataclass
class HttpSource:
    """公开接口/页面。parse 负责把响应体变成商品列表，各站结构不同，由调用方给。"""

    url: str
    parse: Callable[[bytes], list[dict[str, Any]]]
    name: str = "http"
    headers: dict[str, str] = field(default_factory=lambda: {"User-Agent": "Mozilla/5.0"})
    timeout: float = 20.0

    def items(self) -> Iterable[dict[str, Any]]:
        request = urllib.request.Request(self.url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            body = resp.read()
        for row in self.parse(body):
            yield {**row, "source": self.name, "source_url": row.get("source_url") or self.url}


@dataclass
class BrowserSource:
    """需要 JS 渲染或要登录态的公开页：用 CDP 客户端读 DOM（只读，不绕验证码）。"""

    session: Any
    url: str
    extract_js: str
    name: str = "browser"

    def items(self) -> Iterable[dict[str, Any]]:
        self.session.navigate(self.url)
        self.session.wait_ready(timeout=30)
        raw = self.session.evaluate(self.extract_js, await_promise=False) or "[]"
        rows = json.loads(raw) if isinstance(raw, str) else raw
        for row in rows:
            yield {**row, "source": self.name, "source_url": row.get("source_url") or self.url}


# ---------- 清洗 ----------

_SPACE = re.compile(r"\s+")


def normalize_title(text: str | None) -> str:
    return _SPACE.sub(" ", (text or "").strip())


def parse_price(value: Any) -> float | None:
    """把 "1 290,00 ₽" / "1290" / 1290 这类写法都变成 float；认不出来返回 None。"""
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value or "")
    digits = re.sub(r"[^\d.,]", "", text).replace(" ", "")
    if not digits:
        return None
    if "," in digits and "." in digits:
        digits = digits.replace(",", "")
    else:
        digits = digits.replace(",", ".")
    try:
        price = float(digits)
    except ValueError:
        return None
    return price if price > 0 else None


def fingerprint(item: dict[str, Any]) -> str:
    """去重指纹：优先用来源 + 来源 id；没有 id 就用归一化标题 + 价格。"""
    source = str(item.get("source") or "")
    source_id = str(item.get("source_id") or "").strip()
    if source_id:
        raw = f"{source}|{source_id}"
    else:
        raw = f"{source}|{normalize_title(item.get('title')).lower()}|{item.get('price')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def clean(item: dict[str, Any]) -> dict[str, Any]:
    """一条来源数据 → 落库结构，带上 status/note（不合格的不进上架队列）。"""
    title = normalize_title(item.get("title"))
    price = parse_price(item.get("price"))
    images = [url for url in (item.get("images") or []) if str(url).strip()]
    problems = []
    if len(title) < MIN_TITLE_LEN:
        problems.append(f"标题过短（{len(title)} 字）")
    if price is None:
        problems.append("价格缺失或不是正数")
    if not images:
        problems.append("没有图片")
    return {
        "source": item.get("source"),
        "source_id": item.get("source_id"),
        "source_url": item.get("source_url"),
        "title": title,
        "price": price,
        "currency": item.get("currency") or CURRENCY,
        "images": images,
        "attributes": item.get("attributes") or {},
        "raw": item,
        "fingerprint": fingerprint(item),
        "status": "ready" if not problems else "skipped",
        "note": "；".join(problems) if problems else "",
    }


def collect(store: OzonStore, source: Source) -> dict[str, Any]:
    """跑一轮采集：清洗 → 去重落库。返回统计。"""
    cleaned = [clean(row) for row in source.items()]
    # 同一批里来源重复的，只留最后一条（统计口径也按唯一条数报，免得"ready 6 实际入库 5"）
    unique: dict[str, dict[str, Any]] = {}
    for row in cleaned:
        unique[row["fingerprint"]] = row
    rows = list(unique.values())
    stats = store.upsert_collected(rows)
    ready = sum(1 for row in rows if row["status"] == "ready")
    skipped = sum(1 for row in rows if row["status"] == "skipped")
    report = {"source": source.name, "fetched": len(cleaned),
              "unique": len(rows), "duplicates_in_batch": len(cleaned) - len(rows),
              "ready": ready, "skipped": skipped, **stats}
    store.log_event("collect", source.name, json.dumps(report, ensure_ascii=False))
    return report
