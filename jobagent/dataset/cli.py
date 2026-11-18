"""训练数据流水线入口：清洗 → 去重 → 质检 → 指令样本 → 报告。

用法：
  python -m jobagent.dataset run --in docs --in PROJECT_INDEX.md --out data/dataset
  python -m jobagent.dataset run --in <目录> --min-chars 200 --dedupe-threshold 0.85
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .clean import Document, drop_near_dupes, exact_dedupe, load_documents, split_paragraphs
from .instructions import build_instructions, write_jsonl
from .quality import assert_report_ok, quality_report


def _collect(inputs: list[str], *, min_chars: int, paragraph: bool) -> tuple[list[Document], int]:
    docs: list[Document] = []
    raw_count = 0
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            found = load_documents(path, min_chars=min_chars)
        elif path.is_file():
            found = load_documents(path.parent, suffixes=(path.suffix,), min_chars=min_chars)
            found = [doc for doc in found if Path(doc.source).name == path.name]
        else:
            print(f"跳过（路径不存在）：{item}")
            continue
        raw_count += len(found)
        if paragraph:
            for doc in found:
                docs.extend(split_paragraphs(doc))
        else:
            docs.extend(found)
    return docs, raw_count


def run(args: argparse.Namespace) -> int:
    docs, raw_count = _collect(args.inputs, min_chars=args.min_chars, paragraph=not args.no_split)
    original = len(docs)
    docs, exact_dropped = exact_dedupe(docs)
    docs, near_dropped = drop_near_dupes(docs, threshold=args.dedupe_threshold)
    samples = build_instructions(docs)
    report = quality_report(docs, dropped=near_dropped, source_total=raw_count)
    report["exact_duplicates_dropped"] = len(exact_dropped)
    report["stage"] = {"输入文件": raw_count, "切段后": original, "完全相同丢弃": len(exact_dropped),
                       "近似重复丢弃": len(near_dropped), "保留": len(docs),
                       "指令样本": len(samples)}
    problems = assert_report_ok(report)
    report["problems"] = problems
    report["verdict"] = "可用" if not problems else "需要处理"

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_jsonl([doc.as_dict() for doc in docs], out / "clean.jsonl")
    write_jsonl(samples, out / "instructions.jsonl")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    print(f"输入 {raw_count} 个文件 → 切段 {original} → 保留 {len(docs)} → 指令样本 {len(samples)}")
    print(f"长度：中位 {report['chars']['median']} 字，最长 {report['chars']['max']} 字；"
          f"重复占比 {report['dup_ratio']:.1%}；空洞样本 {report['empty_or_hollow']['count']} 条；"
          f"个人信息命中 {report['pii']['hits']} 处")
    print(f"判定：{report['verdict']}" + (f"（{'；'.join(problems)}）" if problems else ""))
    print(f"报告写到 {out / 'report.json'}")
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobagent.dataset")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="跑一遍清洗+去重+质检+指令样本")
    p.add_argument("--in", dest="inputs", nargs="+", required=True, help="文件或目录（可多个）")
    p.add_argument("--out", default="data/dataset", help="输出目录")
    p.add_argument("--min-chars", type=int, default=120, help="单份最小字数")
    p.add_argument("--dedupe-threshold", type=float, default=0.8, help="近似重复阈值")
    p.add_argument("--no-split", action="store_true", help="不按段落切分")
    p.set_defaults(func=run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
