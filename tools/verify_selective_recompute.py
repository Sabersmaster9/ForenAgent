"""验证依赖图与选择性重算。

核心断言：**选择性重算的结果必须与全量重算逐条一致**。
选择性只在「复用哪些未受影响条款、跳过哪些裁决器」上做文章，
绝不能改变最终裁决。

用法：
    python tools/verify_selective_recompute.py
"""

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.ruler_agent.dependency import RuleDependency, previous_disability_results  # noqa: E402
from agents.ruler_agent.disability_engine import match_disability_v2  # noqa: E402
from agents.ruler_agent.fact_adapter import adapt_inputs  # noqa: E402
from agents.ruler_agent.ruler_agent_main import (  # noqa: E402
    DISABILITY_CLAUSE_INDEX,
    calc_joint_loss,
    match_disability,
)

CASE_DIR = ROOT / "test_result" / "001"
LEGACY_CLAUSES = [c.get("clause") for c in DISABILITY_CLAUSE_INDEX if isinstance(c, dict)]


def load_case():
    structuring = json.loads((CASE_DIR / "02_structuring.json").read_text(encoding="utf-8"))
    judge = json.loads((CASE_DIR / "04_judge.json").read_text(encoding="utf-8"))
    return structuring, judge


def run_full(structuring, judge):
    facts = adapt_inputs(structuring, judge)
    return match_disability_v2(
        facts=facts,
        legacy_index=DISABILITY_CLAUSE_INDEX,
        legacy_matcher=match_disability,
        calc_joint_loss=calc_joint_loss,
        muscle_grade="M5",
    )


def signature(results):
    return sorted((r.clause, r.grade, r.branch_id, round(float(r.loss_pct or 0.0), 3)) for r in results)


def main() -> int:
    failures = []

    def check(name, ok, detail=""):
        print(f"  [{'OK' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    structuring, judge = load_case()

    print("1) 基线全量运行")
    base_results, base_audit = run_full(structuring, judge)
    base_sig = signature(base_results)
    print(f"     条款 {len(base_sig)} 条，前三：{base_sig[:3]}")
    check("产生裁决结果", len(base_sig) > 0)

    previous = previous_disability_results({"audit": base_audit})
    check("可从审计块还原历史结果", len(previous) > 0, f"recovered={len(previous)}")

    print("2) 修改一个受 ROM 影响的字段")
    judge2 = copy.deepcopy(judge)
    rom = judge2.setdefault("section4", {}).setdefault("structured_rom", {})
    # 找到该案已确认的任一侧/关节/方向，把角度改小以触发不同的查表区间
    changed = False
    for side, joints in list(rom.items()):
        if not isinstance(joints, dict):
            continue
        for joint, directions in list(joints.items()):
            if not isinstance(directions, dict):
                continue
            for direction, value in list(directions.items()):
                if isinstance(value, (int, float)) and value > 40:
                    rom[side][joint][direction] = float(value) - 40.0
                    changed = True
                    print(f"     {side}/{joint}/{direction}: {value} -> {value - 40.0}")
                    break
            if changed:
                break
        if changed:
            break
    if not changed:
        rom.setdefault("左", {}).setdefault("膝关节", {})["屈曲"] = 95.0
        print("     结构化 ROM 为空，注入一条合成测量：左/膝关节/屈曲 = 95.0")

    print("3) 依赖解析")
    dep = RuleDependency()
    plan = dep.plan(
        [{"target": "section4.structured_rom", "value": 95.0, "apply": True}],
        legacy_clauses=LEGACY_CLAUSES,
    )
    print(f"     受影响条款 {len(plan['affected_clauses'])}：{sorted(plan['affected_clauses'])}")
    print(f"     重跑裁决器 {plan['run_matchers']}")
    print(f"     跳过裁决器 {plan['skip_matchers']}")
    print(f"     可复用条款 {len(plan['reusable_clauses'])} 条")
    check("解析出受影响条款", len(plan["affected_clauses"]) > 0, plan["reason"])
    check("成功跳过至少一个裁决器（选择性生效）", len(plan["skip_matchers"]) > 0)
    check("复用集合非空", len(plan["reusable_clauses"]) > 0)

    print("4) 选择性重算 vs 全量重算")
    sel_results, sel_audit = run_full_selective(structuring, judge2, {**plan, "previous_results": previous})
    full_results, _ = run_full(structuring, judge2)
    sel_sig, full_sig = signature(sel_results), signature(full_results)

    check("选择性结果与全量结果逐条一致", sel_sig == full_sig,
          f"selective={len(sel_sig)} full={len(full_sig)}"
          + ("" if sel_sig == full_sig else f"\n        差异：{set(sel_sig) ^ set(full_sig)}"))
    check("选择性模式确实跳过了裁决器",
          bool(sel_audit.get("recompute_plan", {}).get("skipped_matchers")),
          str(sel_audit.get("recompute_plan", {}).get("skipped_matchers")))
    reused = sel_audit.get("recompute_plan", {}).get("reused_clauses", [])
    print(f"     复用条款 {len(reused)} 条：{reused[:6]}{' ...' if len(reused) > 6 else ''}")

    print("5) 无法识别的回填目标必须退回全量重算")
    unknown_plan = dep.plan([{"target": "some.unknown.field", "value": 1, "apply": True}],
                            legacy_clauses=LEGACY_CLAUSES)
    check("未知目标触发全量重算", unknown_plan["full_rerun"] is True)
    check("未知目标下不跳过任何裁决器", len(unknown_plan["skip_matchers"]) == 0)

    print("6) 空回填不得改变结果")
    empty_plan = dep.plan([], legacy_clauses=LEGACY_CLAUSES)
    empty_results, _ = run_full_selective(structuring, judge, {**empty_plan, "previous_results": previous})
    check("空回填下结果与基线一致", signature(empty_results) == base_sig)

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("全部通过")
    return 0


def run_full_selective(structuring, judge, selective):
    facts = adapt_inputs(structuring, judge)
    return match_disability_v2(
        facts=facts,
        legacy_index=DISABILITY_CLAUSE_INDEX,
        legacy_matcher=match_disability,
        calc_joint_loss=calc_joint_loss,
        muscle_grade="M5",
        selective=selective,
    )


if __name__ == "__main__":
    raise SystemExit(main())
