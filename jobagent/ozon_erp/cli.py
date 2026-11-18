"""命令行入口：python -m jobagent.ozon_erp <命令>

没有真实凭据也能跑：加 `--mock` 就用本地模拟卖家（自带一批可复现的数据），
同步、漏单检测、对账、报表整条链都能跑通并自测。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from . import config as ozon_config
from . import collect as collection
from . import listing
from . import reconcile as reconciliation
from . import sync as ozon_sync
from .client import OzonClient
from .mock import MockOzon
from .store import OzonStore


class MockSupplierSource:
    """`--source mock` 用的本地货源（见 collect.MockSupplier）。"""

    def items(self):
        return collection.MockSupplier.demo().items()

    name = "mock-supplier"


def _build(args: argparse.Namespace):
    settings = ozon_config.load(Path(args.config) if getattr(args, "config", None) else None)
    if getattr(args, "db", ""):
        settings.db_path = Path(args.db)
    if not args.mock and not settings.configured:
        raise SystemExit(
            "没有 Ozon 凭据。请设置环境变量 OZON_CLIENT_ID / OZON_API_KEY，"
            "或在 config.toml 里加 [ozon] 段；只想本地跑通流程就加 --mock。")
    store = OzonStore(settings.db_path)
    if args.mock:
        mock = MockOzon.demo(with_gaps=getattr(args, "gaps", False),
                             fail_every=getattr(args, "fail_every", 0))
        client = OzonClient("mock-client", "mock-key", base_url="mock://ozon",
                            rate_per_second=1000, max_retries=3,
                            transport=mock.transport, sleeper=lambda _s: None)
    else:
        client = OzonClient(settings.client_id, settings.api_key, base_url=settings.base_url,
                            rate_per_second=settings.rate_per_second,
                            max_retries=settings.max_retries, timeout=settings.timeout)
    return settings, store, client


def _settings_and_store(args: argparse.Namespace):
    """只读命令用：同样支持 --db 覆盖，否则 --db 会被忽略（2026-10-15 实测踩到）。"""
    settings = ozon_config.load(Path(args.config) if getattr(args, "config", None) else None)
    if getattr(args, "db", ""):
        settings.db_path = Path(args.db)
    return settings, OzonStore(settings.db_path)


def cmd_status(args: argparse.Namespace) -> int:
    settings, store = _settings_and_store(args)
    print(settings.describe())
    print(json.dumps(store.counts(), ensure_ascii=False))
    for row in store.conn.execute("SELECT name, cursor, last_ok_at, note FROM sync_state"):
        print(f"  游标 {row['name']:12s} {row['cursor'] or '—':26s} "
              f"最后成功 {row['last_ok_at'] or '—'}｜{row['note'] or ''}")
    errors = store.open_errors()
    if errors:
        print(f"未解决错误 {len(errors)} 条（最近 3 条）：")
        for row in errors[:3]:
            print(f"  [{row['scope']}] {row['key']}｜{row['error'][:90]}")
    store.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings, store, client = _build(args)
    print(settings.describe())
    results = client.doctor()
    for row in results:
        flag = "OK  " if row["ok"] else "FAIL"
        print(f"{flag} {row['endpoint']:22s} {row['detail']}")
    store.close()
    return 0 if all(row["ok"] for row in results) else 1


def cmd_sync(args: argparse.Namespace) -> int:
    settings, store, client = _build(args)
    what = args.what
    reports: list[dict] = []
    if what in ("products", "all"):
        reports.append(ozon_sync.sync_products(client, store))
    if what in ("orders", "all"):
        reports.append(ozon_sync.sync_orders(client, store, kind=args.kind,
                                             since=args.since or "",
                                             overlap_minutes=settings.overlap_minutes,
                                             backfill_days=args.backfill_days))
    if what in ("finance", "all"):
        reports.append(ozon_sync.sync_finance(client, store, days_back=args.days_back))
    for report in reports:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    print("库内：", json.dumps(store.counts(), ensure_ascii=False))
    if getattr(client, "retries", 0):
        print(f"限流/错误重试次数：{client.retries}")
    store.close()
    return 0


def cmd_gaps(args: argparse.Namespace) -> int:
    settings, store = _settings_and_store(args)
    result = ozon_sync.detect_gaps(store)
    print(f"订单在、无流水：{len(result['orders_without_money'])} 条")
    for row in result["orders_without_money"][:10]:
        print(f"   {row['posting_number']}｜{row['created_at']}｜{row['amount']}")
    print(f"有流水、无订单（真漏单）：{len(result['money_without_orders'])} 条")
    for row in result["money_without_orders"][:10]:
        print(f"   {row['posting_number']}｜{row['amount']}｜{row['n']} 条流水")
    print(f"未解决同步错误：{len(result['open_errors'])} 条")
    for row in result["open_errors"][:10]:
        print(f"   [{row['scope']}] {row['key']}｜{row['error'][:100]}")
    store.close()
    return 0 if result["ok"] else 1


def cmd_retry(args: argparse.Namespace) -> int:
    settings, store, client = _build(args)
    print(json.dumps(ozon_sync.retry_failed(client, store), ensure_ascii=False))
    print("库内：", json.dumps(store.counts(), ensure_ascii=False))
    store.close()
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """采集：把来源的商品抓进采集箱（清洗 + 去重）。默认用本地假货源。"""
    settings, store = _settings_and_store(args)
    source = MockSupplierSource()
    report = collection.collect(store, source)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    rows = store.collected_list(limit=args.show)
    for row in rows:
        flag = "READY " if row["status"] == "ready" else "SKIP  "
        print(f"  {flag} #{row['id']:3d} {row['title'][:40]:42s} "
              f"{row['price'] or '—':>8} {row['currency']}｜{row['note'] or ''}")
    print("采集箱状态：", json.dumps(store.listing_summary()["collected"], ensure_ascii=False))
    store.close()
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """上架：采集箱里 status=ready 的转成任务 → 校验 → 调 Ozon 官方 API 提交。"""
    settings, store, client = _build(args)
    translator = None
    if args.translate:
        translator = _mock_translator(args.translate)
    report = listing.publish(store, client, limit=args.limit, translator=translator,
                             brand=args.brand, keywords=args.keywords or None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["translated"]:
        print("（没接翻译：标题按原文提交，需要俄语标题时把翻译函数接上）")
    if args.retry_failed:
        back = listing.retry_failed(store)
        print(f"失败任务放回队列：{back} 条")
    print("上架状态：", json.dumps(store.listing_summary()["tasks"], ensure_ascii=False))
    store.close()
    return 0


def _mock_translator(flag: str):
    """演示用翻译：把常见词做个极简替换，说明"翻译是插在这儿"的。

    真接大模型时，把这个函数换成调用 `jobagent.llm.LLM`（凭据现读现用）。
    """
    table = {"保温壶": "термос", "无线蓝牙耳机": "беспроводные наушники", "车载手机支架": "автодержатель",
             "LED 台灯": "LED лампа", "硅胶厨房刷": "силиконовая щётка"}
    if flag != "demo":
        return lambda text: text

    def translate(text: str) -> str:
        out = text
        for zh, ru in table.items():
            out = out.replace(zh, ru)
        return out
    return translate


def cmd_listing_status(args: argparse.Namespace) -> int:
    settings, store = _settings_and_store(args)
    summary = store.listing_summary()
    print("采集箱：", json.dumps(summary["collected"], ensure_ascii=False))
    print("上架任务：", json.dumps(summary["tasks"], ensure_ascii=False))
    rows = store.conn.execute(
        "SELECT t.id, t.offer_id, t.status, t.product_id, t.error, c.title "
        "FROM listing_tasks t JOIN collected_products c ON c.id=t.collected_id "
        "ORDER BY t.id DESC LIMIT ?", (args.show,)).fetchall()
    for row in rows:
        print(f"  #{row['id']:3d} {row['status']:10s} {row['offer_id']:12s} "
              f"{row['title'][:26]:28s} {'→ ' + row['product_id'] if row['product_id'] else ''}"
              f"{'  ' + row['error'] if row['error'] else ''}")
    store.close()
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    settings, store = _settings_and_store(args)
    result = reconciliation.reconcile(store, tolerance=args.tolerance)
    print(f"对账 {result['orders']} 单：缺流水 {len(result['missing_money'])}、"
          f"金额不符 {len(result['amount_mismatch'])}、佣金不符 {len(result['fee_mismatch'])}、"
          f"应结不符 {len(result['payout_mismatch'])}、多流水 {len(result['extra_money'])}")
    for key, title in (("missing_money", "缺流水"), ("amount_mismatch", "金额不符"),
                       ("fee_mismatch", "佣金不符"), ("payout_mismatch", "应结不符"),
                       ("extra_money", "多流水")):
        for row in result[key][:8]:
            number = row.get("posting_number") or row.get("posting_number")
            if key == "fee_mismatch":
                detail = f"记的佣金 {row.get('expected_fee')}｜实际 {row.get('actual_fee')}"
            elif key == "payout_mismatch":
                detail = f"应结 {row.get('expected_payout')}｜实结 {row.get('net')}"
            elif key == "extra_money":
                detail = f"金额 {row.get('amount')}｜流水 {row.get('n')} 条"
            else:
                detail = f"应收 {row.get('expected')}｜收入流水 {row.get('collected')}"
            print(f"   [{title}] {number}｜{detail}")
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(reconciliation.render_report(store, result), encoding="utf-8")
    print(f"报告写到 {out}")
    store.close()
    return 0 if result["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobagent.ozon_erp", description="Ozon 专用 ERP（自建原型）")
    parser.add_argument("--config", help="配置文件路径（默认项目 config.toml）")
    parser.add_argument("--db", help="覆盖数据库路径")
    parser.add_argument("--mock", action="store_true", help="用本地模拟卖家，不连网")
    parser.add_argument("--gaps", action="store_true", help="mock 模式：造出三类差异")
    parser.add_argument("--fail-every", type=int, default=0, dest="fail_every",
                        help="mock 模式：每 N 次调用返回一次 429，验证重试")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="看库内概况与同步游标")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="逐个端点做一次体检（真实账号接好先跑这个）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("sync", help="同步 products / orders / finance / all")
    p.add_argument("what", choices=["products", "orders", "finance", "all"])
    p.add_argument("--kind", default="fbs", choices=["fbs", "fbo"], help="订单类型")
    p.add_argument("--since", default="", help="起始时间（ISO8601，留空按游标+重叠窗口）")
    p.add_argument("--backfill-days", type=int, default=30, dest="backfill_days",
                   help="首次同步（没有游标）时往回补多少天")
    p.add_argument("--days-back", type=int, default=7, dest="days_back", help="财务回看天数")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("gaps", help="漏单检测")
    p.set_defaults(func=cmd_gaps)

    p = sub.add_parser("retry-failed", help="只重跑失败的那一段")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("reconcile", help="对账并出日报")
    p.add_argument("--tolerance", type=float, default=0.01, help="金额容忍差")
    p.add_argument("--report", default="data/ozon-report.md", help="报告输出路径")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("collect", help="采集商品进采集箱（清洗 + 去重）")
    p.add_argument("--show", type=int, default=12, help="打印多少条")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("publish", help="把采集箱里的商品批量上架到 Ozon")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--translate", choices=["demo", "none"], default="none",
                   help="翻译：demo 用内置词表演示，none 保留原文")
    p.add_argument("--brand", default="", help="标题重组时放在最前面的品牌")
    p.add_argument("--keywords", nargs="*", help="标题重组要带上的关键词")
    p.add_argument("--retry-failed", action="store_true", dest="retry_failed",
                   help="提交后把失败任务放回队列")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("listing-status", help="看采集箱与上架任务状态")
    p.add_argument("--show", type=int, default=15)
    p.set_defaults(func=cmd_listing_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.time()
    code = args.func(args)
    if args.command in ("sync", "retry-failed"):
        print(f"用时 {time.time() - started:.2f} 秒")
    return code
