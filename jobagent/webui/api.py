"""管理面板的数据与动作实现。每个动作都真的干活，失败原因原样返回。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..analyze import analyze_job, load_grades
from ..collect import collect, mark_duplicates
from ..config import ROOT, load as load_config
from ..llm import LLM
from ..materials import build_all
from ..profile import Profile
from ..report import write_report
from ..resume_service import build_resume_for_job, evidence_report
from ..safety import assert_confirmed
from ..scheduler import control as scheduler_control
from ..scheduler import status as scheduler_status
from ..sources import Filters, describe_all, get as get_source
from ..sources.manual import ManualSource
from ..store import Store, loads
from ..tools import registry
from ..tools.catalog import KINDS, STATES, Catalog
from ..tools.discover import discover
from ..tools.evaluate import evaluate

APPLICATION_STATUS_LABELS = {
    "to_apply": "待投递", "applied": "已投递", "chatting": "沟通中",
    "interview": "面试中", "offer": "已录用", "rejected": "已拒绝", "dropped": "已放弃",
}


def _open(store_path: Any) -> Store:
    return Store(Path(store_path))


def overview(store_path: Any) -> dict[str, Any]:
    store = _open(store_path)
    catalog = Catalog(store.conn)
    registry.sync_catalog(catalog)
    stats = store.stats()
    tool_stats = catalog.stats()
    llm = LLM()
    ok, detail = llm.available()
    data = {
        "ok": True,
        "stats": stats,
        "tools": tool_stats,
        "status_labels": APPLICATION_STATUS_LABELS,
        "llm": {"available": ok, "detail": detail},
        "scheduler": scheduler_status(),
        "sources": describe_all(),
        "config": {
            "keywords": load_config().search.keywords,
            "cities": load_config().search.cities,
        },
    }
    store.close()
    return data


def jobs(store_path: Any, query: dict[str, list[str]]) -> dict[str, Any]:
    store = _open(store_path)
    status = (query.get("status") or [None])[0] or None
    keyword = (query.get("q") or [""])[0].strip().lower()
    min_score = float((query.get("min_score") or [0])[0] or 0)
    rows = store.list_jobs(status=status, limit=300, order="score")
    out = []
    for job in rows:
        blob = f"{job['title']} {job['company']} {job['city']}".lower()
        if keyword and keyword not in blob:
            continue
        if job["score"] is not None and job["score"] < min_score:
            continue
        analysis = store.get_analysis(job["id"])
        out.append({
            "id": job["id"], "title": job["title"], "company": job["company"],
            "title_zh": job["title_zh"] if "title_zh" in job.keys() else "",
            "city": job["city"], "platform": job["platform"], "url": job["url"],
            "salary_text": job["salary_text"], "score": job["score"], "status": job["status"],
            "verdict": analysis["verdict"] if analysis else None,
            "fit_score": analysis["fit_score"] if analysis else None,
            "valid": job["valid"], "validity_note": job["validity_note"],
            "quality_flags": loads(job["quality_flags"], []) or [],
            "first_seen": job["first_seen"], "dup_of": job["dup_of"],
            "has_description": bool(job["description"]),
        })
    store.close()
    return {"ok": True, "jobs": out, "count": len(out),
            "status_labels": APPLICATION_STATUS_LABELS}


def job_detail(store_path: Any, job_id: int) -> dict[str, Any]:
    store = _open(store_path)
    job = store.get_job(job_id)
    if not job:
        store.close()
        return {"ok": False, "error": f"没有这个岗位 #{job_id}"}
    analysis = store.get_analysis(job_id)
    data = {
        "ok": True,
        "job": dict(job),
        "analysis": ({
            "verdict": analysis["verdict"], "fit_score": analysis["fit_score"],
            "engine": analysis["engine"], "created_at": analysis["created_at"],
            "requirements": loads(analysis["requirements"], []),
            "strengths": loads(analysis["strengths"], []),
            "risks": loads(analysis["risks"], []),
            "gaps": loads(analysis["gaps"], []),
            "questions": loads(analysis["questions"], []),
        } if analysis else None),
        "materials": [dict(row) for row in store.list_materials(job_id=job_id, limit=60)],
        "resumes": [dict(row) for row in store.list_resumes(job_id=job_id, limit=10)],
        "applications": [dict(row) for row in store.conn.execute(
            "SELECT * FROM applications WHERE job_id=?", (job_id,))],
        "status_labels": APPLICATION_STATUS_LABELS,
    }
    store.close()
    return data


def applications(store_path: Any) -> dict[str, Any]:
    store = _open(store_path)
    out = []
    for row in store.list_applications(limit=300):
        events = store.application_events(row["id"])
        out.append({**dict(row), "events": [dict(event) for event in events]})
    store.close()
    return {"ok": True, "applications": out, "labels": APPLICATION_STATUS_LABELS}


def materials(store_path: Any, query: dict[str, list[str]]) -> dict[str, Any]:
    store = _open(store_path)
    job_id = (query.get("job_id") or [None])[0]
    rows = store.list_materials(job_id=int(job_id) if job_id else None, limit=200)
    store.close()
    return {"ok": True, "materials": [dict(row) for row in rows]}


def resumes(store_path: Any) -> dict[str, Any]:
    store = _open(store_path)
    out = []
    for row in store.list_resumes(limit=100):
        checks = loads(row["checks"], {}) or {}
        out.append({
            "id": row["id"], "direction": row["direction"], "variant": row["variant"],
            "job_id": row["job_id"], "created_at": row["created_at"],
            "docx_path": row["docx_path"], "pdf_path": row["pdf_path"], "md_path": row["md_path"],
            "score": checks.get("score"), "issues": checks.get("issues", []),
            "recruiter_view": checks.get("recruiter_view", []),
            "layout": checks.get("layout", {}), "fixes": checks.get("fixes", []),
        })
    store.close()
    return {"ok": True, "resumes": out}


def tools(store_path: Any) -> dict[str, Any]:
    store = _open(store_path)
    catalog = Catalog(store.conn)
    registry.sync_catalog(catalog)
    data = {
        "ok": True,
        "tools": [tool.as_dict() for tool in catalog.list()],
        "stats": catalog.stats(),
        "states": STATES,
        "kinds": list(KINDS),
    }
    store.close()
    return data


def events(store_path: Any) -> dict[str, Any]:
    store = _open(store_path)
    rows = [dict(row) for row in store.recent_events(limit=80)]
    store.close()
    return {"ok": True, "events": rows}


def tool_events(store_path: Any, query: dict[str, list[str]]) -> dict[str, Any]:
    store = _open(store_path)
    catalog = Catalog(store.conn)
    name = (query.get("name") or [None])[0]
    limit = int((query.get("limit") or [40])[0])
    rows = [dict(row) for row in catalog.events(name, limit=limit)]
    store.close()
    return {"ok": True, "events": rows}


def sources() -> dict[str, Any]:
    return {"ok": True, "sources": describe_all()}


def evidence() -> dict[str, Any]:
    return {"ok": True, "report": evidence_report()}


def llm_status() -> dict[str, Any]:
    llm = LLM()
    ok, detail = llm.available()
    return {"ok": True, "available": ok, "detail": detail, "model": llm.model}


# ---------------- 动作 ----------------

def scan(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    keywords = payload.get("keywords") or config.search.keywords
    cities = payload.get("cities") or config.search.cities
    names = payload.get("sources") or config.sources_enabled or ["tencent", "remoteok"]
    filters = Filters(
        keywords=list(keywords), cities=list(cities),
        limit=int(payload.get("limit") or 25), pages=int(payload.get("pages") or 1),
        exclude_keywords=list(config.search.exclude_keywords),
    )
    store = _open(store_path)
    source_objects = [get_source(name) for name in names if name != "manual"]
    report = collect(store, source_objects, filters)
    store.close()
    return {"ok": True, "report": report.as_dict()}


def manual_import(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    text = payload.get("text") or ""
    if not text.strip():
        return {"ok": False, "error": "没有可导入的内容"}
    config = load_config()
    source = ManualSource(texts=[text])
    result = source.fetch(Filters(limit=5, keywords=list(config.search.keywords)))
    if not result.jobs:
        return {"ok": False, "error": result.error or "没解析出岗位，请至少写一行「职位：xxx」"}
    store = _open(store_path)
    new, updated = store.upsert_jobs("manual", result.jobs)
    store.log_event("import", "manual", f"手工导入：新增 {new}，更新 {updated}")
    store.close()
    return {"ok": True, "new": new, "updated": updated,
            "jobs": [{"title": job["title"], "company": job["company"]} for job in result.jobs]}


def analyze(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    store = _open(store_path)
    profile = Profile.load(ROOT / "profile.json")
    grades = load_grades()
    job_id = payload.get("job_id")
    limit = int(payload.get("limit") or 20)
    if job_id:
        targets = [store.get_job(int(job_id))]
    else:
        targets = store.list_jobs(status="new", limit=limit) or store.list_jobs(limit=limit)
    done = []
    for job in targets:
        if not job:
            continue
        analysis = analyze_job(job, profile=profile, grades=grades)
        store.save_analysis(job["id"], analysis)
        store.save_score(job["id"], analysis["fit_score"],
                         analysis["strengths"], analysis["gaps"])
        done.append({"job_id": job["id"], "title": job["title"],
                     "verdict": analysis["verdict"], "score": analysis["fit_score"]})
    store.close()
    return {"ok": True, "analyzed": done}


def dedupe(store_path: Any) -> dict[str, Any]:
    store = _open(store_path)
    found = mark_duplicates(store)
    store.close()
    return {"ok": True, "duplicates": [{"dup": a, "keep": b, "score": round(c, 1)}
                                       for a, b, c in found]}


def make_materials(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    job_id = payload.get("job_id")
    if not job_id:
        return {"ok": False, "error": "需要 job_id"}
    store = _open(store_path)
    job = store.get_job(int(job_id))
    if not job:
        store.close()
        return {"ok": False, "error": f"没有这个岗位 #{job_id}"}
    row = store.get_analysis(int(job_id))
    if not row:
        analysis = analyze_job(job)
        store.save_analysis(job["id"], analysis)
    else:
        requirements = loads(row["requirements"], [])
        analysis = {
            "verdict": row["verdict"], "fit_score": row["fit_score"], "engine": row["engine"],
            "requirements": requirements,
            "strengths": loads(row["strengths"], []),
            "risks": loads(row["risks"], []),
            "gaps": loads(row["gaps"], []),
            "questions": loads(row["questions"], []),
            "missing": [item for item in requirements if item.get("grade") in ("C", "D")],
            "checks": {"direction": {"value": ""}},
        }
    use_llm = bool(payload.get("use_llm"))
    bundle = build_all(job, analysis, llm=LLM() if use_llm else None)
    engine = "llm+rule" if bundle["quality"]["polished_by_model"] else "rule"
    store.add_material(job_id=int(job_id), kind="self_intro", title="自我介绍",
                       text=bundle["self_intro"], engine=engine)
    store.add_material(job_id=int(job_id), kind="cover_letter", title="求职信",
                       text=bundle["cover_letter"], engine=engine)
    for index, item in enumerate(bundle["chat_messages"]):
        store.add_material(job_id=int(job_id), kind="chat", title=item["title"],
                           text=item["text"], meta={"index": index})
    store.add_material(job_id=int(job_id), kind="interview", title="面试提纲",
                       text="\n\n".join(f"问：{item['question']}\n答：\n{item['outline']}"
                                        for item in bundle["interview"]))
    store.add_material(job_id=int(job_id), kind="gap_plan", title="技能缺口与补法",
                       text="\n".join(f"- {item['gap']} → {item['action']}"
                                      for item in bundle["gap_plan"]))
    store.close()
    return {"ok": True, "engine": engine,
            "materials": {key: value for key, value in bundle.items() if key != "quality"},
            "quality": bundle["quality"]}


def make_resume(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    result = build_resume_for_job(job_id=payload.get("job_id"),
                                  direction=payload.get("direction"),
                                  db_path=store_path)
    return {"ok": True, "result": result}


def translate(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from ..translate import translate_jobs

    store = _open(store_path)
    report = translate_jobs(store, limit=int(payload.get("limit") or 5),
                            only_missing=not payload.get("force"),
                            job_id=payload.get("job_id"))
    store.close()
    return {"ok": True, "report": report.as_dict()}


def set_application(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    job_id = payload.get("job_id")
    status = payload.get("status") or "to_apply"
    if not job_id:
        return {"ok": False, "error": "需要 job_id"}
    if status not in APPLICATION_STATUS_LABELS:
        return {"ok": False, "error": f"未知状态：{status}"}
    store = _open(store_path)
    assert_confirmed(store, "submit_application", confirmed=bool(payload.get("confirm")),
                     detail=f"岗位 #{job_id} 状态改为 {APPLICATION_STATUS_LABELS[status]}",
                     subject=f"application:{job_id}")
    app_id = store.set_application_status(
        int(job_id), status, note=payload.get("note"),
        resume_id=payload.get("resume_id"), channel=payload.get("channel"),
        contact=payload.get("contact"))
    store.close()
    return {"ok": True, "application_id": app_id, "status": status,
            "status_label": APPLICATION_STATUS_LABELS[status]}


def set_job_status(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    job_id = payload.get("job_id")
    status = payload.get("status")
    if not job_id or not status:
        return {"ok": False, "error": "需要 job_id 和 status"}
    store = _open(store_path)
    store.set_status(int(job_id), str(status))
    store.log_event("job_status", f"#{job_id}", str(status))
    store.close()
    return {"ok": True}


def report(store_path: Any) -> dict[str, Any]:
    return {"ok": True, **write_report(store_path)}


def verify_tools(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    store = _open(store_path)
    catalog = Catalog(store.conn)
    registry.sync_catalog(catalog)
    if payload.get("all"):
        result = registry.verify_all(catalog)
    elif payload.get("name"):
        result = registry.verify(str(payload["name"]), catalog)
    else:
        store.close()
        return {"ok": False, "error": "需要 name 或 all=true"}
    store.close()
    return {"ok": True, "result": result}


def tool_discover(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    store = _open(store_path)
    catalog = Catalog(store.conn)
    result = discover(catalog, queries=payload.get("queries"),
                      per_page=int(payload.get("per_page") or 4))
    store.close()
    return {"ok": True, "result": result}


def tool_evaluate(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    name = payload.get("name")
    if not name:
        return {"ok": False, "error": "需要 name（owner/repo）"}
    store = _open(store_path)
    catalog = Catalog(store.conn)
    result = evaluate(catalog, str(name))
    store.close()
    return {"ok": True, "result": result.as_dict()}


def schedule(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action") or "status"
    extra = {key: payload[key] for key in ("interval_minutes", "keywords", "cities")
             if key in payload}
    return {"ok": True, "result": scheduler_control(action, **extra)}


def _send_guard(store_path: Any, payload: dict[str, Any]) -> dict[str, Any]:
    store = _open(store_path)
    assert_confirmed(store, "send_message", confirmed=bool(payload.get("confirm")),
                     detail=f"岗位 #{payload.get('job_id')}", subject="send_message")
    store.close()
    return {"ok": False, "error": "确认已记账，但自动发送没有启用：发送要在 BOSS 登录态的调试"
                                  "浏览器里逐条执行（python -m jobagent send），"
                                  "确认后再发，避免误发或触发风控"}


ROUTES = {
    "/api/scan": scan,
    "/api/import": manual_import,
    "/api/analyze": analyze,
    "/api/dedupe": lambda store_path, payload: dedupe(store_path),
    "/api/materials": make_materials,
    "/api/resume": make_resume,
    "/api/translate": translate,
    "/api/application": set_application,
    "/api/job/status": set_job_status,
    "/api/report": lambda store_path, payload: report(store_path),
    "/api/tools/verify": verify_tools,
    "/api/tools/discover": tool_discover,
    "/api/tools/evaluate": tool_evaluate,
    "/api/scheduler": schedule,
    "/api/schedule/run": lambda store_path, payload: {
        "ok": True, "result": scheduler_control("run")},
    "/api/send": _send_guard,
}


def action(store_path: Any, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if path not in ROUTES:
        raise KeyError(path)
    return ROUTES[path](store_path, payload)
