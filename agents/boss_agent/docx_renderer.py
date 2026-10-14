"""docx_renderer — 把审计后的鉴定意见书正文渲染成 .docx。

正文本身是 ``report_renderer.render_report`` 产出的纯文本 Markdown，这里只做
"套版式"这一步：识别标题、文号、章节号、编号段落，逐段套用司法文书版式。

版式全部集中在下面 STYLE 区。鉴定中心若有自己的模板（字体、字号、行距、
页边距），只需要改这一块，不必动渲染逻辑。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Cm, Pt


# ============================================================
# STYLE —— 司法文书版式（如需贴合鉴定中心模板，只改这一段）
# ============================================================

TITLE_FONT = "方正小标宋简体"   # 文书名（司法鉴定意见书）
TITLE_SIZE_PT = 22.0            # 二号
TITLE_LINES = ("司法鉴定意见书",)  # 视为大标题的行；其余抬头行走 CENTER/DOC_NO

CENTER_FONT = "方正小标宋简体"   # 抬头第一行（鉴定中心名）
CENTER_SIZE_PT = 22.0

DOC_NO_FONT = "仿宋_GB2312"
DOC_NO_SIZE_PT = 16.0           # 三号

HEADING1_FONT = "黑体"          # 一、基本情况
HEADING1_SIZE_PT = 16.0
HEADING2_FONT = "楷体_GB2312"   # （一）检验方法
HEADING2_SIZE_PT = 16.0

BODY_FONT = "仿宋_GB2312"
BODY_SIZE_PT = 16.0             # 三号
BODY_BOLD = False

LINE_SPACING_PT = 28.0          # 固定行距
FIRST_LINE_INDENT_CHARS = 2     # 首行缩进字符数

PAGE_WIDTH_CM = 21.0            # A4
PAGE_HEIGHT_CM = 29.7
MARGIN_TOP_CM = 3.7
MARGIN_BOTTOM_CM = 3.5
MARGIN_LEFT_CM = 2.8
MARGIN_RIGHT_CM = 2.6

# ============================================================


_HEADING1_RE = re.compile(r"^\s*[一二三四五六七八九十]+、")
_HEADING2_RE = re.compile(r"^\s*（[一二三四五六七八九十]+）")
_DOC_NO_RE = re.compile(r"第\s*[0-9〇一二三四五六七八九十]+\s*号")
_NUMBERED_RE = re.compile(r"^\s*\d+\s*[.、]")


def _set_run_font(run, font_name: str, size_pt: float, bold: bool = False) -> None:
    """设置 run 的字体（含中文 eastAsia 属性，否则 Word 会用默认宋体）。"""
    run.font.size = Pt(size_pt)
    run.bold = bold
    run.font.name = font_name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:eastAsia", "w:ascii", "w:hAnsi"):
        rfonts.set(qn(attr), font_name)


def _set_first_line_indent_chars(paragraph, chars: int) -> None:
    """按"字符"设置首行缩进（w:firstLineChars），字号变化时缩进会跟着走。"""
    if chars <= 0:
        return
    pf = paragraph._p.get_or_add_pPr()
    ind = pf.find(qn("w:ind"))
    if ind is None:
        ind = OxmlElement("w:ind")
        pf.append(ind)
    ind.set(qn("w:firstLineChars"), str(int(chars * 100)))


def _add_paragraph(
    doc: Document,
    text: str,
    font_name: str,
    size_pt: float,
    *,
    align: Optional[int] = None,
    bold: bool = False,
    indent_chars: int = 0,
    space_before_pt: float = 0.0,
    space_after_pt: float = 0.0,
) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    paragraph.paragraph_format.line_spacing = Pt(LINE_SPACING_PT)
    paragraph.paragraph_format.space_before = Pt(space_before_pt)
    paragraph.paragraph_format.space_after = Pt(space_after_pt)
    paragraph.paragraph_format.first_line_indent = Pt(0)
    if align is not None:
        paragraph.alignment = align
    _set_first_line_indent_chars(paragraph, indent_chars)
    run = paragraph.add_run(text)
    _set_run_font(run, font_name, size_pt, bold=bold)


def classify_lines(report_text: str) -> List[Tuple[str, str]]:
    """把正文按行归类，返回 [(role, text)]。

    role ∈ {center, title, doc_no, heading1, heading2, numbered, body, blank}

    抬头区 = 第一个「一、」之前的所有非空行。这一段是鉴定中心名 + 文书名 +
    文号，不能按正文处理，否则会被套上首行缩进并左对齐。
    """
    lines = (report_text or "").replace("\r\n", "\n").split("\n")
    classified: List[Tuple[str, str]] = []
    in_header = True
    header_index = 0
    for raw in lines:
        text = raw.strip()
        if not text:
            classified.append(("blank", ""))
            continue
        if in_header:
            if _HEADING1_RE.match(text):
                in_header = False
            else:
                if _DOC_NO_RE.search(text) and len(text) <= 40:
                    classified.append(("doc_no", text))
                elif any(text == t for t in TITLE_LINES):
                    classified.append(("title", text))
                elif header_index == 0:
                    classified.append(("center", text))
                else:
                    classified.append(("title", text))
                header_index += 1
                continue
        if _HEADING1_RE.match(text):
            classified.append(("heading1", text))
            continue
        if _HEADING2_RE.match(text):
            classified.append(("heading2", text))
            continue
        role = "numbered" if _NUMBERED_RE.match(text) else "body"
        classified.append((role, text))
    return classified


def render_report_docx(
    report_md: str,
    output_path: str,
    *,
    center_name: str = "浙江大学司法鉴定中心",
) -> str:
    """把鉴定意见书正文渲染为 .docx，返回输出路径。"""
    doc = Document()

    section = doc.sections[0]
    section.page_width = Cm(PAGE_WIDTH_CM)
    section.page_height = Cm(PAGE_HEIGHT_CM)
    section.top_margin = Cm(MARGIN_TOP_CM)
    section.bottom_margin = Cm(MARGIN_BOTTOM_CM)
    section.left_margin = Cm(MARGIN_LEFT_CM)
    section.right_margin = Cm(MARGIN_RIGHT_CM)

    # 正文默认样式兜底，避免 Word 里出现 Calibri/宋体混排。
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(BODY_SIZE_PT)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)

    rows = classify_lines(report_md)

    # 抬头：正文若未自带中心名，先在标题前补一行。
    has_center = any(role in {"center", "title"} for role, _ in rows)
    if not has_center and center_name:
        _add_paragraph(doc, center_name, CENTER_FONT, CENTER_SIZE_PT, align=WD_ALIGN_PARAGRAPH.CENTER)

    pending_blank = False
    for role, text in rows:
        if role == "blank":
            pending_blank = True
            continue
        if role == "center":
            _add_paragraph(doc, text, CENTER_FONT, CENTER_SIZE_PT, align=WD_ALIGN_PARAGRAPH.CENTER)
        elif role == "title":
            _add_paragraph(
                doc, text, TITLE_FONT, TITLE_SIZE_PT,
                align=WD_ALIGN_PARAGRAPH.CENTER,
                space_before_pt=0.0 if not pending_blank else 6.0,
            )
        elif role == "doc_no":
            _add_paragraph(
                doc, text, DOC_NO_FONT, DOC_NO_SIZE_PT,
                align=WD_ALIGN_PARAGRAPH.CENTER, space_before_pt=12.0,
            )
        elif role == "heading1":
            _add_paragraph(
                doc, text, HEADING1_FONT, HEADING1_SIZE_PT,
                space_before_pt=12.0 if pending_blank else 6.0,
            )
        elif role == "heading2":
            _add_paragraph(doc, text, HEADING2_FONT, HEADING2_SIZE_PT, space_before_pt=6.0)
        else:
            _add_paragraph(
                doc, text, BODY_FONT, BODY_SIZE_PT,
                bold=BODY_BOLD, indent_chars=FIRST_LINE_INDENT_CHARS,
            )
        pending_blank = False

    doc.save(output_path)
    return output_path
