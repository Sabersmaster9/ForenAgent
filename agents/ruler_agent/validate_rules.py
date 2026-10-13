"""Validation CLI for the Ruler Agent rule engine."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List

try:
    from .coverage import build_coverage_reports, validate_schema
    from .fact_adapter import adapt_inputs
    from .disability_engine import match_disability_v2
    from .period_engine import match_period_v2
    from . import ruler_agent_main as legacy
except ImportError:  # 支持直接脚本执行
    from coverage import build_coverage_reports, validate_schema
    from fact_adapter import adapt_inputs
    from disability_engine import match_disability_v2
    from period_engine import match_period_v2
    import ruler_agent_main as legacy

BASE_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = BASE_DIR / "fixtures"


def _load_json(path: str | Path) -> Dict[str, Any] | List[Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_from_data(structuring_data: Dict[str, Any], judge_data: Dict[str, Any] | None = None):
    facts = adapt_inputs(structuring_data, judge_data or {})
    disabilities, da = match_disability_v2(
        facts=facts,
        legacy_index=legacy.DISABILITY_CLAUSE_INDEX,
        legacy_matcher=legacy.match_disability,
        calc_joint_loss=legacy.calc_joint_loss,
        muscle_grade="M5",
    )
    period, pa = match_period_v2(facts, legacy.PERIOD_CLAUSE_INDEX, legacy._select_period_sub)
    return disabilities, period, {**da, **pa}


def check_coverage() -> int:
    summary = build_coverage_reports()
    print("[OK] 覆盖矩阵已生成")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def check_schema() -> int:
    errors = validate_schema()
    if errors:
        print("[X] schema 检查失败")
        for err in errors:
            print("-", err)
        return 1
    print("[OK] schema 检查通过")
    return 0


def check_synthetic() -> int:
    errors: List[str] = []
    disability_cases = _load_json(FIXTURE_DIR / "synthetic_disability_cases.json")
    for case in disability_cases:
        disabilities, _, _ = _run_from_data(case.get("facts", {}), {})
        expected = case.get("expected", {})
        matched = [
            r for r in disabilities
            if r.clause == expected.get("clause")
            and (not expected.get("branch_id") or r.branch_id == expected.get("branch_id"))
            and r.grade == expected.get("grade")
            and (not expected.get("match_type") or r.match_type == expected.get("match_type"))
        ]
        if not matched:
            errors.append(f"{case.get('case_id')}: 未命中预期伤残 {expected}; 实际={[{'clause': r.clause, 'branch_id': r.branch_id, 'grade': r.grade, 'match_type': r.match_type} for r in disabilities]}")
    period_cases = _load_json(FIXTURE_DIR / "synthetic_period_cases.json")
    for case in period_cases:
        _, period, _ = _run_from_data(case.get("facts", {}), {})
        expected = case.get("expected", {})
        manual_expected = expected.get("requires_manual_period_assessment")
        manual_ok = True if manual_expected is None else period.requires_manual_period_assessment == manual_expected
        if expected.get("clause_contains") not in period.clause or period.误工 != expected.get("误工") or period.护理 != expected.get("护理") or period.营养 != expected.get("营养") or not manual_ok:
            errors.append(f"{case.get('case_id')}: 三期不符合预期 {expected}; 实际={period}")
    if errors:
        print("[X] synthetic 检查失败")
        for err in errors:
            print("-", err)
        return 1
    print("[OK] synthetic 检查通过")
    return 0


def check_case(case_path: str, judge_json: str | None, expected_case_id: str | None) -> int:
    structuring_data = _load_json(case_path)
    judge_data = _load_json(judge_json) if judge_json else {}
    disabilities, period, audit = _run_from_data(structuring_data, judge_data)
    print("[INFO] 伤残结果:")
    print(json.dumps([{"clause": r.clause, "branch_id": r.branch_id, "grade": r.grade, "grade_cn": r.grade_cn, "text": r.text} for r in disabilities], ensure_ascii=False, indent=2))
    print("[INFO] 三期结果:")
    print(json.dumps({"clause": period.clause, "误工": period.误工, "护理": period.护理, "营养": period.营养, "ranges": period.ranges}, ensure_ascii=False, indent=2))
    if not expected_case_id:
        return 0
    expected_path = FIXTURE_DIR / f"{expected_case_id}.json"
    if not expected_path.exists():
        print(f"[X] 预期夹具不存在: {expected_path}")
        return 1
    expected = _load_json(expected_path)
    ed = expected.get("expected_disability", {})
    ep = expected.get("expected_period", {})
    errors = []
    if ed:
        ok = any(r.clause == ed.get("clause") and r.grade == ed.get("grade") and (not ed.get("branch_id") or r.branch_id == ed.get("branch_id")) for r in disabilities)
        if not ok:
            errors.append(f"伤残未命中预期: {ed}")
        rejected = audit.get("rejected_disability_clauses", [])
        if ed.get("rejected_clause") and not any(x.get("clause") == ed.get("rejected_clause") for x in rejected):
            errors.append(f"未记录预期拒绝条款: {ed.get('rejected_clause')}")
    if ep:
        if ep.get("clause_contains") not in period.clause or period.误工 != ep.get("误工") or period.护理 != ep.get("护理") or period.营养 != ep.get("营养"):
            errors.append(f"三期未命中预期: {ep}")
    if errors:
        print("[X] case 检查失败")
        for err in errors:
            print("-", err)
        return 1
    print(f"[OK] case 检查通过: {expected_case_id}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="validate Ruler Agent rule engine")
    parser.add_argument("--check", choices=["coverage", "schema", "synthetic"], help="运行内置检查")
    parser.add_argument("--case", help="structuring JSON 路径")
    parser.add_argument("--judge-json", help="judge JSON 路径")
    parser.add_argument("--expected-case-id", help="fixtures/<id>.json 中的预期")
    args = parser.parse_args()

    if args.check == "coverage":
        raise SystemExit(check_coverage())
    if args.check == "schema":
        raise SystemExit(check_schema())
    if args.check == "synthetic":
        raise SystemExit(check_synthetic())
    if args.case:
        raise SystemExit(check_case(args.case, args.judge_json, args.expected_case_id))
    parser.print_help()


if __name__ == "__main__":
    main()
