"""语料质量报告：长度分布、重复率、乱码比例、个人信息残留、标签一致性。

报告是给"这批数据能不能拿去训练"用的，所以只列可复核的数字和命中项，
不给"质量优秀"这种结论；阈值写在 RULES 里，判定由 assert_report_ok 做。
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from typing import Any, Iterable

from .clean import Document

# 手机号/邮箱/身份证：训练语料里不该留（先报出来，脱敏决策交给人）
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "邮箱": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "身份证": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "微信号": re.compile(r"(?:微信|weixin|wx)\s*[:：]?\s*[A-Za-z][A-Za-z0-9_-]{5,19}"),
}

REPLACEMENT_CHAR = "\ufffd"

RULES = {
    "min_docs": 10,              # 样本太少，说明没准备好
    "max_dup_ratio": 0.10,       # 近似重复占比上限
    "max_empty_ratio": 0.05,     # 空洞样本（复读、无实义）占比上限
    "min_median_chars": 80,      # 样本中位长度下限
    "max_pii_hits": 20,          # 个人信息命中上限（超过要先脱敏）
}


def scan_pii(text: str) -> dict[str, list[str]]:
    """扫一遍个人信息，返回 {类型: [命中的原文]}。"""
    hits: dict[str, list[str]] = {}
    for name, pattern in PII_PATTERNS.items():
        found = pattern.findall(text or "")
        if found:
            hits[name] = found[:5]
    return hits


def _is_hollow(text: str) -> bool:
    """空洞样本：去掉重复字符后没剩多少内容（"好的好的好的…""。。。")。"""
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return True
    unique_ratio = len(set(compact)) / len(compact)
    return unique_ratio < 0.15 or len(compact) < 20


def quality_report(docs: list[Document], *, near_pairs: list[dict[str, Any]] | None = None,
                   dropped: list[dict[str, Any]] | None = None,
                   source_total: int | None = None) -> dict[str, Any]:
    """按这批文档出一份质量报告（全部数字可复核）。"""
    lengths = [doc.chars for doc in docs] or [0]
    pii: dict[str, list[str]] = {}
    pii_docs: list[str] = []
    hollow: list[str] = []
    for doc in docs:
        found = scan_pii(doc.text)
        if found:
            pii_docs.append(doc.doc_id)
            for name, values in found.items():
                pii.setdefault(name, []).extend(values)
        if _is_hollow(doc.text):
            hollow.append(doc.doc_id)
    dup_count = len(near_pairs or []) + len(dropped or [])
    total = len(docs) + dup_count
    language = _language_mix(docs)
    return {
        "source_docs": source_total if source_total is not None else total,
        "kept_docs": len(docs),
        "dropped_duplicates": dup_count,
        "dup_ratio": round(dup_count / total, 4) if total else 0.0,
        "chars": {"total": sum(lengths), "min": min(lengths), "max": max(lengths),
                  "median": int(statistics.median(lengths)),
                  "mean": int(statistics.mean(lengths))},
        "sentences": {"total": sum(len(doc.sentences) for doc in docs),
                      "mean": round(statistics.mean([len(doc.sentences) for doc in docs] or [0]), 1)},
        "empty_or_hollow": {"count": len(hollow), "ratio": round(len(hollow) / len(docs), 4)
                            if docs else 0.0, "samples": hollow[:5]},
        "pii": {"docs": len(pii_docs), "hits": sum(len(v) for v in pii.values()),
                "kinds": {k: len(v) for k, v in pii.items()},
                "samples": {k: v[:2] for k, v in pii.items()}},
        "language": language,
        "near_dupe_pairs": (near_pairs or [])[:5],
        "thresholds": RULES,
    }


def _language_mix(docs: Iterable[Document]) -> dict[str, float]:
    """粗判语言占比：按字符类别统计，够用就行。"""
    han = latin = other = 0
    for doc in docs:
        for ch in doc.text:
            if "\u4e00" <= ch <= "\u9fff":
                han += 1
            elif ch.isascii() and ch.isalnum():
                latin += 1
            elif not ch.isspace():
                other += 1
    total = han + latin + other or 1
    return {"中文": round(han / total, 3), "英文数字": round(latin / total, 3),
            "其他": round(other / total, 3)}


def assert_report_ok(report: dict[str, Any]) -> list[str]:
    """按 RULES 判定这批数据能不能用；返回问题清单（空列表 = 通过）。"""
    problems: list[str] = []
    if report.get("kept_docs", 0) < RULES["min_docs"]:
        problems.append(f"样本只有 {report.get('kept_docs')} 条，少于 {RULES['min_docs']} 条")
    if report.get("dup_ratio", 0) > RULES["max_dup_ratio"]:
        problems.append(f"重复占比 {report['dup_ratio']:.0%}，超过 {RULES['max_dup_ratio']:.0%}")
    if (report.get("empty_or_hollow") or {}).get("ratio", 0) > RULES["max_empty_ratio"]:
        problems.append(f"空洞样本占比 {(report['empty_or_hollow']['ratio']):.0%} 超过上限")
    if (report.get("chars") or {}).get("median", 0) < RULES["min_median_chars"]:
        problems.append(f"样本中位长度 {(report['chars']['median'])} 字，偏短")
    if (report.get("pii") or {}).get("hits", 0) > RULES["max_pii_hits"]:
        problems.append(f"个人信息命中 {report['pii']['hits']} 处，需要先脱敏")
    return problems
