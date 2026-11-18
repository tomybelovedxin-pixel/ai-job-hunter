"""AI 电商客服原型：意图路由 + 工具取数 + 双路检索 + 引用与拒答 + 高风险转人工。

只做可复核的那一层：规则与阈值都写在代码里，评测脚本能跑出数字。
不用大模型也成立；接上大模型只是把话术润色得更好，判定链路不变。
"""

from .engine import Agent, Answer, build_agent, run_eval

__all__ = ["Agent", "Answer", "build_agent", "run_eval"]
