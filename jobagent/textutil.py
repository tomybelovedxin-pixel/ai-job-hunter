"""文本处理：HTML 转文本、空白规整、描述清洗。

优先用 beautifulsoup4（若已安装），没有就退回标准库正则实现。
两条路径的输出格式一致，降级不会中断流水线。
"""

from __future__ import annotations

import html as html_module
import re

_BLOCK = re.compile(r"</(p|div|li|tr|h[1-6]|ul|ol|table|section|article)>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_SPACES = re.compile(r"[ \t\u00a0\u3000]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# 数据源里常见的双重编码（UTF-8 被当成 cp1252 解过一次），修掉再入库
_MOJIBAKE = re.compile(r"[ÃÂâ][\x80-\xbf\u0080-\u00bf]|â€|Ã©|Ã¨|Ã\xa0")

# RemoteOK 会在正文尾部塞一段防爬标记，取数时去掉，避免污染 JD 正文
_REMOTEOK_TRAILER = re.compile(
    r"\s*Please mention the word.*$", re.I | re.S)


def bs4_available() -> bool:
    try:
        import bs4  # noqa: F401
    except Exception:
        return False
    return True


def html_to_text(raw: str | None) -> str:
    """HTML 转纯文本。保留段落换行，去掉脚本样式和多余空白。"""
    if not raw:
        return ""
    text = raw
    if bs4_available():
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(text, "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = soup.get_text("\n")
        except Exception:
            text = _regex_text(raw)
    else:
        text = _regex_text(raw)
    return clean_text(text)


def _regex_text(raw: str) -> str:
    text = _SCRIPT.sub(" ", raw)
    text = _BR.sub("\n", text)
    text = _BLOCK.sub("\n", text)
    text = _TAG.sub("", text)
    return html_module.unescape(text)


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACES.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    keep: list[str] = []
    for line in lines:
        if line or (keep and keep[-1]):
            keep.append(line)
    return _BLANK_LINES.sub("\n\n", "\n".join(keep)).strip()


def strip_source_trailer(text: str) -> str:
    """去掉数据源自带的推广/防爬尾注。"""
    if not text:
        return text
    return _REMOTEOK_TRAILER.sub("", text).strip()


def fix_mojibake(text: str) -> str:
    """修双重编码。只在确认能还原出更合理的文本时才采用结果。"""
    if not text or not _MOJIBAKE.search(text):
        return text
    for source, target in (("cp1252", "utf-8"), ("latin-1", "utf-8")):
        try:
            repaired = text.encode(source, "strict").decode(target, "strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired.count("\ufffd") or len(repaired) < len(text) * 0.6:
            continue
        return repaired
    return text


def one_line(text: str, limit: int = 160) -> str:
    text = clean_text(text).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# BOSS 一类站点把数字做成图标字体，抓下来是私有区码点（如 \ue035 表示 4），
# 不解码就会显示成方框。这里按 0–9 的对应关系还原。
_PUA_DIGITS = {chr(0xE031 + index): str(index) for index in range(10)}
_PUA_PATTERN = re.compile("[" + "".join(_PUA_DIGITS) + "]")


def decode_pua_digits(text: str | None) -> str:
    if not text:
        return text or ""
    return _PUA_PATTERN.sub(lambda match: _PUA_DIGITS[match.group(0)], text)


def has_pua_digits(text: str | None) -> bool:
    return bool(text) and bool(_PUA_PATTERN.search(text or ""))
