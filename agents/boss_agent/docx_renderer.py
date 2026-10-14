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
SIGNATURE_INDENT_CHARS = 4      # 落款签名区缩进（样本实测约 56pt ≈ 4 字符）

# ---------- 首页「声明」页（原文取自真实意见书样本）----------
DECLARATION_ENABLED = True
LICENSE_NO = "330105001"
DECLARATION_TITLE = "声  明"
DECLARATION_FONT = "仿宋_GB2312"
DECLARATION_SIZE_PT = 12.0
DECLARATION_LINE_SPACING_PT = 20.0
DECLARATION_STATEMENTS = (
    "1.司法鉴定实行鉴定人负责制度。司法鉴定机构和司法鉴定人按照法律法规和规章的规定的要求，"
    "采用相关技术标准和技术规范，依法独立、客观、公正地进行鉴定并出具鉴定意见，不受任何组织和个人的非法干预。",
    "2.委托方应向本机构提供真实、完整、充分的鉴定材料，并对鉴定材料的真实性与合法性负责。"
    "提供虚假鉴定材料导致鉴定意见错误的，本机构不承担任何责任。",
    "3.受鉴定材料、鉴定时间等客观条件的限制，本鉴定意见仅对本次委托方提供的鉴定材料负责。",
    "4.鉴定意见属于第三方专业性意见，使用司法鉴定意见书，应当保证其完整性和严肃性，"
    "仅供办案机关等相关方参考，其是否被采信取决于相关方的审查和判断。",
    "5.依据司法部《司法鉴定程序通则》第四十一条之规定，本机构在必要时，可以对书写错误的鉴定文书作补正说明；"
    "补正说明是本鉴定文书的组成部分。",
    "6.当事人认为本机构和鉴定人在执业活动中有违法违规行为，并能提出有关事实和理由的，"
    "可以根据司法部《司法鉴定执业活动投诉处理办法》规定，向本机构反映0571-86971563，或向杭州市司法局投诉0571-85256592。",
    "7.鉴定意见属于鉴定人的专业意见。当事人仅对鉴定意见有异议，不属投诉受理范围。"
    "可通过向法院申请鉴定人出庭作证，或者通过申请补正、补充鉴定、重新鉴定等法定途径解决。司法行政部门不得撤销司法鉴定文书。",
    "8.司法鉴定意见书是否作为定案或者认定事实的根据，取决于办案机关的审查判断，司法鉴定机构和司法鉴定人无权干涉。",
)
DECLARATION_CONTACT_LINES = (
    "地址：杭州市凯旋路268号浙江大学华家池校区（邮政编码：310029）",
    "联系电话/传真：0571-86971580（法医临床） 88208161（法医病理）",
    "87217286（法医精神） 86971976（法医物证） 86971559（文书鉴定）",
    "官方网址：http://sfjd.zju.edu.cn/",
)

# ============================================================


_HEADING1_RE = re.compile(r"^\s*[一二三四五六七八九十]+、")
_HEADING2_RE = re.compile(r"^\s*（[一二三四五六七八九十]+）")
_DOC_NO_RE = re.compile(r"第\s*[0-9〇一二三四五六七八九十]+\s*号")
_NUMBERED_RE = re.compile(r"^\s*\d+\s*[.、]")
# 落款区：签名行与中文数字日期（如 二Ｏ二五年八月七日）
_SIGNATURE_RE = re.compile(r"^\s*(?:司法鉴定人签名|《司法鉴定人执业证》证号|鉴定人签名|执业证号)\s*[：:]")
_DATE_CN_RE = re.compile(r"^\s*[一二三四五六七八九十〇Ｏ零]{2,4}\s*年\s*[一二三四五六七八九十〇Ｏ零]{1,3}\s*月\s*[一二三四五六七八九十〇Ｏ零]{1,3}\s*日\s*$")


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
    line_spacing_pt: Optional[float] = None,
    extra_runs: Optional[List[Tuple[str, str, float]]] = None,
) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    paragraph.paragraph_format.line_spacing = Pt(line_spacing_pt or LINE_SPACING_PT)
    paragraph.paragraph_format.space_before = Pt(space_before_pt)
    paragraph.paragraph_format.space_after = Pt(space_after_pt)
    paragraph.paragraph_format.first_line_indent = Pt(0)
    if align is not None:
        paragraph.alignment = align
    _set_first_line_indent_chars(paragraph, indent_chars, size_pt)
    run = paragraph.add_run(text)
    _set_run_font(run, font_name, size_pt, bold=bold)
    # 同一段落内的追加片段，用于「标签：值」这类混排（标签黑体、内容仿宋）。
    for seg_text, seg_font, seg_size in extra_runs or []:
        if not seg_text:
            continue
        seg_run = paragraph.add_run(seg_text)
        _set_run_font(seg_run, seg_font, seg_size)


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
        if _SIGNATURE_RE.match(text):
            classified.append(("signature", text))
            continue
        if _DATE_CN_RE.match(text):
            classified.append(("date_cn", text))
            continue
        role = "numbered" if _NUMBERED_RE.match(text) else "body"
        classified.append((role, text))
    return classified


