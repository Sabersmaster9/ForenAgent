"""验证人机协同闭环：系统标记的待补充项 → GUI 回填 → 进入正文 → 重跑不丢。

核心方法是“哨兵测试”，不依赖人工维护的字段清单：
往某个候选字段写入一个独特值再渲染，看它是否真的出现在报告正文里。
这样能发现“字段清单写着会进正文、实际没进”或反过来的契约错误。

用法：
    python -m machine_eval.check_human_loop 001 007
    python -m machine_eval.check_human_loop            # 检查全部已跑案件
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = ROOT / "test_result"
STAGES = ["02_structuring.json", "03_visual.json", "04_judge.json", "05_ruler.json"]

sys.path.insert(0, str(ROOT))

from agents.boss_agent.boss_agent_main import (  # noqa: E402
    _target_reaches_report,
    apply_human_overrides,
    audit_answers_to_overrides,
    apply_visual_pre_review_to_visual_payload,
    build_audit_questions,
    build_pre_audit_supplement_session,
    build_visual_pre_review_session,
    pre_audit_supplement_to_overrides,
    visual_pre_review_to_overrides,
)
from agents.boss_agent.report_renderer import render_report  # noqa: E402
from agents.ruler_agent.ruler_agent_main import run_pipeline as ruler_run  # noqa: E402

TEXT_SENTINEL = "哨兵甲乙丙"
NUM_SENTINEL = 7654321
# 结论类字段要用“长得像结论”的哨兵：渲染器只把形如伤残结论的文字放到结论位置，
# 裸哨兵会被正确拒收，从而得到假阴性。与 grade_number 用数字哨兵是同一个道理。
CONCLUSION_SENTINEL = f"其损伤及后遗症尚未构成伤残等级（{TEXT_SENTINEL}）"


def _load(case: str) -> Optional[Dict[str, dict]]:
    case_dir = RESULT_DIR / case
    if not all((case_dir / s).exists() for s in STAGES):
        return None
    data = {}
    for stage in STAGES:
        try:
            data[stage] = json.loads((case_dir / stage).read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return data


def _render(data: Dict[str, dict]) -> str:
    return render_report(
        structuring=data["02_structuring.json"],
        judge=data["04_judge.json"],
        ruler=data["05_ruler.json"],
        report_date="2026年10月13日",
    )


# 人工回填的 target_json 用 structuring/judge/visual 表示，这里映射到阶段文件。
ROOT_STAGE = {
    "structuring": "02_structuring.json",
    "visual": "03_visual.json",
    "judge": "04_judge.json",
}


def _set_path(data: Dict[str, dict], target_root: str, target: str, value: Any) -> bool:
    """按点分路径写入；路径不存在时创建。返回是否成功。"""
    node: Any = data[ROOT_STAGE.get(target_root, "02_structuring.json")]
    parts = [p for p in target.split(".") if p]
    if not parts:
        return False
    for part in parts[:-1]:
        if not isinstance(node, dict):
            return False
        node = node.setdefault(part, {})
    if not isinstance(node, dict):
        return False
    node[parts[-1]] = value
    return True


def sentinel_reaches_report(data: Dict[str, dict], target_root: str, target: str, draft: str = "") -> bool:
    """把哨兵值写进字段，看它是否出现在渲染结果里。

    有两类字段不能按“单字段写入”探测，需要按真实结构构造：
    - human_disability_confirmation.* 是一组联动字段，单独写 clause 不会生效；
    - human_text_patches 是 {锚点行: 替换文字} 字典，必须同时给出锚点。
    """
    if target.startswith("forensic_facts.human_text_patches"):
        anchor = next(
            (line.strip() for line in (draft or "").splitlines()
             if line.strip() and "：" in line and len(line.strip()) > 8),
            "",
        )
        if not anchor:
            return False
        probe = copy.deepcopy(data)
        _set_path(probe, target_root, "forensic_facts.human_text_patches", {anchor: TEXT_SENTINEL})
        try:
            return TEXT_SENTINEL in _render(probe)
        except Exception:
            return False

    if target.startswith("forensic_facts.human_disability_confirmation."):
        # 该组字段是一个联动结构，且分“控制字段”和“内容字段”两类：
        #   - conclusion_type / grade_number 决定走阳性还是阴性分支，值本身不逐字入文；
        #   - standard_name / clause / clause_text / reason 会作为正文文字出现。
        # 两类必须用不同的探测方式，否则会把控制字段误报成“字段丢失”。
        prefix = "forensic_facts.human_disability_confirmation."
        field = target[len(prefix):]

        def _probe(conclusion_type: str, grade: str, set_target: bool) -> Dict[str, dict]:
            probe = copy.deepcopy(data)
            _set_path(probe, target_root, prefix + "conclusion_type", conclusion_type)
            _set_path(probe, target_root, prefix + "grade_number", grade)
            _set_path(probe, target_root, prefix + "clause_text", CONCLUSION_SENTINEL)
            _set_path(probe, target_root, prefix + "clause", "5.9.6.2")
            if set_target:
                sentinel = CONCLUSION_SENTINEL if field == "clause_text" else TEXT_SENTINEL
                _set_path(probe, target_root, target, sentinel)
            return probe

        def _renders(probe: Dict[str, dict]) -> str:
            try:
                return _render(probe)
            except Exception:
                return ""

        if field == "conclusion_type":
            # 控制字段：验证“改变它会不会改变正文”，而不是找它本身。
            positive = _renders(_probe("positive_grade", "9", False))
            negative = _renders(_probe("no_disability", "0", False))
            return bool(positive) and positive != negative
        if field == "grade_number":
            # 等级在正文里渲染成中文（八级），不能用字符串哨兵探测；阴性不渲染等级。
            return "八级" in _renders(_probe("positive_grade", "8", False))
        # 内容字段：阳性与阴性两条渲染路径都应把它写进正文。
        # 只按阳性构造探针，会漏掉阴性分支丢弃人工表述的问题（2026-10-13 实际漏过一次）。
        outcomes: List[bool] = []
        for conclusion_type, grade in (("positive_grade", "9"), ("no_disability", "0")):
            rendered = _renders(_probe(conclusion_type, grade, True))
            outcomes.append(bool(rendered) and TEXT_SENTINEL in rendered)
        return bool(outcomes) and all(outcomes)

    for sentinel in (TEXT_SENTINEL, str(NUM_SENTINEL)):
        probe = copy.deepcopy(data)
        if not _set_path(probe, target_root, target, sentinel):
            continue
        try:
            rendered = _render(probe)
        except Exception:
            continue
        if sentinel in rendered:
            return True
    return False


def collect_question_targets(data: Dict[str, dict]) -> List[Tuple[str, str, str]]:
    """收集审计问题里出现的全部 (target_json, target, 问题摘要)。"""
    draft = _render(data)
    session = build_audit_questions(
        data["02_structuring.json"],
        data["03_visual.json"],
        data["04_judge.json"],
        data["05_ruler.json"],
        draft,
    )
    found: List[Tuple[str, str, str]] = []
    seen = set()
    for question in session.get("questions", []) or []:
        for field in question.get("fields", []) or []:
            if not isinstance(field, dict):
                continue
            target = str(field.get("target") or "")
            root = str(field.get("target_json") or ("judge" if target.startswith("section4") else "structuring"))
            if target and (root, target) not in seen:
                seen.add((root, target))
                found.append((root, target, str(question.get("question") or "")[:34]))
    return found


def check_contract(case: str, data: Dict[str, dict]) -> List[str]:
    """逐字段核对“契约清单”与“哨兵实测”是否一致。"""
    problems: List[str] = []
    draft = _render(data)
    for root, target, summary in collect_question_targets(data):
        claimed = _target_reaches_report(target)
        actual = sentinel_reaches_report(data, root, target, draft)
        if claimed != actual:
            # final_metadata 等字段可能只在特定条件下才写入正文，这里如实报告差异。
            problems.append(
                f"    契约判定={claimed} 实测={actual}  {target}   (问题：{summary})"
            )
    return problems


def round_trip(case: str, data: Dict[str, dict]) -> Tuple[int, List[str]]:
    """模拟 GUI 全部作答，走完 回填 → 重跑 Ruler → 重渲染，检查改动是否落地。"""
    draft = _render(data)
    session = build_audit_questions(
        data["02_structuring.json"],
        data["03_visual.json"],
        data["04_judge.json"],
        data["05_ruler.json"],
        draft,
    )

    answers = []
    expected: List[str] = []
    for index, question in enumerate(session.get("questions", []) or [], start=1):
        fields = [f for f in (question.get("fields") or []) if isinstance(f, dict)]
        if not fields:
            continue
        marker = f"模拟答复{index:02d}"
        field_values = {f.get("target"): marker for f in fields if f.get("target")}
        # 数字类字段给一个合法数值，避免渲染异常。
        for field in fields:
            target = str(field.get("target") or "")
            if re.search(r"(days|count|number|serial|year|_no)$", target):
                field_values[target] = str(NUM_SENTINEL)
        answers.append({
            "question_id": question.get("id"),
            "question": question,
            "status": "answered",
            "answer_text": marker,
            "evidence_note": "自动测试",
            "field_values": field_values,
            "operator": "自检",
        })
        expected.append(marker)

    session["answers"] = answers
    overrides = audit_answers_to_overrides(session)

    workdir = Path(tempfile.mkdtemp())
    try:
        for stage in STAGES:
            (workdir / stage).write_text(json.dumps(data[stage], ensure_ascii=False), encoding="utf-8")
        apply_human_overrides(
            str(workdir / "02_structuring.json"),
            str(workdir / "04_judge.json"),
            _write_overrides(workdir, overrides),
            str(workdir),
            lambda _: None,
        )
        ruler_run(str(workdir / "02_structuring.json"), str(workdir / "04_judge.json"), str(workdir))
        merged = {
            "02_structuring.json": json.loads((workdir / "02_structuring.json").read_text(encoding="utf-8")),
            "03_visual.json": data["03_visual.json"],
            "04_judge.json": json.loads((workdir / "04_judge.json").read_text(encoding="utf-8")),
            "05_ruler.json": json.loads((workdir / "05_ruler.json").read_text(encoding="utf-8")),
        }
        first = _render(merged)
        # 再渲染一次，模拟“重跑后重渲染”，改动应保持。
        second = _render(merged)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    missed = [m for m in expected if m not in first]
    unstable = [m for m in expected if (m in first) != (m in second)]
    notes: List[str] = []
    if missed:
        notes.append(f"    未落入正文的答复：{', '.join(missed[:6])}" + ("…" if len(missed) > 6 else ""))
    if unstable:
        notes.append(f"    重渲染后不稳定的答复：{', '.join(unstable[:6])}")
    return len(expected), notes


def check_pre_audit(data: Dict[str, dict]) -> List[str]:
    """审计前补充窗口：填写的体格检查/辅助检查是否真的进入报告正文。"""
    notes: List[str] = []
    session = build_pre_audit_supplement_session(
        data["02_structuring.json"], data["03_visual.json"], data["04_judge.json"]
    )
    session["field_values"] = {
        "section4.physical_exam.manual_findings": f"{TEXT_SENTINEL}查体所见",
        "section4.auxiliary_checks.manual_review_opinion": f"{TEXT_SENTINEL}审片结论",
    }
    overrides = pre_audit_supplement_to_overrides(session)
    if not (overrides.get("fact_overrides") or []):
        return ["    审计前补充：填写后未生成回填项"]

    workdir = Path(tempfile.mkdtemp())
    try:
        for stage in STAGES:
            (workdir / stage).write_text(json.dumps(data[stage], ensure_ascii=False), encoding="utf-8")
        apply_human_overrides(
            str(workdir / "02_structuring.json"),
            str(workdir / "04_judge.json"),
            _write_overrides(workdir, overrides),
            str(workdir),
            lambda _: None,
        )
        merged = {
            "02_structuring.json": json.loads((workdir / "02_structuring.json").read_text(encoding="utf-8")),
            "03_visual.json": data["03_visual.json"],
            "04_judge.json": json.loads((workdir / "04_judge.json").read_text(encoding="utf-8")),
            "05_ruler.json": data["05_ruler.json"],
        }
        rendered = _render(merged)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if f"{TEXT_SENTINEL}查体所见" not in rendered:
        notes.append("    审计前补充：体格检查未进入正文")
    if f"{TEXT_SENTINEL}审片结论" not in rendered:
        notes.append("    审计前补充：辅助检查未进入正文")
    return notes


def check_visual_review(data: Dict[str, dict]) -> List[str]:
    """量角器预审窗口：采纳一条读数后，是否生成回填项并更新视觉载荷。"""
    notes: List[str] = []
    session = build_visual_pre_review_session(data["02_structuring.json"], data["03_visual.json"])
    items = session.get("items") or []
    if not items:
        return ["    视觉预审：本案无可预审图片，跳过"]
    first = items[0]
    session["answers"] = {
        str(first.get("review_id")): {
            "adopt": "adopt",
            "joint": first.get("joint") or "肩关节",
            "side": first.get("side") or "左",
            "direction": first.get("direction") or "前屈上举",
            "angle": first.get("default_angle") or 90,
            "reason": "自检",
        }
    }
    overrides = visual_pre_review_to_overrides(session)
    count = len(overrides.get("fact_overrides") or [])
    if count == 0:
        notes.append("    视觉预审：采纳读数后未生成任何回填项")
    updated = apply_visual_pre_review_to_visual_payload(copy.deepcopy(data["03_visual.json"]), session)
    if updated == data["03_visual.json"]:
        notes.append("    视觉预审：采纳后视觉载荷未发生变化")
    return notes


def _write_overrides(workdir: Path, overrides: dict) -> str:
    path = workdir / "overrides.json"
    path.write_text(json.dumps(overrides, ensure_ascii=False), encoding="utf-8")
    return str(path)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="人机协同闭环自检")
    parser.add_argument("cases", nargs="*", help="案件编号；省略则检查全部已跑案件")
    args = parser.parse_args(argv)

    if args.cases:
        case_ids = args.cases
    else:
        case_ids = sorted(
            (p.name for p in RESULT_DIR.iterdir() if p.is_dir() and (p / STAGES[0]).exists()),
            key=lambda x: (len(x), x),
        )

    total_contract = 0
    total_questions = 0
    for case in case_ids:
        data = _load(case)
        if not data:
            print(f"[跳过] {case}：缺少阶段文件")
            continue
        print(f"\n[{case}]")
        problems = check_contract(case, data)
        targets = collect_question_targets(data)
        print(f"  审计问题涉及的字段数：{len(targets)}")
        if problems:
            total_contract += len(problems)
            print(f"  ⚠ 契约清单与实测不一致 {len(problems)} 处：")
            for line in problems:
                print(line)
        else:
            print("  ✓ 契约清单与哨兵实测完全一致")

        expected, notes = round_trip(case, data)
        total_questions += expected
        print(f"  模拟作答 {expected} 项")
        if notes:
            for line in notes:
                print(line)
        else:
            print("  ✓ 全部答复均落入正文，且重渲染后保持")

        for label, extra in (("审计前补充", check_pre_audit(data)), ("视觉预审", check_visual_review(data))):
            if extra:
                for line in extra:
                    print(line)
            else:
                print(f"  ✓ {label}窗口回填正常")

    print(f"\n汇总：检查 {len(case_ids)} 案，契约不一致 {total_contract} 处，模拟答复 {total_questions} 项")
    return 0 if total_contract == 0 else 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
