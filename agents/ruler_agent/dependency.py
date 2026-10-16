"""依赖关系与选择性重算支持。

把「人工修正了哪个字段」解析为「哪些条款需要重新裁决、哪些可以直接复用
上一次结果」，使规则引擎的重算范围与修正的影响范围一致，而不是无条件全量重跑。

依赖关系有两个来源：

1. **条款 → 事实字段**：取自全量规则集的 ``required_fact_fields``，
   即每条条款裁决所必需的事实字段路径。
2. **修正目标 → 事实字段**：人工回填的 ``target``（写入 02_structuring.json
   或 04_judge.json 的字段路径）到规则字段路径的映射。两者命名空间不同，
   以显式别名表桥接；无法识别的目标按「影响全部条款」保守处理。

安全约定：**只复用不受影响的条款结果，且被复用结果必须来自上一次运行**。
无法判定影响范围时一律全量重算，不做乐观复用。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

R = "forensic_facts."

# 规则字段组：人工回填目标路径 → 受其影响的规则事实字段前缀
TARGET_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    # 关节活动度：Judge 的结构化 ROM 与候选值都进入 ROM 查表
    "section4.structured_rom": (
        R + "rom_measurements",
        R + "rule_features.major_joint_loss_percent_max",
        R + "rule_features.major_joint_loss_25_plus",
    ),
    "forensic_facts.structured_rom": (
        R + "rom_measurements",
        R + "rule_features.major_joint_loss_percent_max",
    ),
    "section4.visual_rom_candidates": (R + "rom_measurements",),
    "forensic_facts.rom_measurements": (R + "rom_measurements",),
    # 骨折特征
    "forensic_facts.fracture_features.vertebral": (
        R + "fracture_features.vertebral",
        R + "rule_features.vertebral_count",
        R + "rule_features.vertebra_levels",
        R + "rule_features.two_vertebral_compression_fractures",
        R + "rule_features.one_vertebral_fracture_with_surgery",
    ),
    "forensic_facts.fracture_features.rib": (
        R + "fracture_features.rib",
        R + "rule_features.rib_fracture_count",
        R + "rule_features.rib_fracture_six_plus",
        R + "rule_features.rib_fracture_twelve_plus",
    ),
    "forensic_facts.fracture_features.limb": (
        R + "fracture_features.limb",
        R + "rule_features.limb_fractures",
    ),
    # 瘢痕与烧伤
    "forensic_facts.scar_burn_facts": (
        R + "scar_burn_facts",
        R + "rule_features.scar_bsa_percent",
        R + "rule_features.body_scar_pct",
        R + "rule_features.face_scar_total_length_cm",
        R + "rule_features.face_linear_scar_length_cm",
        R + "rule_features.scar",
    ),
    # 眼与神经
    "forensic_facts.eye_facts": (R + "eye_facts", R + "rule_features.eyeball_missing_or_removed",
                                 R + "rule_features.blindness_level_one_eye",
                                 R + "rule_features.visual_field_effective_pct_one_eye"),
    "forensic_facts.neuro_facts": (R + "neuro_facts", R + "rule_features.craniotomy_done",
                                   R + "rule_features.objective_neuro_deficit",
                                   R + "rule_features.adl_score"),
    # 手术与治疗
    "forensic_facts.treatment_events": (R + "treatment_events", R + "rule_features.long_bone_internal_fixation",
                                        R + "rule_features.long_bone_internal_fixation_items"),
    "forensic_facts.amputation_features": (R + "amputation_features",
                                           R + "rule_features.hand_or_foot_function_score"),
    "forensic_facts.ligament_meniscus_features": (
        R + "ligament_meniscus_features",
        R + "rule_features.knee_ligament_meniscus_collateral_surgery_affects_function",
    ),
    "forensic_facts.fracture_healing_status": (R + "fracture_healing_status",
                                               R + "rule_features.delayed_union_or_nonunion",
                                               R + "rule_features.chronic_osteomyelitis_long_nonunion"),
    "forensic_facts.period_rule_features": (R + "period_rule_features",),
    "forensic_facts.rule_features": (R + "rule_features",),
}

# 裁决器分组：每个裁决函数可能产出的规则族。
# 用于判断某个裁决器是否可以整体跳过。
MATCHER_FAMILIES: Dict[str, Optional[Set[str]]] = {
    "spine": {"vertebral_fracture"},
    "rib": {"rib_fracture"},
    "structured_feature": None,          # 其余自动条款（None = 取补集）
    "rom": {"joint_function"},
    "legacy": None,                      # 由调用方传入条款索引
}


def _segments(path: str) -> List[str]:
    return [s for s in str(path or "").split(".") if s]


def _is_prefix(short: str, long: str) -> bool:
    a, b = _segments(short), _segments(long)
    return len(a) <= len(b) and b[: len(a)] == a


class RuleDependency:
    """条款与事实字段之间的依赖关系，支持影响范围解析。"""

    def __init__(self, rule_inventory=None):
        if rule_inventory is None:
            import rule_inventory as rule_inventory  # noqa: WPS433
        self._ri = rule_inventory
        self.disability_rules: List[Dict[str, Any]] = list(self._ri.build_disability_rules())
        self._clause_rule = {r["clause_id"]: r for r in self.disability_rules}
        self._auto_clauses = {r["clause_id"] for r in self.disability_rules
                              if r["disposition"] == "auto_decidable"}
        # 字段前缀 → 条款集合
        self._field_to_clauses: Dict[str, Set[str]] = {}
        for rule in self.disability_rules:
            for field in rule.get("required_fact_fields") or []:
                self._field_to_clauses.setdefault(field, set()).add(rule["clause_id"])

    # ---------------------------------------------------------------- 条款侧

    @property
    def auto_clauses(self) -> Set[str]:
        return set(self._auto_clauses)

    def families_of(self, clause_id: str) -> Set[str]:
        rule = self._clause_rule.get(clause_id)
        return {rule["rule_family"]} if rule else set()

    def matcher_clause_sets(self, legacy_clauses: Optional[Iterable[str]] = None) -> Dict[str, Set[str]]:
        """每个裁决器可能的条款集合。"""
        by_family: Dict[str, Set[str]] = {}
        for clause in self._auto_clauses:
            for family in self.families_of(clause):
                by_family.setdefault(family, set()).add(clause)

        spine = set(by_family.get("vertebral_fracture", set()))
        rib = set(by_family.get("rib_fracture", set()))
        rom = set(by_family.get("joint_function", set()))
        structured = self._auto_clauses - spine - rib - rom
        legacy = set(legacy_clauses or ())
        return {
            "spine": spine,
            "rib": rib,
            "structured_feature": structured,
            "rom": rom,
            "legacy": legacy,
        }

    # ---------------------------------------------------------------- 字段侧

    def clauses_for_fields(self, field_paths: Sequence[str]) -> Set[str]:
        """给定事实字段路径，返回受影响的条款集合。"""
        affected: Set[str] = set()
        for path in field_paths:
            for field, clauses in self._field_to_clauses.items():
                if _is_prefix(path, field) or _is_prefix(field, path):
                    affected |= clauses
        return affected

    def _aliased_fields(self, target: str) -> Optional[Set[str]]:
        """把人工回填目标路径展开为规则事实字段。无法识别时返回 None。"""
        target = str(target or "").strip()
        if not target:
            return None
        fields: Set[str] = set()
        matched = False
        for prefix, mapped in TARGET_FIELD_ALIASES.items():
            if _is_prefix(prefix, target) or _is_prefix(target, prefix):
                fields.update(mapped)
                matched = True
        if not matched and target.startswith("forensic_facts."):
            # 直接写在 forensic_facts 命名空间下的字段按段前缀匹配
            fields.add(target)
            matched = True
        return fields if matched else None

    def resolve_overrides(self, overrides: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, Any]:
        """解析人工回填的影响范围。

        返回 ``affected_clauses``、``field_paths``、``full_rerun`` 与原因。
        ``full_rerun`` 为真表示存在无法识别的目标，必须全量重算。
        """
        field_paths: List[str] = []
        affected: Set[str] = set()
        unknown: List[str] = []
        for item in overrides or []:
            if not isinstance(item, dict):
                continue
            target = item.get("target") or ""
            fields = self._aliased_fields(target)
            if fields is None:
                unknown.append(str(target))
                continue
            field_paths.extend(sorted(fields))
            affected |= self.clauses_for_fields(sorted(fields))
        return {
            "field_paths": sorted(set(field_paths)),
            "affected_clauses": affected,
            "unknown_targets": unknown,
            "full_rerun": bool(unknown),
        }

    def plan(self, overrides: Optional[Sequence[Dict[str, Any]]],
             legacy_clauses: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """生成重算计划：哪些裁决器需要重跑，哪些条款可复用。"""
        resolved = self.resolve_overrides(overrides)
        matcher_sets = self.matcher_clause_sets(legacy_clauses)
        if resolved["full_rerun"]:
            return {
                **resolved,
                "run_matchers": sorted(matcher_sets),
                "skip_matchers": [],
                "reusable_clauses": set(),
                "reason": "存在无法识别的回填目标，保守全量重算",
            }
        affected = resolved["affected_clauses"]
        run, skip = [], []
        reusable: Set[str] = set()
        for name, clauses in matcher_sets.items():
            if name == "legacy" and not clauses:
                # 没有索引信息时不给 legacy 做复用判断
                run.append(name)
                continue
            if clauses & affected:
                run.append(name)
            else:
                skip.append(name)
                reusable |= clauses - affected
        return {
            **resolved,
            "run_matchers": sorted(run),
            "skip_matchers": sorted(skip),
            "reusable_clauses": reusable,
            "reason": "按依赖关系选择性重算" if skip else "全部裁决器均受影响",
        }


def disability_result_from_audit(entry: Dict[str, Any]):
    """把上一次运行审计块中的条款记录还原为 DisabilityResult。"""
    from rule_models import DisabilityResult  # noqa: WPS433

    return DisabilityResult(
        clause=str(entry.get("clause") or ""),
        text=str(entry.get("text") or ""),
        grade=int(entry.get("grade") or 0),
        joint=str(entry.get("joint") or ""),
        injured_side=str(entry.get("injured_side") or ""),
        loss_pct=float(entry.get("loss_pct") or 0.0),
        match_type=str(entry.get("match_type") or ""),
        details=list(entry.get("details") or []),
        branch_id=str(entry.get("branch_id") or ""),
        warnings=list(entry.get("warnings") or []),
        conclusion_type=str(entry.get("conclusion_type") or ""),
    )


def previous_disability_results(ruler_payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从上一份 05_ruler.json 中取出可复用的条款结果。"""
    if not isinstance(ruler_payload, dict):
        return {}
    audit = ruler_payload.get("audit") or {}
    entries = audit.get("candidate_disability_clauses") or []
    out: Dict[str, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        clause = str(entry.get("clause") or "")
        if clause and clause not in out:
            out[clause] = disability_result_from_audit(entry)
    return out
