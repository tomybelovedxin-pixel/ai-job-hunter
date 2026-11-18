"""Ozon Seller API 客户端：鉴权、限流、重试、分页。

端点集中写在 ENDPOINTS 里，一处改全局生效。
⚠️ 2026-10-15 本机连不上 docs.ozon.ru / api-seller.ozon.ru（代理对这些域名 TLS 被断），
所以这些路径与字段是**按公开文档结构整理、未经联网核对**。接真实账号的第一步是
`python -m jobagent.ozon_erp doctor`，它会逐个端点打一次最小请求，把不匹配的报出来。
解析一律用"取不到就给默认值"的写法，字段小改不会把整条链路打断。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

ENDPOINTS: dict[str, str] = {
    # 商品
    "product_list": "/v3/product/list",
    "product_info": "/v3/product/info/list",
    "product_stocks": "/v4/product/info/stocks",
    "product_prices": "/v5/product/info/prices",
    "product_import": "/v3/product/import",
    # 订单（邮包）
    "posting_fbs_list": "/v3/posting/fbs/list",
    "posting_fbo_list": "/v2/posting/fbo/list",
    # 财务
    "finance_transactions": "/v3/finance/transaction/list",
}


class OzonError(RuntimeError):
    """接口层错误。retryable=True 表示值得重试（429/5xx/网络）。"""

    def __init__(self, message: str, *, status: int | None = None,
                 code: str = "", retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


@dataclass
class RateLimiter:
    """最简单的"每秒不超过 N 次"限流：记时间戳，超了就睡到下一个窗口。"""

    per_second: float = 8.0
    _stamps: list[float] = field(default_factory=list)

    def wait(self) -> None:
        if self.per_second <= 0:
            return
        now = time.monotonic()
        window = [stamp for stamp in self._stamps if now - stamp < 1.0]
        if len(window) >= self.per_second:
            sleep_for = 1.0 - (now - window[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            window = [stamp for stamp in self._stamps if now - stamp < 1.0]
        window.append(now)
        self._stamps = window


def urllib_transport(base_url: str, path: str, payload: dict[str, Any],
                     headers: dict[str, str], timeout: float) -> tuple[int, dict[str, Any]]:
    """默认传输层：标准库 POST JSON。代理走环境变量 HTTPS_PROXY（本机是 7897）。"""
    url = base_url.rstrip("/") + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    for key, value in headers.items():
        request.add_header(key, value)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {"message": raw[:200]}
        return exc.code, data
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OzonError(f"网络不可达：{exc}", retryable=True) from exc


class OzonClient:
    """一个卖家账号的客户端。transport 可替换，测试用 mock.MockOzon。"""

    def __init__(self, client_id: str, api_key: str, *, base_url: str = "",
                 rate_per_second: float = 8.0, max_retries: int = 4, timeout: float = 30.0,
                 transport: Callable[..., tuple[int, dict[str, Any]]] | None = None,
                 sleeper: Callable[[float], None] = time.sleep):
        self.client_id = client_id
        self.api_key = api_key
        self.base_url = base_url or "https://api-seller.ozon.ru"
        self.max_retries = max_retries
        self.timeout = timeout
        self.limiter = RateLimiter(per_second=rate_per_second)
        self.transport = transport or urllib_transport
        self.sleeper = sleeper
        self.calls: list[dict[str, Any]] = []      # 调用流水（排查与自测用）
        self.retries = 0

    # ---------- 底层调用 ----------

    def post(self, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        path = ENDPOINTS.get(endpoint, endpoint)
        headers = {"Client-Id": self.client_id, "Api-Key": self.api_key,
                   "Accept": "application/json"}
        payload = payload or {}
        last_error: OzonError | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            started = time.time()
            try:
                status, data = self.transport(self.base_url, path, payload, headers, self.timeout)
            except OzonError as exc:
                last_error = exc
                status, data = 0, {}
            else:
                if 200 <= status < 300:
                    self.calls.append({"endpoint": endpoint, "status": status,
                                       "seconds": round(time.time() - started, 3),
                                       "attempt": attempt + 1, "ok": True})
                    return data if isinstance(data, dict) else {"result": data}
                message = str(data.get("message") or data.get("error") or "")[:200]
                retryable = status == 429 or 500 <= status < 600
                last_error = OzonError(f"{endpoint} 返回 {status}：{message}",
                                       status=status, retryable=retryable)
                if not retryable:
                    self.calls.append({"endpoint": endpoint, "status": status,
                                       "attempt": attempt + 1, "ok": False, "error": message})
                    raise last_error
            self.retries += 1
            self.calls.append({"endpoint": endpoint, "status": status,
                               "attempt": attempt + 1, "ok": False, "retry": True})
            backoff = min(2 ** attempt * 0.5, 8.0)
            self.sleeper(backoff)
        raise last_error or OzonError(f"{endpoint} 重试 {self.max_retries} 次仍失败")

    # ---------- 商品 ----------

    def iter_products(self, *, limit: int = 1000, page_size: int = 100,
                      max_pages: int | None = None) -> Iterator[dict[str, Any]]:
        """按 last_id 游标翻商品；返回 {product_id, offer_id}。"""
        last_id = ""
        pages = 0
        while True:
            data = self.post("product_list", {
                "filter": {"visibility": "ALL"},
                "last_id": last_id,
                "limit": min(page_size, limit),
            })
            result = data.get("result") or {}
            items = result.get("items") or []
            for item in items:
                yield {"product_id": str(item.get("product_id") or ""),
                       "offer_id": str(item.get("offer_id") or "")}
            last_id = str(result.get("last_id") or "")
            pages += 1
            total = result.get("total")
            if not items or not last_id or (max_pages and pages >= max_pages):
                return
            if total is not None and pages * page_size >= int(total):
                return

    def product_info(self, product_ids: list[str]) -> list[dict[str, Any]]:
        """商品详情：名称、条码、类目、体积重量等。"""
        if not product_ids:
            return []
        data = self.post("product_info", {"product_id": product_ids})
        items = data.get("items") or (data.get("result") or {}).get("items") or []
        return list(items)

    def product_stocks(self, offer_ids: list[str] | None = None,
                       product_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """库存：FBS/FBO 的 present / reserved。"""
        cursor = ""
        out: list[dict[str, Any]] = []
        while True:
            data = self.post("product_stocks", {
                "filter": {"offer_id": offer_ids or [], "product_id": product_ids or []},
                "limit": 1000,
                "cursor": cursor,
            })
            items = data.get("items") or (data.get("result") or {}).get("items") or []
            out.extend(items)
            cursor = str(data.get("cursor") or "")
            if not items or not cursor:
                return out

    def import_products(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """批量上架/更新商品。返回 result（含每条 offer_id 的处理结果）。"""
        if not items:
            return {}
        data = self.post("product_import", {"items": items})
        return data.get("result") if isinstance(data.get("result"), dict) else data

    # ---------- 订单 ----------

    def iter_postings(self, *, kind: str = "fbs", since: str, to: str,
                      status: str = "", page_size: int = 100,
                      max_pages: int | None = None) -> Iterator[dict[str, Any]]:
        """增量拉邮包（订单）。since/to 是 ISO8601（UTC）。"""
        endpoint = "posting_fbs_list" if kind.lower() == "fbs" else "posting_fbo_list"
        offset = 0
        pages = 0
        while True:
            payload: dict[str, Any] = {
                "dir": "ASC",
                "filter": {"since": since, "to": to},
                "limit": page_size,
                "offset": offset,
                "with": {"analytics_data": False, "financial_data": True},
            }
            if status:
                payload["filter"]["status"] = status
            data = self.post(endpoint, payload)
            result = data.get("result") or {}
            postings = result.get("postings") if isinstance(result, dict) else result
            postings = postings or []
            for posting in postings:
                yield posting
            pages += 1
            if len(postings) < page_size or (max_pages and pages >= max_pages):
                return
            offset += page_size

    # ---------- 财务 ----------

    def iter_finance(self, *, date_from: str, date_to: str,
                     page_size: int = 1000, max_pages: int | None = None) -> Iterator[dict]:
        page = 1
        while True:
            data = self.post("finance_transactions", {
                "filter": {"date": {"from": date_from, "to": date_to},
                           "operation_type": [], "posting_number": ""},
                "page": page,
                "page_size": page_size,
            })
            result = data.get("result") or {}
            operations = result.get("operations") or []
            for operation in operations:
                yield operation
            page_count = int(result.get("page_count") or 1)
            if not operations or page >= page_count or (max_pages and page >= max_pages):
                return
            page += 1

    # ---------- 体检 ----------

    def doctor(self) -> list[dict[str, Any]]:
        """对每个端点各打一次最小请求，返回 {endpoint, ok, detail}。"""
        probes = [
            ("product_list", {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1}),
            ("posting_fbs_list", {"dir": "ASC", "filter": {"since": "2024-01-01T00:00:00Z",
                                                           "to": "2024-01-02T00:00:00Z"},
                                  "limit": 1, "offset": 0, "with": {}}),
            ("finance_transactions", {"filter": {"date": {"from": "2024-01-01T00:00:00Z",
                                                          "to": "2024-01-02T00:00:00Z"},
                                                 "operation_type": [], "posting_number": ""},
                                      "page": 1, "page_size": 1}),
        ]
        out = []
        for endpoint, payload in probes:
            try:
                data = self.post(endpoint, payload)
                keys = ", ".join(sorted(data.keys())[:4])
                out.append({"endpoint": endpoint, "ok": True, "detail": f"返回字段：{keys}"})
            except OzonError as exc:
                out.append({"endpoint": endpoint, "ok": False, "detail": str(exc)[:160]})
        return out
