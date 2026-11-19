"""命令行入口：python -m jobagent <命令>。"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from . import browser, cdp
from . import commands
from .adapters.boss import BossAdapter
from .adapters.base import AdapterError, BlockedError
from .config import Config, load as load_config, init_user_data, ROOT
from .greeter import draft_greetings
from .matcher import parse_salary, score_job
from .profile import Profile
from .store import Store, loads


def build_context(args: argparse.Namespace) -> tuple[Config, Profile, Store]:
    config_arg = getattr(args, "config", None)
    cfg = load_config(config_arg)
    cfg.ensure_dirs()
    profile_arg = getattr(args, "profile", None)
    profile_path = Path(profile_arg) if profile_arg else cfg.profile_path
    profile = Profile.load(profile_path)
    store = Store(cfg.db_path)
    return cfg, profile, store


def ensure_session(cfg: Config, *, launch: bool = True) -> cdp.CDP:
    managed = browser.get_managed_session(cfg.browser_user_data_dir)
    port = managed["port"] if managed else cfg.browser_port
    if not browser.is_up(port):
        if not launch:
            raise cdp.CDPError(f"{port} 端口上没有受托管的浏览器，先跑 python -m jobagent browser")
        state, port = browser.launch(
            cfg.browser_user_data_dir,
            port=port,
            url="about:blank",
            browser_path=cfg.browser_exe or None,
        )
        print(f"浏览器已{'复用' if state == 'reused' else '启动'}（端口 {port}）", flush=True)
    return cdp.attach(port)


def make_adapter(session: cdp.CDP, cfg: Config, *, verbose: bool = True) -> BossAdapter:
    return BossAdapter(session, dump_dir=cfg.dump_dir, verbose=verbose)


# ---------------- 命令 ----------------

def cmd_init(args: argparse.Namespace) -> int:
    target, created = init_user_data()
    print("========================================================")
    print("  AI Job Hunter 独立本地用户数据目录初始化完成")
    print("========================================================")
    print(f"数据根目录: {target}")
    if created:
        print("新生成的基础配置模板:")
        for c in created:
            print(f"  + {c}")
    else:
        print("原有配置文件完好保留，未发生覆盖。")
    print("\n你可以直接按需编辑 profile.json 与 master_resume.json 完善个人经历。")
    return 0


def cmd_browser(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    cfg.ensure_dirs()
    state, port = browser.launch(
        cfg.browser_user_data_dir,
        port=cfg.browser_port,
        url=getattr(args, "url", None) or "https://www.zhipin.com/web/geek/chat",
        browser_path=cfg.browser_exe or None,
    )
    print("========================================================")
    print(f"  Chrome 专属求职会话 {'已安全复用' if state == 'reused' else '已成功启动'}")
    print("========================================================")
    print(f"监听调试端口: {port}")
    print(f"独立用户目录: {cfg.browser_user_data_dir}")
    print("提示：请在弹出的浏览器中登录 BOSS 直聘账号，保持窗口打开即可进行自动化求职。")
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    return cmd_browser(args)


def cmd_listen(args: argparse.Namespace) -> int:
    from .listener import ChatListener
    cfg = load_config(getattr(args, "config", None))
    auto_accept = True if getattr(args, "auto_accept", False) else cfg.auto_accept_resume
    listener = ChatListener(config=cfg, auto_accept=auto_accept, verbose=True)
    listener.run_forever(interval=getattr(args, "interval", 6.0))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    config, _, _ = build_context(args)
    session = ensure_session(config)
    adapter = make_adapter(session, config)
    if not args.no_nav:
        session.navigate(adapter.home)
        session.wait_ready(timeout=25)
        time.sleep(3)  # 未登录时站点先渲染外壳再跳登录页，等它跳完再判断
    port = config.browser_port
    print("浏览器:", cdp.http_json(port, "/json/version").get("Browser"))
    print("当前页面:", session.url())
    print("标题:", session.evaluate("document.title", await_promise=False))
    block = adapter.check_block()
    print("风控检查:", block or "正常")
    session.close()
    return 0


def scan_with_login_wait(adapter: BossAdapter, kwargs: dict[str, Any],
                         wait_login: int) -> list[dict[str, Any]]:
    """被拦在登录页时，留出时间让你手工登录，然后自动重试一次。"""
    try:
        return adapter.scan(**kwargs)
    except BlockedError as exc:
        if "需要登录" not in str(exc) or wait_login <= 0:
            raise

    print(f"\n{exc}")
    print(f"请在弹出的浏览器窗口里登录（最多等 {wait_login} 秒）...", flush=True)
    deadline = time.monotonic() + wait_login
    logged_in = False
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            if adapter.check_block() is None:
                logged_in = True
                print("看起来登录好了，继续抓取", flush=True)
                break
        except Exception:
            continue
    if not logged_in:
        raise BlockedError(f"等了 {wait_login} 秒还没登录成功")

    time.sleep(2)
    return adapter.scan(**kwargs)


def cmd_scan(args: argparse.Namespace) -> int:
    config, _, store = build_context(args)
    query = args.query or config.scan_query
    city = args.city or config.scan_city
    session = ensure_session(config)
    adapter = make_adapter(session, config)
    run_id = store.start_run("scan")
    try:
        jobs = scan_with_login_wait(
            adapter,
            {
                "query": query,
                "city": city,
                "pages": args.pages,
                "limit": args.limit,
                "city_code": args.city_code,
            },
            args.wait_login,
        )
    except (BlockedError, AdapterError) as exc:
        print(f"抓取失败: {exc}")
        store.finish_run(run_id, {"error": str(exc)})
        session.close()
        return 2
    new, updated = store.upsert_jobs("boss", jobs)
    store.finish_run(run_id, {"found": len(jobs), "new": new, "updated": updated})
    print(f"抓到 {len(jobs)} 条，新增 {new} 条，更新 {updated} 条")
    session.close()
    return 0


def cmd_detail(args: argparse.Namespace) -> int:
    config, _, store = build_context(args)
    if args.ids:
        ids = [int(x) for x in str(args.ids).replace("，", ",").split(",") if x.strip()]
        rows = [store.get_job(job_id) for job_id in ids]
        rows = [r for r in rows if r]
    else:
        rows = store.list_jobs(order="score", limit=args.limit)
    # 正文太短（只有一句话的占位）也算没抓到：2026-10-16 之前只判"完全空"，
    # 结果是 10-100 字的残页永远不再抓，话术也没法按 JD 写。
    rows = [r for r in rows if len(r["description"] or "") < 120]
    if not rows:
        print("没有缺 JD 正文的岗位（都已经抓过了）")
        return 0
    session = ensure_session(config)
    adapter = make_adapter(session, config)
    ok = failed = gone = 0
    for row in rows:
        try:
            detail = adapter.fetch_detail(dict(row))
            store.set_description(row["id"], detail.get("description", ""))
            if detail.get("hr_name"):
                store.conn.execute(
                    "UPDATE jobs SET hr_name=?, hr_title=? WHERE id=?",
                    (detail.get("hr_name"), detail.get("hr_title"), row["id"]),
                )
                store.conn.commit()
            ok += 1
            print(f"OK  {row['title']} @ {row['company']}")
        except (BlockedError, AdapterError) as exc:
            if "JOB_GONE" in str(exc):
                gone += 1
                store.conn.execute(
                    "UPDATE jobs SET valid=0, validity_note=? WHERE id=?",
                    ("岗位已下线（页面不存在）", row["id"]),
                )
                store.conn.commit()
                print(f"GONE {row['title']} @ {row['company']}（已标下线，不再重抓）")
                time.sleep(args.delay)
                continue
            failed += 1
            print(f"FAIL {row['title']} - {exc}")
            if isinstance(exc, BlockedError):
                break
        except Exception as exc:  # 页面超时这类意外不该让整批停在这里
            failed += 1
            print(f"FAIL {row['title']} - {type(exc).__name__}: {exc}")
        time.sleep(args.delay)
    print(f"详情抓取完成：成功 {ok}，失败 {failed}，已下线 {gone}")
    session.close()
    return 0 if failed == 0 else 1


def cmd_rank(args: argparse.Namespace) -> int:
    config, profile, store = build_context(args)
    rows = store.list_jobs(order="recent", limit=args.limit)
    scored = 0
    for row in rows:
        result = score_job(dict(row), profile)
        store.save_score(row["id"], result.total, result.reasons, result.gaps)
        scored += 1
    print(f"打分完成，共 {scored} 条")
    for row in store.list_jobs(order="score", limit=args.top):
        reasons = loads(row["score_reasons"], []) or []
        first = reasons[0] if reasons else "无命中"
        flag = "红线" if row["status"] == "skipped" else ""
        print(f"{row['score'] or 0:5.1f}  {row['title'][:28]:<30} {row['company'][:20]:<22} {first[:36]} {flag}")
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    config, profile, store = build_context(args)
    rows = store.list_jobs(order="score", limit=args.top, min_score=args.min_score)
    created = 0
    for row in rows:
        if row["status"] in ("sent", "skipped"):
            continue
        if store.draft_for_job(row["id"]):
            continue
        for candidate in draft_greetings(dict(row), profile, variants=args.variants):
            store.add_message(row["id"], candidate["text"], candidate["variant"])
            created += 1
    print(f"生成 {created} 条话术草稿")
    if args.show:
        for row in store.list_jobs(order="score", limit=min(args.top, args.show)):
            draft = store.draft_for_job(row["id"])
            if not draft:
                continue
            print(f"\n--- {row['title']} @ {row['company']}（{row['score'] or 0:.0f} 分）---")
            print(draft["text"])
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    config, _, store = build_context(args)
    queue = store.queued_messages(args.limit)
    if not queue:
        print("没有待发送的草稿，先跑 draft")
        return 0
    if not args.send:
        print("当前是演练模式（只验证按钮和输入框，不真发）。要真发加 --send。\n")
    session = ensure_session(config)
    adapter = make_adapter(session, config)
    sent = failed = unverified = 0
    for row in queue:
        # BOSS 的聊天链路只能发 BOSS 岗位；电鸭/海外岗位要走邮件或帖子回复，
        # 混在队列里发会打开别人的页面，所以在这里挡住。
        if "zhipin.com" not in (row["url"] or ""):
            print(f"SKIP {row['title']}（不是 BOSS 岗位，{row['url'] or '无链接'}）"
                  "——这类要走邮件/帖子回复，别用 BOSS 链路发")
            continue
        job = {
            "external_id": row["external_id"],
            "url": row["url"],
            "title": row["title"],
            "company": row["company"],
        }
        try:
            result = adapter.send_greeting(job, row["text"], dry_run=not args.send)
        except (BlockedError, AdapterError) as exc:
            failed += 1
            store.mark_message(row["id"], "failed", error=str(exc))
            print(f"FAIL {row['title']} - {exc}")
            if isinstance(exc, BlockedError):
                break
            continue
        except Exception as exc:  # 页面超时这类意外不该让整批发到一半就停
            failed += 1
            store.mark_message(row["id"], "failed",
                               error=f"{type(exc).__name__}: {exc}")
            print(f"FAIL {row['title']} - {type(exc).__name__}: {exc}")
            continue
        if args.send:
            # 状态分三档：sent 回读通过 / unverified 已点发送但回读没确认 / failed 没发出去。
            # unverified 不能记成 failed——失败池会被"补发"脚本整批重发，那就是盲目重发。
            status = "sent" if result.get("verified") else "unverified"
            store.mark_message(row["id"], status, verified=bool(result.get("verified")))
            store.set_status(row["job_id"], {
                "sent": "sent", "unverified": "sent_unverified",
            }.get(status, "send_failed"))
            if result.get("verified"):
                sent += 1
                print(f"OK  {row['title']}"
                      + ("（发送前核对：已存在，未重复发）" if result.get("deduped") else ""))
            else:
                unverified += 1
                print(f"UNVERIFIED {row['title']} - 状态不明，不重发，先读会话确认："
                      f"{result.get('note') or '回读未确认'}")
        else:
            print(f"演练通过 {row['title']}（按钮 {result.get('clicked')}）")
        time.sleep(args.delay)
    print(f"完成：成功 {sent}，状态不明 {unverified}，失败 {failed}")
    session.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    # 新版报告：岗位 + 分析 + 简历 + 材料 + 应聘记录 + 工具目录 + 审计
    if not getattr(args, "legacy", False):
        from .report import write_report

        result = write_report()
        print(f"报告写到 {result['path']}（{result['chars']} 字）")
        return 0
    config, _, store = build_context(args)
    rows = store.list_jobs(order="score", limit=args.top)
    lines = ["# 找工作进度报告", "", f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}", ""]
    stats = store.stats()
    lines.append(f"岗位总数 {stats['jobs']}：{stats['jobs_by_status']}")
    lines.append(f"话术：{stats['messages']}")
    lines.append("")
    for row in rows:
        reasons = loads(row["score_reasons"], []) or []
        gaps = loads(row["gaps"], []) or []
        salary_min, salary_max = row["salary_min"], row["salary_max"]
        lines.append(f"## {row['title']} @ {row['company']}")
        lines.append(
            f"- 分数 {row['score'] or 0:.1f} | 薪资 {row['salary_text'] or '未写'} "
            f"| 地点 {row['city'] or '未写'} | 状态 {row['status']}"
        )
        if row["url"]:
            lines.append(f"- 链接 {row['url']}")
        if reasons:
            lines.append("- 命中：" + "；".join(reasons[:4]))
        if gaps:
            lines.append("- 待确认：" + "；".join(gaps[:4]))
        lines.append("")
    report_path = config.report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告写到 {report_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _, _, store = build_context(args)
    stats = store.stats()
    print(f"岗位 {stats['jobs']} 条: {stats['jobs_by_status']}")
    print(f"话术: {stats['messages']}")
    print(f"分析 {stats['analyses']} 条，简历 {stats['resumes']} 份，"
          f"材料 {stats['materials']} 份，应聘 {sum(stats['applications'].values())} 条")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobagent", description="自动找工作流水线")
    parser.add_argument("--config", help="配置文件路径，默认优先读用户独立数据空间")
    parser.add_argument("--profile", help="候选人档案路径，默认 profile.json")
    sub = parser.add_subparsers(dest="command", required=True)

    # ---------- 环境与浏览器 ----------
    p = sub.add_parser("init", help="初始化独立用户数据空间与配置模板")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("browser", help="启动受托管的独立 Chrome 调试浏览器")
    p.add_argument("--url", help="启动后打开的地址")
    p.set_defaults(func=cmd_browser)

    p = sub.add_parser("launch", help="起一个带调试端口的浏览器（browser 别名）")
    p.add_argument("--url", help="启动后打开的地址")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("listen", help="启动会话监听守护进程，实时检测新消息并安全处理附件简历")
    p.add_argument("--interval", type=float, default=6.0, help="轮询间隔秒数")
    p.add_argument("--auto-accept", action="store_true", help="显式开启自动同意简历附件")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("probe", help="检查浏览器连接与风控状态")
    p.add_argument("--no-nav", action="store_true", help="不跳转，只看当前页面")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("scan", help="按关键词抓岗位列表")
    p.add_argument("--query", help="搜索关键词")
    p.add_argument("--city", help="城市名，如 泉州")
    p.add_argument("--city-code", help="直接指定平台城市码")
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--wait-login", type=int, default=180,
                   help="被拦在登录页时等你手工登录的秒数，0 表示不等")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("detail", help="补抓岗位 JD 正文")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--ids", help="只抓指定岗位，逗号分隔，如 317,318,320")
    p.add_argument("--delay", type=float, default=2.0)
    p.set_defaults(func=cmd_detail)

    p = sub.add_parser("rank", help="按能力打分排序")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser("draft", help="生成打招呼草稿")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--variants", type=int, default=3)
    p.add_argument("--min-score", type=float, default=25.0)
    p.add_argument("--show", type=int, default=3, help="直接打印前几条")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("send", help="发打招呼（默认演练）")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--delay", type=float, default=6.0)
    p.add_argument("--send", action="store_true", help="真的发出去")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("report", help="导出进度报告")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--legacy", action="store_true", help="用旧版逐岗清单格式")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("status", help="看数据库概况")
    p.set_defaults(func=cmd_status)

    # ---------- 真实岗位数据源 ----------
    p = sub.add_parser("sources", help="列出可用的岗位数据源及其限制")
    p.set_defaults(func=commands.cmd_sources)

    p = sub.add_parser("fetch", help="从公开数据源取岗位并落库")
    p.add_argument("--keywords", nargs="*", help="关键词，默认读 config.toml")
    p.add_argument("--cities", nargs="*", help="城市，默认读 config.toml")
    p.add_argument("--sources", nargs="*", help="数据源，默认读 config.toml")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--pages", type=int, default=1)
    p.set_defaults(func=commands.cmd_fetch)

    p = sub.add_parser("import", help="手工导入 JD（文件，- 表示从标准输入读）")
    p.add_argument("paths", nargs="*", help="JD 文本 / JSON / HTML 文件路径")
    p.set_defaults(func=commands.cmd_import)

    p = sub.add_parser("dedupe", help="跨平台查重并标记")
    p.add_argument("--threshold", type=float, default=92.0)
    p.set_defaults(func=commands.cmd_dedupe)

    p = sub.add_parser("enrich", help="用官方详情接口补齐 JD 里的任职要求")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--all", action="store_true", help="连已经补过的也重抓")
    p.set_defaults(func=commands.cmd_enrich)

    p = sub.add_parser("translate", help="把英文 JD 翻成中文（用已配置的大模型）")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--job", type=int, help="只翻译某个岗位")
    p.add_argument("--all", action="store_true", help="已经翻过的也重翻")
    p.add_argument("--titles-only", action="store_true", help="只补岗位标题的中文")
    p.set_defaults(func=commands.cmd_translate)

    # ---------- 分析与材料 ----------
    p = sub.add_parser("analyze", help="拆解岗位要求并给出匹配结论")
    p.add_argument("--job", type=int, help="只分析某个岗位")
    p.add_argument("--ids", help="只分析指定岗位，逗号分隔，如 40,170,176")
    p.add_argument("--all", action="store_true", help="已分析过的也重跑一遍")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--llm", action="store_true", help="额外用大模型抽一遍要求")
    p.add_argument("--skip-fresh-hours", type=float, default=0.0,
                   help="只处理新信息：JD 没更新且分析没过期的岗位直接复用旧结论（单位小时，0 表示关）")
    p.set_defaults(func=commands.cmd_analyze)

    p = sub.add_parser("resume", help="生成简历（主简历 → 按方向裁剪 → DOCX/PDF → 自检）")
    p.add_argument("--job", type=int, help="按某个岗位定制")
    p.add_argument("--direction", help="直接指定方向，如 AI Agent 应用")
    p.add_argument("--out", help="输出目录，默认 output/resume")
    p.set_defaults(func=commands.cmd_resume)

    p = sub.add_parser("materials", help="生成自我介绍、求职信、私信、面试提纲、缺口清单")
    p.add_argument("--job", type=int, required=True)
    p.add_argument("--direction", help="覆盖方向判断")
    p.add_argument("--llm", action="store_true", help="用大模型润色（失败自动退回规则版）")
    p.set_defaults(func=commands.cmd_materials)

    p = sub.add_parser("evidence", help="核查能力断言的证据等级（A/B/C/D）")
    p.add_argument("--no-run", action="store_true", help="只查文件，不执行检查脚本")
    p.set_defaults(func=commands.cmd_evidence)

    # ---------- 工具目录 ----------
    p = sub.add_parser("tool", help="开源工具的发现、评估、验证、调用与选型")
    p.add_argument("action",
                   choices=["list", "discover", "evaluate", "verify", "call", "route", "plan", "events"])
    p.add_argument("--name", help="工具名或 owner/repo")
    p.add_argument("--query", help="GitHub 检索式")
    p.add_argument("--text", help="任务描述（route / plan 用）")
    p.add_argument("--json", help="调用参数，JSON 字符串")
    p.add_argument("--state", help="只看某个状态：A/B/C/D/E")
    p.add_argument("--all", action="store_true", help="verify 时验证全部")
    p.add_argument("--per-page", type=int, default=5)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=commands.cmd_tool)

    # ---------- 界面与调度 ----------
    p = sub.add_parser("ui", help="启动中文管理面板")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.set_defaults(func=commands.cmd_ui)

    p = sub.add_parser("schedule", help="定时检索：start / stop / status / logs / run")
    p.add_argument("action", nargs="?", default="status",
                   choices=["start", "stop", "status", "logs", "run"])
    p.add_argument("--interval", type=int, help="间隔分钟数")
    p.add_argument("--lines", type=int, default=40)
    p.set_defaults(func=commands.cmd_schedule)

    p = sub.add_parser("llm", help="检查大模型配置是否可用")
    p.add_argument("--check", action="store_true", help="真发一次最小请求验证连通性")
    p.set_defaults(func=commands.cmd_llm)

    p = sub.add_parser("stats", help="看岗位与工具目录概况")
    p.set_defaults(func=commands.cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n中断")
        return 130
    except cdp.CDPError as exc:
        print(f"浏览器层错误: {exc}")
        return 3
    except Exception as exc:
        from .safety import NeedsConfirmation
        from .sources.base import SourceError
        from .tools.registry import ToolError
        if isinstance(exc, (ToolError, NeedsConfirmation, SourceError)):
            print(str(exc))
            return 4
        raise


if __name__ == "__main__":
    sys.exit(main())
