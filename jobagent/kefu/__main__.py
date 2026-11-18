"""命令行入口：python -m jobagent.kefu ask|eval|demo|tools

  ask "问题"      问一句，打印回答、来源与判定轨迹
  eval            跑一遍评测用例，出三项指标
  demo            跑一段固定对话，看完整链路
  tools           看四个工具各自的调用留痕
"""

from __future__ import annotations

import sys

from . import data as kb_data
from .engine import build_agent, run_eval


def print_answer(result) -> None:
    print(f"\n【提问】{result.query}")
    print(f"【意图】{result.intent}    置信度 {result.confidence}"
          f"{'    已转人工：' + result.escalate_reason if result.escalated else ''}")
    print("【回答】")
    for line in result.reply.split("\n"):
        print("   " + line)
    print("【来源】" + ("；".join(result.citations) if result.citations else "无（拒答不留来源）"))
    for step in result.trace:
        print("   · " + step)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "demo"

    if action == "ask":
        if len(argv) < 2:
            print('用法: python -m jobagent.kefu ask "你们的退款多久到账"')
            return 2
        agent = build_agent()
        print_answer(agent.answer(" ".join(argv[1:])))
        return 0

    if action == "eval":
        result = run_eval()
        print(f"用例 {result['total']} 条")
        print(f"意图准确率  {result['intent']['ok']}/{result['intent']['total']}"
              f"  {result['intent']['rate']:.1%}")
        print(f"回答正确率  {result['answer']['ok']}/{result['answer']['total']}"
              f"  {result['answer']['rate']:.1%}")
        print(f"拒答正确率  {result['refuse']['ok']}/{result['refuse']['total']}"
              f"  {result['refuse']['rate']:.1%}")
        print(f"转人工工单  {result['tickets']} 张")
        if result["false_refuse"]:
            print("误拒答：" + "；".join(result["false_refuse"]))
        if result["failures"]:
            print("\n未通过：")
            for item in result["failures"]:
                print(f"  - {item['q']} → {item['why']}")
        else:
            print("\n全部用例通过")
        return 0

    if action == "tools":
        agent = build_agent()
        for query in ["静音落地风扇 A100 有货吗", "202610120001 现在到哪了"]:
            agent.answer(query)
        for row in agent.tools.log:
            print(row)
        return 0

    # demo：一段固定对话，覆盖回答、拒答、转人工三种走向
    agent = build_agent()
    scripted = [
        "下单多久发货",
        "静音落地风扇 A100 多少钱",
        "折叠电脑桌 B200 有货吗",
        "202610120001 现在到哪了",
        "七天无理由退货运费谁承担",
        "改收货地址怎么弄",
        "帮我写一段产品宣传文案",
    ]
    for query in scripted:
        print_answer(agent.answer(query))
        print("-" * 60)
    print(f"本轮共 {len(agent.tools.log)} 次工具调用，{len(agent.tickets)} 张转人工工单")
    print(f"知识库文档 {len(kb_data.DOCS)} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
