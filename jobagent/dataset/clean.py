"""语料清洗与去重（纯标准库）。

处理的对象是"要拿去训练的文本"：先把格式噪声去掉，再压掉空行和样板句，
然后做两级去重——完全相同的一份只留一次，近似重复（改了几个字、换了空白）按
shingle 集合的 Jaccard 相似度判，超过阈值只留信息量更大的那一份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

TEXT_SUFFIXES = (".md", ".txt", ".jsonl", ".rst")
ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff]")
MULTI_SPACE = re.compile(r"[ \t\u3000]{2,}")
MULTI_NEWLINE = re.compile(r"\n{3,}")
HTML_TAG = re.compile(r"<[^>]{1,40}>")

# 样板句：页脚、免责声明、导航、来源说明——这类句子重复率高，对训练没价值
BOILERPLATE = re.compile(
    r"(版权|免责声明|版权所有|转载|扫码关注|点击(这里|上方)|关注我们|返回顶部|"
    r"Powered by|copyright|all rights reserved|隐私政策|使用条款|京ICP|闽ICP)",
    re.I)


@dataclass
class Document:
    doc_id: str
    source: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def sentences(self) -> list[str]:
        return [s for s in re.split(r"[。！？!?\n]+", self.text) if len(s.strip()) >= 4]

    def shingles(self, size: int = 3) -> set[str]:
        """按字符切 shingle：中文没有词边界，用字符 n-gram 更稳。"""
        clean = re.sub(r"\s+", "", self.text)
        return {clean[i:i + size] for i in range(max(0, len(clean) - size + 1))}

    def as_dict(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "source": self.source, "chars": self.chars,
                "sentences": len(self.sentences), "text": self.text[:400], **self.meta}


def normalize(text: str) -> str:
    """统一换行与空白、去掉零宽字符和 HTML 残片，不改动任何事实性内容。"""
    result = ZERO_WIDTH.sub("", text or "")
    result = result.replace("\r\n", "\n").replace("\r", "\n")
    result = HTML_TAG.sub("", result)
    result = MULTI_SPACE.sub(" ", result)
    result = MULTI_NEWLINE.sub("\n\n", result)
    return result.strip()


def drop_boilerplate(text: str) -> str:
    """去掉样板行和过短的行，保留段落结构。"""
    kept: list[str] = []
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        if BOILERPLATE.search(stripped):
            continue
        if len(re.sub(r"[\W_]+", "", stripped)) < 3:   # 只剩符号/编号的行
            continue
        kept.append(stripped)
    return MULTI_NEWLINE.sub("\n\n", "\n".join(kept)).strip()


def load_documents(root: str | Path, *, suffixes: Iterable[str] = TEXT_SUFFIXES,
                   min_chars: int = 120) -> list[Document]:
    """把目录里的文本读成文档对象（跳过太短的和样板内容）。"""
    base = Path(root)
    docs: list[Document] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in tuple(suffixes):
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = drop_boilerplate(normalize(raw))
        if len(text) < min_chars:
            continue
        docs.append(Document(doc_id=str(path.relative_to(base)).replace("\\", "/"),
                             source=str(path), text=text))
    return docs


def split_paragraphs(doc: Document, *, min_chars: int = 60) -> list[Document]:
    """按空行切成段落级样本：训练语料一般按段落组织，不按整份文件。"""
    parts: list[Document] = []
    for index, chunk in enumerate(re.split(r"\n{2,}", doc.text)):
        chunk = chunk.strip()
        if len(chunk) < min_chars:
            continue
        parts.append(Document(doc_id=f"{doc.doc_id}#{index + 1}", source=doc.source,
                              text=chunk, meta={"from": doc.doc_id}))
    return parts


def exact_dedupe(docs: list[Document]) -> tuple[list[Document], list[tuple[str, str]]]:
    """完全相同正文只留第一份。返回 (保留的文档, [(被丢的 doc_id, 留下的 doc_id)])。"""
    seen: dict[str, str] = {}
    kept: list[Document] = []
    dropped: list[tuple[str, str]] = []
    for doc in docs:
        key = re.sub(r"\s+", "", doc.text)
        if key in seen:
            dropped.append((doc.doc_id, seen[key]))
            continue
        seen[key] = doc.doc_id
        kept.append(doc)
    return kept, dropped


def similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def near_dupes(docs: list[Document], *, threshold: float = 0.8) -> list[dict[str, Any]]:
    """近似重复检测：返回超过阈值的文档对（含相似度）。"""
    sets = [(doc, doc.shingles()) for doc in docs]
    pairs: list[dict[str, Any]] = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            score = similarity(sets[i][1], sets[j][1])
            if score >= threshold:
                pairs.append({"a": sets[i][0].doc_id, "b": sets[j][0].doc_id,
                              "score": round(score, 3)})
    return sorted(pairs, key=lambda item: -item["score"])


def drop_near_dupes(docs: list[Document], *, threshold: float = 0.8
                    ) -> tuple[list[Document], list[dict[str, Any]]]:
    """近似重复只留信息量更大的那一份（更长的），并记录被丢掉的。"""
    kept: list[Document] = []
    dropped: list[dict[str, Any]] = []
    shingles: list[set[str]] = []
    for doc in docs:
        current = doc.shingles()
        duplicate_of = None
        for index, existing in enumerate(shingles):
            score = similarity(current, existing)
            if score >= threshold:
                duplicate_of = (index, score)
                break
        if duplicate_of is None:
            kept.append(doc)
            shingles.append(current)
            continue
        index, score = duplicate_of
        other = kept[index]
        if doc.chars > other.chars:          # 更长的那份留下来
            dropped.append({"dropped": other.doc_id, "kept": doc.doc_id,
                            "score": round(score, 3)})
            kept[index] = doc
            shingles[index] = current
        else:
            dropped.append({"dropped": doc.doc_id, "kept": other.doc_id,
                            "score": round(score, 3)})
    return kept, dropped
