"""联系/回复的增量状态：把"这次要处理的新信息"判定出来。

设计目标（对应"只处理新信息"）：
- 每条会话只在本地留一份摘要：最近一次看到的对方回复、我方条数、对方条数、看到的时间；
- 下次只看会话列表的预览文本就知道哪几条变过，只打开变过的，不重复点开全部会话；
- 状态带时间戳，超过有效期的按"可能过期"处理，不无限期复用旧数据。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from .config import ROOT

STATE_FILE = ROOT / "data" / "contact-state.json"
# 超过这个小时数没更新的摘要，不再当作"当前状态"
STALE_HOURS = 72.0
# 用户明确说"不用管了"的对象放这里，报告里不再提示跟进
MUTE_KEY = "muted"

_SPACE = re.compile(r"\s+")
# 会话列表每一行的第一行是"什么时候来的消息"：19:53 / 昨天 / 星期一 / 10-14 / 2026-10-14。
# 它是会变的，不能进会话键——否则同一条会话每次回一句就换一个键，增量判断永远全部算 new
#（2026-10-16 实测：40 条会话全被标成 new，"只处理新信息"等于失效）。
_TIME_LINE = re.compile(
    r"^(?:\d{1,2}:\d{2}|昨天|前天|今天|星期[一二三四五六日天]|\d{1,2}-\d{1,2}|\d{4}-\d{1,2}-\d{1,2})$"
)


def normalize(text: str | None) -> str:
    return _SPACE.sub("", (text or "").strip().lower())


def preview_of(row_text: str, *, limit: int = 60) -> str:
    """会话列表里一行文本的预览部分（最后一行通常是对方的话或状态）。"""
    lines = [line.strip() for line in (row_text or "").split("\n") if line.strip()]
    return lines[-1][:limit] if lines else ""


def conv_key(row_text: str) -> str:
    """会话的唯一键：列表行里那一行「姓名+公司+职位」。

    2026-10-16 实测的实际行结构是：
        ["19:53", "罗聪福州益品丰餐饮管理业务跟单员", "[送达]", "谢谢。福州坐班这条…"]
    前一行是会变的时间，后面还有会变的状态与预览；只有中间那行是人+公司+岗位，
    拿它当键才能做到"同一条会话只算一次"。
    """
    lines = [line.strip() for line in (row_text or "").split("\n") if line.strip()]
    while lines and _TIME_LINE.match(lines[0]):
        lines = lines[1:]
    return normalize(lines[0] if lines else row_text)[:60]


def load_state(path: Path | None = None) -> dict[str, Any]:
    path = Path(path or STATE_FILE)
    if not path.exists():
        return {"updated_at": "", "conversations": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"updated_at": "", "conversations": {}}
    data.setdefault("conversations", {})
    return data


def save_state(state: dict[str, Any], path: Path | None = None) -> Path:
    path = Path(path or STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def age_hours(stamp: str) -> float:
    """时间戳到现在的小时数；解析不了就当作很旧。"""
    try:
        then = time.mktime(time.strptime((stamp or "")[:19], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 1e9
    return (time.time() - then) / 3600.0


def classify_rows(rows: Iterable[dict[str, Any]], state: dict[str, Any],
                  *, stale_hours: float = STALE_HOURS) -> list[dict[str, Any]]:
    """给会话列表的每一行打标：new / changed / stale / unchanged。

    rows 里每项至少要有 text（列表行原文）；返回同样顺序的列表，附 key/status/seen。
    """
    known = state.get("conversations", {})
    out = []
    for row in rows:
        key = conv_key(row.get("text") or "")
        preview = preview_of(row.get("text") or "")
        seen = known.get(key)
        if not seen:
            status = "new"
        elif normalize(seen.get("preview", "")) != normalize(preview):
            status = "changed"
        elif age_hours(seen.get("last_seen", "")) > stale_hours:
            status = "stale"
        else:
            status = "unchanged"
        out.append({**row, "key": key, "preview": preview, "status": status,
                    "seen": seen or {}})
    return out


def record(state: dict[str, Any], *, key: str, name: str = "", company: str = "",
           job: str = "", mine: int = 0, theirs: int = 0,
           last_theirs: str = "", preview: str = "") -> None:
    """把一条会话的最新状态写进 state（调用方负责 save）。"""
    state.setdefault("conversations", {})[key] = {
        "name": name, "company": company, "job": job,
        "mine": mine, "theirs": theirs,
        "last_theirs": (last_theirs or "")[:300],
        "preview": preview,
        "last_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    convs = state.get("conversations", {})
    replied = [item for item in convs.values() if item.get("theirs")]
    return {
        "conversations": len(convs),
        "replied": len(replied),
        "updated_at": state.get("updated_at", ""),
    }


def muted_set(state: dict[str, Any]) -> set[str]:
    return {str(item).strip() for item in state.get(MUTE_KEY, []) if str(item).strip()}


def mute(state: dict[str, Any], *names: str) -> list[str]:
    """把对象标记为"不再跟进"（用户明确说过不用管的）。返回当前完整名单。"""
    current = muted_set(state)
    for name in names:
        cleaned = (name or "").strip()
        if cleaned:
            current.add(cleaned)
    state[MUTE_KEY] = sorted(current)
    return state[MUTE_KEY]


def is_muted(name: str, muted: set[str]) -> bool:
    """公司名是否在"不再跟进"名单里（双向包含，兼容截断名）。"""
    target = (name or "").strip()
    if not target or not muted:
        return False
    return any(item in target or target in item for item in muted)
