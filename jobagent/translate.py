"""把英文 JD 翻成中文，缓存进库。

只用已经配置好的大模型接口（默认复用本机 Codex 的 DeepSeek）。
翻译结果标成"机翻"，和原文分开存，原文永远保留。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .llm import LLM
from .store import Store

ENGLISH_HINT = re.compile(r"[A-Za-z]{3,}")
FRAGMENTS = re.compile(r"(https?://\S+|www\.\S+|\S+@\S+)")


def looks_english(text: str) -> bool:
    """粗略判断这段是不是英文：中文字数占比很低、且英文字母足够多。"""
    if not text:
        return False
    sample = text[:4000]
    cjk = len(re.findall(r"[\u4e00-\u9fff]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    return latin > 200 and cjk < latin * 0.15


def title_needs_translation(title: str) -> bool:
    """标题的判断标准和正文不同：没有中文、且有 3 个以上连续字母就翻。"""
    title = title or ""
    return bool(re.search(r"[A-Za-z]{3,}", title)) and not re.search(r"[\u4e00-\u9fff]", title)


@dataclass
class TranslateReport:
    scanned: int = 0
    translated: int = 0
    skipped: int = 0
    failed: list[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"scanned": self.scanned, "translated": self.translated,
                "skipped": self.skipped, "failed": self.failed or []}


def translate_text(llm: LLM, text: str) -> tuple[bool, str, str]:
    """返回 (是否成功, 译文或错误信息, 模型名)。"""
    body = FRAGMENTS.sub(" ", text)[:6000]
    prompt = (
        "把下面这段招聘信息翻译成简体中文。要求：\n"
        "1. 只输出译文，不要加解释、不要总结、不要重复原文；\n"
        "2. 技术名词保留通用写法（例如 Python、ES、BM25、RRF、FastAPI 可以留原文）；\n"
        "3. 数字、薪资、地名、公司名保持原样；\n"
        "4. 分点的地方保留分点。\n\n" + body
    )
    result = llm.chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=2000)
    if not result.ok:
        return False, result.error, result.model
    return True, result.text.strip(), result.model


def translate_title(llm: LLM, title: str) -> str:
    """岗位标题也翻一下：中英混排的标题保留技术名词。"""
    if not title or not title_needs_translation(title):
        return ""
    prompt = (
        "把下面这个招聘岗位标题翻译成简体中文，只输出译文，不要引号、不要解释。"
        "技术名词和公司名保留原文，例如 Python、AI、Data Engineer 可以保留。\n\n" + title
    )
    result = llm.chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=80)
    if not result.ok:
        return ""
    cleaned = result.text.strip().strip('"“”').split("\n")[0]
    return cleaned[:80]


def translate_jobs(store: Store, *, limit: int = 20, only_missing: bool = True,
                   job_id: int | None = None, llm: LLM | None = None,
                   titles_only: bool = False) -> TranslateReport:
    report = TranslateReport(failed=[])
    engine = llm or LLM()
    ok, detail = engine.available()
    if not ok:
        report.failed.append("模型不可用：" + detail)
        return report

    if job_id:
        rows = [store.get_job(job_id)]
    else:
        rows = list(store.conn.execute(
            "SELECT * FROM jobs WHERE description IS NOT NULL AND description <> '' "
            "ORDER BY id DESC"))
    for row in rows:
        if not row:
            continue
        if limit and report.scanned >= limit:
            break
        if titles_only:
            if not title_needs_translation(str(row["title"] or "")):
                report.skipped += 1
                continue
            if row["title_zh"] and only_missing:
                report.skipped += 1
                continue
            report.scanned += 1
            title_zh = translate_title(engine, str(row["title"]))
            if not title_zh:
                report.failed.append(f"#{row['id']} {str(row['title'])[:24]}：标题翻译失败")
                continue
            store.conn.execute("UPDATE jobs SET title_zh=? WHERE id=?", (title_zh, row["id"]))
            store.conn.commit()
            report.translated += 1
            continue
        description = row["description"] or ""
        existing = row["description_zh"] if "description_zh" in row.keys() else None
        if existing and only_missing:
            report.skipped += 1
            continue
        if not looks_english(description):
            report.skipped += 1
            continue
        report.scanned += 1
        success, payload, model = translate_text(engine, description)
        if not success:
            report.failed.append(f"#{row['id']} {str(row['title'])[:20]}：{payload}")
            continue
        title_zh = ""
        if title_needs_translation(str(row["title"] or "")):
            title_zh = translate_title(engine, str(row["title"]))
        store.conn.execute(
            "UPDATE jobs SET description_zh=?, translated_at=?, "
            "title_zh=COALESCE(NULLIF(?, ''), title_zh) WHERE id=?",
            (payload, __import__("time").strftime("%Y-%m-%d %H:%M:%S"), title_zh, row["id"]))
        store.conn.commit()
        report.translated += 1
    store.log_event("translate", "jobs", f"翻译 {report.translated} 条英文 JD（模型 {engine.model}）")
    return report
