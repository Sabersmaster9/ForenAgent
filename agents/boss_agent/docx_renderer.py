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
# STYLE —— 实测自鉴定中心真实意见书（files/知识库/格式示例/017.docx、018.docx）
#
# 两份样本的页面与段落参数完全一致，下列数值为逐段提取所得。
# 鉴定中心若改模板，改这一段即可，不必动渲染逻辑。
# ============================================================

# 页面：A4，上下左右边距按实测值（不是党政公文那套 3.7/3.5/2.8/2.6）
PAGE_WIDTH_CM = 21.0
PAGE_HEIGHT_CM = 29.7
MARGIN_TOP_CM = 2.5
MARGIN_BOTTOM_CM = 2.0
MARGIN_LEFT_CM = 2.5
MARGIN_RIGHT_CM = 2.0
HEADER_DISTANCE_CM = 1.5
FOOTER_DISTANCE_CM = 1.75

# 抬头：中心名 + 文书名，黑体二号的一半是 18pt（样本实测 18pt）
CENTER_FONT = "黑体"
CENTER_SIZE_PT = 18.0
TITLE_FONT = "黑体"
TITLE_SIZE_PT = 18.0
TITLE_LINES = ("司法鉴定意见书",)

# 文号：宋体 10.5pt，右对齐
DOC_NO_FONT = "宋体"
DOC_NO_SIZE_PT = 10.5

# 一级标题（一、基本情况）：黑体 16pt
HEADING1_FONT = "黑体"
HEADING1_SIZE_PT = 16.0

# 二级标题（（一）检验方法）：样本里与正文同字体同字号，不做加粗区分
HEADING2_FONT = "仿宋_GB2312"
HEADING2_SIZE_PT = 14.0

# 「一、基本情况」内的标签行（委托人：/受理日期：/被鉴定人：…）用黑体
LABEL_FONT = "黑体"
LABEL_SIZE_PT = 14.0
LABEL_RE = re.compile(
    r"^\s*(?:委托人|委托鉴定事项|受理日期|鉴定材料|鉴定地点|在场人员|被鉴定人|送检人)\s*[：:]"
)

# 正文：仿宋_GB2312 14pt
BODY_FONT = "仿宋_GB2312"
BODY_SIZE_PT = 14.0
BODY_BOLD = False

LINE_SPACING_PT = 25.0          # 固定行距（样本实测）
FIRST_LINE_INDENT_CHARS = 2     # 首行缩进 2 字符；样本里 14pt 正文为 28pt，一致

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


def _set_first_line_indent_chars(paragraph, chars: int, size_pt: float) -> None:
    """按"字符"设置首行缩进，与真实样本一致。

    样本同时写了 w:firstLineChars（Word 按字号换算）和 w:firstLine（磅值兜底），
    后者用于不识别字符缩进的旧阅读器。两者都写，避免打开位置不一致。
    """
    if chars <= 0:
        return
    pf = paragraph._p.get_or_add_pPr()
    ind = pf.find(qn("w:ind"))
    if ind is None:
        ind = OxmlElement("w:ind")
        pf.append(ind)
    ind.set(qn("w:firstLineChars"), str(int(chars * 100)))
    ind.set(qn("w:firstLine"), str(int(chars * size_pt * 20)))  # twips = 磅 × 20


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
    _set_first_line_indent_chars(paragraph, indent_chars, size_pt)
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
        if LABEL_RE.match(text):
            # 「一、基本情况」里的标签行在样本中用黑体，与正文仿宋区分。
            classified.append(("label", text))
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
            # 样本里一级标题同样是首行缩进 2 字符（16pt×2 = 32pt）
            _add_paragraph(
                doc, text, HEADING1_FONT, HEADING1_SIZE_PT,
                indent_chars=FIRST_LINE_INDENT_CHARS,
                space_before_pt=12.0 if pending_blank else 6.0,
            )
        elif role == "heading2":
            _add_paragraph(
                doc, text, HEADING2_FONT, HEADING2_SIZE_PT,
                indent_chars=FIRST_LINE_INDENT_CHARS,
            )
        elif role == "label":
            _add_paragraph(
                doc, text, LABEL_FONT, LABEL_SIZE_PT,
                indent_chars=FIRST_LINE_INDENT_CHARS,
            )
        else:
            _add_paragraph(
                doc, text, BODY_FONT, BODY_SIZE_PT,
                bold=BODY_BOLD, indent_chars=FIRST_LINE_INDENT_CHARS,
            )
        pending_blank = False

    doc.save(output_path)
    return output_path
