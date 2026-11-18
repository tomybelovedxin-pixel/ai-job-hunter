"""对外消息的共用规则入口。

单一事实来源是本机全局规则 ``~/.codex/AGENTS.md``。这里只做三件事：

1. 读取并压缩规则，给"本来就要调用模型"的步骤当 system 提示；
2. 判断当前语境下这条消息该不该提问；
3. 发送前的统一检查（复用 msgquality，并补上"无必要追问"和"多轮重新自我介绍"）。

不调用模型、不联网；模板路径只做本地判断，毫秒级开销。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .msgquality import check_message, char_len

_CACHE: dict[str, Any] = {"mtime": None, "text": "", "compact": ""}


def rules_path() -> Path:
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return Path(home) / "AGENTS.md"


def load_rules() -> str:
    """读全局规则；文件变了才重读。"""
    path = rules_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    if _CACHE["mtime"] != mtime:
        try:
            _CACHE["text"] = path.read_text(encoding="utf-8")
        except OSError:
            _CACHE["text"] = ""
        _CACHE["mtime"] = mtime
        _CACHE["compact"] = ""
    return _CACHE["text"]


def _section(text: str, heading: str) -> str:
    match = re.search(re.escape(heading) + r"(.*?)(?=\n## |\Z)", text, re.S)
    return match.group(1).strip() if match else ""


def compact_rules(*, limit: int = 900) -> str:
    """从全局规则里抽"直接回答 + 真实性"两节，压到 900 字以内。"""
    text = load_rules()
    if not text:
        return ""
    if _CACHE["compact"]:
        return _CACHE["compact"]
    parts = []
    for heading in ("## 1. 直接回答", "## 4. 真实性优先"):
        body = _section(text, heading)
        if body:
            parts.append(body)
    out = "\n".join(parts).strip()
    if len(out) > limit:
        cut = out.rfind("\n", 0, limit)
        out = out[: cut if cut > limit * 0.6 else limit].rstrip()
    _CACHE["compact"] = out
    return out


def system_prompt(register: str = "对外消息") -> str:
    """给已调用模型的步骤用的 system 提示；规则缺失时退化为一句最小约束。"""
    rules = compact_rules()
    if not rules:
        rules = ("先回答对方真正问的那件事；不写客套开场和结尾；不编造事实、数字和经历；"
                 "不确定就说不确定。")
    return (
        f"按下面这份本机全局表达规则写「{register}」。用户当前的要求优先。\n\n"
        f"{rules}\n\n"
        "场景补充：只在需要推进对话时才提问；对方只是陈述或已给出信息时不追问。"
        "不要复述对方已知的信息，不要每条都重新自我介绍。"
    )


_QUESTION_WORDS = re.compile(
    r"(吗|呢|什么|怎么|如何|哪|谁|多少|几个|是否|能不能|会不会|有没有|多久|"
    r"可不可以|行不行|要不要|需不需要)"
)


def needs_question(last_theirs: str, stage: str = "") -> bool:
    """对方在提问、或这一步确实缺信息时才需要回问。"""
    text = (last_theirs or "").strip()
    if not text:
        return False
    if "?" in text or "？" in text:
        return True
    if _QUESTION_WORDS.search(text):
        return True
    return False


_SELF_INTRO = re.compile(r"(我叫|我是做|我有.{0,4}年|三年|五年|十年).{0,20}(经验|开发|后端|前端)")


def check_outgoing(text: str, *, stage: str = "reply", may_ask: bool = False) -> dict[str, Any]:
    """发送前的统一检查：msgquality + 追问/复述类问题。"""
    result = check_message(
        text, stage="first_contact" if stage in ("first_contact", "first") else "reply"
    )
    issues = list(result.get("issues") or [])
    questions = (text or "").count("？") + (text or "").count("?")
    if questions and not may_ask:
        issues.append({"kind": "无必要追问",
                       "detail": f"{questions} 个问号，但当前语境不需要提问"})
    if stage not in ("first_contact", "first") and _SELF_INTRO.search(text or ""):
        issues.append({"kind": "多轮重复自我介绍", "detail": "对方已知的信息不必再说一遍"})
    return {"ok": not issues, "issues": issues,
            "metrics": {**result.get("metrics", {}), "chars": char_len(text or "")}}
