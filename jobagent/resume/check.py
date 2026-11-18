"""简历质量闸门：事实、语言、针对性、篇幅、排版五类检查。

检查规则是显式的，命中的位置和原因都写出来，方便你决定改不改。
需要在生成后立刻复用，所以这里同时负责"渲染 + 检查"的编排。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .render import RenderError, render_docx, render_markdown, render_pdf
from .tailor import ResumeDoc
from ..textquality import TELLS as AI_TELLS

NUMBER_WITH_UNIT = re.compile(r"\d[\d.,]*\s*(?:个|条|项|%|天|页|倍|万|份|次|家|人|小时)")

WEAK_OPENERS = ("负责", "参与", "配合", "协助", "帮助")
ACTION_HINTS = ("自己写", "自己搭", "把", "做了", "接了", "跑了", "改成", "写出", "定位", "拆成",
                "建了", "独立", "实测", "接入了", "换成", "逐条", "判重", "对上")


def _all_bullets(doc: ResumeDoc) -> list[str]:
    return [bullet for project in doc.projects for bullet in project["bullets"]]


def _all_texts(doc: ResumeDoc) -> list[str]:
    """所有面向读者的条目，含"成果"行。"""
    texts = list(_all_bullets(doc))
    texts += [project.get("result", "") for project in doc.projects if project.get("result")]
    texts += [project.get("tagline", "") for project in doc.projects if project.get("tagline")]
    return texts


def _claim_text(report: dict[str, Any] | None) -> str:
    if not report:
        return ""
    usable = [claim for claim in report.get("claims", [])
              if claim.get("verified_grade") in ("A", "B")]
    return json.dumps(usable, ensure_ascii=False)


def _similarity(left: str, right: str) -> float:
    try:
        from rapidfuzz import fuzz

        return float(fuzz.token_set_ratio(left, right))
    except Exception:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, left, right).ratio() * 100


def run_checks(doc: ResumeDoc, *, report: dict[str, Any] | None = None,
               job: dict[str, Any] | None = None) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    job = job or {}
    bullets = _all_texts(doc)
    claim_text = _claim_text(report)

    # 1. 事实：数字必须有出处
    for bullet in bullets:
        for match in NUMBER_WITH_UNIT.finditer(bullet):
            token = match.group(0).strip()
            digits = re.sub(r"[^\d.]", "", token)
            # 有出处 = 数字作为独立数字出现在已核查的证据里（不会误匹配 1791802**99**78 这种长串）
            has_source = token in claim_text
            if not has_source and digits:
                has_source = bool(re.search(rf"(?<![\d.]){re.escape(digits)}(?![\d])", claim_text))
            if not has_source:
                issues.append({
                    "kind": "unverified_number", "severity": "high",
                    "where": bullet[:40],
                    "detail": f"数字「{token}」在已核查的证据里找不到出处，要么补证据要么删掉",
                })
    grades = {claim.get("id"): claim.get("verified_grade", "D")
              for claim in (report or {}).get("claims", [])}
    confirmed = {claim.get("id") for claim in (report or {}).get("claims", [])
                 if claim.get("user_confirmed")}
    for claim_id in doc.used_claims:
        if grades.get(claim_id) == "D":
            if claim_id in confirmed:
                issues.append({
                    "kind": "self_reported", "severity": "medium", "where": claim_id,
                    "detail": "这条是你本人确认的自述（本机没有实物证据），已按你的口径写入；"
                              "面试时会被追问实现细节，保证能讲清再投",
                })
                continue
            issues.append({
                "kind": "weak_claim", "severity": "high", "where": claim_id,
                "detail": f"断言 {claim_id} 目前是 D 级（证据不足），不该出现在成品简历里",
            })

    # 2. 语言：机器腔与自我评价
    for label, pattern, suggestion in AI_TELLS:
        for bullet in bullets + [doc.summary]:
            for match in re.finditer(pattern, bullet or ""):
                issues.append({
                    "kind": "ai_tell", "severity": "medium",
                    "where": match.group(0)[:30],
                    "detail": f"{label}：{suggestion}",
                })

    # 3. 重复：条目之间相似度过高
    for index, left in enumerate(bullets):
        for right in bullets[index + 1:]:
            score = _similarity(left, right)
            if score >= 85:
                issues.append({
                    "kind": "duplicate", "severity": "medium",
                    "where": left[:26],
                    "detail": f"和另一条重复度 {score:.0f}%，留信息量大的那条",
                })

    # 4. 篇幅
    total_chars = sum(len(bullet) for bullet in bullets)
    if total_chars > 1400:
        issues.append({"kind": "length", "severity": "medium", "where": "全文",
                       "detail": f"条目正文 {total_chars} 字，一页会挤，建议压到 1400 字以内"})
    for bullet in bullets:
        if len(bullet) > 150:
            issues.append({"kind": "bullet_too_long", "severity": "medium",
                           "where": bullet[:26], "detail": f"单条 {len(bullet)} 字，读起来费劲"})
    if len(doc.projects) > 4:
        issues.append({"kind": "too_many_projects", "severity": "medium", "where": "项目数",
                       "detail": f"{len(doc.projects)} 个项目太散，挑最相关的几个"})
    for project in doc.projects:
        if len(project["bullets"]) > 5:
            issues.append({"kind": "too_many_bullets", "severity": "low",
                           "where": project["name"],
                           "detail": f"{len(project['bullets'])} 条，建议不超过 5 条"})

    # 5. 针对性：目标岗位的核心要求有没有被回应
    coverage: dict[str, Any] = {}
    if job:
        keywords = _job_keywords(job)
        resume_text = " ".join(bullets + [doc.summary] + [group["items"] for group in doc.skill_groups])
        hit = [word for word in keywords if word and word in resume_text]
        missed = [word for word in keywords if word and word not in resume_text]
        coverage = {
            "keywords": keywords, "hit": hit, "missed": missed,
            "ratio": round(len(hit) / len(keywords), 2) if keywords else 0.0,
        }
        if keywords and coverage["ratio"] < 0.35:
            issues.append({
                "kind": "jd_gap", "severity": "medium", "where": job.get("title", ""),
                "detail": "岗位要求里能对上的关键词太少：" + "、".join(missed[:6]),
            })

    # 6. 可读性：条目开头是不是具体动作
    for bullet in bullets:
        if bullet.startswith(WEAK_OPENERS) and not any(hint in bullet[:16] for hint in ACTION_HINTS):
            issues.append({"kind": "weak_verb", "severity": "low", "where": bullet[:26],
                           "detail": "以「负责/参与」这类词开头，看不出你具体做了什么"})

    severity_weight = {"high": 12, "medium": 5, "low": 2}
    score = max(0, 100 - sum(severity_weight.get(issue["severity"], 3) for issue in issues))
    return {
        "score": score,
        "issues": issues,
        "stats": {
            "projects": len(doc.projects),
            "bullets": len(bullets),
            "bullet_chars": total_chars,
            "direction": doc.direction,
        },
        "jd_coverage": coverage,
    }


def _job_keywords(job: dict[str, Any]) -> list[str]:
    """从岗位文本里挑出能力关键词。优先用档案里的能力词表，避免凭空猜。"""
    keywords: list[str] = []
    try:
        from ..config import ROOT
        from ..profile import Profile

        profile = Profile.load(ROOT / "profile.json")
        vocabulary = [kw for cap in profile.capabilities for kw in cap.keywords]
    except Exception:
        vocabulary = ["python", "sqlite", "接口", "自动化", "agent", "大模型", "测试"]
    text = " ".join(str(job.get(key) or "") for key in ("title", "description", "tags")).lower()
    for word in vocabulary:
        if word and word.lower() in text:
            keywords.append(word)
    return list(dict.fromkeys(keywords))[:12]


def render_and_check(
    doc: ResumeDoc,
    out_dir: str | Path,
    *,
    stem: str,
    report: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """渲染三个格式并做检查。缺依赖时降级为 Markdown，并说明原因。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    warnings: list[str] = []
    paths["md"] = render_markdown(doc, out_dir / f"{stem}.md")
    try:
        paths["docx"] = render_docx(doc, out_dir / f"{stem}.docx")
    except RenderError as exc:
        warnings.append(str(exc))
    try:
        paths["pdf"] = render_pdf(doc, out_dir / f"{stem}.pdf")
    except RenderError as exc:
        warnings.append(str(exc))

    checks = run_checks(doc, report=report, job=job)
    layout = _inspect_pdf(paths.get("pdf"), bullets=_all_texts(doc))
    checks["layout"] = layout
    checks["issues"].extend(layout.get("issues", []))
    checks["warnings"] = warnings
    checks["paths"] = paths
    checks["recruiter_view"] = recruiter_review(doc, job, checks)
    return checks


