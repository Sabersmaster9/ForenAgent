"""把 ForenAgent 输出与同案专家鉴定意见书做逐项对比。

用法：
    python -m machine_eval.compare_with_expert 001 002 007
    python -m machine_eval.compare_with_expert            # 对比全部已跑案件

对比分三层，对应人工-智能体工作流里的不同责任边界：

1. 关键结论（必须一致）：伤残等级、适用条款、三期天数。这些由规则引擎裁决，
   系统没有理由判错，不一致即缺陷。
2. 可判定身份事实（不得写错）：姓名、性别、出生日期、身份证号。系统有能力判对，
   写错属文书事故；判不出应留空并转人工，不应编造。
3. 人工补充章节（允许空缺）：资料摘要、体格检查、在场人员、附件张数。按工作流设计，
   这些本就依赖鉴定人录入，空缺可接受，但必须在审计报告中被标记出来。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = ROOT / "test_result"
OUT_CSV = ROOT / "machine_eval" / "outputs" / "expert_comparison.csv"

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def extract_grade(text: str) -> str:
    """伤残等级：返回 '9' / '0'(不构成) / ''(未能判定)。"""
    body = re.sub(r"\s+", "", text or "")
    if re.search(r"(?:尚未|不|未)构成伤残", body):
        return "0"
    matches = re.findall(r"(?:已?构成|评定为|构成)([一二三四五六七八九十]{1,3})级伤残", body)
    if matches:
        value = CN_NUM.get(matches[-1])
        if value:
            return str(value)
    return ""


def extract_clauses(text: str) -> List[str]:
    """适用条款号，按出现顺序去重。"""
    body = re.sub(r"\s+", "", text or "")
    found: List[str] = []
    for match in re.finditer(r"第?(\d+(?:\.\d+){1,4})(?:条|[（(][a-z][）)])?", body):
        clause = match.group(1)
        if clause not in found:
            found.append(clause)
    return found


def extract_periods(text: str) -> Dict[str, Optional[int]]:
    """三期天数，兼容“误工期限以120日”“护理期限、营养期限均以60日”等写法。"""
    body = re.sub(r"\s+", "", text or "")
    result: Dict[str, Optional[int]] = {"误工": None, "护理": None, "营养": None}
    for label in list(result):
        match = re.search(label + r"(?:期限)?(?:均)?以(\d+)日左右为宜", body)
        if match:
            result[label] = int(match.group(1))
    # “护理期限、营养期限均以60日左右为宜”这一写法只出现一次天数，需回填两处。
    for match in re.finditer(r"([^，。；]{2,20}期限)均以(\d+)日左右为宜", body):
        for label in result:
            if label in match.group(1) and result[label] is None:
                result[label] = int(match.group(2))
    return result


def extract_identity(text: str) -> Dict[str, str]:
    body = re.sub(r"[ \t]+", "", text or "")
    result: Dict[str, str] = {}
    match = re.search(r"被鉴定人[:：]([一-龥]{2,5})", body)
    if match:
        result["name"] = match.group(1)
    # 专家意见书里“公民身份号码”“身份证号码”“身份证号”写法混用，都要能匹配。
    match = re.search(
        r"(?:公民身份号码|公民身份证号码|身份证号码|身份证号|身份号码)[:：]?\s*([0-9Xx]{15,18})",
        body,
    )
    if match:
        result["id"] = match.group(1).upper()
    match = re.search(r"被鉴定人[:：][一-龥]{2,5}[，,]([男女])", body)
    if match:
        result["gender"] = match.group(1)
    match = re.search(r"((?:19|20)\d{2}年\d{1,2}月\d{1,2}日)出生", body)
    if match:
        result["birth"] = match.group(1)
    return result


def compare_case(case_id: str) -> Optional[dict]:
    case_dir = RESULT_DIR / case_id
    expert_path = case_dir / "expert_reference.md"
    ai_path = case_dir / "06_boss_reviewed.md"
    if not expert_path.exists() or not ai_path.exists():
        return None

    expert_text = _read(expert_path)
    ai_text = _read(ai_path)
    structuring = _read_json(case_dir / "02_structuring.json")

    expert_grade = extract_grade(expert_text)
    ai_grade = extract_grade(ai_text)
    expert_clauses = extract_clauses(expert_text)
    ai_clauses = extract_clauses(ai_text)
    expert_periods = extract_periods(expert_text)
    ai_periods = extract_periods(ai_text)
    expert_id = extract_identity(expert_text)
    ai_id = extract_identity(ai_text)

    row = {
        "case_id": case_id,
        "gold_grade": expert_grade,
        "ai_grade": ai_grade,
        "grade_ok": expert_grade != "" and expert_grade == ai_grade,
        "gold_clause": "|".join(expert_clauses[:4]),
        "ai_clause": "|".join(ai_clauses[:4]),
        "clause_ok": bool(set(expert_clauses) & set(ai_clauses)),
        # 结论层
        "gold_periods": "/".join(str(expert_periods[k] or "-") for k in ("误工", "护理", "营养")),
        "ai_periods": "/".join(str(ai_periods[k] or "-") for k in ("误工", "护理", "营养")),
        "period_ok": expert_periods == ai_periods,
        # 身份层：系统有能力判对，判错即缺陷
        "gold_id": expert_id.get("id", ""),
        "ai_id": structuring.get("subject_id", ""),
        "id_ok": bool(expert_id.get("id")) and expert_id.get("id") == structuring.get("subject_id"),
        "gold_birth": expert_id.get("birth", ""),
        "ai_birth": structuring.get("subject_birth", ""),
        "gold_name": expert_id.get("name", ""),
        "ai_name": structuring.get("subject_name", ""),
        # 人工补充层：允许空缺，只看系统是否留下了可见标记
        "ai_summary_items": len(structuring.get("medical_summary_items") or []),
        "ai_present_personnel": "空" if not structuring.get("present_personnel") else "有",
        "ai_has_physical_exam": "无" if "未能从材料中形成" in ai_text else "有",
    }
    return row


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ForenAgent 输出 vs 专家鉴定意见书逐项对比")
    parser.add_argument("cases", nargs="*", help="案件编号；省略则对比全部已跑案件")
    args = parser.parse_args(argv)

    if args.cases:
        case_ids = args.cases
    else:
        case_ids = sorted(
            (p.name for p in RESULT_DIR.iterdir() if p.is_dir() and (p / "expert_reference.md").exists()),
            key=lambda x: (len(x), x),
        )

    rows = [r for r in (compare_case(c) for c in case_ids) if r]
    if not rows:
        print("没有找到可对比的案件（需要同时存在 expert_reference.md 与 06_boss_reviewed.md）。")
        return 1

    header = f"{'案件':<7}{'伤残等级':<12}{'条款':<10}{'三期(误/护/营)':<20}{'身份证':<10}{'摘要':<6}{'体检':<6}"
    print(header)
    print("-" * len(header))
    for r in rows:
        grade = f"{r['ai_grade'] or '-'} vs {r['gold_grade'] or '-'}"
        grade_mark = "✓" if r["grade_ok"] else "✗"
        clause_mark = "✓" if r["clause_ok"] else "✗"
        period_mark = "✓" if r["period_ok"] else "✗"
        id_mark = "✓" if r["id_ok"] else "✗"
        print(
            f"{r['case_id']:<7}{grade+grade_mark:<12}{clause_mark:<10}"
            f"{r['ai_periods']+' '+period_mark:<20}{id_mark:<10}"
            f"{r['ai_summary_items']:<6}{r['ai_has_physical_exam']:<6}"
        )

    total = len(rows)
    print()
    print(f"案件数 {total}")
    print(f"  伤残等级一致 : {sum(1 for r in rows if r['grade_ok'])}/{total}")
    print(f"  条款命中     : {sum(1 for r in rows if r['clause_ok'])}/{total}")
    print(f"  三期一致     : {sum(1 for r in rows if r['period_ok'])}/{total}")
    print(f"  身份证正确   : {sum(1 for r in rows if r['id_ok'])}/{total}")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n明细已写入 {OUT_CSV.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK，直接打印 ✓/✗ 会抛 UnicodeEncodeError。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
