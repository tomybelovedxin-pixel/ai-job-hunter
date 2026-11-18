"""中文文本质量检查（规则层）。

规则来自本机那套自然表达系统（去ai味道项目）的思路：把"AI 腔"拆成可检查的痕迹，
命中就指出来，不为了骗过检测器改写，也不给分数排行榜。
同一套规则同时服务简历、求职信和聊天话术。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# (类别, 正则, 建议)
TELLS: list[tuple[str, str, str]] = [
    ("套话", r"综上所述|总而言之|总的来说|值得注意的是|不难看出|由此可见",
     "总结句是空转，说完就停"),
    ("套话", r"首先[，,].{0,40}其次|其一[，,].{0,30}其二|最后[，,].{0,20}总之",
     "连接词堆叠，让内容自己带出顺序"),
    ("空词", r"赋能|闭环|深度融合|全面提升|全方位|多维度|一站式|生态化|底层逻辑",
     "没有信息量的大词，换成具体做了什么"),
    ("自我评价", r"具备优秀的?|出色的?沟通能力|学习能力强|责任心强|抗压能力强|富有激情",
     "自我评价代替不了事实，写做过什么"),
    ("并列三项", r"：([^：。；\n]{1,18})、([^：。；\n]{1,18})、([^：。；\n]{1,18})",
     "冒号后固定三项是机器节奏，有几项写几项"),
    ("通用收尾", r"希望对你有帮助|如有需要.{0,6}随时|仅供参考|欢迎随时联系我",
     "通用收尾没有信息量，换成具体的下一步"),
    ("复述开场", r"^(你说的|您提到的|关于你提到的).{0,30}[，,。]",
     "先把对方的话复述一遍再回答，人很少这么写"),
    ("空泛肯定", r"非常(有价值|有意义|重要)|思路很(清晰|棒)|这是个好问题",
     "空泛的肯定等于没回答"),
]

LOGICAL_OPENERS = ("因此", "所以", "然而", "并且", "同时", "此外")


@dataclass
class TextIssue:
    kind: str
    hit: str
    suggestion: str
    index: int

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "hit": self.hit, "suggestion": self.suggestion,
                "index": self.index}


def scan(text: str) -> dict[str, Any]:
    """扫一遍文本，返回命中项和可读性统计。不做改写。"""
    text = text or ""
    issues: list[TextIssue] = []
    for kind, pattern, suggestion in TELLS:
        for match in re.finditer(pattern, text):
            issues.append(TextIssue(kind, match.group(0)[:40], suggestion, match.start()))

    sentences = [s for s in re.split(r"[。！？\n]+", text) if s.strip()]
    lengths = [len(s) for s in sentences] or [0]
    avg = sum(lengths) / len(lengths)
    variance = sum((length - avg) ** 2 for length in lengths) / len(lengths)
    opener_count = sum(1 for s in sentences if s.strip().startswith(LOGICAL_OPENERS))
    return {
        "chars": len(text),
        "sentences": len(sentences),
        "avg_sentence": round(avg, 1),
        "length_variance": round(variance, 1),
        "logical_openers": opener_count,
        "issues": [issue.as_dict() for issue in issues],
        "issue_kinds": sorted({issue.kind for issue in issues}),
    }


def clean(text: str, *, keep_numbers: bool = True) -> str:
    """只做机械清理：去掉空词、通用收尾和多余的连接词开头。

    只动命中的片段，不做整段重写；事实、数字、名称一律保留。
    """
    result = text or ""
    result = re.sub(r"(赋能|闭环|深度融合|全面提升|全方位|多维度|一站式|生态化|底层逻辑)", "", result)
    result = re.sub(r"(希望对你有帮助|如有需要.{0,6}随时|仅供参考|欢迎随时联系我)[。.]?", "", result)
    result = re.sub(r"(综上所述|总而言之|总的来说|值得注意的是|不难看出|由此可见)[，,]", "", result)
    result = re.sub(r"(首先|其次|最后)[，,]", "", result)
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
