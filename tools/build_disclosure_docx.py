"""把技术交底书 Markdown 构建为自包含的单份 Word 交底书（附图内嵌）。

与 md_to_docx.py 的区别：本脚本面向**交付给专利代理人的单份交底书**，
在「附图及附图的简单说明」一节后自动插入对应的附图，并按图号排序、
居中、加页码式图注，使一份 .docx 即可完整表达一项发明。

用法：
    <env>/python.exe tools/build_disclosure_docx.py \
        --md  <交底书.md> \
        --figures <附图目录> \
        --out <输出.docx>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

CJK_BODY = "宋体"
CJK_HEAD = "黑体"
MONO = "Consolas"
IMAGE_WIDTH_CM = 15.0

HEADING_LEVELS = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
_INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")
_FIG_LINE = re.compile(r"^(图\s*(\d+)\s*是.*)$")
_FIG_FILE = re.compile(r"图\s*(\d+)[_．.]?.*\.png$")


def _set_cjk(run, cjk: str = CJK_BODY, mono: bool = False) -> None:
    run.font.name = MONO if mono else cjk
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), MONO if mono else cjk)


def add_runs(paragraph, text: str) -> None:
    for piece in _INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            run = paragraph.add_run(piece[2:-2])
            run.bold = True
            _set_cjk(run)
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            run = paragraph.add_run(piece[1:-1])
            run.font.size = Pt(9.5)
            _set_cjk(run, mono=True)
        else:
            run = paragraph.add_run(piece)
            _set_cjk(run)


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and len(s) > 2


def _split_row(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\|[\s:\-|]+\|", line.strip()))


def index_figures(figure_dir: Path) -> dict:
    """按图号索引附图 PNG。"""
    found = {}
    if not figure_dir or not figure_dir.is_dir():
        return found
    for path in sorted(figure_dir.glob("*.png")):
        m = _FIG_FILE.match(path.name)
        if m:
            found[int(m.group(1))] = path
    return found


def insert_figures(doc: Document, numbers, figures: dict, log) -> int:
    """插入附图，每张独占一段，居中，图下加编号。"""
    inserted = 0
    for num in numbers:
        path = figures.get(num)
        doc.add_paragraph()
        if path is None:
            para = doc.add_paragraph()
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = para.add_run(f"【图{num} 未找到对应文件】")
            run.font.color.rgb = RGBColor(0xA3, 0x3A, 0x2E)
            _set_cjk(run)
            log.append(f"    图{num}：未找到文件")
            continue
        para = doc.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        para.add_run().add_picture(str(path), width=Cm(IMAGE_WIDTH_CM))
        caption = doc.add_paragraph()
        caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = caption.add_run(f"图{num}")
        run.font.size = Pt(10)
        _set_cjk(run)
        inserted += 1
        log.append(f"    图{num} ← {path.name}")
    return inserted


def build(md_path: Path, figure_dir: Path, out_path: Path) -> None:
    lines = md_path.read_text(encoding="utf-8").splitlines()
    figures = index_figures(figure_dir)
    doc = Document()

    style = doc.styles["Normal"]
    style.font.name = CJK_BODY
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), CJK_BODY)

    log: list = []
    pending_figs: list = []      # 已读到的「图N是…」编号
    in_figure_section = False

    def flush_figures() -> None:
        nonlocal pending_figs
        if pending_figs:
            insert_figures(doc, pending_figs, figures, log)
            pending_figs = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_figures()
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            para = doc.add_paragraph()
            para.paragraph_format.left_indent = Pt(12)
            run = para.add_run("\n".join(block))
            run.font.size = Pt(9)
            _set_cjk(run, mono=True)
            continue

        if _is_table_row(line):
            flush_figures()
            rows = []
            while i < len(lines) and _is_table_row(lines[i]):
                if not _is_separator(lines[i]):
                    rows.append(_split_row(lines[i]))
                i += 1
            if rows:
                width = max(len(r) for r in rows)
                table = doc.add_table(rows=0, cols=width)
                table.style = "Table Grid"
                for r_idx, row in enumerate(rows):
                    cells = table.add_row().cells
                    for c_idx in range(width):
                        text = row[c_idx] if c_idx < len(row) else ""
                        para = cells[c_idx].paragraphs[0]
                        para.text = ""
                        add_runs(para, text)
                        for run in para.runs:
                            run.font.size = Pt(9)
                            if r_idx == 0:
                                run.bold = True
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_figures()
            title = m.group(2)
            in_figure_section = bool(re.match(r"^六[、.]\s*附图", title))
            heading = doc.add_heading("", level=HEADING_LEVELS[len(m.group(1))])
            add_runs(heading, title)
            for run in heading.runs:
                _set_cjk(run, CJK_HEAD)
                run.font.color.rgb = RGBColor(0, 0, 0)
            i += 1
            continue

        if re.fullmatch(r"-{3,}|\*{3,}", stripped):
            i += 1
            continue

        # 附图说明行：收集图号，读完连续行后统一插图
        fig = _FIG_LINE.match(stripped)
        if fig and in_figure_section:
            para = doc.add_paragraph()
            add_runs(para, fig.group(1))
            pending_figs.append(int(fig.group(2)))
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if not _FIG_LINE.match(nxt):
                flush_figures()
                in_figure_section = False
            i += 1
            continue

        flush_figures()

        if stripped.startswith(">"):
            para = doc.add_paragraph()
            para.paragraph_format.left_indent = Pt(18)
            add_runs(para, stripped.lstrip("> ").strip())
            for run in para.runs:
                run.font.size = Pt(9.5)
                run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
            i += 1
            continue

        m_cb = re.match(r"^-\s*\[([ xX])\]\s*(.*)$", stripped)
        m_ul = re.match(r"^[-*]\s+(.*)$", stripped)
        m_ol = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if m_cb:
            para = doc.add_paragraph(style="List Bullet")
            add_runs(para, ("☑ " if m_cb.group(1).lower() == "x" else "☐ ") + m_cb.group(2))
            i += 1
            continue
        if m_ul:
            para = doc.add_paragraph(style="List Bullet")
            add_runs(para, m_ul.group(1))
            i += 1
            continue
        if m_ol:
            para = doc.add_paragraph(style="List Number")
            add_runs(para, m_ol.group(2))
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        para = doc.add_paragraph()
        add_runs(para, stripped)
        para.paragraph_format.space_after = Pt(6)
        i += 1

    flush_figures()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))

    sys.stdout.reconfigure(encoding="utf-8")
    print(f"  已生成 {out_path.name}（{out_path.stat().st_size // 1024} KB）")
    print(f"  内嵌附图 {sum(1 for x in log if '←' in x)} 张")
    for entry in log:
        print(entry)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--md", required=True)
    parser.add_argument("--figures", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    build(Path(args.md), Path(args.figures), Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