def _inspect_pdf(path: str | None, *, bullets: list[str]) -> dict[str, Any]:
    """排版检查：页数、有没有文字被截断（拿末条文字的结尾去 PDF 里找）。"""
    if not path:
        return {"pages": None, "issues": [], "note": "没生成 PDF（缺 reportlab），未能检查排版"}
    result: dict[str, Any] = {"pages": None, "issues": [], "note": ""}
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        result["pages"] = len(reader.pages)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        squeezed = re.sub(r"\s+", "", text)
        missing = []
        for bullet in bullets:
            tail = re.sub(r"\s+", "", bullet)[-12:]
            if tail and tail not in squeezed:
                missing.append(bullet[:24])
        if missing:
            result["issues"].append({
                "kind": "truncated", "severity": "high", "where": "、".join(missing[:3]),
                "detail": "PDF 里找不到这些条目的结尾，可能是分页被截断或字体没嵌进去",
            })
        if result["pages"] and result["pages"] > 1:
            result["issues"].append({
                "kind": "too_many_pages", "severity": "medium",
                "where": f"{result['pages']} 页",
                "detail": "招聘方一般只看一页，删掉一条或压缩项目数",
            })
    except ImportError:
        result["note"] = "没装 pypdf，跳过 PDF 文本校验"
    except Exception as exc:
        result["note"] = f"PDF 检查失败：{type(exc).__name__}: {exc}"
    return result