def _add_declaration_page(doc: Document, center_name: str) -> None:
    """按真实样本生成首页：中心名 + 文书名 + 许可证号 + 声明 + 联系方式。

    这一页是机构的固定文案，版式与正文不同：声明正文行距 20 磅、仿宋 12pt，
    抬头两行仍用与正文一致的标题字体。
    """
    _add_paragraph(doc, center_name, CENTER_FONT, CENTER_SIZE_PT, align=WD_ALIGN_PARAGRAPH.CENTER)
    _add_paragraph(doc, "司法鉴定意见书", TITLE_FONT, TITLE_SIZE_PT, align=WD_ALIGN_PARAGRAPH.CENTER)

    def _decl(text: str, **kwargs: Any) -> None:
        _add_paragraph(
            doc, text, DECLARATION_FONT, DECLARATION_SIZE_PT,
            line_spacing_pt=DECLARATION_LINE_SPACING_PT, **kwargs,
        )

    for _ in range(2):
        _decl("")
    _decl(f"司法鉴定机构许可证号：      {LICENSE_NO}", align=WD_ALIGN_PARAGRAPH.CENTER)
    _decl("")
    _decl(DECLARATION_TITLE, align=WD_ALIGN_PARAGRAPH.CENTER)
    for statement in DECLARATION_STATEMENTS:
        _decl(statement, indent_chars=FIRST_LINE_INDENT_CHARS)
    _decl("")
    for line in DECLARATION_CONTACT_LINES:
        _decl(line)


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

    # 首页声明页，之后分页进入正文。
    if DECLARATION_ENABLED:
        _add_declaration_page(doc, center_name)
        doc.add_page_break()

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
            # 样本里只有「标签：」用黑体，后面填的内容仍是正文仿宋，必须同段混排。
            match = LABEL_RE.match(text)
            label_text = text[: match.end()] if match else text
            value_text = text[match.end():] if match else ""
            _add_paragraph(
                doc, label_text, LABEL_FONT, LABEL_SIZE_PT,
                indent_chars=FIRST_LINE_INDENT_CHARS,
                extra_runs=[(value_text, BODY_FONT, BODY_SIZE_PT)],
            )
        elif role == "signature":
            # 落款签名与执业证号，样本里靠右缩进排版
            _add_paragraph(
                doc, text, BODY_FONT, BODY_SIZE_PT,
                indent_chars=SIGNATURE_INDENT_CHARS,
            )
        elif role == "date_cn":
            _add_paragraph(
                doc, text, BODY_FONT, BODY_SIZE_PT,
                align=WD_ALIGN_PARAGRAPH.RIGHT,
                indent_chars=SIGNATURE_INDENT_CHARS,
            )
        else:
            _add_paragraph(
                doc, text, BODY_FONT, BODY_SIZE_PT,
                bold=BODY_BOLD, indent_chars=FIRST_LINE_INDENT_CHARS,
            )
        pending_blank = False

    doc.save(output_path)
    return output_path
