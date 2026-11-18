"""训练数据准备流水线：清洗 → 去重 → 质检 → 指令样本 → 报告。

做这个模块的原因：岗位里有一类（AI 训练师、数据标注训练、语料处理）要求的是
"把原始文本弄成能训练的干净数据"。原来自评为"没有这个能力"，是因为本机确实没有
可复核的产物。这里把这件事补上：纯标准库实现，输入任意文本目录，输出
清洗后的语料、指令样本和一份可复核的质量报告。

参考的开源实现（只借鉴做法，不直接依赖）：
- ChenghaoMou/text-dedup：shingle + MinHash 近重复检测
- bigcode-project/datatrove、NVIDIA/NeMo-Curator：分阶段的数据治理流水线
  （规范化 → 过滤 → 去重 → 报告）
本机版本把这两条思路压成标准库实现，便于在 Windows 上直接跑、可自检。
"""

from .clean import (
    Document, drop_near_dupes, exact_dedupe, load_documents, near_dupes, normalize,
    split_paragraphs,
)
from .instructions import build_instructions
from .quality import quality_report, scan_pii

__all__ = [
    "Document", "load_documents", "normalize", "exact_dedupe", "near_dupes",
    "drop_near_dupes", "split_paragraphs", "quality_report", "scan_pii",
    "build_instructions",
]
