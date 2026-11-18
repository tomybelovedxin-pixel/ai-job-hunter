"""发送守卫：把"这条还要不要发"的判断从模型手里拿走，写成可测的纯函数。

背景（2026-10-14 真实事故）：回读校验没通过时发送工具会回退回车，
结果是同一段话术发了两遍。这里只做一件事——发送前后都用会话文本自己核对，
发送状态不明确时以"会话里已经出现这条话术"为准，不重复发。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable

# 比对用的最短长度：太短的（"你好""在吗"）不参与"是否已发过"的判断，
# 否则会把默认招呼语误判成我们发过的定制话术。
MIN_COMPARE_CHARS = 12
# 两条话术的相似度阈值：超过就当成同一条
SAME_RATIO = 0.92

_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[，。、；：！？,.;:!?~～…\-—_（）()\[\]【】\"'“”‘’]")


def normalize(text: str | None) -> str:
    """把一条消息压成可比较的形式：去空白、去标点。"""
    if not text:
        return ""
    return _PUNCT.sub("", _SPACE.sub("", text)).lower()


def is_same_message(a: str | None, b: str | None) -> bool:
    """判断两条消息是不是同一条（归一化后相等，或高度相似）。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = sorted((na, nb), key=len)
    if len(shorter) < MIN_COMPARE_CHARS:
        return False
    if shorter in longer and len(shorter) / len(longer) >= SAME_RATIO:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= SAME_RATIO


def already_sent(mine: Iterable[str], text: str) -> bool:
    """这条话术是不是已经在会话里出现过了（只比对我方气泡）。"""
    return any(is_same_message(item, text) for item in mine or [])


def last_mine(mine: Iterable[str]) -> str:
    items = list(mine or [])
    return items[-1] if items else ""


def decide_send(state: dict[str, Any], text: str) -> dict[str, Any]:
    """发送前的判断。state 形如 {mine: [...], theirs: [...], input_len: int}。

    返回 {"action": "skip"|"send", "reason": "..."}：
    - 会话里已经出现过这条话术 → skip（不重复发）
    - 输入框里还留着这条话术、但会话里没有 → 上一次很可能没发出去，按 send 处理
    """
    mine = state.get("mine") or []
    if already_sent(mine, text):
        return {"action": "skip", "reason": "会话里已经有同一条话术，不再重复发送"}
    return {"action": "send", "reason": "会话里没有这条话术"}


def verdict_after_send(verified: bool, delivered: bool) -> str:
    """发送结果 → 消息状态：sent / unverified / failed。

    关键点：回读没通过时给 unverified（状态不明），不是 failed。
    未知状态不能进"失败重发"池，否则就是盲目重发。
    """
    if verified:
        return "sent"
    if delivered:
        return "unverified"
    return "failed"


STATUS_LABELS = {
    "draft": "待发送",
    "sent": "已发送（回读校验通过）",
    "unverified": "状态不明（已点发送，回读没确认）",
    "failed": "发送失败（没点成发送）",
}
