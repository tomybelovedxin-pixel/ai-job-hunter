"""简历引擎：主简历事实源 → 按岗位方向裁剪 → 渲染 DOCX/PDF → 质量自检。"""

from .check import render_and_check, run_checks
from .render import render_docx, render_markdown, render_pdf
from .tailor import DIRECTIONS, ResumeDoc, autofix, build_doc, detect_direction

__all__ = [
    "DIRECTIONS",
    "ResumeDoc",
    "build_doc",
    "detect_direction",
    "autofix",
    "render_docx",
    "render_pdf",
    "render_markdown",
    "run_checks",
    "render_and_check",
]
