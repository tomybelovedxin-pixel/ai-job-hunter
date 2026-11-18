"""渲染层：双栏版式（左侧联系方式/技能/教育，右侧概述与项目），输出 DOCX、PDF、Markdown。

项目按结构化块写：项目描述 / 技术栈 / 主要工作 / 成果。
版式参考中文技术简历通行做法（单页、层级清楚、技术栈用标签、时间右对齐）；
双栏是经典款，投递时仍保持单页、左侧不承载关键经历，避免解析器读偏。

中文字体用系统自带的微软雅黑，不打包字体、不外传。
python-docx 或 reportlab 缺失时只出 Markdown，并把缺什么写进返回值。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .tailor import ResumeDoc

FONT_DIR = Path(r"C:\Windows\Fonts")
REGULAR_TTF = FONT_DIR / "msyh.ttc"
BOLD_TTF = FONT_DIR / "msyhbd.ttc"

ACCENT = "#1F5FA8"
INK = "#1A1A1A"
GREY = "#454545"
LIGHT = "#7A7A7A"
CHIP_BG = "#EEF3F9"
RULE = "#C9D4E2"
FONT = "微软雅黑"


class RenderError(RuntimeError):
    """渲染失败：缺依赖、缺字体或路径不可写。"""


def _safe(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text or "")


def _contact_rows(doc: ResumeDoc) -> list[tuple[str, str]]:
    basics = doc.basics
    age_val = basics.get("age", "")
    if age_val:
        age_str = f"{age_val} 岁" if str(age_val).isdigit() else str(age_val)
    else:
        age_str = ""
    rows = [
        ("年龄", age_str),
        ("城市", basics.get("city", "")),
        ("工作方式", basics.get("work_mode", "")),
        ("手机", basics.get("phone", "")),
        ("邮箱", basics.get("email", "")),
    ]
    return [(label, value) for label, value in rows if value]


def _intent_text(doc: ResumeDoc) -> str:
    if doc.target_title:
        return f"应聘岗位：{doc.target_title}（方向 {doc.direction}）"
    return f"求职意向：{doc.direction}"


# ---------------------------------------------------------------- 标签

def _chips(items: list[str], *, font_size: float, max_width: float,
           font_name: str = "YaHei") -> list[Any]:
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.platypus import Paragraph, Table, TableStyle

    style = ParagraphStyle("chip", fontName=font_name, fontSize=font_size,
                           leading=font_size + 3, textColor=colors.HexColor("#26456B"))
    rows: list[list[str]] = []
    current: list[str] = []
    current_width = 0.0
    for item in items:
        width = pdfmetrics.stringWidth(item, font_name, font_size) + 11
        if current and current_width + width > max_width:
            rows.append(current)
            current, current_width = [], 0.0
        current.append(item)
        current_width += width + 3
    if current:
        rows.append(current)

    tables: list[Any] = []
    for row in rows:
        cells = [Paragraph(_safe(text), style) for text in row]
        widths = [pdfmetrics.stringWidth(text, font_name, font_size) + 11 for text in row]
        if sum(widths) > max_width:
            scale = max_width / sum(widths)
            widths = [width * scale for width in widths]
        table = Table([cells], colWidths=widths, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 2.2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(CHIP_BG)),
            ("LINEBELOW", (0, 0), (-1, -1), 1.4, colors.white),
            ("LINEAFTER", (0, 0), (-1, -1), 1.4, colors.white),
        ]))
        tables.append(table)
    return tables


# ---------------------------------------------------------------- DOCX

def _docx_shaded_run(paragraph: Any, text: str, *, size: float) -> Any:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    run = paragraph.add_run(f" {_safe(text)} ")
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor.from_string("26456B")
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), CHIP_BG.lstrip("#"))
    run._element.get_or_add_rPr().append(shd)
    return run


def render_docx(doc: ResumeDoc, path: str | Path) -> str:
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Cm, Pt, RGBColor
    except Exception as exc:  # pragma: no cover
        raise RenderError(f"缺少 python-docx，无法生成 DOCX（{exc}）")

    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    section.top_margin, section.bottom_margin = Cm(1.2), Cm(1.0)
    section.left_margin, section.right_margin = Cm(1.5), Cm(1.5)
    normal = document.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(9.4)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 1.1

    table = document.add_table(rows=1, cols=2)
    table.autofit = False
    left_cell, right_cell = table.rows[0].cells
    left_cell.width = Cm(5.35)
    right_cell.width = Cm(12.65)

    def para(cell: Any, text: str = "", *, size: float = 9.4, bold: bool = False,
             color: str = INK, before: float = 0, after: float = 0,
             indent: float = 0.0) -> Any:
        item = cell.add_paragraph()
        item.paragraph_format.space_before = Pt(before)
        item.paragraph_format.space_after = Pt(after)
        if indent:
            item.paragraph_format.left_indent = Cm(indent)
        if text:
            run = item.add_run(_safe(text))
            run.font.name = FONT
            run.font.size = Pt(size)
            run.bold = bold
            run.font.color.rgb = RGBColor.from_string(color.lstrip("#"))
            run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
        return item

    def rule(cell: Any, color: str = RULE, thickness: int = 6, after: float = 3) -> None:
        item = para(cell, after=after)
        borders = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), str(thickness))
        bottom.set(qn("w:space"), "1")
        bottom.set(qn("w:color"), color.lstrip("#"))
        borders.append(bottom)
        item._p.get_or_add_pPr().append(borders)

    def side_heading(cell: Any, title: str) -> None:
        para(cell, title, size=10.5, bold=True, color=ACCENT, before=8, after=2)
        rule(cell)

    def main_heading(title: str) -> None:
        para(right_cell, f"▌ {title}", size=11.5, bold=True, color=ACCENT, before=7, after=2)
        rule(right_cell)

    # 左栏
    side_heading(left_cell, "联系方式")
    for label, value in _contact_rows(doc):
        item = para(left_cell, after=1.5)
        lead = item.add_run(f"{_safe(label)}　")
        lead.font.name = FONT
        lead.font.size = Pt(8.6)
        lead.font.color.rgb = RGBColor.from_string(LIGHT.lstrip("#"))
        lead._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
        body = item.add_run(_safe(value))
        body.font.name = FONT
        body.font.size = Pt(9.2)
        body._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    if doc.skill_groups:
        side_heading(left_cell, "专业技能")
        for group in doc.skill_groups:
            para(left_cell, group["label"], size=9.2, bold=True, after=1)
            para(left_cell, group["items"], size=8.8, color=GREY, after=3)
    if doc.education:
        side_heading(left_cell, "教育背景")
        para(left_cell, doc.education, size=9, color=GREY)

    # 右栏
    para(right_cell, doc.basics.get("name", ""), size=19, bold=True, after=1)
    if doc.headline:
        para(right_cell, doc.headline, size=10.5, bold=True, color=ACCENT, after=2)
    rule(right_cell, ACCENT, thickness=10, after=3)
    para(right_cell, _intent_text(doc), size=9, color=GREY, after=1)

    if doc.summary:
        main_heading("个人概述")
        para(right_cell, doc.summary, size=9.4, color=GREY)

    if doc.matches:
        main_heading("与本岗位的对应")
        for line in doc.matches:
            item = para(right_cell, after=0.8, indent=0.35)
            bullet = item.add_run("· ")
            bullet.font.name = FONT
            bullet.font.size = Pt(9.2)
            bullet.font.color.rgb = RGBColor.from_string(ACCENT.lstrip("#"))
            body = item.add_run(_safe(line))
            body.font.name = FONT
            body.font.size = Pt(9.2)
            body.font.color.rgb = RGBColor.from_string(GREY.lstrip("#"))
            body._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)

    if doc.projects:
        main_heading("项目经历")
        for project in doc.projects:
            header = para(right_cell, before=3, after=1)
            header.paragraph_format.tab_stops.add_tab_stop(Cm(12.65), WD_TAB_ALIGNMENT.RIGHT)
            left = header.add_run(_safe(project["name"]))
            left.bold = True
            left.font.name = FONT
            left.font.size = Pt(10.4)
            left._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
            meta = "　".join(x for x in [project.get("period", ""), project.get("role", "")]
                             if x)
            right = header.add_run(f"\t{_safe(meta)}")
            right.font.name = FONT
            right.font.size = Pt(8.4)
            right.font.color.rgb = RGBColor.from_string(LIGHT.lstrip("#"))
            right._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
            if project.get("tech"):
                stack = para(right_cell, after=1)
                for piece in project["tech"][:6]:
                    _docx_shaded_run(stack, str(piece), size=8.0)
                    stack.add_run(" ").font.size = Pt(2)
            if project.get("tagline"):
                item = para(right_cell, after=0.8)
                lead = item.add_run("项目描述　")
                lead.bold = True
                lead.font.name = FONT
                lead.font.size = Pt(8.8)
                lead.font.color.rgb = RGBColor.from_string(ACCENT.lstrip("#"))
                lead._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
                body = item.add_run(_safe(project["tagline"]))
                body.font.name = FONT
                body.font.size = Pt(9.2)
                body.font.color.rgb = RGBColor.from_string(GREY.lstrip("#"))
                body._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
            if project["bullets"]:
                item = para(right_cell, "主要工作", size=8.8, bold=True, color=ACCENT, after=0.8)
                for index, bullet in enumerate(project["bullets"], start=1):
                    para(right_cell, f"{index}. {_safe(bullet)}", size=9.2, color=GREY,
                         after=0.8, indent=0.35)
            if project.get("result"):
                item = para(right_cell, after=1)
                lead = item.add_run("成果　")
                lead.bold = True
                lead.font.name = FONT
                lead.font.size = Pt(8.8)
                lead.font.color.rgb = RGBColor.from_string(ACCENT.lstrip("#"))
                lead._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
                body = item.add_run(_safe(project["result"]))
                body.font.name = FONT
                body.font.size = Pt(9.2)
                body.font.color.rgb = RGBColor.from_string(GREY.lstrip("#"))
                body._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)

    # 说明：doc.others（没进正文的项目）按当前口径不渲染，数据仍保留在事实源里，
    # 需要恢复时把这一段放回来即可。

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    return str(path)


# ---------------------------------------------------------------- PDF

def render_pdf(doc: ResumeDoc, path: str | Path) -> str:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_RIGHT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            BaseDocTemplate, Frame, FrameBreak, HRFlowable, KeepTogether, PageTemplate,
            Paragraph, Spacer, Table, TableStyle,
        )
    except Exception as exc:  # pragma: no cover
        raise RenderError(f"缺少 reportlab，无法生成 PDF（{exc}）")
    if not REGULAR_TTF.exists():
        raise RenderError(f"找不到中文字体 {REGULAR_TTF}")

    pdfmetrics.registerFont(TTFont("YaHei", str(REGULAR_TTF), subfontIndex=0))
    bold_font = "YaHei"
    if BOLD_TTF.exists():
        pdfmetrics.registerFont(TTFont("YaHei-Bold", str(BOLD_TTF), subfontIndex=0))
        bold_font = "YaHei-Bold"

    def style(name: str, **kwargs: Any) -> ParagraphStyle:
        kwargs.setdefault("wordWrap", "CJK")
        return ParagraphStyle(name, **kwargs)

    accent, ink, grey, light = (colors.HexColor(x) for x in (ACCENT, INK, GREY, LIGHT))
    styles = {
        "name": style("name", fontName=bold_font, fontSize=20, leading=24, textColor=ink),
        "headline": style("headline", fontName=bold_font, fontSize=10.5, leading=14,
                          textColor=accent),
        "meta": style("meta", fontName="YaHei", fontSize=9, leading=13, textColor=grey),
        "side_label": style("side_label", fontName="YaHei", fontSize=8.6, leading=12.5,
                            textColor=light),
        "side_head": style("side_head", fontName=bold_font, fontSize=10.5, leading=14,
                           textColor=accent),
        "side_text": style("side_text", fontName="YaHei", fontSize=8.9, leading=13,
                           textColor=grey),
        "side_group": style("side_group", fontName="YaHei", fontSize=9.2, leading=13,
                            textColor=ink),
        "section": style("section", fontName=bold_font, fontSize=11.5, leading=15,
                         textColor=accent),
        "body": style("body", fontName="YaHei", fontSize=9.4, leading=14, textColor=grey),
        "project": style("project", fontName=bold_font, fontSize=10.4, leading=13.5,
                         textColor=ink),
        "project_meta": style("project_meta", fontName="YaHei", fontSize=8.4, leading=13.5,
                              textColor=light, alignment=TA_RIGHT),
        "field": style("field", fontName="YaHei", fontSize=9.2, leading=13.6, textColor=grey),
        "work": style("work", fontName="YaHei", fontSize=9.2, leading=13.4, textColor=grey,
                      leftIndent=11, bulletIndent=0.5, spaceBefore=0.4),
        "others": style("others", fontName="YaHei", fontSize=9.2, leading=13.6,
                        textColor=grey, spaceBefore=0.6),
    }

    left_width, gap = 5.35 * cm, 0.7 * cm
    left_margin = right_margin = 1.5 * cm
    top_margin, bottom_margin = 1.15 * cm, 1.0 * cm
    page_w, page_h = A4
    right_width = page_w - left_margin - right_margin - left_width - gap
    frame_height = page_h - top_margin - bottom_margin

    document = BaseDocTemplate(
        str(path), pagesize=A4, leftMargin=left_margin, rightMargin=right_margin,
        topMargin=top_margin, bottomMargin=bottom_margin,
        title=f"{doc.basics.get('name', '')} 的简历", author=doc.basics.get("name", ""),
    )
    document.addPageTemplates([PageTemplate(id="two-col", frames=[
        Frame(left_margin, bottom_margin, left_width, frame_height,
              leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0),
        Frame(left_margin + left_width + gap, bottom_margin, right_width, frame_height,
              leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0),
    ])])

    def side_heading(title: str) -> list[Any]:
        return [
            Spacer(1, 8),
            Paragraph(title, styles["side_head"]),
            HRFlowable(width="100%", thickness=0.5, color=colors.HexColor(RULE),
                       spaceBefore=1.5, spaceAfter=3),
        ]

    def main_heading(title: str) -> list[Any]:
        return [
            Spacer(1, 6),
            Paragraph(f"▌ {title}", styles["section"]),
            HRFlowable(width="100%", thickness=0.5, color=colors.HexColor(RULE),
                       spaceBefore=1.5, spaceAfter=3),
        ]

    left: list[Any] = []
    left += side_heading("联系方式")
    for label, value in _contact_rows(doc):
        left.append(Paragraph(f"<font size=8.4 color='{LIGHT}'>{_safe(label)}</font>　"
                              f"{_safe(value)}", styles["side_text"]))
    if doc.skill_groups:
        left += side_heading("专业技能")
        for group in doc.skill_groups:
            left.append(Paragraph(f"<b>{_safe(group['label'])}</b>", styles["side_group"]))
            left.append(Paragraph(_safe(group["items"]), styles["side_text"]))
            left.append(Spacer(1, 2.5))
    if doc.education:
        left += side_heading("教育背景")
        left.append(Paragraph(_safe(doc.education), styles["side_text"]))

    right: list[Any] = [Paragraph(_safe(doc.basics.get("name", "")), styles["name"])]
    if doc.headline:
        right.append(Paragraph(_safe(doc.headline), styles["headline"]))
    right.append(HRFlowable(width="100%", thickness=1.1, color=accent,
                            spaceBefore=2.5, spaceAfter=3))
    right.append(Paragraph(_safe(_intent_text(doc)), styles["meta"]))

    if doc.summary:
        right += main_heading("个人概述")
        right.append(Paragraph(_safe(doc.summary), styles["body"]))

    if doc.matches:
        right += main_heading("与本岗位的对应")
        for line in doc.matches:
            right.append(Paragraph(_safe(line), styles["work"], bulletText="·"))

    if doc.projects:
        right += main_heading("项目经历")
        for project in doc.projects:
            meta = "　".join(x for x in [project.get("period", ""), project.get("role", "")]
                             if x)
            header = Table(
                [[Paragraph(_safe(project["name"]), styles["project"]),
                  Paragraph(_safe(meta), styles["project_meta"])]],
                colWidths=[right_width * 0.55, right_width * 0.45],
            )
            header.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5),
            ]))
            block: list[Any] = [header]
            if project.get("tech"):
                chips = _chips([str(x) for x in project["tech"][:6]], font_size=8.0,
                               max_width=right_width)
                if chips:
                    block.append(Spacer(1, 1.5))
                    block.extend(chips)
            if project.get("tagline"):
                block.append(Paragraph(
                    f"<font color='{ACCENT}'><b>项目描述</b></font>　"
                    f"{_safe(project['tagline'])}", styles["field"]))
            if project["bullets"]:
                block.append(Paragraph(
                    f"<font color='{ACCENT}'><b>主要工作</b></font>", styles["field"]))
                for index, bullet in enumerate(project["bullets"], start=1):
                    block.append(Paragraph(_safe(bullet), styles["work"],
                                           bulletText=f"{index}."))
            if project.get("result"):
                block.append(Paragraph(
                    f"<font color='{ACCENT}'><b>成果</b></font>　{_safe(project['result'])}",
                    styles["field"]))
            block.append(Spacer(1, 3))
            right.append(KeepTogether(block))

    # 说明：doc.others 按当前口径不渲染，数据保留在 master_resume.json 里备用。

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.build(left + [FrameBreak()] + right)
    return str(path)


# ---------------------------------------------------------------- Markdown

def render_markdown(doc: ResumeDoc, path: str | Path) -> str:
    lines = [f"# {doc.basics.get('name', '')}", ""]
    if doc.headline:
        lines.append(f"**{doc.headline}**")
    lines += ["", _intent_text(doc)]
    for label, value in _contact_rows(doc):
        lines.append(f"- {label}：{value}")
    if doc.education:
        lines.append(f"- 教育背景：{doc.education}")
    if doc.summary:
        lines += ["", "## 个人概述", "", doc.summary]
    if doc.matches:
        lines += ["", "## 与本岗位的对应", ""]
        lines += [f"- {line}" for line in doc.matches]
    if doc.skill_groups:
        lines += ["", "## 专业技能", ""]
        lines += [f"- **{group['label']}**：{group['items']}" for group in doc.skill_groups]
    if doc.projects:
        lines += ["", "## 项目经历", ""]
        for project in doc.projects:
            meta = "　".join(x for x in [project.get("period", ""), project.get("role", "")]
                             if x)
            lines.append(f"### {project['name']}　{meta}")
            lines.append("")
            if project.get("tech"):
                lines.append("技术栈：" + " / ".join(str(x) for x in project["tech"]))
            if project.get("tagline"):
                lines.append("")
                lines.append(f"**项目描述**　{project['tagline']}")
            if project["bullets"]:
                lines += ["", "**主要工作**", ""]
                lines += [f"{index}. {bullet}"
                          for index, bullet in enumerate(project["bullets"], start=1)]
            if project.get("result"):
                lines += ["", f"**成果**　{project['result']}"]
            lines.append("")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return str(path)
