"""进度报告：把库里的岗位、分析、材料、应聘记录和工具目录导成 Markdown。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import load as load_config
from .store import Store, loads
from .tools.catalog import STATES, Catalog


def _section(title: str, lines: list[str]) -> list[str]:
    return ["", f"## {title}", "", *lines] if lines else ["", f"## {title}", "", "（暂无）"]


def build_report(db_path: str | Path | None = None) -> str:
    config = load_config()
    store = Store(db_path or config.db_path)
    catalog = Catalog(store.conn)
    stats = store.stats()
    lines = [
        "# 求职进度报告",
        "",
        f"生成时间：{__import__('time').strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 总览",
        "",
        f"- 岗位：{stats['jobs']} 条（{', '.join(f'{k} {v}' for k, v in stats['jobs_by_status'].items()) or '按状态暂无'}）",
        f"- 已完成分析：{stats['analyses']} 条",
        f"- 生成简历：{stats['resumes']} 份",
        f"- 求职材料：{stats['materials']} 份",
        f"- 应聘记录：{sum(stats['applications'].values())} 条"
        f"（{', '.join(f'{k} {v}' for k, v in stats['applications'].items()) or '暂无'}）",
        f"- 工具目录：{catalog.stats()['total']} 个（可自主调用 {catalog.stats()['auto_callable']} 个）",
    ]

    top = store.list_jobs(min_score=1, limit=15, order="score")
    rows: list[str] = []
    for job in top:
        analysis = store.get_analysis(job["id"])
        verdict = analysis["verdict"] if analysis else "未分析"
        reasons = loads(job["score_reasons"], []) or []
        gaps = loads(job["gaps"], []) or []
        rows.append(
            f"- **{job['title']}**｜{job['company'] or '未写公司'}｜{job['city'] or ''}"
            f"｜分数 {job['score'] if job['score'] is not None else '—'}｜{verdict}"
            f"｜来源 {job['platform']}"
        )
        if reasons[:2]:
            rows.append(f"  - 命中：{'；'.join(reasons[:2])}")
        if gaps[:2]:
            rows.append(f"  - 缺口：{'；'.join(gaps[:2])}")
        if job["url"]:
            rows.append(f"  - 链接：{job['url']}")
    lines += _section("推荐优先看的岗位", rows)

    apps = store.list_applications(limit=50)
    app_rows = [
        f"- {row['title']}｜{row['company'] or ''}｜状态 {row['status']}"
        f"｜更新 {row['updated_at']}"
        + (f"｜简历 {row['resume_pdf'] or row['resume_docx'] or '未记录'}" if row["resume_id"] else "")
        for row in apps
    ]
    lines += _section("应聘记录", app_rows)

    resumes = store.list_resumes(limit=15)
    resume_rows = [
        f"- #{row['id']}｜{row['direction']}｜{row['variant']}"
        f"｜{row['pdf_path'] or row['docx_path'] or row['md_path']}"
        f"｜{row['created_at']}"
        for row in resumes
    ]
    lines += _section("已生成简历", resume_rows)

    materials = store.list_materials(limit=20)
    material_rows = [
        f"- [{row['kind']}] {row['title'] or ''}｜{row['created_at']}"
        for row in materials
    ]
    lines += _section("求职材料", material_rows)

    # 技能缺口聚合：把所有分析里的缺口按出现次数排序
    gap_counts: dict[str, int] = {}
    for row in store.conn.execute("SELECT gaps FROM analyses"):
        for gap in loads(row["gaps"], []) or []:
            key = str(gap)[:48]
            gap_counts[key] = gap_counts.get(key, 0) + 1
    gap_rows = [f"- {text}（出现 {count} 次）"
                for text, count in sorted(gap_counts.items(), key=lambda item: -item[1])[:10]]
    lines += _section("技能缺口与下一步", gap_rows or [
        "- 还没有分析过岗位，先跑 `python -m jobagent analyze --limit 20`",
    ])

    tool_rows = []
    for tool in catalog.list()[:30]:
        tool_rows.append(
            f"- {tool.name}｜{tool.state} {STATES[tool.state]}｜{tool.kind}"
            f"｜许可证 {tool.license or '未声明'}"
            f"｜{'可自主调用' if tool.auto_call and tool.state == 'D' else '需人工介入'}"
        )
    lines += _section("工具目录（开源能力）", tool_rows)

    events = store.recent_events(limit=15)
    event_rows = [f"- {row['created_at']}｜{row['kind']}｜{row['subject']}｜{(row['detail'] or '')[:60]}"
                  for row in events]
    lines += _section("最近动作", event_rows)

    store.close()
    return "\n".join(lines).strip() + "\n"


def write_report(db_path: str | Path | None = None,
                 out_path: str | Path | None = None) -> dict[str, Any]:
    config = load_config()
    target = Path(out_path) if out_path else config.report_path
    text = build_report(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return {"path": str(target), "chars": len(text)}
