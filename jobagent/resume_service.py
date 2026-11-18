"""简历生成服务：把事实源、证据核查、方向裁剪、渲染和自检串成一条命令。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import ROOT, load as load_config
from .evidence import REPORT_PATH, build_report
from .resume import ResumeDoc, autofix, build_doc, render_and_check
from .resume.tailor import load_master
from .store import Store

OUT_DIR = ROOT / "output" / "resume"


def evidence_report(*, refresh: bool = False) -> dict[str, Any]:
    if refresh or not REPORT_PATH.exists():
        return build_report()
    try:
        return json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return build_report()


def _stem(doc: ResumeDoc, job: Any | None) -> str:
    name = doc.basics.get("name", "resume")
    if job is not None:
        company = re.sub(r"[^\w\u4e00-\u9fff]", "", str(job["company"] or "公司"))[:10]
        title = re.sub(r"[^\w\u4e00-\u9fff]", "", str(job["title"] or "岗位"))[:16]
        return f"{name}_{doc.direction}_{company}_{title}_j{job['id']}"
    return f"{name}_{doc.direction}"


def build_resume_for_job(
    *,
    job_id: int | None = None,
    direction: str | None = None,
    job_title: str | None = None,
    job_company: str | None = None,
    out_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    max_rounds: int = 2,
) -> dict[str, Any]:
    config = load_config()
    store = Store(db_path or config.db_path)
    job = store.get_job(int(job_id)) if job_id else None
    # 岗位不在库里（比如对方在聊天里直接要简历）时，用岗位名生成"只按方向定制"的版本，
    # 这样文件标题仍然写着应聘岗位，不必先把岗位塞进库里
    pseudo_job = None
    if job is None and job_title:
        pseudo_job = {"id": 0, "title": job_title, "company": job_company or "",
                      "url": "", "city": "", "description": "", "tags": []}
    report = evidence_report()
    master = load_master()

    analysis = None
    if job_id:
        row = store.get_analysis(int(job_id))
        if row:
            import json as _json

            analysis = {
                "verdict": row["verdict"],
                "fit_score": row["fit_score"],
                "requirements": _json.loads(row["requirements"] or "[]"),
            }
    target_job = dict(job) if job else pseudo_job
    doc = build_doc(master, report=report, job=target_job,
                    analysis=analysis, direction=direction)
    target_dir = Path(out_dir) if out_dir else OUT_DIR
    stem = _stem(doc, job or pseudo_job)
    job_dict = dict(job) if job else pseudo_job

    checks = render_and_check(doc, target_dir, stem=stem, report=report, job=job_dict)
    rounds = 0
    fixes: list[str] = []
    while rounds < max_rounds:
        blocking = [issue for issue in checks["issues"]
                    if issue["kind"] in ("bullet_too_long", "length", "duplicate",
                                         "too_many_projects", "unverified_number",
                                         "too_many_pages")]
        if not blocking:
            break
        for issue in blocking:
            if issue["kind"] == "unverified_number":
                prefix = issue["where"][:20]
                for project in doc.projects:
                    before = len(project["bullets"])
                    project["bullets"] = [b for b in project["bullets"] if not b.startswith(prefix)]
                    if len(project["bullets"]) != before:
                        fixes.append(f"删掉缺出处的条目：{prefix}…")
        fixes.extend(autofix(doc, blocking))
        rounds += 1
        checks = render_and_check(doc, target_dir, stem=stem, report=report, job=job_dict)
    checks["fixes"] = fixes
    checks["note"] = ("渲染依赖缺失，只生成了 Markdown" if "docx" not in checks["paths"]
                      or "pdf" not in checks["paths"] else "已生成 DOCX 与 PDF")

    resume_id = store.add_resume(
        direction=doc.direction,
        variant="master" if not job else "tailored",
        job_id=int(job_id) if job_id else None,
        docx_path=checks["paths"].get("docx"),
        pdf_path=checks["paths"].get("pdf"),
        md_path=checks["paths"].get("md"),
        content=doc.as_dict(),
        checks={key: value for key, value in checks.items() if key != "paths"},
    )
    store.log_event("resume", f"#{resume_id}",
                    f"{doc.direction}；分 {checks.get('score')}；"
                    f"{checks['paths'].get('pdf') or checks['paths'].get('md')}")
    store.close()

    return {
        "resume_id": resume_id,
        "direction": doc.direction,
        "job_id": job_id,
        "score": checks.get("score"),
        "paths": checks["paths"],
        "issues": checks["issues"],
        "recruiter_view": checks.get("recruiter_view", []),
        "jd_coverage": checks.get("jd_coverage", {}),
        "layout": checks.get("layout", {}),
        "warnings": checks.get("warnings", []),
        "fixes": fixes,
        "stats": checks.get("stats", {}),
        "used_claims": doc.used_claims,
    }
