"""Ozon ERP 的本地库：商品、库存、订单、财务流水、同步游标、错误与事件。

所有写入都是幂等的（唯一键 ON CONFLICT），所以"同一条数据同步两次"不会产生重复行——
这是漏单/错账防护的第一层。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id   TEXT PRIMARY KEY,
    offer_id     TEXT UNIQUE,
    name         TEXT,
    barcode      TEXT,
    category_id  INTEGER,
    volume_weight REAL,
    raw          TEXT,
    synced_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stocks (
    offer_id   TEXT PRIMARY KEY,
    product_id TEXT,
    kind       TEXT,
    present    INTEGER,
    reserved   INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS postings (
    posting_number TEXT PRIMARY KEY,
    order_id       INTEGER,
    order_number   TEXT,
    kind           TEXT,
    status         TEXT,
    created_at     TEXT,
    currency       TEXT,
    amount         REAL,
    commission     REAL,
    payout         REAL,
    raw            TEXT,
    synced_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS postings_created_idx ON postings(created_at);

CREATE TABLE IF NOT EXISTS posting_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_number TEXT NOT NULL,
    sku            TEXT,
    offer_id       TEXT,
    name           TEXT,
    quantity       INTEGER,
    price          REAL,
    amount         REAL,
    UNIQUE (posting_number, sku, offer_id)
);

CREATE TABLE IF NOT EXISTS finance_ops (
    operation_id   INTEGER PRIMARY KEY,
    operation_type TEXT,
    operation_date TEXT,
    posting_number TEXT,
    order_id       INTEGER,
    amount         REAL,
    currency       TEXT,
    kind           TEXT,
    raw            TEXT,
    synced_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS finance_posting_idx ON finance_ops(posting_number);

CREATE TABLE IF NOT EXISTS sync_state (
    name        TEXT PRIMARY KEY,
    cursor      TEXT,
    last_run_at TEXT,
    last_ok_at  TEXT,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS sync_errors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,
    key        TEXT,
    error      TEXT,
    created_at TEXT NOT NULL,
    resolved   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    subject    TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL
);

-- 采集来的商品（还没上架）
CREATE TABLE IF NOT EXISTS collected_products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT,
    source_id   TEXT,
    source_url  TEXT,
    title       TEXT,
    price       REAL,
    currency    TEXT,
    images      TEXT,
    attributes  TEXT,
    raw         TEXT,
    fingerprint TEXT UNIQUE,
    status      TEXT NOT NULL DEFAULT 'new',   -- new/ready/skipped/published/failed
    note        TEXT,
    collected_at TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS collected_status_idx ON collected_products(status);

-- 上架任务（一条采集记录一个任务，UNIQUE 保证不会重复上架）
CREATE TABLE IF NOT EXISTS listing_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_id INTEGER NOT NULL UNIQUE REFERENCES collected_products(id),
    offer_id     TEXT UNIQUE,
    product_id   TEXT,
    payload      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending/published/failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

# 财务流水里算"收入"和"费用"的类型关键字（按 Ozon 文档的 operation_type 命名习惯整理）
REVENUE_HINTS = ("DeliveredToCustomer", "OperationItemAgent", "OperationAgent")
COMMISSION_HINTS = ("Commission", "Service", "Fee", "Delivery", "Marketplace")


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class OzonStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- 写入（全部幂等） ----------

    def upsert_products(self, items: Iterable[dict[str, Any]]) -> int:
        stamp = now()
        count = 0
        for item in items:
            product_id = str(item.get("product_id") or item.get("id") or "").strip()
            if not product_id:
                continue
            self.conn.execute(
                """INSERT INTO products (product_id, offer_id, name, barcode, category_id,
                                         volume_weight, raw, synced_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(product_id) DO UPDATE SET
                     offer_id=COALESCE(excluded.offer_id, products.offer_id),
                     name=COALESCE(excluded.name, products.name),
                     barcode=COALESCE(excluded.barcode, products.barcode),
                     category_id=COALESCE(excluded.category_id, products.category_id),
                     volume_weight=COALESCE(excluded.volume_weight, products.volume_weight),
                     raw=excluded.raw, synced_at=excluded.synced_at""",
                (product_id, item.get("offer_id") or None, item.get("name") or None,
                 item.get("barcode") or None, item.get("category_id"),
                 item.get("volume_weight"), json.dumps(item, ensure_ascii=False), stamp),
            )
            count += 1
        self.conn.commit()
        return count

    def upsert_stocks(self, items: Iterable[dict[str, Any]]) -> int:
        stamp = now()
        count = 0
        for item in items:
            offer_id = str(item.get("offer_id") or "").strip()
            if not offer_id:
                continue
            for stock in item.get("stocks") or [{}]:
                self.conn.execute(
                    """INSERT INTO stocks (offer_id, product_id, kind, present, reserved, updated_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(offer_id) DO UPDATE SET
                         product_id=excluded.product_id, kind=excluded.kind,
                         present=excluded.present, reserved=excluded.reserved,
                         updated_at=excluded.updated_at""",
                    (offer_id, str(item.get("product_id") or ""), stock.get("type") or "fbs",
                     int(stock.get("present") or 0), int(stock.get("reserved") or 0), stamp),
                )
                count += 1
        self.conn.commit()
        return count

    def upsert_postings(self, postings: Iterable[dict[str, Any]], *, kind: str = "fbs") -> int:
        """写订单 + 明细。同一条邮包重复同步只更新，不新增。"""
        stamp = now()
        count = 0
        for posting in postings:
            number = str(posting.get("posting_number") or "").strip()
            if not number:
                continue
            products = posting.get("products") or []
            amount = sum(float(p.get("price") or 0) * int(p.get("quantity") or 0)
                         for p in products)
            financial = (posting.get("financial_data") or {}).get("products") or []
            commission = sum(float(f.get("commission_amount") or 0) for f in financial)
            payout = sum(float(f.get("payout") or 0) for f in financial)
            self.conn.execute(
                """INSERT INTO postings (posting_number, order_id, order_number, kind, status,
                                         created_at, currency, amount, commission, payout, raw, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(posting_number) DO UPDATE SET
                     status=excluded.status, amount=excluded.amount,
                     commission=excluded.commission, payout=excluded.payout,
                     raw=excluded.raw, synced_at=excluded.synced_at""",
                (number, posting.get("order_id"), str(posting.get("order_number") or ""),
                 kind, str(posting.get("status") or ""), str(posting.get("created_at") or ""),
                 (products[0].get("currency_code") if products else "RUB"),
                 round(amount, 2), round(commission, 2), round(payout, 2),
                 json.dumps(posting, ensure_ascii=False), stamp),
            )
            for item in products:
                self.conn.execute(
                    """INSERT INTO posting_items (posting_number, sku, offer_id, name,
                                                  quantity, price, amount)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(posting_number, sku, offer_id) DO UPDATE SET
                         quantity=excluded.quantity, price=excluded.price,
                         amount=excluded.amount, name=excluded.name""",
                    (number, str(item.get("sku") or ""), str(item.get("offer_id") or ""),
                     item.get("name") or "", int(item.get("quantity") or 0),
                     float(item.get("price") or 0),
                     round(float(item.get("price") or 0) * int(item.get("quantity") or 0), 2)),
                )
            count += 1
        self.conn.commit()
        return count

    def upsert_finance(self, operations: Iterable[dict[str, Any]]) -> int:
        stamp = now()
        count = 0
        for op in operations:
            operation_id = op.get("operation_id")
            if operation_id is None:
                continue
            posting = op.get("posting") or {}
            self.conn.execute(
                """INSERT INTO finance_ops (operation_id, operation_type, operation_date,
                                            posting_number, order_id, amount, currency, kind,
                                            raw, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(operation_id) DO UPDATE SET
                     amount=excluded.amount, operation_type=excluded.operation_type,
                     raw=excluded.raw, synced_at=excluded.synced_at""",
                (int(operation_id), str(op.get("operation_type") or ""),
                 str(op.get("operation_date") or ""),
                 str(posting.get("posting_number") or ""),
                 posting.get("order_id"), float(op.get("amount") or 0),
                 str(op.get("currency_code") or "RUB"), str(op.get("type") or ""),
                 json.dumps(op, ensure_ascii=False), stamp),
            )
            count += 1
        self.conn.commit()
        return count

    # ---------- 同步状态与错误 ----------

    def set_cursor(self, name: str, cursor: str, *, note: str = "", ok: bool = True) -> None:
        stamp = now()
        row = self.conn.execute("SELECT name FROM sync_state WHERE name=?", (name,)).fetchone()
        if row:
            self.conn.execute(
                "UPDATE sync_state SET cursor=?, last_run_at=?, "
                "last_ok_at=CASE WHEN ? THEN ? ELSE last_ok_at END, note=? WHERE name=?",
                (cursor, stamp, 1 if ok else 0, stamp, note, name))
        else:
            self.conn.execute(
                "INSERT INTO sync_state (name, cursor, last_run_at, last_ok_at, note) "
                "VALUES (?,?,?,?,?)",
                (name, cursor, stamp, stamp if ok else None, note))
        self.conn.commit()

    def get_cursor(self, name: str) -> str:
        row = self.conn.execute("SELECT cursor FROM sync_state WHERE name=?", (name,)).fetchone()
        return str(row["cursor"] or "") if row else ""

    def log_error(self, scope: str, key: str, error: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO sync_errors (scope, key, error, created_at) VALUES (?,?,?,?)",
            (scope, key, error[:500], now()))
        self.conn.commit()
        return int(cursor.lastrowid)

    def resolve_errors(self, scope: str, key: str | None = None) -> int:
        if key:
            cursor = self.conn.execute(
                "UPDATE sync_errors SET resolved=1 WHERE scope=? AND key=? AND resolved=0",
                (scope, key))
        else:
            cursor = self.conn.execute(
                "UPDATE sync_errors SET resolved=1 WHERE scope=? AND resolved=0", (scope,))
        self.conn.commit()
        return cursor.rowcount

    def open_errors(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM sync_errors WHERE resolved=0 ORDER BY id DESC LIMIT ?", (limit,)))

    def log_event(self, kind: str, subject: str = "", detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events (kind, subject, detail, created_at) VALUES (?,?,?,?)",
            (kind, subject, detail[:500], now()))
        self.conn.commit()

    # ---------- 查询 ----------

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("products", "stocks", "postings", "posting_items", "finance_ops"):
            out[table] = self.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        out["open_errors"] = len(self.open_errors())
        return out

    def orders_without_money(self, older_than_hours: float = 0.0) -> list[sqlite3.Row]:
        """订单在库、但财务一条流水都没有（可能还没结算，也可能是真缺账）。"""
        return list(self.conn.execute(
            "SELECT p.posting_number, p.created_at, p.amount, p.status "
            "FROM postings p LEFT JOIN finance_ops f ON f.posting_number = p.posting_number "
            "WHERE f.operation_id IS NULL ORDER BY p.created_at"))

    def money_without_orders(self) -> list[sqlite3.Row]:
        """财务有流水、但订单库里找不到这条邮包 —— 订单同步漏了（真漏单）。"""
        return list(self.conn.execute(
            "SELECT f.posting_number, SUM(f.amount) amount, COUNT(*) n, "
            "MIN(f.operation_date) first_seen "
            "FROM finance_ops f LEFT JOIN postings p ON p.posting_number = f.posting_number "
            "WHERE p.posting_number IS NULL AND f.posting_number <> '' "
            "GROUP BY f.posting_number ORDER BY f.operation_date"))

    def posting_ledger(self) -> list[dict[str, Any]]:
        """每单一条账：应收、实收、佣金、净额、应结。对账用。

        口径（2026-10-15 修正：一开始把"佣金"当成了差额，导致所有单都被判不符）：
        - expected      订单金额（products 里 price × quantity 之和）
        - collected     收入类流水（正数）之和，应等于 expected
        - fees          费用类流水（负数）绝对值，应等于订单里的 commission
        - net           collected - fees，应等于订单里的 payout（应结）
        """
        rows = self.conn.execute(
            "SELECT posting_number, created_at, status, amount, commission, payout "
            "FROM postings "
            "ORDER BY created_at").fetchall()
        ops: dict[str, dict[str, float]] = {}
        for op in self.conn.execute(
                "SELECT posting_number, operation_type, amount FROM finance_ops "
                "WHERE posting_number <> ''"):
            item = ops.setdefault(op["posting_number"], {"net": 0.0, "revenue": 0.0, "fees": 0.0,
                                                          "lines": 0})
            amount = float(op["amount"] or 0)
            item["net"] += amount
            item["lines"] += 1
            if any(hint in (op["operation_type"] or "") for hint in COMMISSION_HINTS) and amount < 0:
                item["fees"] += amount
            elif amount > 0:
                item["revenue"] += amount
        ledger = []
        for row in rows:
            money = ops.get(row["posting_number"], {"net": 0.0, "revenue": 0.0,
                                                    "fees": 0.0, "lines": 0})
            amount = float(row["amount"] or 0)
            ledger.append({
                "posting_number": row["posting_number"],
                "created_at": row["created_at"],
                "status": row["status"],
                "expected": round(amount, 2),
                "collected": round(money["revenue"], 2),
                "fees": round(abs(money["fees"]), 2),
                "net": round(money["net"], 2),
                "diff": round(amount - money["revenue"], 2),
                "expected_payout": round(float(row["payout"] or 0), 2),
                "expected_fee": round(abs(float(row["commission"] or 0)), 2),
                "payout_diff": round(float(row["payout"] or 0) - money["net"], 2),
                "lines": int(money["lines"]),
            })
        return ledger

    def close(self) -> None:
        self.conn.close()

    # ---------- 采集与上架 ----------

    def upsert_collected(self, items: Iterable[dict[str, Any]]) -> dict[str, int]:
        """采集结果落库。用 fingerprint 去重：同一条来源商品再采一次只更新，不新增。"""
        stamp = now()
        stats = {"new": 0, "updated": 0, "skipped": 0}
        for item in items:
            fingerprint = str(item.get("fingerprint") or "").strip()
            if not fingerprint:
                stats["skipped"] += 1
                continue
            row = self.conn.execute(
                "SELECT id FROM collected_products WHERE fingerprint=?", (fingerprint,)).fetchone()
            status = str(item.get("status") or "new")
            if row:
                self.conn.execute(
                    """UPDATE collected_products SET title=COALESCE(?, title),
                       price=COALESCE(?, price), currency=COALESCE(?, currency),
                       images=COALESCE(?, images), attributes=COALESCE(?, attributes),
                       raw=COALESCE(?, raw), status=?, note=?, updated_at=?
                       WHERE id=?""",
                    (item.get("title"), item.get("price"), item.get("currency"),
                     json.dumps(item.get("images") or [], ensure_ascii=False) if item.get("images") else None,
                     json.dumps(item.get("attributes") or {}, ensure_ascii=False) if item.get("attributes") else None,
                     json.dumps(item.get("raw") or {}, ensure_ascii=False), status,
                     item.get("note"), stamp, row["id"]))
                stats["updated"] += 1
            else:
                self.conn.execute(
                    """INSERT INTO collected_products (source, source_id, source_url, title, price,
                       currency, images, attributes, raw, fingerprint, status, note,
                       collected_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item.get("source"), str(item.get("source_id") or ""), item.get("source_url"),
                     item.get("title"), item.get("price"), item.get("currency"),
                     json.dumps(item.get("images") or [], ensure_ascii=False),
                     json.dumps(item.get("attributes") or {}, ensure_ascii=False),
                     json.dumps(item.get("raw") or {}, ensure_ascii=False), fingerprint,
                     status, item.get("note"), stamp, stamp))
                stats["new"] += 1
        self.conn.commit()
        return stats

    def collected_list(self, *, status: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        sql = "SELECT * FROM collected_products"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params))

    def create_listing_tasks(self, *, limit: int = 50) -> int:
        """把 status=ready 的采集记录转成上架任务。UNIQUE(collected_id) 保证不会重复建。"""
        stamp = now()
        created = 0
        rows = self.conn.execute(
            "SELECT c.* FROM collected_products c LEFT JOIN listing_tasks t "
            "ON t.collected_id = c.id WHERE c.status='ready' AND t.id IS NULL "
            "ORDER BY c.id LIMIT ?", (limit,)).fetchall()
        for row in rows:
            self.conn.execute(
                """INSERT INTO listing_tasks (collected_id, offer_id, status, created_at, updated_at)
                   VALUES (?,?, 'pending', ?, ?)""",
                (row["id"], f"SKU-{row['id']:06d}", stamp, stamp))
            created += 1
        self.conn.commit()
        return created

    def pending_listing_tasks(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT t.*, c.title, c.price, c.currency, c.images, c.attributes, c.source, "
            "c.source_id FROM listing_tasks t JOIN collected_products c ON c.id = t.collected_id "
            "WHERE t.status='pending' ORDER BY t.id LIMIT ?", (limit,)))

    def mark_listing(self, task_id: int, status: str, *, product_id: str = "",
                     error: str = "", payload: str = "") -> None:
        self.conn.execute(
            """UPDATE listing_tasks SET status=?, product_id=COALESCE(NULLIF(?, ''), product_id),
               error=?, payload=COALESCE(NULLIF(?, ''), payload),
               attempts=attempts+1, updated_at=? WHERE id=?""",
            (status, product_id, error[:500] or None, payload, now(), task_id))
        self.conn.execute(
            "UPDATE collected_products SET status=?, updated_at=? WHERE id="
            "(SELECT collected_id FROM listing_tasks WHERE id=?)",
            ("published" if status == "published" else "failed", now(), task_id))
        self.conn.commit()

    def listing_summary(self) -> dict[str, int]:
        out = {row["status"]: row["c"] for row in self.conn.execute(
            "SELECT status, COUNT(*) c FROM listing_tasks GROUP BY status")}
        collected = {row["status"]: row["c"] for row in self.conn.execute(
            "SELECT status, COUNT(*) c FROM collected_products GROUP BY status")}
        return {"tasks": out, "collected": collected}

    def __enter__(self) -> "OzonStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