def recruiter_review(doc: ResumeDoc, job: dict[str, Any] | None,
                     checks: dict[str, Any]) -> list[str]:
    """以初筛视角找最可能被淘汰的问题。"""
    notes: list[str] = []
    job = job or {}
    bullets = _all_bullets(doc)
    if not bullets:
        return ["没有任何项目条目，初筛过不去"]
    first_project = doc.projects[0] if doc.projects else None
    if first_project and first_project.get("grade") == "B":
        notes.append("第一屏最强的项目只有 B 级证据，容易被追问细节；把 A 级项目提前更稳")
    if job.get("title"):
        title_words = [w for w in re.split(r"[\s/、（）()·\-—]+", str(job["title"]))
                       if len(w) >= 2 and not re.fullmatch(r"[^\w\u4e00-\u9fff]+", w)]
        # 中文标题常常没有分隔符（"电商客服AI训练主管"整串是一个词），
        # 整串找不到就把长词拆成 2–4 字的子词再找一次，避免误报"没回应标题关键词"
        extra: list[str] = []
        for word in title_words:
            if len(word) <= 4:
                continue
            for size in (2, 3, 4):
                extra.extend(word[i:i + size] for i in range(0, len(word) - size + 1))
        title_words = title_words + [w for w in extra if len(w) >= 2]
        resume_text = " ".join(bullets + [doc.summary])
        if title_words and not any(word in resume_text for word in title_words):
            notes.append(f"简历里没有回应岗位标题「{job['title']}」里的关键词，初筛可能直接被跳过")
    if not any(re.search(r"\d", bullet) for bullet in bullets):
        notes.append("条目里没有任何数字或规模描述，读起来像职责说明而不是成果")
    if checks.get("stats", {}).get("bullet_chars", 0) > 1200:
        notes.append("单页信息量偏大，招聘方扫读时抓不到重点")
    if not doc.summary:
        notes.append("缺少开头概述，招聘方需要自己总结你是谁")
    return notes
