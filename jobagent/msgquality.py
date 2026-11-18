"""对外消息的质量检查（规则层，可离线跑）。

检查的是"这条消息能不能直接发给招聘方"，不是文风打分。口径来自用户 2026-10-17 的
任务书和本仓库的既有约定：

1. 首条 50—120 个汉字（必要时可长一点，但不能为了显专业堆词）；
2. 一条消息最多突出 1—2 个优势点，不罗列技术名词；
3. 不重复用同一套开头、结尾和固定提问；
4. 不写内部术语（RRF 融合 / 双路召回 / 输出约束链 / 依据闸门 这类）；
5. 不虚构经历：把"待验证/AI 辅助能做"说成"做过"就是不合格；
6. 只在能推动对话时提问，最多一个问题。

单条检查用 `check_message`，一批消息之间的重复度用 `check_batch`。
两个函数都只报告问题，不改写消息。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# 首条的长度区间（汉字计；英文和数字按 0.5 个汉字折算，避免技术名词把长度撑虚）
FIRST_MIN, FIRST_MAX = 50, 120
FIRST_HARD_MAX = 150
REPLY_MIN, REPLY_MAX = 25, 200

# 用户点名的固定开头：不能每条消息都用
BANNED_OPENERS = (
    "看到 JD", "看到jd", "这条我直接对得上", "这类活我做过", "你们 JD 里有一条我直接对得上",
    "不寒暄了", "我说下我的情况", "先给结论，再给东西", "先给结论再给东西",
)

# 用户点名的固定提问：直接禁用
BANNED_QUESTIONS = (
    "现在最缺的是应用侧还是底层平台", "现在最缺的是应用侧，还是底层平台",
    "你们现在最急的是哪一块", "你们现在最急的是哪一块？",
    "这个岗是你在面，还是技术负责人面",
)

# 不该出现在对外消息里的内部说法（用户 2026-10-17 点名的四个，加上同类的实现细节）
INTERNAL_JARGON = (
    "RRF", "rrf", "双路召回", "输出约束链", "依据闸门", "拒答", "BM25", "bm25",
    "向量召回", "幻觉", "召回率", "证据闸门", "rubric", "A/B 级", "prompt 工程",
    "定制简历", "定制版", "（PDF）", "(PDF)",
)

# 内部术语 → 人话（生成时先把说给自己听的说法换掉，再检查）
JARGON_REWRITE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"BM25\s*与?\s*自哈希向量双路召回"), "关键词与语义两路检索"),
    (re.compile(r"BM25/向量双路|双路召回", re.I), "关键词和语义两路找"),
    (re.compile(r"RRF\s*融合", re.I), "两路结果合并"),
    (re.compile(r"低分拒答|拒答闸门|依据闸门"), "查不到依据就说不知道"),
    (re.compile(r"输出约束链|输出校验链", re.I), "输出校验"),
    (re.compile(r"证据闸门"), "证据核对"),
    (re.compile(r"自哈希向量"), "语义向量"),
    (re.compile(r"低分拒答"), "查不到依据就说不知道"),
    (re.compile(r"把?「[^」]{0,24}→[^」]{0,24}」拆成多步流水线"), "从取数到判定、生成、校验这条链"),
    (re.compile(r"多步流水线"), "一条链"),
    (re.compile(r"离线自检可复现"), "离线自检能复跑"),
]

# 不能给的承诺：没有大规模交付记录，就不把话说到这个份上
UNSUPPORTED_CLAIM = (
    "保证", "一定不会", "绝对不会", "百分百", "100% 没问题", "包您满意", "包交付",
    "精通", "资深", "专家级", "多年经验", "五年经验", "10 年经验",
)

# 自贬或主动说弱点（用户口径：缺口一个字都不提，沉默可以，自贬不行）
SELF_DEPRECATION = (
    "没有经验", "没有相关经验", "没做过但我愿意学", "愿意学习", "我们不会",
    "我不占优势", "没有企业经验", "经验不足，但", "小白",
)

QUESTION_MARK = re.compile(r"[?？]")
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
CN_CHAR = re.compile(r"[\u4e00-\u9fff]")
DIGIT_CLAIM = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?)(?![A-Za-z0-9_.])")


def char_len(text: str) -> int:
    """按"读起来多长"算长度：汉字算 1，ASCII 词与数字算半个。"""
    text = text or ""
    han = len(CN_CHAR.findall(text))
    rest = len(re.sub(r"[\u4e00-\u9fff\s]", "", text))
    return int(round(han + rest * 0.5))


def dejargon(text: str) -> str:
    """把内部实现说法换成能直接念给招聘方听的话（不改事实，只换说法）。"""
    result = text or ""
    for pattern, plain in JARGON_REWRITE:
        result = pattern.sub(plain, result)
    return result


def safe_evidence(text: str, *, limit: int = 48) -> str:
    """把内部依据原文变成能直接发出去的一句：先去术语，再逐句检查，
    仍然带内部术语的句子直接丢掉；实在剩下不了就返回空串（宁可不写）。
    """
    plain = dejargon(text or "")
    clauses = [c.strip() for c in re.split(r"[；;。\n]", plain) if c.strip()]
    kept: list[str] = []
    for clause in clauses:
        if any(word in clause for word in INTERNAL_JARGON):
            continue
        kept.append(clause)
        if sum(len(c) for c in kept) >= limit:
            break
    out = "；".join(kept)
    return out[:limit].strip("；，,")


# 引子：这些是换着用的连接语，指纹里要跳过它们
CONNECTOR_PREFIXES = (
    "看到你们招", "看到你们在做", "看到你们在招", "刷到你们这个岗", "这个岗跟我做的事挺像",
    "你们招的这个方向我一直在做", "这个岗跟我做的东西挺像",
)


def _normalize_head(text: str, width: int = 6) -> str:
    """取开头若干个字做指纹：去掉称呼、小引子、标点和空白。

    引子（"看到你们招 X""刷到你们这个岗"）本来就是要换着用的固定连接语，
    不该算成"开头重复"；真正要比的是引子之后那句实话怎么起头。
    """
    stripped = re.sub(r"^(你好|您好|hi|hello)[，,。！!~ ]*", "", (text or "").strip(), flags=re.I)
    for prefix in CONNECTOR_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    stripped = re.sub(r"[\s，,。！!~「」“”\"'（）()]+", "", stripped)
    return stripped[:width]


def head_key(text: str, width: int = 6) -> str:
    """对外公开的开头指纹：生成和检查用同一个口径，避免"生成时算宽松、检查时算严格"。"""
    return _normalize_head(text, width)


def _normalize_tail(text: str, width: int = 8) -> str:
    """取结尾的指纹：整段最后一句（去标点）的前若干个字。"""
    parts = [p for p in re.split(r"[。！？!?；;\n]+", (text or "").strip()) if p.strip()]
    if not parts:
        return ""
    tail = re.sub(r"[\s，,。！!~「」“”\"'（）()]+", "", parts[-1])
    return tail[:width]


def bigrams(text: str) -> set[str]:
    clean = re.sub(r"[\s，,。！!？?；;：:「」“”\"'（）()、·\-—]+", "", text or "")
    return {clean[i:i + 2] for i in range(max(0, len(clean) - 1))}


def similarity_sets(left: set[str], right: set[str]) -> float:
    """已经算好 2-gram 集合时的相似度（批量打分时用这个，别重复切词）。"""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def similarity(left: str, right: str) -> float:
    """两条消息的 2-gram Jaccard 相似度：用来发现"换汤不换药"的群发感。"""
    return similarity_sets(bigrams(left), bigrams(right))


def check_message(
    text: str,
    *,
    stage: str = "first_contact",
    focus_count: int = 0,
    verified_claims: Iterable[str] = (),
    unverified_claims: Iterable[str] = (),
    enforce_number_sources: bool = False,
) -> dict[str, Any]:
    """检查一条消息。返回 {ok, issues:[{kind, detail}], metrics}。

    focus_count：这条消息突出了几个优势点（由生成方给出，用于判断"最多两个"）。
    verified_claims / unverified_claims：消息里出现的、分别有证据和没证据的能力说法。
    没证据的说法出现在消息里 → 直接判不合格（不许把待验证说成已验证）。
    enforce_number_sources：打开后，正文里的阿拉伯数字必须能在 verified_claims 找到；
    这是给自动入队用的硬闸门，普通人工文本检查默认不启用。
    """
    text = (text or "").strip()
    issues: list[dict[str, str]] = []
    first = stage in ("first_contact", "first")
    low, high, hard = ((FIRST_MIN, FIRST_MAX, FIRST_HARD_MAX) if first
                       else (REPLY_MIN, REPLY_MAX, REPLY_MAX))
    length = char_len(text)

    if not text:
        issues.append({"kind": "空消息", "detail": "没有正文"})
    if length < low:
        issues.append({"kind": "太短", "detail": f"{length} 字，低于 {stage} 的下限 {low}"})
    if length > high:
        issues.append({"kind": "偏长", "detail": f"{length} 字，超过 {high}（硬上限 {hard}）"})
    if length > hard:
        issues.append({"kind": "超硬上限", "detail": f"{length} 字，超过 {hard}"})

    if PLACEHOLDER.search(text):
        issues.append({"kind": "占位符没填", "detail": PLACEHOLDER.search(text).group(0)})

    head = _normalize_head(text, 12)
    for phrase in BANNED_OPENERS:
        if phrase in head:
            issues.append({"kind": "固定开头", "detail": phrase})
            break
    for phrase in BANNED_QUESTIONS:
        if phrase in text:
            issues.append({"kind": "固定提问", "detail": phrase})
            break
    for phrase in INTERNAL_JARGON:
        if phrase in text:
            issues.append({"kind": "内部术语", "detail": phrase})
            break
    for phrase in UNSUPPORTED_CLAIM:
        if phrase in text:
            issues.append({"kind": "过度承诺", "detail": phrase})
            break
    for phrase in SELF_DEPRECATION:
        if phrase in text:
            issues.append({"kind": "自贬/说弱点", "detail": phrase})
            break

    questions = len(QUESTION_MARK.findall(text))
    if questions > 1:
        issues.append({"kind": "问题太多", "detail": f"{questions} 个问号"})
    if focus_count > 2:
        issues.append({"kind": "优势点太多", "detail": f"突出了 {focus_count} 个"})

    sentences = [s.strip() for s in re.split(r"[。！？!?\n]+", text)
                 if s.strip()]
    openers: list[str] = []
    for sentence in sentences:
        match = re.match(r"^([\u4e00-\u9fff]|[A-Za-z]+)", sentence)
        openers.append(match.group(1) if match else "")
    repeated = next((opener for opener in ("我", "这块", "这个")
                     if opener and openers.count(opener) >= 3), "")
    if repeated:
        issues.append({"kind": "句式节奏重复",
                       "detail": f"{repeated}开头连续出现 {openers.count(repeated)} 次"})

    verified_text = "\n".join(str(claim or "") for claim in verified_claims)
    if enforce_number_sources:
        allowed = set(DIGIT_CLAIM.findall(verified_text))
        unsourced = [value for value in DIGIT_CLAIM.findall(text) if value not in allowed]
        if unsourced:
            issues.append({"kind": "无来源数字",
                           "detail": "、".join(dict.fromkeys(unsourced))})

    for claim in unverified_claims:
        if claim and claim in text:
            issues.append({"kind": "把待验证说成已具备", "detail": claim})
            break

    metrics = {
        "chars": length,
        "questions": questions,
        "focus_count": focus_count,
        "head": head,
        "tail": _normalize_tail(text),
        "sentence_openers": openers,
        "unsourced_numbers": ([value for value in DIGIT_CLAIM.findall(text)
                               if value not in set(DIGIT_CLAIM.findall(verified_text))]
                              if enforce_number_sources else []),
        "verified_hits": [c for c in verified_claims if c and c in text],
    }
    return {"ok": not issues, "issues": issues, "metrics": metrics}


def check_batch(messages: list[dict[str, Any]], *, similarity_limit: float = 0.5,
                head_limit: int = 1, tail_limit: int = 1) -> dict[str, Any]:
    """检查一批消息之间的重复度（这是"一天几十家收到同一段话"的直接测法）。

    messages 元素：{"text": str, "label": str, "stage": str}
    返回：开头指纹重复、结尾指纹重复、两两相似度超限、以及最像的一对。
    """
    heads: dict[str, list[str]] = {}
    tails: dict[str, list[str]] = {}
    for item in messages:
        text = item.get("text") or ""
        label = item.get("label") or ""
        head = _normalize_head(text)
        tail = _normalize_tail(text)
        if head:
            heads.setdefault(head, []).append(label)
        if tail:
            tails.setdefault(tail, []).append(label)

    head_dupes = {k: v for k, v in heads.items() if len(v) > head_limit}
    tail_dupes = {k: v for k, v in tails.items() if len(v) > tail_limit}

    worst = {"a": "", "b": "", "score": 0.0}
    over: list[dict[str, Any]] = []
    for i in range(len(messages)):
        for j in range(i + 1, len(messages)):
            left, right = messages[i], messages[j]
            score = similarity(left.get("text") or "", right.get("text") or "")
            if score > worst["score"]:
                worst = {"a": left.get("label") or "", "b": right.get("label") or "",
                         "score": round(score, 3)}
            if score > similarity_limit:
                over.append({"a": left.get("label") or "", "b": right.get("label") or "",
                             "score": round(score, 3)})

    return {
        "count": len(messages),
        "head_dupes": head_dupes,
        "tail_dupes": tail_dupes,
        "similar_pairs": over[:10],
        "worst_pair": worst,
        "ok": not head_dupes and not tail_dupes and not over,
        "similarity_limit": similarity_limit,
    }


def audit_text_quality(text: str) -> dict[str, Any]:
    """顺带跑一遍本仓库既有的"去 AI 味"规则，内部术语和套话命中一起报出来。"""
    from .textquality import scan

    return scan(text)
