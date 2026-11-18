"""本地模拟卖家：不连网也能把同步、重试、幂等、漏单、对账整条链跑一遍。

它实现的是同一个 transport 协议（base_url/path/payload/headers/timeout -> (status, body)），
所以 OzonClient 一行都不用改。可以在构造时注入两类"捣乱"：
- fail_every=N：每 N 次调用返回一次 429，用来验证重试；
- duplicate_page_every=N：每 N 页重复返回上一页，用来验证幂等去重。
"""

from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MockOzon:
    fail_every: int = 0
    duplicate_page_every: int = 0
    products: list[dict[str, Any]] = field(default_factory=list)
    stocks: list[dict[str, Any]] = field(default_factory=list)
    postings: list[dict[str, Any]] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _page_seen: dict[str, int] = field(default_factory=dict)

    # ---------- 造数据 ----------

    @classmethod
    def demo(cls, *, products: int = 6, orders: int = 12, with_gaps: bool = False,
             fail_every: int = 0, duplicate_page_every: int = 0,
             seed: int = 7) -> "MockOzon":
        """造一批可复现的数据：商品、库存、订单、财务流水（其中 $with_gaps$ 造三处异常）。"""
        rng = random.Random(seed)
        mock = cls(fail_every=fail_every, duplicate_page_every=duplicate_page_every)
        for index in range(products):
            product_id = 100000 + index
            offer_id = f"SKU-{1000 + index}"
            mock.products.append({
                "product_id": str(product_id), "offer_id": offer_id,
                "id": product_id,
                "name": f"Товар {index + 1} / 商品 {index + 1}",
                "barcode": f"46000000000{index}",
                "category_id": 15600 + index,
                "volume_weight": round(rng.uniform(0.2, 3.0), 2),
            })
            mock.stocks.append({
                "product_id": str(product_id), "offer_id": offer_id,
                "stocks": [{"type": "fbs", "present": rng.randint(0, 40),
                            "reserved": rng.randint(0, 3)}],
            })

        for index in range(orders):
            product = mock.products[index % len(mock.products)]
            quantity = rng.randint(1, 3)
            price = round(rng.uniform(500, 3000), 2)
            posting_number = f"{60000000 + index}-{index % 7 + 1:04d}-1"
            created = f"2026-10-{10 + index % 5:02d}T{index % 24:02d}:15:00Z"
            order = {
                "posting_number": posting_number,
                "order_id": 700000 + index,
                "order_number": f"ON-{700000 + index}",
                "status": rng.choice(["awaiting_deliver", "delivering", "delivered"]),
                "created_at": created,
                "in_process_at": created,
                "products": [{
                    "sku": int(product["product_id"]),
                    "offer_id": product["offer_id"],
                    "name": product["name"],
                    "quantity": quantity,
                    "price": str(price),
                    "currency_code": "RUB",
                }],
                "financial_data": {"products": [{
                    "product_id": int(product["product_id"]),
                    "commission_amount": round(price * quantity * 0.12, 2),
                    "payout": round(price * quantity * 0.88, 2),
                }]},
            }
            mock.postings.append(order)

            gross = round(price * quantity, 2)
            commission = round(gross * 0.12, 2)
            payout = round(gross - commission, 2)
            mock.operations.append({
                "operation_id": 900000 + index * 2,
                "operation_type": "OperationAgentDeliveredToCustomer",
                "operation_date": created,
                "posting": {"posting_number": posting_number,
                            "order_id": order["order_id"]},
                "amount": gross, "currency_code": "RUB", "type": "orders",
            })
            mock.operations.append({
                "operation_id": 900001 + index * 2,
                "operation_type": "OperationMarketplaceServiceItemDelivery",
                "operation_date": created,
                "posting": {"posting_number": posting_number,
                            "order_id": order["order_id"]},
                "amount": -commission, "currency_code": "RUB", "type": "services",
            })

        if with_gaps:
            # 1) 缺流水：第 1 单的金额流水被抹掉
            first = mock.postings[0]["posting_number"]
            mock.operations = [op for op in mock.operations
                               if not (op["posting"]["posting_number"] == first
                                       and op["amount"] > 0)]
            # 2) 金额不符：第 2 单的服务费流水多扣了 5 卢布
            second = mock.postings[1]["posting_number"]
            for op in mock.operations:
                if op["posting"]["posting_number"] == second and op["amount"] < 0:
                    op["amount"] = round(op["amount"] - 5, 2)
            # 3) 多流水：凭空多一条无主订单的流水
            mock.operations.append({
                "operation_id": 999999,
                "operation_type": "OperationAgentDeliveredToCustomer",
                "operation_date": "2026-10-14T10:00:00Z",
                "posting": {"posting_number": "00000000-0000-1", "order_id": 0},
                "amount": 1234.56, "currency_code": "RUB", "type": "orders",
            })
        return mock

    # ---------- transport ----------

    def transport(self, base_url: str, path: str, payload: dict[str, Any],
                  headers: dict[str, str], timeout: float) -> tuple[int, dict[str, Any]]:
        self.calls.append({"path": path, "payload": copy.deepcopy(payload),
                           "client_id": headers.get("Client-Id", "")})
        if not headers.get("Client-Id") or not headers.get("Api-Key"):
            return 401, {"message": "Client-Id/Api-Key is missing"}
        if self.fail_every and len(self.calls) % self.fail_every == 0:
            return 429, {"message": "Too Many Requests (mock)"}
        handler = {
            "/v3/product/list": self._product_list,
            "/v3/product/info/list": self._product_info,
            "/v4/product/info/stocks": self._stocks,
            "/v3/product/import": self._product_import,
            "/v3/posting/fbs/list": lambda p: self._postings(p, "fbs"),
            "/v2/posting/fbo/list": lambda p: self._postings(p, "fbo"),
            "/v3/finance/transaction/list": self._finance,
        }.get(path)
        if handler is None:
            return 404, {"message": f"unknown path {path}"}
        return 200, handler(payload)

    # ---------- 各端点 ----------

    def _product_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = int(payload.get("limit") or 100)
        last_id = str(payload.get("last_id") or "")
        start = 0
        if last_id:
            start = next((i + 1 for i, p in enumerate(self.products)
                          if p["product_id"] == last_id), len(self.products))
        page = self.products[start:start + limit]
        return {"result": {
            "items": [{"product_id": p["product_id"], "offer_id": p["offer_id"]} for p in page],
            "total": len(self.products),
            "last_id": page[-1]["product_id"] if page else "",
        }}

    def _product_info(self, payload: dict[str, Any]) -> dict[str, Any]:
        wanted = {str(x) for x in (payload.get("product_id") or payload.get("offer_id") or [])}
        items = [p for p in self.products
                 if str(p["product_id"]) in wanted or p["offer_id"] in wanted]
        return {"items": items}

    def _stocks(self, payload: dict[str, Any]) -> dict[str, Any]:
        filt = payload.get("filter") or {}
        wanted = {str(x) for x in (filt.get("product_id") or filt.get("offer_id") or [])}
        items = [s for s in self.stocks
                 if not wanted or str(s["product_id"]) in wanted or s["offer_id"] in wanted]
        # 模拟一次重复页（游标不前进时客户端必须去重）
        cursor = ""
        self._page_seen["stocks"] = self._page_seen.get("stocks", 0) + 1
        if self.duplicate_page_every and self._page_seen["stocks"] % self.duplicate_page_every == 0:
            cursor = "mock-cursor"
        return {"items": items, "cursor": cursor}

    def _product_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        """模拟上架：必填项不全的这一条失败，其余成功；同一个 offer_id 再提交算更新。"""
        results = []
        for item in payload.get("items") or []:
            offer_id = str(item.get("offer_id") or "")
            errors = []
            if not item.get("name") or len(str(item.get("name"))) < 5:
                errors.append({"message": "name is too short"})
            if float(item.get("price") or 0) <= 0:
                errors.append({"message": "price must be positive"})
            if not item.get("images"):
                errors.append({"message": "images are required"})
            if errors:
                results.append({"offer_id": offer_id, "status": "failed", "errors": errors})
                continue
            existing = next((p for p in self.products if p["offer_id"] == offer_id), None)
            if existing:
                existing.update({"name": item.get("name"), "price": item.get("price")})
                product_id = existing["product_id"]
                status = "updated"
            else:
                product_id = str(300000 + len(self.products))
                self.products.append({"product_id": product_id, "offer_id": offer_id,
                                      "id": int(product_id), "name": item.get("name"),
                                      "barcode": item.get("barcode") or "",
                                      "category_id": item.get("category_id"),
                                      "volume_weight": item.get("volume_weight")})
                status = "imported"
            results.append({"offer_id": offer_id, "product_id": product_id,
                            "status": status, "errors": []})
        return {"result": {"task_id": 900000 + len(self.calls), "items": results}}

    def _postings(self, payload: dict[str, Any], kind: str) -> dict[str, Any]:
        filt = payload.get("filter") or {}
        since = str(filt.get("since") or "")
        to = str(filt.get("to") or "")
        status = str(filt.get("status") or "")
        offset = int(payload.get("offset") or 0)
        limit = int(payload.get("limit") or 100)
        rows = [p for p in self.postings
                if (not since or p["created_at"] >= since)
                and (not to or p["created_at"] <= to)
                and (not status or p["status"] == status)]
        rows.sort(key=lambda p: (p["created_at"], p["posting_number"]))
        page = rows[offset:offset + limit]
        self._page_seen[f"postings-{kind}"] = self._page_seen.get(f"postings-{kind}", 0) + 1
        if (self.duplicate_page_every
                and self._page_seen[f"postings-{kind}"] % self.duplicate_page_every == 0
                and offset > 0):
            page = rows[offset - limit:offset] + page      # 混入上一页
        return {"result": {"postings": page}}

    def _finance(self, payload: dict[str, Any]) -> dict[str, Any]:
        filt = payload.get("filter") or {}
        date = filt.get("date") or {}
        date_from = str(date.get("from") or "")
        date_to = str(date.get("to") or "")
        page = int(payload.get("page") or 1)
        page_size = int(payload.get("page_size") or 1000)
        rows = [op for op in self.operations
                if (not date_from or op["operation_date"] >= date_from)
                and (not date_to or op["operation_date"] <= date_to)]
        rows.sort(key=lambda op: (op["operation_date"], op["operation_id"]))
        total = max(1, (len(rows) + page_size - 1) // page_size)
        start = (page - 1) * page_size
        return {"result": {"operations": rows[start:start + page_size],
                           "page_count": total}}

    # ---------- 辅助 ----------

    def add_posting(self, **overrides: Any) -> dict[str, Any]:
        """加一单（测试漏单/局部恢复用）。"""
        base = copy.deepcopy(self.postings[-1]) if self.postings else {
            "products": [{"sku": 1, "offer_id": "SKU-1000", "name": "x",
                          "quantity": 1, "price": "100", "currency_code": "RUB"}]}
        base.update(overrides)
        self.postings.append(base)
        return base

    def as_json(self) -> str:
        return json.dumps({"products": len(self.products), "postings": len(self.postings),
                           "operations": len(self.operations)}, ensure_ascii=False)
