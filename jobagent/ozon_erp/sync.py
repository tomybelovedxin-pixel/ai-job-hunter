"""同步任务：商品/库存、订单、财务流水。

三条硬规矩（对应"防漏单错账"）：
1. **游标只在整批落库成功后才前进**——失败的那一批下次会重取，不会跳过；
2. **每批失败只重试这一批**，并写进 sync_errors，供 `--retry-failed` 局部恢复；
3. **回看重叠窗口**：订单按 created_at 增量拉时，起点往回退一点（默认 30 分钟），
   防止平台侧时间戳边界上的单被漏掉；重复拉到靠唯一键幂等消化。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .client import OzonClient, OzonError
from .store import OzonStore

CURSOR_PRODUCTS = "products"
CURSOR_STOCKS = "stocks"
CURSOR_ORDERS_FBS = "orders:fbs"
CURSOR_ORDERS_FBO = "orders:fbo"
CURSOR_FINANCE = "finance"


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_shift(iso_text: str, *, minutes: int = 0, days: int = 0) -> str:
    base = iso_text or utc_now_iso()
    try:
        parsed = dt.datetime.strptime(base[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        parsed = dt.datetime.now(dt.timezone.utc)
    return (parsed + dt.timedelta(minutes=minutes, days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync_products(client: OzonClient, store: OzonStore, *, page_size: int = 100) -> dict[str, Any]:
    """商品列表 → 详情 → 库存。列表只给 id，详情补名称/类目，库存单独一个端点。"""
    report: dict[str, Any] = {"listed": 0, "detailed": 0, "stocked": 0, "errors": []}
    ids: list[str] = []
    try:
        for item in client.iter_products(page_size=page_size):
            ids.append(item["product_id"])
            store.upsert_products([item])
            report["listed"] += 1
    except OzonError as exc:
        store.log_error("products:list", "", str(exc))
        report["errors"].append(f"列表拉取失败：{exc}")

    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        try:
            store.upsert_products(client.product_info(batch))
            report["detailed"] += len(batch)
            store.resolve_errors("products:info", key="|".join(batch[:1]))
        except OzonError as exc:
            store.log_error("products:info", batch[0] if batch else "", str(exc))
            report["errors"].append(f"详情批次 {batch[:1]} 失败：{exc}")

    try:
        stocks = client.product_stocks()
        report["stocked"] = store.upsert_stocks(stocks)
        store.resolve_errors("products:stocks")
    except OzonError as exc:
        store.log_error("products:stocks", "", str(exc))
        report["errors"].append(f"库存拉取失败：{exc}")

    store.set_cursor(CURSOR_PRODUCTS, utc_now_iso(),
                     note=f"商品 {report['listed']} 条", ok=not report["errors"])
    store.log_event("sync", "products", str(report))
    return report


def sync_orders(client: OzonClient, store: OzonStore, *, kind: str = "fbs",
                since: str = "", to: str = "", page_size: int = 100,
                overlap_minutes: int = 30, backfill_days: int = 30) -> dict[str, Any]:
    """增量拉订单。游标 = 已成功落库的最大 created_at（下次回退 overlap 分钟）。

    第一次跑没有游标时，默认往回补 backfill_days 天，否则只能拉到最近半小时的单
   （2026-10-15 实测：不带历史回填时订单数会是 0）。
    """
    cursor_name = CURSOR_ORDERS_FBS if kind == "fbs" else CURSOR_ORDERS_FBO
    cursor = store.get_cursor(cursor_name)
    if since:
        start = since
    elif cursor:
        start = iso_shift(cursor, minutes=-overlap_minutes)
    else:
        start = iso_shift(utc_now_iso(), days=-backfill_days)
    end = to or utc_now_iso()
    report: dict[str, Any] = {"kind": kind, "since": start, "to": end,
                              "fetched": 0, "saved": 0, "errors": []}

    offset = 0
    while True:
        batch: list[dict[str, Any]] = []
        try:
            for posting in client.iter_postings(kind=kind, since=start, to=end,
                                                page_size=page_size, max_pages=1):
                batch.append(posting)
        except OzonError as exc:
            store.log_error(f"orders:{kind}", f"offset={offset}", str(exc))
            report["errors"].append(f"offset={offset} 拉取失败：{exc}")
            break
        if not batch:
            break
        try:
            store.upsert_postings(batch, kind=kind)
            store.resolve_errors(f"orders:{kind}", key=f"offset={offset}")
        except Exception as exc:                      # 落库失败也算这一批失败
            store.log_error(f"orders:{kind}", f"offset={offset}", f"落库失败：{exc}")
            report["errors"].append(f"offset={offset} 落库失败：{exc}")
            break
        report["fetched"] += len(batch)
        report["saved"] += len(batch)
        latest = max(str(p.get("created_at") or "") for p in batch)
        if latest:
            store.set_cursor(cursor_name, latest,
                             note=f"{kind} 拉到 {latest}", ok=True)
        if len(batch) < page_size:
            break
        offset += page_size
        # 增量拉取按时间窗口做，翻页只发生在同一窗口内；窗口重取靠幂等消化
        start = iso_shift(latest, minutes=-1) if latest else start

    if not report["errors"]:
        # 整轮没有失败：把这个 scope 之前留下的错误一起清掉（局部恢复的收尾）
        store.resolve_errors(f"orders:{kind}")
    store.log_event("sync", f"orders:{kind}", str(report))
    return report


def sync_finance(client: OzonClient, store: OzonStore, *, date_from: str = "",
                 date_to: str = "", days_back: int = 7) -> dict[str, Any]:
    """拉财务流水（佣金、服务费、回款）。默认回看最近 N 天，靠 operation_id 幂等。"""
    end = date_to or utc_now_iso()
    start = date_from or iso_shift(end, days=-days_back)
    report: dict[str, Any] = {"from": start, "to": end, "saved": 0, "errors": []}
    try:
        operations = list(client.iter_finance(date_from=start, date_to=end))
        report["saved"] = store.upsert_finance(operations)
        store.resolve_errors("finance")
    except OzonError as exc:
        store.log_error("finance", f"{start}~{end}", str(exc))
        report["errors"].append(str(exc))
    store.set_cursor(CURSOR_FINANCE, end, note=f"流水 {report['saved']} 条",
                     ok=not report["errors"])
    store.log_event("sync", "finance", str(report))
    return report


def detect_gaps(store: OzonStore) -> dict[str, Any]:
    """漏单检测：两边对不上的都列出来，宁多报不漏报。"""
    orders_no_money = store.orders_without_money()
    money_no_orders = store.money_without_orders()
    open_errors = [dict(row) for row in store.open_errors()]
    return {
        "orders_without_money": [dict(row) for row in orders_no_money],
        "money_without_orders": [dict(row) for row in money_no_orders],
        "open_errors": open_errors,
        "ok": not money_no_orders and not open_errors,
    }


def retry_failed(client: OzonClient, store: OzonStore) -> dict[str, Any]:
    """局部恢复：只针对有未解决错误的 scope 重跑受影响的那一段。"""
    scopes = {row["scope"] for row in store.open_errors(limit=200)}
    done: list[str] = []
    for scope in sorted(scopes):
        if scope.startswith("orders:"):
            kind = scope.split(":", 1)[1]
            sync_orders(client, store, kind=kind)
            done.append(scope)
        elif scope.startswith("products"):
            sync_products(client, store)
            done.append("products")
        elif scope.startswith("finance"):
            sync_finance(client, store)
            done.append("finance")
    return {"scopes": sorted(scopes), "retried": done}
