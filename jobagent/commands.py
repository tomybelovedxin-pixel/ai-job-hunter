"""新增子命令的实现。cli.py 只负责解析参数，具体动作放在这里。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .analyze import analyze_job, analyze_text, load_grades
from .collect import collect, mark_duplicates
from .config import ROOT, load as load_config
from .evidence import build_report, usable_claims
from .llm import LLM
from .materials import build_all
from .profile import Profile
from .report import write_report
from .resume_service import build_resume_for_job, evidence_report
from .scheduler import control as scheduler_control
from .scheduler import logs as scheduler_logs
from .sources import Filters, SourceError, describe_all, get as get_source
from .sources.manual import ManualSource
from .store import Store, loads
from .tools import registry
from .tools.catalog import STATES, Catalog
from .tools.discover import discover
from .tools.evaluate import evaluate
from .tools.planner import plan as plan_task
from .tools.router import route


def _store(args: Any = None) -> Store:
    return Store(_config(args).db_path)


def _config(args: Any = None):
    custom = getattr(args, "config", None) if args is not None else None
    return load_config(Path(custom)) if custom else load_config()


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


# ---------------- 数据源与岗位 ----------------

def cmd_sources(args: Any) -> int:
    for item in describe_all():
        flags = []
        if item["requires_login"]:
            flags.append("需登录")
        if item["city_scoped"]:
            flags.append("支持城市过滤")
        print(f"{item['name']:10s} {item['label']}")
        print(f"           用途：{item['notes']}")
        print(f"           主页：{item['homepage']}　{'｜'.join(flags) or '公开取数'}")
    return 0


def cmd_fetch(args: Any) -> int:
    config = _config(args)
    store = Store(config.db_path)
    keywords = args.keywords or config.search.keywords
    cities = args.cities or config.search.cities
    names = args.sources or config.sources_enabled or ["tencent", "remoteok"]
    filters = Filters(keywords=list(keywords), cities=list(cities),
                      limit=args.limit, pages=args.pages,
                      exclude_keywords=list(config.search.exclude_keywords))
    sources = [get_source(name) for name in names if name != "manual"]
    report = collect(store, sources, filters)
    for item in report.sources:
        state = f"失败：{item.error}" if item.error else f"取到 {item.fetched}"
        print(f"{item.source:10s} {state}｜新增 {item.new}｜更新 {item.updated}"
              f"｜可疑标记 {item.flagged}｜{item.seconds:.1f}s")
    totals = report.totals
    print(f"合计：新增 {totals['new']}，更新 {totals['updated']}，"
          f"可疑 {totals['flagged']}，失败源 {totals['failed_sources']}")
    store.close()
    return 0


def cmd_import(args: Any) -> int:
    config = _config(args)
    store = Store(config.db_path)
    paths = [Path(p) for p in args.paths if p != "-"]
    texts = []
    if "-" in args.paths or not args.paths:
        print("从标准输入读取 JD 文本（Ctrl+Z 回车结束）：", file=sys.stderr)
        texts.append(sys.stdin.read())
    source = ManualSource(paths=paths, texts=texts)
    result = source.fetch(Filters(limit=50, keywords=list(config.search.keywords)))
    if not result.jobs:
        print(f"没有解析出岗位：{result.error or '内容里缺职位名称'}")
        store.close()
        return 1
    new, updated = store.upsert_jobs("manual", result.jobs)
    store.log_event("import", "manual", f"手工导入新增 {new} 更新 {updated}")
    for job in result.jobs:
        print(f"OK  {job['title']}｜{job['company'] or '未写公司'}｜{job['city'] or '未写城市'}")
    print(f"导入完成：新增 {new}，更新 {updated}")
    store.close()
    return 0


def cmd_dedupe(args: Any) -> int:
    store = _store(args)
    found = mark_duplicates(store, threshold=args.threshold)
    if not found:
        print("没有发现跨平台重复岗位")
    for drop_id, keep_id, score in found:
        row = store.get_job(drop_id)
        print(f"#{drop_id} 与 #{keep_id} 重复（{score:.0f}）：{row['title'] if row else ''}")
    store.close()
    return 0


def cmd_enrich(args: Any) -> int:
    from .enrich import enrich_tencent

    store = _store(args)
    report = enrich_tencent(store, limit=args.limit, missing_only=not args.all)
    print(f"扫描 {report.scanned} 条，补齐 {report.updated} 条，跳过 {report.skipped} 条")
    for error in report.errors:
        print("失败：" + error)
    store.close()
    return 0


def cmd_translate(args: Any) -> int:
    from .translate import translate_jobs

    store = _store(args)
    report = translate_jobs(store, limit=args.limit, only_missing=not args.all,
                            job_id=args.job, titles_only=getattr(args, "titles_only", False))
    print(f"扫描 {report.scanned} 条英文 JD，翻译 {report.translated} 条，跳过 {report.skipped} 条")
    for item in report.failed[:5]:
        print("失败：" + item)
    store.close()
    return 0


def cmd_analyze(args: Any) -> int:
    store = _store(args)
    profile = Profile.load(ROOT / "profile.json")
    grades = load_grades()
    if args.job:
        targets = [store.get_job(int(args.job))]
    elif getattr(args, "ids", None):
        ids = [int(x) for x in str(args.ids).replace("，", ",").split(",") if x.strip()]
        targets = [store.get_job(job_id) for job_id in ids]
    elif getattr(args, "all", False):
        targets = store.list_jobs(limit=args.limit)
    else:
        targets = store.list_jobs(status="new", limit=args.limit) or \
            store.list_jobs(limit=args.limit)
    # 只处理新信息：--skip-fresh-hours N 时，JD 没更新且分析没过期的岗位直接复用旧结论
    skip_fresh = float(getattr(args, "skip_fresh_hours", 0) or 0)
    reused: list[str] = []
    try:
        for job in targets:
            if not job:
                continue
            if skip_fresh:
                ok, why = store.analysis_reusable(job["id"], hours=skip_fresh)
                if ok:
                    reused.append(f"#{job['id']} {job['title']}：{why}")
                    continue
            analysis = analyze_job(job, profile=profile, grades=grades)
            if args.llm:
                analysis = _llm_enhance(job, analysis)
            store.save_analysis(job["id"], analysis)
            store.save_score(job["id"], analysis["fit_score"],
                             analysis["strengths"], analysis["gaps"])
            print(f"\n=== #{job['id']} {job['title']}｜{job['company'] or ''} ===")
            print(f"结论：{analysis['verdict']}（{analysis['fit_score']} 分）"
                  f"　依据：{analysis['verdict_reason']}")
            if analysis["job_problem"]:
                print("要解决什么：" + "；".join(analysis["job_problem"][:2])[:120])
            if analysis["strengths"]:
                print("对得上的：" + "；".join(analysis["strengths"][:3]))
            if analysis["gaps"]:
                print("缺口：" + "；".join(analysis["gaps"][:3]))
            if analysis["advice"]:
                print("建议：" + "；".join(analysis["advice"]))
    finally:
        store.close()
    if reused:
        print(f"\n复用了 {len(reused)} 条已有分析（JD 没变、结论没过期）：")
        for line in reused[:10]:
            print("  复用 " + line)
    return 0


def _llm_enhance(job: Any, analysis: dict[str, Any]) -> dict[str, Any]:
    """用大模型补一遍要求抽取，失败就保持规则结果。"""
    llm = LLM()
    ok, detail = llm.available()
    if not ok:
        analysis["engine"] = "rule"
        analysis.setdefault("risks", []).append(f"大模型不可用，已退回规则引擎：{detail}")
        return analysis
    result = llm.extract_requirements(job["description"] or "")
    if not result.ok:
        analysis["engine"] = "rule"
        analysis.setdefault("risks", []).append(f"大模型抽取失败，已退回规则引擎：{result.error}")
        return analysis
    extra = [line.strip("-• 　\t") for line in result.text.split("\n") if len(line.strip()) > 6]
    analysis["engine"] = f"rule+llm({result.model})"
    analysis["llm_requirements"] = extra[:20]
    return analysis


# ---------------- 简历与材料 ----------------

def cmd_resume(args: Any) -> int:
    result = build_resume_for_job(job_id=args.job, direction=args.direction,
                                  out_dir=args.out, db_path=_config(args).db_path)
    print(f"方向：{result['direction']}｜自检得分：{result['score']}")
    for kind, path in result["paths"].items():
        print(f"{kind.upper():5s} {path}")
    if result.get("layout", {}).get("pages"):
        print(f"页数：{result['layout']['pages']}")
    if result.get("fixes"):
        print("自动修正：" + "；".join(result["fixes"]))
    for warning in result.get("warnings", []):
        print("提示：" + warning)
    problems = [issue for issue in result["issues"]]
    if problems:
        print("自检发现（按严重度）：")
        for issue in problems:
            print(f"  [{issue['severity']}] {issue['kind']}｜{issue['where']}｜{issue['detail']}")
    for note in result.get("recruiter_view", []):
        print("初筛视角：" + note)
    return 0


def cmd_materials(args: Any) -> int:
    store = _store(args)
    job = store.get_job(int(args.job))
    if not job:
        print(f"没有这个岗位 #{args.job}")
        store.close()
        return 1
    row = store.get_analysis(int(args.job))
    if row:
        requirements = loads(row["requirements"], [])
        analysis = {
            "verdict": row["verdict"], "fit_score": row["fit_score"], "engine": row["engine"],
            "requirements": requirements,
            "strengths": loads(row["strengths"], []),
            "risks": loads(row["risks"], []),
            "gaps": loads(row["gaps"], []),
            "questions": loads(row["questions"], []),
            "missing": [item for item in requirements if item.get("grade") in ("C", "D")],
            "checks": {"direction": {"value": args.direction or ""}},
        }
    else:
        analysis = analyze_job(job, profile=Profile.load(ROOT / "profile.json"))
        store.save_analysis(job["id"], analysis)
    bundle = build_all(job, analysis, llm=LLM() if args.llm else None)
    engine = "llm+rule" if bundle["quality"]["polished_by_model"] else "rule"
    store.add_material(job_id=job["id"], kind="self_intro", title="自我介绍",
                       text=bundle["self_intro"], engine=engine)
    store.add_material(job_id=job["id"], kind="cover_letter", title="求职信",
                       text=bundle["cover_letter"], engine=engine)
    for index, item in enumerate(bundle["chat_messages"]):
        store.add_material(job_id=job["id"], kind="chat", title=item["title"],
                           text=item["text"], meta={"index": index})
    store.add_material(job_id=job["id"], kind="interview", title="面试提纲",
                       text="\n\n".join(f"问：{item['question']}\n答：\n{item['outline']}"
                                        for item in bundle["interview"]))
    store.add_material(job_id=job["id"], kind="gap_plan", title="技能缺口与补法",
                       text="\n".join(f"- {item['gap']} → {item['action']}"
                                      for item in bundle["gap_plan"]))
    store.close()
    print(f"# 目标岗位：{job['title']}｜{job['company'] or ''}（引擎 {engine}）\n")
    print("## 自我介绍\n" + bundle["self_intro"] + "\n")
    print("## 求职信\n" + bundle["cover_letter"] + "\n")
    print("## 打招呼与回复建议")
    for item in bundle["chat_messages"]:
        print(f"\n### {item['title']}\n{item['text']}")
    print("\n## 面试提纲")
    for item in bundle["interview"]:
        print(f"\n问：{item['question']}\n答：\n{item['outline']}")
    print("\n## 需要补的证据")
    for item in bundle["gap_plan"]:
        print(f"- {item['gap']} → {item['action']}")
    text_issues = bundle["quality"]["self_intro"]["issues"] + \
        bundle["quality"]["cover_letter"]["issues"]
    if text_issues:
        print("\n文本自检命中：")
        for issue in text_issues:
            print(f"- [{issue['kind']}] {issue['hit']}：{issue['suggestion']}")
    return 0


def cmd_evidence(args: Any) -> int:
    report = build_report(run_checks=not args.no_run)
    summary = report["summary"]
    print(f"核查时间：{report['generated_at']}")
    print("等级说明：" + "；".join(f"{k} {v[:14]}" for k, v in report["legend"].items()))
    print(f"共 {summary['total']} 条断言："
          + "　".join(f"{grade} {count}" for grade, count in summary["by_grade"].items())
          + f"｜可写进简历 {summary['resume_usable']} 条")
    if summary["downgraded"]:
        print("被下调的断言：")
        for item in summary["downgraded"]:
            print(f"  {item['id']}：自评 {item['declared']} → 核查 {item['verified']}")
    print()
    for claim in report["claims"]:
        mark = "OK " if claim["verified_grade"] in ("A", "B") else "!! "
        print(f"{mark}[{claim['declared_grade']}→{claim['verified_grade']}] {claim['project']}｜{claim['claim'][:60]}")
        for check in claim["checks"]:
            print(f"      {'✔' if check['ok'] else '✘'} {check['kind']}:{check['value']}"
                  + (f"（{check['detail'][:60]}）" if check["detail"] else ""))
        if claim["missing"]:
            print("      待补：" + "；".join(claim["missing"]))
        for caveat in claim["caveats"]:
            print("      注意：" + caveat)
    usable = usable_claims(report)
    print(f"\n可用于简历的断言 {len(usable)} 条："
          + "、".join(claim["id"] for claim in usable))
    return 0


# ---------------- 工具目录 ----------------

def cmd_tool(args: Any) -> int:
    action = args.action
    store = _store(args)
    catalog = Catalog(store.conn)
    registry.sync_catalog(catalog)
    try:
        if action == "list":
            state = args.state
            for tool in catalog.list(state=state):
                flag = "可自主调用" if (tool.state == "D" and tool.auto_call) else "需人工介入"
                print(f"{tool.name:28s} {tool.state} {STATES[tool.state]:<12s} {tool.kind:<10s} "
                      f"{tool.license or '未声明':<14s} {flag}")
                if args.verbose:
                    print(f"    用途：{tool.purpose[:80] or '未填'}")
                    print(f"    依赖：{tool.runtime[:90] or '未填'}")
                    print(f"    风险：{(tool.risk or '无记录')[:90]}")
                    print(f"    失败处理：{tool.failure_policy[:80] or '未填'}")
            print(f"\n统计：{catalog.stats()}")
            return 0
        if action == "discover":
            queries = [args.query] if args.query else None
            result = discover(catalog, queries=queries, per_page=args.per_page)
            print(f"检索式 {result['queries']} 条｜新增候选 {len(result['added'])} 个｜"
                  f"刷新 {len(result['refreshed'])} 个")
            for name in result["added"]:
                print("  新增 " + name)
            for error in result["errors"]:
                print("  失败 " + error)
            print("候选状态为 A（已发现，尚未验证），下一步用 tool evaluate 评估。")
            return 0
        if action == "evaluate":
            result = evaluate(catalog, args.name)
            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, default=str))
            return 0
        if action == "verify":
            if args.all:
                result = registry.verify_all(catalog)
                print(f"通过 {len(result['passed'])} 个：{'、'.join(result['passed'])}")
                for item in result["failed"]:
                    print(f"未通过 {item['name']}：{item['detail']}")
                return 0
            if not args.name:
                print("需要工具名，或加 --all")
                return 1
            result = registry.verify(args.name, catalog)
            print(f"{'通过' if result['ok'] else '未通过'}：{result['detail']}")
            return 0
        if action == "call":
            payload = json.loads(args.json) if args.json else {}
            result = registry.call(args.name, catalog=catalog, **payload)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str)[:4000])
            return 0
        if action == "route":
            result = route(args.text, catalog)
            print(f"任务：{result['task']}")
            print(f"识别意图：{result['intent'] or '未识别'}")
            print(f"选择：{result['chosen']['name'] if result['chosen'] else '无'}")
            print(f"理由：{result['reason']}")
            print(f"兜底：{result['fallback']}")
            if args.verbose and result["candidates"]:
                print("候选：")
                for item in result["candidates"]:
                    print(f"  {item['name']}｜{item['state']} {item['state_label']}｜"
                          f"{'可自主调用' if item['auto_callable'] else '不可自主调用'}")
            return 0
        if action == "plan":
            result = plan_task(args.text, catalog)
            print(f"任务：{result['task']}｜模板：{result['workflow']}")
            for step in result["steps"]:
                print(f"{step['step']}. {step['goal']}")
                print(f"   意图 {step['intent']}｜工具 {step['tool'] or '无'}（{step['tool_state']}）"
                      f"｜入口 {step['entry'] or '—'}")
                print(f"   选择理由：{step['why']}")
                if step["fallback"]:
                    print(f"   兜底：{step['fallback']}")
            return 0
        if action == "events":
            for row in catalog.events(args.name if args.name else None, limit=args.limit):
                print(f"{row['created_at']}｜{row['name']}｜{row['kind']}｜"
                      f"{'OK' if row['ok'] else 'FAIL'}｜{(row['detail'] or '')[:110]}")
            return 0
        print(f"没有这个动作：{action}")
        return 1
    finally:
        store.close()


# ---------------- 界面、调度、模型 ----------------

def cmd_ui(args: Any) -> int:
    from .webui.server import serve

    serve(port=args.port, open_browser=not args.no_browser, db_path=_store(args).path)
    return 0


def cmd_schedule(args: Any) -> int:
    store = _store(args)
    action = args.action
    if action == "run":
        store.close()
        print(json.dumps(scheduler_control("run"), ensure_ascii=False))
        return 0
    if action == "start":
        result = scheduler_control("start", interval_minutes=args.interval)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif action == "stop":
        print(json.dumps(scheduler_control("stop"), ensure_ascii=False, indent=2))
    elif action == "logs":
        for line in scheduler_logs(args.lines):
            print(line)
    else:
        print(json.dumps(scheduler_control("status"), ensure_ascii=False, indent=2))
    store.close()
    return 0


def cmd_llm(args: Any) -> int:
    llm = LLM()
    ok, detail = llm.available()
    print(("可用：" if ok else "不可用：") + detail)
    if not ok:
        return 1
    if args.check:
        result = llm.health_check()
        if result.ok:
            print(f"连通性正常：模型 {result.model}，用时 {result.seconds:.2f}s，回复「{result.text[:20]}」")
            return 0
        print("连通性失败：" + result.error)
        return 2
    return 0


def cmd_stats(args: Any) -> int:
    store = _store(args)
    catalog = Catalog(store.conn)
    print(json.dumps({"jobs": store.stats(), "tools": catalog.stats()},
                     ensure_ascii=False, indent=2, default=str))
    store.close()
    return 0
