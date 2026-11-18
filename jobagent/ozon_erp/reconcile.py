"""对账与报表：订单应收 vs 财务流水实收，差异分类列清楚。

差异分四类，每类都能落到具体邮包号：
- missing_money   订单有、财务一条没有（未结算或真缺账）
- amount_mismatch 实收净额与订单金额对不上（超出容忍值）
- fee_mismatch    实际扣的佣金/服务费与订单里记的佣金对不上
- extra_money     财务有流水、订单库里没有这条邮包（订单同步漏了，真漏单）
"""

from __future__ import annotations

import time
from typing import Any

from .store import OzonStore

TOLERANCE = 0.01


def reconcile(store: OzonStore, *, tolerance: float = TOLERANCE) -> dict[str, Any]:
    ledger = store.posting_ledger()
    missing, mismatch, fee_mismatch, payout_mismatch = [], [], [], []
    for row in ledger:
        if row["lines"] == 0:
            missing.append(row)
            continue
        if abs(row["diff"]) > tolerance:
            mismatch.append(row)
        if row["expected_fee"] and abs(row["fees"] - row["expected_fee"]) > tolerance:
            fee_mismatch.append({**row, "actual_fee": row["fees"]})
        if row["expected_payout"] and abs(row["payout_diff"]) > tolerance:
            payout_mismatch.append(row)
    extra = [dict(row) for row in store.money_without_orders()]
    return {
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "orders": len(ledger),
        "missing_money": missing,
        "amount_mismatch": mismatch,
        "fee_mismatch": fee_mismatch,
        "payout_mismatch": payout_mismatch,
        "extra_money": extra,
        "ok": not (missing or mismatch or fee_mismatch or payout_mismatch or extra),
    }


def daily_summary(store: OzonStore) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS orders, "
        "ROUND(SUM(amount), 2) AS amount, ROUND(SUM(commission), 2) AS commission, "
        "ROUND(SUM(payout), 2) AS payout FROM postings GROUP BY day ORDER BY day").fetchall()
    return [dict(row) for row in rows]


def render_report(store: OzonStore, result: dict[str, Any]) -> str:
    counts = store.counts()
    lines = [
        "# Ozon 店铺对账日报",
        "",
        f"生成时间：{result['checked_at']}",
        f"库内：商品 {counts['products']}、库存行 {counts['stocks']}、"
        f"订单 {counts['postings']}、订单明细 {counts['posting_items']}、"
        f"财务流水 {counts['finance_ops']}、未解决错误 {counts['open_errors']}",
        "",
        "## 一、按天汇总",
        "",
        "| 日期 | 订单 | 销售额 | 佣金 | 应结 |",
        "|---|---|---|---|---|",
    ]
    for row in daily_summary(store):
        lines.append(f"| {row['day']} | {row['orders']} | {row['amount']} | "
                     f"{row['commission']} | {row['payout']} |")
    if not daily_summary(store):
        lines.append("| — | 0 | 0 | 0 | 0 |")

    lines += ["", "## 二、差异",
              f"对账订单 {result['orders']} 单："
              f"缺流水 {len(result['missing_money'])}、金额不符 {len(result['amount_mismatch'])}、"
              f"佣金不符 {len(result['fee_mismatch'])}、"
              f"应结不符 {len(result['payout_mismatch'])}、"
              f"多流水 {len(result['extra_money'])}", ""]
    if result["missing_money"]:
        lines += ["### 缺流水（订单在、钱没记）", "",
                  "| 邮包号 | 下单时间 | 订单金额 | 状态 |", "|---|---|---|---|"]
        for row in result["missing_money"]:
            lines.append(f"| {row['posting_number']} | {row['created_at']} | "
                         f"{row['expected']} | {row['status']} |")
        lines.append("")
    if result["amount_mismatch"]:
        lines += ["### 金额不符", "",
                  "| 邮包号 | 应收 | 收入流水 | 差额 |", "|---|---|---|---|"]
        for row in result["amount_mismatch"]:
            lines.append(f"| {row['posting_number']} | {row['expected']} | "
                         f"{row['collected']} | {row['diff']} |")
        lines.append("")
    if result["fee_mismatch"]:
        lines += ["### 佣金/服务费不符", "",
                  "| 邮包号 | 订单记的佣金 | 流水实际扣 |", "|---|---|---|"]
        for row in result["fee_mismatch"]:
            lines.append(f"| {row['posting_number']} | {row['expected_fee']} | "
                         f"{row['actual_fee']} |")
        lines.append("")
    if result["payout_mismatch"]:
        lines += ["### 应结不符（净额与订单记的 payout 对不上）", "",
                  "| 邮包号 | 应结 | 实结 | 差额 |", "|---|---|---|---|"]
        for row in result["payout_mismatch"]:
            lines.append(f"| {row['posting_number']} | {row['expected_payout']} | "
                         f"{row['net']} | {row['payout_diff']} |")
        lines.append("")
    if result["extra_money"]:
        lines += ["### 多流水（钱到了但订单库里没有 → 订单同步漏了）", "",
                  "| 邮包号 | 金额 | 流水条数 | 最早时间 |", "|---|---|---|---|"]
        for row in result["extra_money"]:
            lines.append(f"| {row['posting_number']} | {row['amount']} | "
                         f"{row['n']} | {row['first_seen']} |")
        lines.append("")
    if result["ok"]:
        lines.append("没有问题：订单金额与财务流水逐单对得上。")
    lines.append("")
    return "\n".join(lines)
