"""补齐 JD 正文：把列表页拿不到的"任职要求"补进来。

腾讯招聘的列表接口只给职责，详情接口才给任职要求；这一步按岗位 ID 回补，
补不到就跳过并记录原因，不影响已有内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .sources.tencent import TencentSource
from .store import Store


@dataclass
class EnrichReport:
    scanned: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"scanned": self.scanned, "updated": self.updated,
                "skipped": self.skipped, "errors": self.errors or []}


def enrich_tencent(store: Store, *, limit: int = 30, missing_only: bool = True) -> EnrichReport:
    report = EnrichReport(errors=[])
    source = TencentSource()
    sql = "SELECT * FROM jobs WHERE platform='tencent'"
    rows = list(store.conn.execute(sql))
    picked = 0
    for row in rows:
        description = row["description"] or ""
        if missing_only and "任职要求" in description:
            report.skipped += 1
            continue
        if picked >= limit:
            break
        picked += 1
        report.scanned += 1
        raw = row["raw_card"]
        post_id = ""
        try:
            import json

            post_id = str((json.loads(raw) if raw else {}).get("post_id") or "")
        except Exception:
            post_id = ""
        post_id = post_id or str(row["external_id"])
        try:
            detail = source.fetch_detail(post_id)
        except Exception as exc:
            report.errors.append(f"#{row['id']} {row['title'][:18]}：{type(exc).__name__}: {exc}")
            continue
        requirement = detail.get("requirement") or ""
        if not requirement:
            report.skipped += 1
            continue
        combined = description
        if "任职要求" not in combined:
            combined = f"{description}\n\n任职要求：\n{requirement}".strip()
        store.set_description(row["id"], combined)
        report.updated += 1
    store.log_event("enrich", "tencent", f"补齐 {report.updated} 条 JD 要求")
    return report
