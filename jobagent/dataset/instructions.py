"""把清洗后的语料做成指令样本（instruction / input / output）。

这里不生成"凭空编造"的答案：答案一律从原文里抽，用确定性的规则产出，
训练用途是"让模型学会按格式抽取和整理"，不是教它编事实。
四类任务，每类都有明确判定标准（tests/dataset_check.py 会验）：

1. 概括  —— 输出取原文前两句，要求覆盖开头的核心信息
2. 抽取  —— 给定字段名，输出从原文里抽出来的对应句子；抽不到就写"未提及"
3. 改写  —— 把口语化的句子改成书面句子（数字、名称一律保留）
4. 判断  —— 给定判定标准，输出"符合/不符合 + 依据句"
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .clean import Document

FIELDS = {
    "要求": re.compile(r"(要求|需要|必须|熟悉|掌握|具备)"),
    "职责": re.compile(r"(负责|参与|承担|维护|搭建)"),
    "地点": re.compile(r"(泉州|厦门|福州|晋江|石狮|南安|远程)"),
}


def _sentences(doc: Document) -> list[str]:
    parts = [s.strip() for s in re.split(r"(?<=[。！？；])", doc.text) if len(s.strip()) >= 8]
    return parts or [doc.text.strip()]


def _summarize_sample(doc: Document) -> dict[str, Any] | None:
    sentences = _sentences(doc)
    if len(sentences) < 2:
        return None
    head = "".join(sentences[:2])
    return {
        "task": "概括",
        "instruction": "用两句话概括下面这段材料的主要内容，不要添加材料里没有的信息。",
        "input": doc.text[:600],
        "output": head[:200],
        "source": doc.doc_id,
    }


def _extract_sample(doc: Document) -> dict[str, Any] | None:
    sentences = _sentences(doc)
    for field, pattern in FIELDS.items():
        hit = next((s for s in sentences if pattern.search(s)), "")
        if not hit:
            continue
        return {
            "task": "抽取",
            "instruction": f"从下面的材料里找出与「{field}」有关的一句原文；找不到就回答“未提及”。",
            "input": doc.text[:600],
            "output": hit[:160],
            "source": doc.doc_id,
        }
    return None


def _rewrite_sample(doc: Document) -> dict[str, Any] | None:
    """口语改书面：只做确定的替换（不改变事实），输出仍来自原文。"""
    sentences = [s for s in _sentences(doc) if re.search(r"(挺|弄|搞|咋|咱|啥)", s)]
    if not sentences:
        return None
    raw = sentences[0]
    polished = (raw.replace("挺", "比较").replace("弄", "处理").replace("搞", "做")
                .replace("咋", "怎么").replace("咱", "我们").replace("啥", "什么"))
    if polished == raw:
        return None
    return {
        "task": "改写",
        "instruction": "把下面这句改成书面表达，数字和名称必须原样保留。",
        "input": raw[:200],
        "output": polished[:200],
        "source": doc.doc_id,
    }


def _judge_sample(doc: Document) -> dict[str, Any] | None:
    sentences = _sentences(doc)
    if len(sentences) < 3:
        return None
    rule = "这段材料里是否明确提到了时间或数量"
    hit = next((s for s in sentences if re.search(r"(\d+|每天|每周|每月|小时|分钟)", s)), "")
    verdict = "符合" if hit else "不符合"
    return {
        "task": "判断",
        "instruction": f"按下面的标准判断材料：{rule}。先回答“符合”或“不符合”，再给依据句。",
        "input": doc.text[:600],
        "output": f"{verdict}。依据：{hit[:120]}" if hit else f"{verdict}。材料里没有出现时间或数量。",
        "source": doc.doc_id,
    }


BUILDERS = (_summarize_sample, _extract_sample, _rewrite_sample, _judge_sample)


def build_instructions(docs: list[Document]) -> list[dict[str, Any]]:
    """每份文档尽量产出一类样本，四类轮流覆盖。"""
    samples: list[dict[str, Any]] = []
    for index, doc in enumerate(docs):
        for offset in range(len(BUILDERS)):
            builder = BUILDERS[(index + offset) % len(BUILDERS)]
            sample = builder(doc)
            if sample:
                samples.append(sample)
                break
    return samples


def write_jsonl(rows: list[dict[str, Any]], path: str | Path) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return str(target)
