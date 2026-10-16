"""Deterministic disability matching driven by forensic facts."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .rule_models import DisabilityResult, RulerFacts


UNKNOWN_VALUES = {None, "", "不详", "未查找到", "无", "None", "none", "null"}
NO_DISABILITY_TEXT_VALUES = {"no_disability", "不构成伤残", "未构成伤残", "不构成", "未构成", "0", 0}


def _to_float(value: Any) -> float | None:
    if value in UNKNOWN_VALUES:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("%", "").replace(" ", "").replace("，", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int:
    parsed = _to_float(value)
    return int(parsed) if parsed is not None else 0


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "0", "否", "无", "不详", "未查找到"}
    return bool(value)


def _feature_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


_VALID_LEVEL_RANGES = {"C": (1, 7), "T": (1, 12), "L": (1, 5), "S": (1, 5)}


def _valid_vertebral_levels(levels: Any) -> List[str]:
    if isinstance(levels, str):
        levels = [levels]
    result: List[str] = []
    for level in levels or []:
        match = re.match(r"^([CTLS])(\d{1,2})$", str(level or "").upper())
        if not match:
            continue
        prefix, number = match.groups()
        low, high = _VALID_LEVEL_RANGES.get(prefix, (0, -1))
        normalized = f"{prefix}{int(number)}"
        if low <= int(number) <= high and normalized not in result:
            result.append(normalized)
    return result


def _stable_spine_compression_evidence(vertebral: Dict[str, Any], levels: List[str]) -> bool:
    if not levels:
        return False
    evidence_by_level = vertebral.get("compression_evidence_by_level") or {}
    if isinstance(evidence_by_level, dict):
        supported = [level for level in levels if evidence_by_level.get(level)]
        if len(supported) >= len(levels):
            return True
    if len(levels) >= 2 and _truthy(vertebral.get("compression_fracture")) and _to_int(vertebral.get("count")) >= len(levels):
        # Structured fact layers may already encode the clause fact even when OCR snippets are noisy.
        return True
    evidence_text = "\n".join(str(x) for x in (vertebral.get("evidence") or []) if x)
    if len(levels) >= 2:
        supported = 0
        for level in levels:
            cn = {"C": "颈", "T": "胸", "L": "腰", "S": "骶"}.get(level[0], "") + level[1:]
            pattern = rf"(?:{re.escape(level)}|{re.escape(cn)}椎?)[^。；，,\n]{{0,50}}(?:压缩|楔形|变扁|骨折)|(?:压缩|楔形|变扁|骨折)[^。；，,\n]{{0,50}}(?:{re.escape(level)}|{re.escape(cn)}椎?)"
            if re.search(pattern, evidence_text, re.I):
                supported += 1
        return supported >= len(levels)
    return bool(re.search(r"椎体[^。；，,\n]{0,20}(?:压缩性骨折|压缩|楔形|变扁)", evidence_text)) or _truthy(vertebral.get("compression_fracture"))


def _negative_result(text: str, match_type: str, branch_id: str, details: List[Dict[str, Any]] | None = None, clause: str = "", warnings: List[str] | None = None) -> DisabilityResult:
    return DisabilityResult(
        clause=clause,
        text=text,
        grade=0,
        match_type=match_type,
        details=details or [],
        branch_id=branch_id,
        warnings=warnings or [],
        conclusion_type="no_disability",
    )


def _human_no_disability_confirmation(facts: RulerFacts) -> Dict[str, Any]:
    confirmation = _feature_dict(facts.forensic_facts.get("human_disability_confirmation"))
    conclusion = str(confirmation.get("conclusion_type") or "").strip()
    grade_number = confirmation.get("grade_number")
    if conclusion in NO_DISABILITY_TEXT_VALUES or grade_number in NO_DISABILITY_TEXT_VALUES or str(grade_number).strip() in NO_DISABILITY_TEXT_VALUES:
        if str(conclusion or "").strip() in {"positive_grade", "positive", "grade"}:
            return {}
        return confirmation
    return {}


def _compression_degree_gte_1_3(vertebral: Dict[str, Any]) -> bool | None:
    raw_degree = vertebral.get("compression_degree")
    if isinstance(raw_degree, str):
        compact = raw_degree.replace(" ", "")
        if any(token in compact for token in ["未达1/3", "小于1/3", "不足1/3", "未达三分之一", "小于三分之一"]):
            return False
        if any(token in compact for token in ["达1/3", "超过1/3", "大于1/3", "达三分之一", "超过三分之一"]):
            return True
    degree = _to_float(raw_degree)
    if degree is None:
        # Some upstream versions may expose per-level degrees.
        degrees = vertebral.get("compression_degrees") or []
        if isinstance(degrees, list) and degrees:
            parsed = [_to_float(x) for x in degrees]
            parsed = [x for x in parsed if x is not None]
            if parsed:
                return all(x >= 1 / 3 or x >= 33.3 for x in parsed)
        return None
    return degree >= 1 / 3 or degree >= 33.3


def _spine_disability(facts: RulerFacts, rejected: List[Dict[str, Any]]) -> List[DisabilityResult]:
    results: List[DisabilityResult] = []
    vertebral = _feature_dict(facts.fracture_features.get("vertebral"))
    rf = facts.rule_features
    levels = _valid_vertebral_levels(vertebral.get("levels") or rf.get("vertebra_levels") or [])
    raw_count = int(vertebral.get("count") or rf.get("vertebral_count") or 0)
    count = min(raw_count, len(levels)) if levels else 0
    compression = _truthy(vertebral.get("compression_fracture") or rf.get("two_vertebral_compression_fractures")) and _stable_spine_compression_evidence(vertebral, levels)
    has_surgery = _truthy(vertebral.get("has_spine_surgery") or rf.get("one_vertebral_fracture_with_surgery"))
    appendix_fracture = _truthy(vertebral.get("has_corresponding_appendix_fracture") or rf.get("vertebral_appendix_fracture"))
    burst_fracture = _truthy(vertebral.get("burst_fracture") or vertebral.get("comminuted_fracture") or rf.get("vertebral_burst_fracture"))
    canal_occupancy = _truthy(vertebral.get("canal_occupancy") or vertebral.get("spinal_canal_bony_occupancy") or rf.get("spinal_canal_bony_occupancy"))
    posterior_count = _to_int(vertebral.get("transverse_spinous_pedicle_fracture_count") or rf.get("transverse_spinous_pedicle_fracture_count"))
    affects_function = _truthy(vertebral.get("affects_function") or rf.get("spine_affects_function"))
    degree_gte = _compression_degree_gte_1_3(vertebral)
    evidence = {
        "vertebral_count": count,
        "vertebra_levels": levels,
        "compression_fracture": compression,
        "has_spine_surgery": has_surgery,
        "has_corresponding_appendix_fracture": appendix_fracture,
        "burst_fracture": burst_fracture,
        "spinal_canal_bony_occupancy": canal_occupancy,
        "posterior_element_fracture_count": posterior_count,
        "affects_function": affects_function,
        "compression_degree_gte_1_3": degree_gte,
    }

    # Higher grade spine clauses are considered first and explicitly rejected if evidence is insufficient.
    if count >= 3 and has_surgery:
        results.append(DisabilityResult(
            clause="5.8.6.2",
            branch_id="5.8.6.2",
            text="三个以上椎体骨折，经手术治疗后",
            grade=8,
            match_type="forensic_facts_spine",
            details=[evidence],
        ))
        return results

    if count >= 2 and not compression:
        rejected.append({
            "clause": "5.9.6.2",
            "branch_id": "5.9.6.2.b",
            "text": "二椎体压缩性骨折",
            "reason": "椎体节段或压缩性骨折证据不足，未按二椎体伤残条款自动裁决。",
            "facts": evidence,
        })
    if count >= 2 and compression:
        if degree_gte is True:
            results.append(DisabilityResult(
                clause="5.8.6.1",
                branch_id="5.8.6.1",
                text="二椎体压缩性骨折（压缩程度均达1/3）",
                grade=8,
                match_type="forensic_facts_spine",
                details=[evidence],
            ))
            return results
        rejected.append({
            "clause": "5.8.6.1",
            "branch_id": "5.8.6.1",
            "text": "二椎体压缩性骨折（压缩程度均达1/3）",
            "reason": "已识别二椎体压缩性骨折，但压缩程度未稳定达到或未抽取到1/3证据。",
            "facts": evidence,
        })
        results.append(DisabilityResult(
            clause="5.9.6.2",
            branch_id="5.9.6.2.b",
            text="二椎体压缩性骨折",
            grade=9,
            match_type="forensic_facts_spine",
            details=[evidence],
            warnings=["已识别二椎体压缩性骨折，但未稳定抽取压缩程度；未适用5.8.6.1。"],
        ))
        return results

    if count == 1 and burst_fracture and canal_occupancy:
        results.append(DisabilityResult(
            clause="5.9.6.1",
            branch_id="5.9.6.1",
            text="一椎体粉碎性骨折，椎管内骨性占位",
            grade=9,
            match_type="forensic_facts_spine",
            details=[evidence],
        ))
    elif count == 1 and appendix_fracture and has_surgery:
        results.append(DisabilityResult(
            clause="5.9.6.2",
            branch_id="5.9.6.2.a",
            text="一椎体并相应附件骨折，经手术治疗后",
            grade=9,
            match_type="forensic_facts_spine",
            details=[evidence],
        ))
    elif count == 1 and compression and degree_gte is True:
        results.append(DisabilityResult(
            clause="5.10.6.2",
            branch_id="5.10.6.2.a",
            text="一椎体压缩性骨折（压缩程度达1/3）",
            grade=10,
            match_type="forensic_facts_spine",
            details=[evidence],
        ))
    elif count == 1 and compression and degree_gte is False and not has_surgery:
        rejected.append({
            "clause": "5.10.6.2",
            "branch_id": "5.10.6.2.a",
            "text": "一椎体压缩性骨折（压缩程度达1/3）",
            "reason": "已识别一椎体压缩性骨折，但压缩程度明确未达1/3，且未见粉碎性骨折或手术治疗证据。",
            "facts": evidence,
        })
        results.append(_negative_result(
            "一椎体压缩性骨折压缩程度未达1/3，未达脊柱伤残等级阈值",
            "negative_spine_threshold",
            "negative_spine_single_compression_below_threshold",
            details=[evidence],
        ))
    elif count == 1 and burst_fracture:
        results.append(DisabilityResult(
            clause="5.10.6.2",
            branch_id="5.10.6.2.b",
            text="一椎体粉碎性骨折",
            grade=10,
            match_type="forensic_facts_spine",
            details=[evidence],
        ))
    elif count == 1 and has_surgery:
        results.append(DisabilityResult(
            clause="5.10.6.2",
            branch_id="5.10.6.2.c",
            text="一椎体骨折经手术治疗后",
            grade=10,
            match_type="forensic_facts_spine",
            details=[evidence],
        ))

    if posterior_count >= 4 and affects_function:
        results.append(DisabilityResult(
            clause="5.10.6.3",
            branch_id="5.10.6.3",
            text="四处以上横突、棘突或者椎弓根骨折，影响功能",
            grade=10,
            match_type="forensic_facts_spine",
            details=[evidence],
        ))
    elif posterior_count >= 4:
        rejected.append({
            "clause": "5.10.6.3",
            "branch_id": "5.10.6.3",
            "text": "四处以上横突、棘突或者椎弓根骨折，影响功能",
            "reason": "已识别四处以上后方附件骨折，但缺少影响功能的稳定证据。",
            "facts": evidence,
        })
    return results


def _has_stable_rib_disability_evidence(rib: Dict[str, Any]) -> bool:
    evidence_text = " ".join(str(x) for x in (rib.get("evidence") or []) if x)
    if not evidence_text:
        return False
    compact = evidence_text.replace(" ", "")
    if re.search(r"(?:未见|无明显|未发现|未提示|待排|可疑|除外)[^。；，,]{0,30}肋骨?[^。；，,]{0,20}骨折", compact):
        return False
    if any(k in compact for k in ["陈旧性", "陈旧", "既往", "老旧"]) and not any(k in compact for k in ["新近", "本次外伤", "同次外伤", "同一时期", "新鲜"]):
        return False
    return bool(re.search(r"肋骨?[^。；，,]{0,20}骨折|第?[0-9一二三四五六七八九十]+肋", compact))


def _rib_disability(facts: RulerFacts) -> List[DisabilityResult]:
    rib = _feature_dict(facts.fracture_features.get("rib"))
    rf = facts.rule_features
    count = _to_int(rib.get("count") or rib.get("rib_fracture_count") or rf.get("rib_fracture_count"))
    missing_count = _to_int(rib.get("missing_count") or rf.get("rib_missing_count"))
    deformity_count = _to_int(rib.get("deformity_healing_count") or rf.get("rib_deformity_healing_count"))
    evidence = {"rib_fracture_count": count, "rib_missing_count": missing_count, "rib_deformity_healing_count": deformity_count, "evidence": rib.get("evidence", [])}
    if count > 0 and not _has_stable_rib_disability_evidence(rib):
        return [_negative_result(
            "肋骨骨折数量证据不稳定，暂不自动评定伤残等级",
            "negative_rib_unstable_evidence",
            "negative_rib_unstable_evidence",
            details=[evidence],
        )]

    if count >= 12 and deformity_count >= 6:
        return [DisabilityResult("5.8.3.6", "肋骨骨折12根以上并后遗6处畸形愈合", 8, match_type="forensic_facts_rib", details=[evidence], branch_id="5.8.3.6")]
    if count >= 12 or missing_count >= 4:
        return [DisabilityResult("5.9.3.11", "肋骨骨折12根以上，或者肋骨部分缺失4根以上", 9, match_type="forensic_facts_rib", details=[evidence], branch_id="5.9.3.11.a")]
    if count >= 8 and deformity_count >= 4:
        return [DisabilityResult("5.9.3.11", "肋骨骨折8根以上并后遗4处畸形愈合", 9, match_type="forensic_facts_rib", details=[evidence], branch_id="5.9.3.11.b")]
    if count >= 6 or missing_count >= 2:
        return [DisabilityResult("5.10.3.7", "肋骨骨折6根以上，或者肋骨部分缺失2根以上", 10, match_type="forensic_facts_rib", details=[evidence], branch_id="5.10.3.7.a")]
    if count >= 4 and deformity_count >= 2:
        return [DisabilityResult("5.10.3.7", "肋骨骨折4根以上并后遗2处畸形愈合", 10, match_type="forensic_facts_rib", details=[evidence], branch_id="5.10.3.7.b")]
    if count > 0 or missing_count > 0 or deformity_count > 0:
        return [_negative_result(
            "肋骨骨折/缺失/畸形愈合数量未达伤残等级阈值",
            "negative_rib_threshold",
            "negative_rib_below_threshold",
            details=[evidence],
        )]
    return []


def _rom_disability(
    facts: RulerFacts,
    legacy_index: List[Dict[str, Any]],
    calc_joint_loss: Callable[[str, dict, str], dict],
    muscle_grade: str,
) -> List[DisabilityResult]:
    results: List[DisabilityResult] = []
    for joint_name, joint_rom in (facts.rom_data or {}).items():
        if not joint_rom:
            continue
        calc = calc_joint_loss(joint_name, joint_rom, muscle_grade)
        if calc.get("loss_pct", 0) <= 0:
            continue
        best = None
        joint_items = sorted(
            [item for item in legacy_index if item.get("type") == "joint_function_loss" and joint_name in item.get("applies_to", [])],
            key=lambda item: (-float(item.get("min_loss_pct", 0) or 0), int(item.get("grade", 99) or 99)),
        )
        for item in joint_items:
            min_loss = float(item.get("min_loss_pct", 999) or 999)
            max_loss = float(item.get("max_loss_pct", 100.0) or 100.0)
            if min_loss <= float(calc.get("loss_pct", 0) or 0) <= max_loss:
                best = item
                break
        if best:
            results.append(DisabilityResult(
                clause=best["clause"],
                text=best["text"],
                grade=best["grade"],
                joint=joint_name,
                injured_side=calc.get("injured_side", ""),
                loss_pct=calc.get("loss_pct", 0),
                match_type="joint_function_loss",
                details=calc.get("details", []),
            ))
        else:
            negative = _negative_result(
                f"{joint_name}功能丧失{calc.get('loss_pct', 0)}%（未达伤残标准）",
                "below_threshold",
                "negative_rom_below_threshold",
                details=calc.get("details", []),
            )
            negative.joint = joint_name
            negative.injured_side = calc.get("injured_side", "")
            negative.loss_pct = calc.get("loss_pct", 0)
            results.append(negative)
    return results


def _limb_length_disability(facts: RulerFacts) -> List[DisabilityResult]:
    limb = _feature_dict(facts.rule_features.get("limb_length_diff") or facts.fracture_features.get("limb_length_diff"))
    upper = _to_float(limb.get("upper_cm") or limb.get("upper") or facts.rule_features.get("upper_limb_length_diff_cm")) or 0
    lower = _to_float(limb.get("lower_cm") or limb.get("lower") or facts.rule_features.get("lower_limb_length_diff_cm")) or 0
    results: List[DisabilityResult] = []
    if lower >= 8:
        results.append(DisabilityResult("5.7.6.1", "双下肢长度相差8.0cm以上", 7, match_type="forensic_facts_limb_length", details=[{"lower_cm": lower}], branch_id="5.7.6.1"))
    elif lower >= 6:
        results.append(DisabilityResult("5.8.6.7", "双下肢长度相差6.0cm以上", 8, match_type="forensic_facts_limb_length", details=[{"lower_cm": lower}], branch_id="5.8.6.7"))
    elif lower >= 4:
        results.append(DisabilityResult("5.9.6.8", "双下肢长度相差4.0cm以上", 9, match_type="forensic_facts_limb_length", details=[{"lower_cm": lower}], branch_id="5.9.6.8"))
    elif lower >= 2:
        results.append(DisabilityResult("5.10.6.10", "双下肢长度相差2.0cm以上", 10, match_type="forensic_facts_limb_length", details=[{"lower_cm": lower}], branch_id="5.10.6.10"))

    if upper >= 8:
        results.append(DisabilityResult("5.8.6.6", "双上肢长度相差8.0cm以上", 8, match_type="forensic_facts_limb_length", details=[{"upper_cm": upper}], branch_id="5.8.6.6"))
    elif upper >= 6:
        results.append(DisabilityResult("5.9.6.7", "双上肢长度相差6.0cm以上", 9, match_type="forensic_facts_limb_length", details=[{"upper_cm": upper}], branch_id="5.9.6.7"))
    elif upper >= 4:
        results.append(DisabilityResult("5.10.6.9", "双上肢长度相差4.0cm以上", 10, match_type="forensic_facts_limb_length", details=[{"upper_cm": upper}], branch_id="5.10.6.9"))
    return results


def _foot_arch_disability(facts: RulerFacts) -> List[DisabilityResult]:
    foot = _feature_dict(facts.rule_features.get("foot_arch") or facts.fracture_features.get("foot_arch"))
    complete = _to_int(foot.get("complete_destroyed_count") or facts.rule_features.get("foot_arch_complete_destroyed_count"))
    partial = _to_int(foot.get("partial_destroyed_count") or facts.rule_features.get("foot_arch_partial_destroyed_count"))
    evidence = {"complete_destroyed_count": complete, "partial_destroyed_count": partial}
    if complete >= 2:
        return [DisabilityResult("5.7.6.6", "双足足弓结构完全破坏", 7, match_type="forensic_facts_foot_arch", details=[evidence], branch_id="5.7.6.6")]
    if complete >= 1 and partial >= 1:
        return [DisabilityResult("5.8.6.12", "一足足弓结构完全破坏，另一足足弓结构部分破坏", 8, match_type="forensic_facts_foot_arch", details=[evidence], branch_id="5.8.6.12")]
    if complete >= 1 or partial >= 2:
        return [DisabilityResult("5.9.6.14", "双足足弓结构部分破坏；一足足弓结构完全破坏", 9, match_type="forensic_facts_foot_arch", details=[evidence], branch_id="5.9.6.14")]
    if partial >= 1:
        return [DisabilityResult("5.10.6.18", "一足足弓结构部分破坏", 10, match_type="forensic_facts_foot_arch", details=[evidence], branch_id="5.10.6.18")]
    return []


def _pelvis_and_heel_disability(facts: RulerFacts) -> List[DisabilityResult]:
    rf = facts.rule_features
    pelvis = _feature_dict(facts.fracture_features.get("pelvis") or rf.get("pelvis"))
    heel = _feature_dict(facts.fracture_features.get("calcaneus") or rf.get("calcaneus"))
    results: List[DisabilityResult] = []
    if _truthy(pelvis.get("birth_canal_deformity_cannot_natural_delivery") or rf.get("pelvis_birth_canal_deformity_cannot_natural_delivery")):
        results.append(DisabilityResult("5.8.6.3", "女性骨盆骨折致骨产道变形，不能自然分娩", 8, match_type="forensic_facts_pelvis", details=[pelvis], branch_id="5.8.6.3"))
    if _truthy(pelvis.get("two_plus_or_comminuted_severe_deformity") or rf.get("pelvis_fracture_severe_deformity")):
        results.append(DisabilityResult("5.9.6.3", "骨盆两处以上骨折或者粉碎性骨折，严重畸形愈合", 9, match_type="forensic_facts_pelvis", details=[pelvis], branch_id="5.9.6.3"))
    elif _truthy(pelvis.get("two_plus_or_comminuted_deformity") or rf.get("pelvis_fracture_deformity")):
        results.append(DisabilityResult("5.10.6.4", "骨盆两处以上骨折或者粉碎性骨折，畸形愈合", 10, match_type="forensic_facts_pelvis", details=[pelvis], branch_id="5.10.6.4"))

    comminuted_deformity_count = _to_int(heel.get("comminuted_deformity_count") or rf.get("calcaneus_comminuted_deformity_count"))
    if comminuted_deformity_count >= 2:
        results.append(DisabilityResult("5.9.6.13", "双足跟骨粉碎性骨折畸形愈合", 9, match_type="forensic_facts_calcaneus", details=[{"count": comminuted_deformity_count}], branch_id="5.9.6.13"))
    elif comminuted_deformity_count == 1:
        results.append(DisabilityResult("5.10.6.17", "一足跟骨粉碎性骨折畸形愈合", 10, match_type="forensic_facts_calcaneus", details=[{"count": comminuted_deformity_count}], branch_id="5.10.6.17"))
    return results


def _scar_disability(facts: RulerFacts) -> List[DisabilityResult]:
    scar = _feature_dict(
        facts.rule_features.get("scar")
        or facts.forensic_facts.get("scar_features")
        or facts.forensic_facts.get("scar_burn_facts")
        or facts.fracture_features.get("scar_burn")
    )
    body_pct = _to_float(scar.get("body_surface_pct") or facts.rule_features.get("body_scar_pct")) or 0
    face_linear = _to_float(scar.get("face_linear_length_cm") or facts.rule_features.get("face_linear_scar_length_cm")) or 0
    face_center_linear = _to_float(scar.get("face_center_linear_length_cm") or facts.rule_features.get("face_center_linear_scar_length_cm")) or 0
    face_block_area = _to_float(scar.get("face_block_area_cm2") or facts.rule_features.get("face_block_scar_area_cm2")) or 0
    face_patch_area = _to_float(scar.get("face_patch_area_cm2") or facts.rule_features.get("face_patch_scar_area_cm2")) or 0
    burn_pct = _to_float(scar.get("burn_total_tbsa_percent") or facts.rule_features.get("burn_total_tbsa_percent")) or 0
    evidence_text = " ".join(str(x) for x in (scar.get("evidence") or []) if x)
    has_severe_face_disfigurement = bool(
        scar.get("severe_face_disfigurement")
        or facts.rule_features.get("severe_face_disfigurement")
        or (any(k in evidence_text for k in ["严重", "重度", "毁损", "毁容"]) and any(k in evidence_text for k in ["面部", "颜面", "容貌"]))
        or ("睑外翻" in evidence_text and "小口" in evidence_text)
    )
    results: List[DisabilityResult] = []
    if has_severe_face_disfigurement:
        results.append(DisabilityResult("5.2.2.1", "重度容貌毁损", 2, match_type="forensic_facts_face_disfigurement", details=[scar], branch_id="5.2.2.1"))
    elif burn_pct >= 70 and any(k in evidence_text for k in ["面部", "颜面", "睑外翻", "小口", "容貌"]):
        results.append(DisabilityResult("5.2.2.1", "大面积烧伤后遗面部重度容貌毁损", 2, match_type="forensic_facts_burn_face", details=[scar], branch_id="5.2.2.1"))
    if body_pct >= 50:
        results.append(DisabilityResult("5.6.7.1", "皮肤瘢痕形成达体表面积50%", 6, match_type="forensic_facts_scar", details=[{"body_surface_pct": body_pct}], branch_id="5.6.7.1"))
    elif body_pct >= 30:
        results.append(DisabilityResult("5.8.7.1", "皮肤瘢痕形成达体表面积30%", 8, match_type="forensic_facts_scar", details=[{"body_surface_pct": body_pct}], branch_id="5.8.7.1"))
    elif body_pct >= 10:
        results.append(DisabilityResult("5.9.7.1", "皮肤瘢痕形成达体表面积10%", 9, match_type="forensic_facts_scar", details=[{"body_surface_pct": body_pct}], branch_id="5.9.7.1"))
    elif body_pct >= 4:
        results.append(DisabilityResult("5.10.7.2", "皮肤瘢痕形成达体表面积4%", 10, match_type="forensic_facts_scar", details=[{"body_surface_pct": body_pct}], branch_id="5.10.7.2"))

    if face_linear >= 20 or (face_center_linear >= 10 and face_linear >= 10):
        results.append(DisabilityResult("5.9.2.4", "面部条状瘢痕形成，累计长度达20.0cm；或中心区条状瘢痕累计达10.0cm且至少5.0cm位于中心区", 9, match_type="forensic_facts_face_scar", details=[scar], branch_id="5.9.2.4"))
    elif face_linear >= 10 or face_center_linear >= 3:
        results.append(DisabilityResult("5.10.2.4", "面部条状瘢痕形成，累计长度达10.0cm", 10, match_type="forensic_facts_face_scar", details=[scar], branch_id="5.10.2.4"))

    if face_block_area >= 7 or face_patch_area >= 30:
        results.append(DisabilityResult("5.9.2.5", "面部块状瘢痕单块面积达7.0cm²或片状细小瘢痕/色素异常累计达30.0cm²", 9, match_type="forensic_facts_face_scar", details=[scar], branch_id="5.9.2.5"))
    elif face_block_area >= 3 or face_patch_area >= 10:
        results.append(DisabilityResult("5.10.2.5", "面部块状瘢痕单块面积达3.0cm²或片状细小瘢痕/色素异常累计达10.0cm²", 10, match_type="forensic_facts_face_scar", details=[scar], branch_id="5.10.2.5"))

    has_scar_measurement = any(value > 0 for value in [body_pct, face_linear, face_center_linear, face_block_area, face_patch_area])
    if has_scar_measurement and not results:
        results.append(_negative_result(
            "体表/面部瘢痕长度、面积或体表面积百分比未达伤残等级阈值",
            "negative_scar_threshold",
            "negative_scar_below_threshold",
            details=[scar],
        ))
    return results


def _hand_or_foot_function_disability(facts: RulerFacts) -> List[DisabilityResult]:
    rf = facts.rule_features
    amputation = _feature_dict(rf.get("amputation_features") or facts.forensic_facts.get("amputation_features"))
    score = _to_int(rf.get("hand_or_foot_function_score") or amputation.get("hand_or_foot_function_score"))
    evidence = {"hand_or_foot_function_score": score, "amputation_features": amputation}
    if score >= 90:
        return [DisabilityResult("5.6.6.4", "手或者足功能丧失分值≥90分", 6, match_type="forensic_facts_hand_foot_score", details=[evidence], branch_id="5.6.6.4")]
    if score >= 60:
        return [DisabilityResult("5.7.6.7", "手或者足功能丧失分值≥60分", 7, match_type="forensic_facts_hand_foot_score", details=[evidence], branch_id="5.7.6.7")]
    if score >= 40:
        return [DisabilityResult("5.8.6.13", "手或者足功能丧失分值≥40分", 8, match_type="forensic_facts_hand_foot_score", details=[evidence], branch_id="5.8.6.13")]
    if score >= 25:
        return [DisabilityResult("5.9.6.15", "手或者足功能丧失分值≥25分", 9, match_type="forensic_facts_hand_foot_score", details=[evidence], branch_id="5.9.6.15")]
    if score >= 10:
        return [DisabilityResult("5.10.6.19", "手或者足功能丧失分值≥10分", 10, match_type="forensic_facts_hand_foot_score", details=[evidence], branch_id="5.10.6.19")]
    return []


def _limb_structured_disability(facts: RulerFacts) -> List[DisabilityResult]:
    rf = facts.rule_features
    results: List[DisabilityResult] = []
    limb_items = facts.fracture_features.get("limb") or rf.get("limb_fractures") or []
    if isinstance(limb_items, dict):
        limb_items = [limb_items]
    if not isinstance(limb_items, list):
        limb_items = []

    epiphyseal_items = [item for item in limb_items if isinstance(item, dict) and _truthy(item.get("epiphyseal_involved"))]
    if epiphyseal_items:
        results.append(DisabilityResult(
            "5.10.6.7",
            "青少年四肢长骨骨折累及骨骺",
            10,
            match_type="forensic_facts_limb_epiphyseal",
            details=epiphyseal_items[:4],
            branch_id="5.10.6.7",
            warnings=["仅当被鉴定人为青少年且骨折累及骨骺时适用；如年龄字段缺失，应人工复核。"],
        ))

    if rf.get("chronic_osteomyelitis_long_nonunion"):
        results.append(DisabilityResult(
            "5.8.6.5",
            "四肢长骨开放性骨折并发慢性骨髓炎、大块死骨形成，长期不愈（1年以上）",
            8,
            match_type="forensic_facts_healing_status",
            details=[_feature_dict(rf.get("fracture_healing_status"))],
            branch_id="5.8.6.5",
        ))

    results.extend(_hand_or_foot_function_disability(facts))
    return results


def _visual_function_disability(facts: RulerFacts) -> List[DisabilityResult]:
    rf = facts.rule_features
    results: List[DisabilityResult] = []
    blindness = _to_int(rf.get("blindness_level_one_eye"))
    field_pct = _to_float(rf.get("visual_field_effective_pct_one_eye"))
    evidence = {
        "blindness_level_one_eye": blindness,
        "moderate_visual_impairment_one_eye": rf.get("moderate_visual_impairment_one_eye"),
        "severe_visual_impairment_one_eye": rf.get("severe_visual_impairment_one_eye"),
        "visual_field_effective_pct_one_eye": field_pct,
    }
    if blindness >= 4:
        results.append(DisabilityResult("5.8.2.7", "一眼盲目4级", 8, match_type="forensic_facts_visual_function", details=[evidence], branch_id="5.8.2.7"))
    elif blindness >= 3:
        results.append(DisabilityResult("5.9.2.11", "一眼盲目3级", 9, match_type="forensic_facts_visual_function", details=[evidence], branch_id="5.9.2.11"))
    elif rf.get("moderate_visual_impairment_one_eye"):
        results.append(DisabilityResult("5.10.2.15", "一眼中度视力损害", 10, match_type="forensic_facts_visual_function", details=[evidence], branch_id="5.10.2.15"))
    if field_pct is not None:
        if field_pct <= 4:
            results.append(DisabilityResult("5.8.2.8", "一眼视野接近完全缺损，视野有效值≤4%", 8, match_type="forensic_facts_visual_field", details=[evidence], branch_id="5.8.2.8"))
        elif field_pct <= 8:
            results.append(DisabilityResult("5.9.2.13", "一眼视野极度缺损，视野有效值≤8%", 9, match_type="forensic_facts_visual_field", details=[evidence], branch_id="5.9.2.13"))
        elif field_pct <= 48:
            results.append(DisabilityResult("5.10.2.17", "一眼视野中度缺损，视野有效值≤48%", 10, match_type="forensic_facts_visual_field", details=[evidence], branch_id="5.10.2.17"))
    eye_facts = _feature_dict(facts.forensic_facts.get("eye_facts"))
    has_visual_measurement = bool(eye_facts.get("corrected_visual_acuity") or field_pct is not None or eye_facts.get("hess_screen_abnormal") is False)
    if has_visual_measurement and not results:
        results.append(_negative_result(
            "眼部视力、视野或眼球运动检查未达伤残等级阈值",
            "negative_visual_threshold",
            "negative_visual_below_threshold",
            details=[{**evidence, "eye_facts": eye_facts}],
        ))
    return results


def _structured_feature_disability(facts: RulerFacts) -> List[DisabilityResult]:
    rf = facts.rule_features
    results: List[DisabilityResult] = []
    if rf.get("traumatic_arthritis"):
        results.append(DisabilityResult("5.10.6.13", "下肢任一大关节骨折后遗创伤性关节炎", 10, match_type="forensic_facts_traumatic_arthritis", details=[{"evidence": rf.get("traumatic_arthritis_evidence", [])}], branch_id="5.10.6.13"))
    if rf.get("eyeball_missing_or_removed"):
        results.append(DisabilityResult("5.7.2.4", "一眼球缺失或者萎缩", 7, match_type="forensic_facts_eye", branch_id="5.7.2.4"))
    enophthalmos = _to_float(rf.get("orbital_enophthalmos_mm")) or 0
    if enophthalmos >= 5:
        results.append(DisabilityResult("5.9.2.16", "一眼眶骨折后遗眼球内陷5mm以上", 9, match_type="forensic_facts_eye", details=[{"orbital_enophthalmos_mm": enophthalmos}], branch_id="5.9.2.16"))
    elif enophthalmos >= 2:
        results.append(DisabilityResult("5.10.2.9", "一眼眶骨折后遗眼球内陷2mm以上", 10, match_type="forensic_facts_eye", details=[{"orbital_enophthalmos_mm": enophthalmos}], branch_id="5.10.2.9"))
    if rf.get("craniotomy_done"):
        results.append(DisabilityResult("5.10.1.8", "开颅术后", 10, match_type="forensic_facts_neuro", branch_id="5.10.1.8"))
    results.extend(_visual_function_disability(facts))
    if rf.get("major_joint_replacement"):
        results.append(DisabilityResult("5.9.6.5", "四肢任一大关节行关节假体置换术后", 9, match_type="forensic_facts_joint_replacement", branch_id="5.9.6.5"))
    if rf.get("patella_removed_one_side"):
        results.append(DisabilityResult("5.10.6.5", "一侧髌骨切除", 10, match_type="forensic_facts_limb_surgery", branch_id="5.10.6.5"))
    if rf.get("knee_ligament_meniscus_collateral_surgery_affects_function"):
        results.append(DisabilityResult("5.10.6.6", "一侧膝关节交叉韧带、半月板伴侧副韧带撕裂伤经手术治疗后，影响功能", 10, match_type="forensic_facts_ligament", branch_id="5.10.6.6"))
    if rf.get("liver_spleen_pancreas_repair"):
        results.append(DisabilityResult("5.10.4.2", "肝、脾或者胰腺修补术后", 10, match_type="forensic_facts_abdominal_surgery", branch_id="5.10.4.2"))
    if rf.get("stomach_intestine_biliary_repair"):
        results.append(DisabilityResult("5.10.4.3", "胃、肠或者胆道修补术后", 10, match_type="forensic_facts_abdominal_surgery", branch_id="5.10.4.3"))
    if rf.get("gallbladder_removed"):
        results.append(DisabilityResult("5.9.4.8", "胆囊切除术后", 9, match_type="forensic_facts_abdominal_surgery", branch_id="5.9.4.8"))
    results.extend(_limb_length_disability(facts))
    results.extend(_foot_arch_disability(facts))
    results.extend(_pelvis_and_heel_disability(facts))
    results.extend(_scar_disability(facts))
    results.extend(_limb_structured_disability(facts))
    return results


def _human_confirmed_no_disability(facts: RulerFacts) -> List[DisabilityResult]:
    confirmation = _human_no_disability_confirmation(facts)
    if not confirmation:
        return []
    reason = str(confirmation.get("reason") or confirmation.get("clause_text") or "人工审计确认不构成伤残").strip()
    return [_negative_result(
        reason or "人工审计确认不构成伤残",
        "negative_human_confirmed_no_disability",
        "negative_human_confirmed_no_disability",
        details=[{"human_disability_confirmation": confirmation}],
        clause=str(confirmation.get("clause") or ""),
    )]


def _explicit_negative_disability(facts: RulerFacts) -> List[DisabilityResult]:
    text = facts.all_text or ""
    negative_phrases = ["不构成伤残", "未构成伤残", "尚未构成伤残", "不宜评定伤残"]
    if not any(phrase in text for phrase in negative_phrases):
        return []
    return [_negative_result(
        "原始结构化材料中出现明确不构成伤残表述，作为阴性结论证据保留",
        "negative_explicit_text",
        "negative_explicit_no_disability",
        details=[{"evidence": [item for item in facts.negative_rule_features if isinstance(item, str)][:8]}],
    )]


def match_disability_v2(
    facts: RulerFacts,
    legacy_index: List[Dict[str, Any]],
    legacy_matcher: Callable[..., List[Any]],
    calc_joint_loss: Callable[[str, dict, str], dict],
    muscle_grade: str = "M5",
    selective: Optional[Dict[str, Any]] = None,
) -> Tuple[List[DisabilityResult], Dict[str, Any]]:
    """裁决伤残条款。

    ``selective`` 为可选的选择性重算计划：

        {"run_matchers": [...], "reusable_clauses": {...}, "previous_results": {...}}

    给出且带历史结果时，只运行受本次人工修正影响的裁决器，其余条款
    直接复用上一次运行的结果；未给出时行为与全量重算完全一致。
    """
    rejected: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    results: List[DisabilityResult] = []

    human_negative = _human_confirmed_no_disability(facts)
    if human_negative:
        results = human_negative
        for r in results:
            candidates.append({
                "clause": r.clause,
                "branch_id": r.branch_id,
                "text": r.text,
                "grade": r.grade,
                "grade_cn": r.grade_cn,
                "match_type": r.match_type,
                "conclusion_type": r.conclusion_type,
                "details": r.details,
                "warnings": r.warnings,
            })
        return results, {
            "candidate_disability_clauses": candidates,
            "matched_disability_clauses": [],
            "negative_disability_clauses": candidates,
            "rejected_disability_clauses": rejected,
        }

    plan = selective or {}
    previous = dict(plan.get("previous_results") or {})
    reusable = set(plan.get("reusable_clauses") or set())
    selective_active = bool(plan.get("run_matchers") is not None and previous)
    run_matchers = set(plan.get("run_matchers") or [])
    skipped_matchers: List[str] = []
    reused_clauses: List[str] = []

    def _enabled(name: str) -> bool:
        if not selective_active or name in run_matchers:
            return True
        skipped_matchers.append(name)
        return False

    if _enabled("spine"):
        results.extend(_spine_disability(facts, rejected))
    if _enabled("rib"):
        results.extend(_rib_disability(facts))
    if _enabled("structured_feature"):
        results.extend(_structured_feature_disability(facts))
    if _enabled("rom"):
        results.extend(_rom_disability(facts, legacy_index, calc_joint_loss, muscle_grade))

    # 被跳过的裁决器所辖条款直接复用上一次结果；只复用不受影响且已有历史结果的条款。
    if selective_active:
        produced = {r.clause for r in results if r.clause}
        for clause in sorted(reusable):
            if clause in produced:
                continue
            cached = previous.get(clause)
            if cached is None:
                continue
            results.append(cached)
            reused_clauses.append(clause)

    if not any(r.grade > 0 for r in results):
        results.extend(_explicit_negative_disability(facts))

    # Legacy fallback is still useful for currently unstructured clauses, but is not allowed to duplicate clauses.
    existing = {r.clause for r in results if r.clause}
    legacy_results = legacy_matcher(
        diagnoses=facts.diagnoses,
        rom_data={},  # ROM handled above with CLI muscle_grade wired.
        surgery_history=facts.surgery_history,
        exam_findings=facts.exam_findings,
        aux_findings=facts.aux_findings,
    ) if _enabled("legacy") else []
    for r in legacy_results:
        # 旧版曾将“四肢长骨骨折内固定术后”误映射到5.10.6.7；
        # 新版仅在结构化识别“青少年+累及骨骺”时适用该条，避免内固定事实被误判为伤残。
        if getattr(r, "clause", "") == "5.10.6.7":
            continue
        if "椎" in str(getattr(r, "text", "")) or str(getattr(r, "clause", "")).startswith(("5.8.6", "5.9.6", "5.10.6")):
            continue
        if "肋骨" in str(getattr(r, "text", "")):
            continue
        if getattr(r, "clause", "") and r.clause not in existing:
            results.append(DisabilityResult(
                clause=r.clause,
                text=r.text,
                grade=r.grade,
                joint=getattr(r, "joint", ""),
                injured_side=getattr(r, "injured_side", ""),
                loss_pct=getattr(r, "loss_pct", 0.0),
                match_type=f"legacy_{getattr(r, 'match_type', '')}",
                details=getattr(r, "details", []),
            ))
            existing.add(r.clause)

    results.sort(key=lambda r: r.grade if r.grade > 0 else 99)
    for r in results:
        candidates.append({
            "clause": r.clause,
            "branch_id": r.branch_id,
            "text": r.text,
            "grade": r.grade,
            "grade_cn": r.grade_cn,
            "match_type": r.match_type,
            "conclusion_type": getattr(r, "conclusion_type", ""),
            "details": r.details,
            "warnings": r.warnings,
        })
    audit = {
        "candidate_disability_clauses": candidates,
        "matched_disability_clauses": [c for c in candidates if c.get("grade", 0) > 0],
        "negative_disability_clauses": [c for c in candidates if c.get("grade", 0) == 0 and str(c.get("match_type", "")).startswith(("negative", "below_threshold"))],
        "rejected_disability_clauses": rejected,
        "recompute_plan": {
            "selective": selective_active,
            "ran_matchers": sorted(run_matchers) if selective_active else ["spine", "rib", "structured_feature", "rom", "legacy"],
            "skipped_matchers": sorted(set(skipped_matchers)),
            "reused_clauses": reused_clauses,
            "reused_count": len(reused_clauses),
            "recomputed_count": len([c for c in candidates if c.get("clause") not in set(reused_clauses)]),
        },
    }
    return results, audit
