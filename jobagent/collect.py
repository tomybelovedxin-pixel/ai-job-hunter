"""取数编排：逐个数据源取岗位 → 落库 → 去重与有效性标记 → 记账。

一个源失败不影响其它源；失败原因原样写进运行记录，不吞异常。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .sources import Filters, Source, SourceError
from .sources.dedupe import check_job
from .store import Store


@dataclass
class SourceReport:
    source: str
    label: str
    fetched: int = 0
    raw_count: int = 0
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    flagged: int = 0
    seconds: float = 0.0
    error: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": self.label,
            "fetched": self.fetched,
            "raw_count": self.raw_count,
            "new": self.new,
            "updated": self.updated,
            "duplicates": self.duplicates,
            "flagged": self.flagged,
            "seconds": round(self.seconds, 2),
            "error": self.error,
            "note": self.note,
        }


@dataclass
class CollectReport:
    sources: list[SourceReport] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "new": sum(item.new for item in self.sources),
            "updated": sum(item.updated for item in self.sources),
            "duplicates": sum(item.duplicates for item in self.sources),
            "flagged": sum(item.flagged for item in self.sources),
            "failed_sources": sum(1 for item in self.sources if item.error),
        }

    def as_dict(self) -> dict[str, Any]:
        return {"totals": self.totals, "sources": [item.as_dict() for item in self.sources]}


def mark_duplicates(store: Store, *, threshold: float = 92.0) -> list[tuple[int, int, float]]:
    """全库找跨平台重复并落库。返回 [(重复 id, 保留 id, 相似度)]。"""
    from .sources.dedupe import find_duplicates

    rows = store.conn.execute(
        "SELECT id, platform, title, company FROM jobs WHERE valid IS NOT 0 ORDER BY id"
    ).fetchall()
    found = find_duplicates(rows, threshold=threshold)
    for drop_id, keep_id, _score in found:
        store.mark_duplicate(drop_id, keep_id)
        store.mark_validity(drop_id, 0, f"与 #{keep_id} 重复", ["跨平台重复"])
    store.log_event("dedupe", "jobs", f"标记 {len(found)} 条跨平台重复")
    return found


def collect(
    store: Store,
    sources: Iterable[Source],
    filters: Filters,
    *,
    pause: float = 0.6,
    dedupe: bool = True,
) -> CollectReport:
    report = CollectReport()
    for source in sources:
        entry = SourceReport(source=source.name, label=source.label)
        started = time.time()
        run_id = store.start_run(f"collect:{source.name}")
        try:
            result = source.fetch(filters)
            entry.raw_count = result.raw_count
            entry.note = result.note
            entry.fetched = len(result.jobs)
            if result.error:
                entry.error = result.error
            if result.jobs:
                new, updated = store.upsert_jobs(source.name, result.jobs)
                entry.new, entry.updated = new, updated
                if dedupe:
                    for job in result.jobs:
                        row = store.conn.execute(
                            "SELECT id, title, company, last_seen, description FROM jobs "
                            "WHERE platform=? AND external_id=?",
                            (source.name, str(job.get("external_id"))),
                        ).fetchone()
                        if not row:
                            continue
                        # 这一轮只做有效性/可疑度判断；跨平台重复由 dedupe 命令统一处理，
                        # 因为它需要全库比对，跑在取数循环里会拖慢每个源。
                        check = check_job(job)
                        if check.flags or check.valid is False:
                            entry.flagged += 1
                        store.mark_validity(row["id"], check.valid, check.note, check.flags)
        except SourceError as exc:
            entry.error = str(exc)
        except Exception as exc:  # 数据源里的意外错误也要留痕
            entry.error = f"{type(exc).__name__}: {exc}"
        finally:
            entry.seconds = time.time() - started
            store.finish_run(run_id, entry.as_dict())
        store.log_event("collect", source.name, entry.error or entry.note)
        report.sources.append(entry)
        if pause:
            time.sleep(pause)
    return report
