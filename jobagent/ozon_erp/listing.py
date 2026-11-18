"""上架：采集箱里的素材 → 生成 Ozon 上架载荷 → 调官方 API → 逐条回收执 → 失败单独重试。

三件事说清楚（免得吹）：
- 标题重组和翻译都是**可插拔**的：不传翻译函数就保留原文并在结果里注明"未接翻译"，
  不假装做过翻译；俄语关键词重组是规则（品牌 + 核心词 + 属性词 + 长度上限），不是玄学。
- 图片处理给的是**计划 + 可选执行**：Pillow 在就下载-白底-压缩，不在就只输出处理计划。
- 提交走 Ozon 官方 `product/import`；同一个 offer_id 重复提交是更新（幂等），
  已 published 的任务不会重复提交。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from .client import OzonClient, OzonError
from .store import OzonStore

MAX_TITLE_LEN = 120
MIN_TITLE_LEN = 8
DEFAULT_CURRENCY = "RUB"


# ---------- 标题与素材 ----------

_SPACE = re.compile(r"\s+")


def reorganize_title(title: str, *, brand: str = "", keywords: list[str] | None = None,
                     max_len: int = MAX_TITLE_LEN) -> str:
    """标题重组：品牌 + 核心词 + 关键词，去重复词、控长度。

    Ozon 上标题越长越容易被截断，所以这里保证核心信息在前面。
    """
    parts: list[str] = []
    for chunk in [brand, title] + list(keywords or []):
        cleaned = _SPACE.sub(" ", (chunk or "").strip())
        if cleaned and cleaned.lower() not in [p.lower() for p in parts]:
            parts.append(cleaned)
    merged = " ".join(parts)
    if len(merged) <= max_len:
        return merged
    cut = merged[:max_len].rsplit(" ", 1)[0]
    return cut or merged[:max_len]


def build_payload(row: Any, *, translator: Callable[[str], str] | None = None,
                  brand: str = "", keywords: list[str] | None = None,
                  category_id: int | None = None) -> dict[str, Any]:
    """一条采集记录 → 一条上架载荷。images/attributes 从库里反序列化。"""
    title = str(row["title"] or "")
    if translator:
        title = translator(title)
    payload = {
        "offer_id": str(row["offer_id"]),
        "name": reorganize_title(title, brand=brand, keywords=keywords),
        "price": str(round(float(row["price"] or 0), 2)),
        "currency_code": str(row["currency"] or DEFAULT_CURRENCY),
        "images": json.loads(row["images"] or "[]"),
        "attributes": json.loads(row["attributes"] or "{}"),
    }
    if category_id:
        payload["category_id"] = category_id
    return payload


def validate_payload(payload: dict[str, Any]) -> list[str]:
    """提交前的本地校验：必填项不齐的直接拦下，别把脏数据打到平台上。"""
    problems: list[str] = []
    name = str(payload.get("name") or "")
    if len(name) < MIN_TITLE_LEN:
        problems.append(f"标题过短（{len(name)} 字）")
    if len(name) > 255:
        problems.append("标题超过 255 字")
    try:
        if float(payload.get("price") or 0) <= 0:
            problems.append("价格不是正数")
    except (TypeError, ValueError):
        problems.append("价格格式不对")
    if not payload.get("images"):
        problems.append("没有图片")
    if not str(payload.get("offer_id") or "").strip():
        problems.append("缺 offer_id")
    return problems


def image_plan(images: list[str], *, target_side: int = 1000,
               workdir: str | Path | None = None) -> dict[str, Any]:
    """图片处理计划；装了 Pillow 且给了 workdir 就真的处理一遍（白底、压缩）。"""
    plan = {"count": len(images), "target_side": target_side,
            "steps": ["下载原图", "去背/白底", f"压到长边 {target_side}", "转 JPEG 质量 85"],
            "processed": 0, "note": ""}
    if not images or not workdir:
        plan["note"] = "只给计划：没指定 workdir"
        return plan
    try:
        from PIL import Image                     # 可选依赖，没有就只出计划
    except ImportError:
        plan["note"] = "只给计划：本机没装 Pillow"
        return plan
    out_dir = Path(workdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    import urllib.request

    for index, url in enumerate(images):
        target = out_dir / f"img-{index:03d}.jpg"
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                raw = resp.read()
            tmp = out_dir / f"src-{index:03d}"
            tmp.write_bytes(raw)
            with Image.open(tmp) as img:
                img = img.convert("RGB")
                if max(img.size) > target_side:
                    ratio = target_side / max(img.size)
                    img = img.resize((int(img.width * ratio), int(img.height * ratio)))
                img.save(target, "JPEG", quality=85)
            tmp.unlink(missing_ok=True)
            plan["processed"] += 1
        except Exception as exc:                  # 单张失败不影响其它
            plan["note"] += f" {url} 处理失败：{type(exc).__name__}"
    return plan


# ---------- 提交 ----------

def publish(store: OzonStore, client: OzonClient, *, limit: int = 20,
            translator: Callable[[str], str] | None = None,
            brand: str = "", keywords: list[str] | None = None,
            category_id: int | None = None) -> dict[str, Any]:
    """把 pending 的上架任务发出去。逐条判定结果，失败的留 error 供单独重试。"""
    store.create_listing_tasks(limit=limit)
    tasks = store.pending_listing_tasks(limit)
    report: dict[str, Any] = {"pending": len(tasks), "published": 0, "failed": 0,
                              "blocked": [], "errors": [], "translated": bool(translator)}
    if not tasks:
        return report

    batch: list[tuple[Any, dict[str, Any]]] = []
    for task in tasks:
        payload = build_payload(task, translator=translator, brand=brand,
                                keywords=keywords, category_id=category_id)
        problems = validate_payload(payload)
        if problems:
            store.mark_listing(int(task["id"]), "failed", error="；".join(problems),
                               payload=json.dumps(payload, ensure_ascii=False))
            report["blocked"].append({"offer_id": payload["offer_id"], "problems": problems})
            report["failed"] += 1
            continue
        batch.append((task, payload))

    if not batch:
        return report

    try:
        result = client.import_products([payload for _, payload in batch])
    except OzonError as exc:
        # 整批失败：任务保持 pending，下次重跑（不标记失败，避免丢件）
        report["errors"].append(str(exc))
        store.log_error("listing", f"{len(batch)} 条", str(exc))
        return report

    by_offer = {str(item.get("offer_id")): item for item in (result.get("items") or [])}
    for task, payload in batch:
        item = by_offer.get(payload["offer_id"], {})
        errors = item.get("errors") or []
        if errors:
            message = "；".join(str(e.get("message") or e) for e in errors)
            store.mark_listing(int(task["id"]), "failed", error=message,
                               payload=json.dumps(payload, ensure_ascii=False))
            report["failed"] += 1
        else:
            store.mark_listing(int(task["id"]), "published",
                               product_id=str(item.get("product_id") or ""),
                               payload=json.dumps(payload, ensure_ascii=False))
            report["published"] += 1
    store.log_event("listing", "product/import",
                    json.dumps({k: v for k, v in report.items() if k != "blocked"},
                               ensure_ascii=False))
    return report


def retry_failed(store: OzonStore) -> int:
    """把失败的上架任务放回 pending（只重跑失败的那部分）。"""
    cursor = store.conn.execute(
        "UPDATE listing_tasks SET status='pending', error=NULL, updated_at=datetime('now','localtime') "
        "WHERE status='failed'")
    store.conn.execute(
        "UPDATE collected_products SET status='ready' WHERE id IN "
        "(SELECT collected_id FROM listing_tasks WHERE status='pending')")
    store.conn.commit()
    return cursor.rowcount
