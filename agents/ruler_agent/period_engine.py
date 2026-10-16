"""Deterministic three-period matching driven by forensic facts."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Tuple

from .fact_adapter import _filter_valid_vertebral_levels, has_negated_surgery
from .rule_models import PeriodResult, RulerFacts


PERIOD_KEYS = ("误工", "护理", "营养")
EN_TO_CN = {"work_loss_period": "误工", "nursing_period": "护理", "nutrition_period": "营养"}


MANUAL_PERIOD_RULES = [
    {
        "feature": "spinal_cord_contusion_or_compression",
        "clause": "9.4.2",
        "label": "脊髓挫伤、脊髓压迫",
        "reason": "标准写明根据临床治疗情况确定，需结合神经功能、治疗经过和恢复情况人工评估。",
    },
    {
        "feature": "major_vessel_injury",
        "clause": "7.10/10.9",
        "label": "胸内大血管或四肢主要血管损伤",
        "reason": "血管损伤期限与修复方式、缺血程度、并发症直接相关，需人工结合临床材料判断。",
    },
    {
        "feature": "amputation_replantation",
        "clause": "10.7",
        "label": "断肢（指、趾）再植",
        "reason": "标准写明根据临床治疗恢复情况确定，不能仅凭损伤名称自动给天数。",
    },
    {
        "feature": "severe_burn_or_special_injury",
        "clause": "11.1.4/11.2.2/11.3",
        "label": "特重度烧烫伤、全身冻伤或其他特殊因素损伤",
        "reason": "需参照烧伤深度、面积、感染、植皮及恢复情况综合判断。",
    },
]


def _range_from_value(value: Any) -> List[int]:
    if value is None:
        return [0, 0]
    if isinstance(value, list) and len(value) >= 2:
        return [int(value[0]), int(value[1])]
    if isinstance(value, int):
        return [value, value]
    text = str(value).replace("日", "").replace("天", "").replace(" ", "")
    if text in ("", "无", "不详", "根据临床治疗情况确定"):
        return [0, 0]
    for sep in ("~", "～", "-", "—"):
        if sep in text:
            left, right = text.split(sep, 1)
            try:
                return [int(float(left)), int(float(right))]
            except ValueError:
                return [0, 0]
    try:
        val = int(float(text))
        return [val, val]
    except ValueError:
        return [0, 0]


def _choose_upper(rng: List[int]) -> int:
    return int(rng[1]) if rng and len(rng) > 1 else 0


def _normalize_degree_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace(" ", "")


def _spine_degree_text(facts: RulerFacts) -> str:
    vertebral = facts.fracture_features.get("vertebral") or {}
    degree_values: List[Any] = [vertebral.get("compression_degree")]
    if isinstance(vertebral.get("compression_degrees"), list):
        degree_values.extend(vertebral.get("compression_degrees") or [])
    return " ".join(_normalize_degree_text(v) for v in degree_values if v not in (None, "", "不详", "未查找到"))


def _spine_9_1_1_rule_applies(item: Dict[str, Any], facts: RulerFacts) -> bool:
    if str(item.get("clause", "")) != "9.1.1":
        return False
    vertebral = facts.fracture_features.get("vertebral") or {}
    if vertebral.get("has_spine_surgery"):
        return False
    healing = facts.forensic_facts.get("fracture_healing_status") or {}
    no_complex_recovery = not _truthy(facts.rule_features.get("delayed_union_or_nonunion") or healing.get("delayed_union") or healing.get("nonunion"))
    no_neuro = not _truthy(facts.rule_features.get("objective_neuro_deficit") or facts.rule_features.get("spinal_cord_contusion_or_compression"))
    return bool(no_complex_recovery and no_neuro)


def _select_period_values_for_item(item: Dict[str, Any], facts: RulerFacts) -> Dict[str, Any]:
    ranges = item.get("ranges", {}) or {}
    selected: Dict[str, int] = {}
    if _spine_9_1_1_rule_applies(item, facts):
        degree_text = _spine_degree_text(facts)
        has_mild_degree = any(token in degree_text for token in ["未达1/3", "小于1/3", "不足1/3", "未达三分之一", "小于三分之一", "轻度压缩"])
        work_value = 120 if has_mild_degree else 120
        degree_reason = "压缩程度未达1/3/轻度压缩" if has_mild_degree else "未稳定抽取压缩程度，按脊柱骨折非手术治疗常规区间内值并提示人工复核"
        selected = {"误工": work_value, "护理": _choose_upper(ranges.get("护理", [0, 0])), "营养": _choose_upper(ranges.get("营养", [0, 0]))}
        return {
            "selected": selected,
            "policy": "rule_based_interval_selection",
            "reason": f"脊柱骨折非手术治疗，{degree_reason}，未见延迟愈合或明确神经功能损害；误工取区间内规则值{work_value}日，护理、营养取本条款上限。",
        }
    for key in PERIOD_KEYS:
        selected[key] = _choose_upper(ranges.get(key, [0, 0]))
    return {
        "selected": selected,
        "policy": "upper_bound_fallback",
        "reason": "该条款暂未配置更细区间内取值规则，沿用区间上限作为保守建议，需人工结合恢复情况复核。",
    }


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value).replace(" ", "")))
    except (TypeError, ValueError):
        return 0


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "0", "否", "无", "不详", "未查找到"}
    return bool(value)


def _feature_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _period_item(clause: str, label: str, ranges: Dict[str, List[int]], reason: str, source: str, requires_manual: bool = False) -> Dict[str, Any]:
    return {
        "clause": clause,
        "label": label,
        "ranges": ranges,
        "reason": reason,
        "source": source,
        "requires_manual_period_assessment": requires_manual,
    }


def _candidate_clause_to_match(candidate: Dict[str, Any]) -> Dict[str, Any] | None:
    clause = candidate.get("clause") or candidate.get("subitem_id") or ""
    label = candidate.get("label") or candidate.get("title") or ""
    ranges: Dict[str, List[int]] = {}
    for key in PERIOD_KEYS:
        ranges[key] = _range_from_value(candidate.get(key))
    for en, cn in EN_TO_CN.items():
        if candidate.get(en) is not None:
            ranges[cn] = _range_from_value(candidate.get(en))
    manual = bool(candidate.get("requires_manual_period_assessment")) or "根据临床治疗情况确定" in str(candidate)
    if not clause or (not manual and not any(r[1] > 0 for r in ranges.values())):
        return None
    return {
        "clause": clause,
        "label": label,
        "ranges": ranges,
        "reason": candidate.get("reason", ""),
        "source": "forensic_facts.period_rule_features",
        "requires_manual_period_assessment": manual,
    }


def _match_from_period_features(facts: RulerFacts) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for candidate in facts.period_rule_features.get("candidate_clauses", []) or []:
        if isinstance(candidate, dict):
            converted = _candidate_clause_to_match(candidate)
            if converted:
                matches.append(converted)
    return matches


def _has_stable_spine_period_evidence(facts: RulerFacts) -> bool:
    vertebral = facts.fracture_features.get("vertebral") or {}
    text = facts.all_text or ""
    levels = _filter_valid_vertebral_levels(vertebral.get("levels") or vertebral.get("vertebra_levels") or [])
    if not levels:
        return False
    evidence_text = " ".join(str(x) for x in (vertebral.get("evidence") or []) if x)
    spine_text = f"{evidence_text} {text}"
    positive_spine = re.search(r"(?:椎体|胸椎|腰椎|颈椎|骶椎|脊柱)[^。；，,]{0,20}(?:骨折|压缩|楔形|变扁)", evidence_text)
    negated_spine = re.search(r"(?:未见|无|未发现|排除)[^。；，,]{0,12}(?:椎体|胸椎|腰椎|颈椎|骶椎|脊柱)[^。；，,]{0,20}(?:骨折|压缩|楔形|变扁)", spine_text)
    if positive_spine and not negated_spine:
        return True
    for item in facts.recognized_injuries or []:
        if not isinstance(item, dict):
            continue
        item_text = " ".join(str(item.get(key) or "") for key in ["body_region", "bone", "name", "injury", "description", "normalized_name", "name_normalized", "name_raw"])
        if re.search(r"(?:椎体|胸椎|腰椎|颈椎|骶椎|脊柱).*(?:骨折|压缩)", item_text):
            return True
    return False


def _spine_period_from_facts(facts: RulerFacts) -> List[Dict[str, Any]]:
    vertebral = facts.fracture_features.get("vertebral") or {}
    levels = _filter_valid_vertebral_levels(vertebral.get("levels") or vertebral.get("vertebra_levels") or [])
    count = min(int(vertebral.get("count") or facts.rule_features.get("vertebral_count") or 0), len(levels)) if levels else 0
    if count <= 0 or not _has_stable_spine_period_evidence(facts):
        return []
    has_surgery = bool(vertebral.get("has_spine_surgery"))
    if has_surgery:
        return [_period_item(
            "9.1.2",
            "脊柱骨折手术治疗",
            {"误工": [120, 180], "护理": [60, 90], "营养": [60, 90]},
            "已识别脊柱骨折并存在明确手术治疗事实。",
            "forensic_facts.fracture_features.vertebral",
        )]
    return [_period_item(
        "9.1.1",
        "脊柱骨折非手术治疗",
        {"误工": [45, 150], "护理": [45, 60], "营养": [45, 60]},
        "已识别脊柱骨折，且未见明确手术治疗事实。",
        "forensic_facts.fracture_features.vertebral",
    )]


def _has_stable_rib_period_evidence(facts: RulerFacts, rib: Dict[str, Any]) -> bool:
    evidence_text = " ".join(str(x) for x in (rib.get("evidence") or []) if x)
    if not evidence_text:
        return False
    compact = evidence_text.replace(" ", "")
    if re.search(r"(?:未见|无明显|未发现|未提示|待排|可疑|除外)[^。；，,]{0,30}肋骨?[^。；，,]{0,20}骨折", compact):
        return False
    if any(k in compact for k in ["陈旧性", "陈旧", "既往", "老旧"]) and not any(k in compact for k in ["新近", "本次外伤", "同次外伤", "同一时期", "新鲜"]):
        return False
    return bool(re.search(r"肋骨?[^。；，,]{0,20}骨折|第?[0-9一二三四五六七八九十]+肋", compact))


def _rib_period_from_facts(facts: RulerFacts) -> List[Dict[str, Any]]:
    rib = _feature_dict(facts.fracture_features.get("rib"))
    count = _to_int(rib.get("count") or rib.get("rib_fracture_count") or facts.rule_features.get("rib_fracture_count"))
    if count <= 0 or not _has_stable_rib_period_evidence(facts, rib):
        return []
    if count >= 2 or _truthy(rib.get("multiple")):
        return [_period_item("7.2.2", "肋骨多根、多处骨折", {"误工": [60, 120], "护理": [30, 60], "营养": [30, 60]}, "已识别稳定的多根或多处肋骨骨折证据。", "forensic_facts.fracture_features.rib")]
    return [_period_item("7.2.1", "肋骨一处骨折", {"误工": [30, 45], "护理": [7, 15], "营养": [15, 30]}, "已识别稳定的单处肋骨骨折证据。", "forensic_facts.fracture_features.rib")]


def _pelvis_period_from_facts(facts: RulerFacts) -> List[Dict[str, Any]]:
    pelvis = _feature_dict(facts.fracture_features.get("pelvis") or facts.rule_features.get("pelvis"))
    if not pelvis and not facts.rule_features.get("pelvis_fracture"):
        return []
    unstable = _truthy(pelvis.get("unstable") or facts.rule_features.get("pelvis_unstable_fracture"))
    if unstable:
        return [_period_item("9.5.2", "骨盆不稳定型骨折", {"误工": [120, 180], "护理": [60, 90], "营养": [60, 90]}, "已识别骨盆不稳定型骨折。", "forensic_facts.fracture_features.pelvis")]
    return [_period_item("9.5.1", "骨盆稳定型骨折", {"误工": [60, 120], "护理": [20, 30], "营养": [30, 60]}, "已识别骨盆骨折，未见不稳定型证据。", "forensic_facts.fracture_features.pelvis")]


LIMB_FRACTURE_PERIODS = {
    "clavicle": ("10.2.1", "锁骨骨折", {"non_surgery": ([60, 120], [30, 60], [60, 90]), "surgery": ([90, 120], [30, 60], [60, 90])}),
    "scapula": ("10.2.2", "肩胛骨骨折", {"non_surgery": ([60, 120], [30, 60], [30, 60]), "surgery": ([90, 180], [30, 60], [60, 90])}),
    "humerus": ("10.2.3", "肱骨骨折", {"non_surgery": ([60, 180], [30, 60], [60, 90]), "surgery": ([90, 270], [60, 90], [60, 90])}),
    "olecranon": ("10.2.4", "尺骨鹰嘴骨折", {"non_surgery": ([60, 90], [30, 60], [30, 60]), "surgery": ([90, 120], [30, 60], [60, 90])}),
    "ulna_radius": ("10.2.5", "尺桡骨折", {"non_surgery": ([90, 120], [30, 60], [60, 90]), "surgery": ([90, 180], [30, 60], [60, 90])}),
    "wrist": ("10.2.6", "腕骨骨折", {"non_surgery": ([90, 180], [30, 60], [20, 30]), "surgery": ([90, 180], [30, 60], [20, 30])}),
    "finger_metacarpal": ("10.2.7", "指、掌骨骨折", {"non_surgery": ([45, 60], [20, 30], [20, 30]), "surgery": ([30, 90], [20, 30], [20, 30])}),
    "femoral_neck": ("10.2.8", "股骨颈骨折", {"non_surgery": ([240, 365], [120, 180], [30, 90]), "surgery": ([180, 365], [90, 150], [90, 180])}),
    "intertrochanteric": ("10.2.9", "股骨粗隆间骨折", {"non_surgery": ([180, 270], [120, 180], [30, 90]), "surgery": ([180, 270], [90, 180], [90, 180])}),
    "femoral_shaft": ("10.2.10", "股骨干骨折", {"non_surgery": ([90, 180], [60, 120], [30, 90]), "surgery": ([90, 300], [60, 120], [60, 90])}),
    "femoral_distal": ("10.2.11", "股骨远端骨折", {"non_surgery": ([90, 180], [60, 120], [30, 90]), "surgery": ([120, 270], [60, 120], [30, 90])}),
    "patella": ("10.2.12", "髌骨骨折", {"non_surgery": ([120, 150], [30, 60], [30, 60]), "surgery": ([120, 180], [60, 90], [30, 60])}),
    "tibial_plateau": ("10.2.13", "胫骨平台骨折", {"non_surgery": ([90, 150], [60, 90], [30, 60]), "surgery": ([120, 180], [60, 90], [30, 60])}),
    "tibia_fibula": ("10.2.14", "胫腓骨骨折", {"non_surgery": ([120, 180], [30, 90], [60, 90]), "surgery": ([150, 180], [60, 90], [60, 90])}),
    "ankle": ("10.2.15", "踝部骨折", {"non_surgery": ([90, 120], [30, 60], [60, 90]), "surgery": ([90, 180], [30, 60], [60, 90])}),
    "tarsal": ("10.2.16", "舟、楔骨等跗骨骨折", {"non_surgery": ([120, 120], [30, 60], [60, 90]), "surgery": ([120, 120], [30, 60], [60, 90])}),
    "calcaneus_talus": ("10.2.17", "跟、距骨骨折", {"non_surgery": ([90, 180], [60, 90], [60, 90]), "surgery": ([90, 240], [60, 90], [60, 90]), "joint_surface": ([90, 240], [60, 90], [90, 120])}),
    "metatarsal_toe": ("10.2.18", "跖、趾骨及其他跗骨骨折", {"non_surgery": ([90, 120], [30, 60], [60, 90]), "surgery": ([120, 150], [30, 60], [60, 90])}),
}


def _limb_period_from_facts(facts: RulerFacts) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    limb_items = facts.fracture_features.get("limb") or facts.rule_features.get("limb_fractures") or []
    if isinstance(limb_items, dict):
        limb_items = [limb_items]
    if not isinstance(limb_items, list):
        return matches
    for item in limb_items:
        if not isinstance(item, dict):
            continue
        injury_type = item.get("type") or item.get("injury_type") or item.get("bone")
        if injury_type not in LIMB_FRACTURE_PERIODS:
            continue
        clause, title, options = LIMB_FRACTURE_PERIODS[injury_type]
        key = "joint_surface" if _truthy(item.get("joint_surface_involved")) and "joint_surface" in options else ("surgery" if _truthy(item.get("has_surgery") or facts.has_positive_surgery) else "non_surgery")
        work, nursing, nutrition = options[key]
        label = f"{title}{'手术治疗' if key == 'surgery' else '累及关节面' if key == 'joint_surface' else '非手术治疗'}"
        matches.append(_period_item(clause, label, {"误工": list(work), "护理": list(nursing), "营养": list(nutrition)}, "已识别结构化四肢骨折事实。", "forensic_facts.fracture_features.limb"))
    return matches


def _is_negated_ligament_feature(ligament: Dict[str, Any]) -> bool:
    texts = []
    for item in ligament.get("items", []) or []:
        if isinstance(item, dict):
            texts.append(str(item.get("evidence") or ""))
    texts.extend(str(x) for x in ligament.get("evidence", []) or [])
    joined = " ".join(texts).replace(" ", "")
    return bool(joined) and re.search(r"(?:未见|无|未发现|未检出|未提示)[^。；，,]{0,30}(?:撕裂|断裂|损伤|异常)", joined) is not None


def _ligament_period_from_facts(facts: RulerFacts) -> List[Dict[str, Any]]:
    ligament = _feature_dict(facts.rule_features.get("ligament_meniscus_features") or facts.forensic_facts.get("ligament_meniscus_features"))
    if not ligament.get("has_ligament_or_meniscus") or _is_negated_ligament_feature(ligament):
        return []
    matches = [_period_item("10.4", "四肢大关节韧带损伤", {"误工": [60, 120], "护理": [60, 90], "营养": [30, 60]}, "已识别韧带/半月板损伤。", "forensic_facts.ligament_meniscus_features")]
    if any((item.get("kind") == "tendon") for item in ligament.get("items", []) if isinstance(item, dict)):
        matches.append(_period_item("10.5", "主要肌腱断裂", {"误工": [60, 150], "护理": [0, 0], "营养": [0, 0]}, "已识别主要肌腱断裂；护理和营养标准写明结合临床情况。", "forensic_facts.ligament_meniscus_features", requires_manual=True))
    return matches


def _amputation_period_from_facts(facts: RulerFacts) -> List[Dict[str, Any]]:
    amputation = _feature_dict(facts.rule_features.get("amputation_features") or facts.forensic_facts.get("amputation_features"))
    if not amputation.get("has_amputation_or_loss"):
        return []
    if amputation.get("is_replantation"):
        return [_period_item("10.7", "断肢（指、趾）再植", {"误工": [0, 0], "护理": [0, 0], "营养": [0, 0]}, "标准写明根据临床治疗恢复情况确定。", "forensic_facts.amputation_features", requires_manual=True)]
    if amputation.get("level") in {"finger", "toe"}:
        return [_period_item("10.6.2", "断指/断趾", {"误工": [60, 90], "护理": [30, 90], "营养": [30, 60]}, "已识别指/趾离断或缺失。", "forensic_facts.amputation_features")]
    return [_period_item("10.6.1", "断肢", {"误工": [120, 180], "护理": [30, 90], "营养": [90, 90]}, "已识别肢体离断或截肢。", "forensic_facts.amputation_features")]


def _eye_period_from_facts(facts: RulerFacts) -> List[Dict[str, Any]]:
    rf = facts.rule_features
    # 眼部字段在无证据时的取值是「不详」等占位字符串，必须经 _truthy 判定，
    # 否则「未知」会被当作「存在」，把材料未提供写成确诊视力损害。
    if _truthy(rf.get("eyeball_missing_or_removed")):
        return [_period_item("5.1.12", "眼球摘除", {"误工": [30, 60], "护理": [15, 20], "营养": [15, 20]}, "已识别眼球摘除/缺失。", "forensic_facts.eye_facts")]
    if (_truthy(rf.get("blindness_level_one_eye"))
            or _truthy(rf.get("moderate_visual_impairment_one_eye"))
            or _truthy(rf.get("severe_visual_impairment_one_eye"))):
        return [_period_item("5.1.11", "视神经损伤/视觉功能障碍", {"误工": [90, 120], "护理": [30, 45], "营养": [30, 45]}, "已识别视力/视野功能损害。", "forensic_facts.eye_facts")]
    return []


def _burn_scar_period_from_facts(facts: RulerFacts) -> List[Dict[str, Any]]:
    scar = _feature_dict(
        facts.rule_features.get("scar")
        or facts.forensic_facts.get("scar_burn_facts")
        or facts.forensic_facts.get("scar_features")
        or facts.fracture_features.get("scar_burn")
    )
    evidence = " ".join(str(x) for x in (scar.get("evidence") or []) if x)
    joined = f"{facts.all_text or ''} {evidence}"
    burn_pct = _to_int(scar.get("burn_total_tbsa_percent") or facts.rule_features.get("burn_total_tbsa_percent"))
    scar_pct = _to_int(scar.get("body_surface_pct") or scar.get("scar_bsa_percent") or facts.rule_features.get("body_scar_pct"))
    has_burn = bool(burn_pct or scar_pct or any(k in joined for k in ["烧伤", "烫伤", "植皮", "削痂", "清创", "瘢痕", "疤痕"]))
    if not has_burn:
        return []
    if burn_pct >= 70 or scar_pct >= 50 or any(k in joined for k in ["特重度", "大面积", "多次植皮", "睑外翻", "小口畸形"]):
        return [_period_item("11.1", "大面积烧烫伤/瘢痕修复", {"误工": [180, 365], "护理": [120, 365], "营养": [90, 180]}, "已识别大面积烧烫伤、瘢痕或植皮修复事实，按烧烫伤相关条款取保守区间值。", "forensic_facts.scar_burn_facts")]
    return [_period_item("11.1", "烧烫伤/瘢痕相关损伤", {"误工": [30, 120], "护理": [1, 120], "营养": [20, 120]}, "已识别烧烫伤或瘢痕事实。", "forensic_facts.scar_burn_facts")]


def _soft_tissue_period_from_facts(facts: RulerFacts) -> List[Dict[str, Any]]:
    soft = _feature_dict(facts.rule_features.get("soft_tissue") or facts.forensic_facts.get("soft_tissue_features"))
    region = soft.get("region") or "limb"
    wound_len = _to_int(soft.get("wound_length_cm") or facts.rule_features.get("wound_length_cm"))
    abrasion = _truthy(soft.get("abrasion_or_contusion") or facts.rule_features.get("abrasion_or_contusion"))
    if not abrasion and wound_len <= 0:
        return []
    if region == "abdomen":
        if wound_len > 20:
            return [_period_item("8.1.3", "腹部皮肤创长度大于20cm", {"误工": [30, 60], "护理": [1, 15], "营养": [15, 30]}, "已识别腹部较长皮肤创。", "forensic_facts.soft_tissue_features")]
        if wound_len > 0:
            return [_period_item("8.1.2", "腹部皮肤创长度小于或等于20cm", {"误工": [15, 30], "护理": [1, 7], "营养": [1, 15]}, "已识别腹部皮肤创。", "forensic_facts.soft_tissue_features")]
        return [_period_item("8.1.1", "腹部皮肤擦、挫伤", {"误工": [15, 30], "护理": [0, 0], "营养": [1, 7]}, "已识别腹部皮肤擦挫伤。", "forensic_facts.soft_tissue_features")]
    if wound_len > 20:
        return [_period_item("10.1.3", "肢体皮肤创长度大于20cm", {"误工": [20, 30], "护理": [1, 15], "营养": [15, 30]}, "已识别肢体较长皮肤创。", "forensic_facts.soft_tissue_features")]
    if wound_len > 0:
        return [_period_item("10.1.2", "肢体皮肤创长度小于或等于20cm", {"误工": [15, 20], "护理": [1, 7], "营养": [1, 15]}, "已识别肢体皮肤创。", "forensic_facts.soft_tissue_features")]
    return [_period_item("10.1.1", "肢体皮肤擦、挫伤", {"误工": [7, 15], "护理": [0, 0], "营养": [1, 7]}, "已识别肢体皮肤擦挫伤。", "forensic_facts.soft_tissue_features")]


def _manual_period_items_from_facts(facts: RulerFacts) -> List[Dict[str, Any]]:
    manual_items: List[Dict[str, Any]] = []
    for rule in MANUAL_PERIOD_RULES:
        if _truthy(facts.rule_features.get(rule["feature"])):
            manual_items.append(_period_item(
                rule["clause"],
                rule["label"],
                {"误工": [0, 0], "护理": [0, 0], "营养": [0, 0]},
                rule["reason"],
                "forensic_facts.rule_features",
                requires_manual=True,
            ))
    return manual_items


def _structured_period_matches(facts: RulerFacts) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    matches.extend(_rib_period_from_facts(facts))
    matches.extend(_pelvis_period_from_facts(facts))
    matches.extend(_limb_period_from_facts(facts))
    matches.extend(_ligament_period_from_facts(facts))
    matches.extend(_amputation_period_from_facts(facts))
    matches.extend(_eye_period_from_facts(facts))
    matches.extend(_burn_scar_period_from_facts(facts))
    matches.extend(_soft_tissue_period_from_facts(facts))
    return matches


def _rule_match_to_period_item(rule: Dict[str, Any], sub: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "clause": rule.get("clause", ""),
        "label": sub.get("label", ""),
        "ranges": {
            "误工": _range_from_value(sub.get("误工")),
            "护理": _range_from_value(sub.get("护理")),
            "营养": _range_from_value(sub.get("营养")),
        },
        "reason": "legacy keyword fallback",
        "source": "legacy_period_rules",
    }


def _fallback_period_matches(
    facts: RulerFacts,
    period_index: List[Dict[str, Any]],
    select_period_sub: Callable[[dict, bool, str], dict | None],
) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    text = facts.all_text
    has_surgery = facts.has_positive_surgery
    if has_negated_surgery(text) and not facts.has_positive_surgery:
        has_surgery = False
    for rule in period_index:
        if not rule.get("keywords"):
            continue
        if not any(kw in text for kw in rule.get("keywords", [])):
            continue
        sub = select_period_sub(rule, has_surgery, text)
        if sub:
            matches.append(_rule_match_to_period_item(rule, sub))
    return matches


def _dedupe_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    deduped = []
    for item in matches:
        key = (item.get("clause"), item.get("label"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _merge_reasons(reasons: List[str]) -> List[str]:
    merged: List[str] = []
    for reason in reasons:
        text = str(reason or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged[:8]


def _period_display_clauses_from_selection(
    selected_from_items: Dict[str, Dict[str, Any]],
    effective_matches: List[Dict[str, Any]],
    matches: List[Dict[str, Any]],
) -> List[str]:
    clauses: List[str] = []

    def add(clause: Any) -> None:
        text = str(clause or "").strip()
        if text and text not in clauses:
            clauses.append(text)

    for key in PERIOD_KEYS:
        item = selected_from_items.get(key) or {}
        try:
            selected = int(item.get("selected") or 0)
        except Exception:
            selected = 0
        if selected > 0:
            add(item.get("clause"))
    if clauses:
        return clauses
    for item in effective_matches or matches:
        add(item.get("clause"))
        if clauses:
            return clauses
    return clauses


def match_period_v2(
    facts: RulerFacts,
    period_index: List[Dict[str, Any]],
    select_period_sub: Callable[[dict, bool, str], dict | None],
) -> Tuple[PeriodResult, Dict[str, Any]]:
    matches = []
    stable_spine = _has_stable_spine_period_evidence(facts)
    spine_fact_matches = _spine_period_from_facts(facts) if stable_spine else []
    feature_matches = _match_from_period_features(facts)
    if spine_fact_matches:
        spine_clauses = {str(m.get("clause", "")) for m in spine_fact_matches}
        feature_matches = [m for m in feature_matches if not str(m.get("clause", "")).startswith("9.1") or str(m.get("clause", "")) in spine_clauses]
    matches.extend(feature_matches)
    # 脊柱三期以适配后的最终椎体事实为准，避免上游候选条款中的“手术/非手术”误识别覆盖事实层。
    if spine_fact_matches and not any(str(m.get("clause", "")).startswith("9.1") for m in matches):
        matches.extend(spine_fact_matches)
    matches.extend(_structured_period_matches(facts))
    manual_items = _manual_period_items_from_facts(facts)
    matches.extend(manual_items)

    # 只有稳定椎体事实命中 9.1，且不存在明确非脊柱候选时，才允许脊柱条款优先。
    if stable_spine and not any(not str(m.get("clause", "")).startswith("9.1") for m in feature_matches if not m.get("requires_manual_period_assessment")):
        spine_matches = [m for m in matches if str(m.get("clause", "")).startswith("9.1")]
        non_manual_spine = [m for m in spine_matches if not m.get("requires_manual_period_assessment")]
        if non_manual_spine:
            matches = non_manual_spine + [m for m in matches if m.get("requires_manual_period_assessment")]
    # forensic_facts 已给出候选条款或结构化事实时，不再追加宽泛关键词兜底，避免“无手术/候选标签”导致过匹配。
    if not matches:
        matches.extend(_fallback_period_matches(facts, period_index, select_period_sub))
    matches = _dedupe_matches(matches)

    if not matches:
        return PeriodResult(clause="", clause_text="", 误工=0, 护理=0, 营养=0), {
            "candidate_period_clauses": [],
            "matched_period_clauses": [],
            "manual_review_items": [],
        }

    max_values = {"误工": 0, "护理": 0, "营养": 0}
    max_ranges = {"误工": [0, 0], "护理": [0, 0], "营养": [0, 0]}
    selected_from_items: Dict[str, Dict[str, Any]] = {}
    selection_reasons: List[str] = []
    interval_candidates: List[Dict[str, Any]] = []
    policies = set()
    primary = matches[0]
    primary_work = -1
    final_manual_items = []

    for item in matches:
        ranges = item.get("ranges", {})
        if item.get("requires_manual_period_assessment"):
            final_manual_items.append(item)
            continue
        selection = _select_period_values_for_item(item, facts)
        selected_values = selection.get("selected", {}) or {}
        policy = selection.get("policy", "upper_bound_fallback")
        reason = selection.get("reason", "")
        policies.add(policy)
        interval_candidates.append({
            "clause": item.get("clause", ""),
            "label": item.get("label", ""),
            "ranges": ranges,
            "selected": selected_values,
            "selection_policy": policy,
            "selection_reason": reason,
            "source": item.get("source", ""),
            "match_reason": item.get("reason", ""),
        })
        if reason:
            selection_reasons.append(f"{item.get('clause', '')} {item.get('label', '')}：{reason}".strip())
        work_value = int(selected_values.get("误工") or 0)
        if work_value > primary_work:
            primary = item
            primary_work = work_value
        for key in PERIOD_KEYS:
            rng = ranges.get(key, [0, 0])
            value = int(selected_values.get(key) or 0)
            if value > max_values[key]:
                max_values[key] = value
                max_ranges[key] = rng
                selected_from_items[key] = {
                    "clause": item.get("clause", ""),
                    "label": item.get("label", ""),
                    "range": rng,
                    "selected": value,
                    "selection_policy": policy,
                    "selection_reason": reason,
                }

    effective_matches = [item for item in matches if not item.get("requires_manual_period_assessment")]
    display_clauses = _period_display_clauses_from_selection(selected_from_items, effective_matches, matches)
    coverage_clauses: List[str] = []
    for item in effective_matches or matches:
        clause = str(item.get("clause") or "").strip()
        if clause and clause not in coverage_clauses:
            coverage_clauses.append(clause)
    clause_str = "、".join(display_clauses)
    result = PeriodResult(
        clause=clause_str,
        clause_text=f"《人身损害误工期、护理期、营养期评定规范》（GA/T 1193-2014）之{clause_str}条及附录A等相关条款",
        误工=max_values["误工"],
        护理=max_values["护理"],
        营养=max_values["营养"],
        sub_label=primary.get("label", ""),
        ranges=max_ranges,
        selected_value_policy="rule_based_interval_selection" if "rule_based_interval_selection" in policies else "upper_bound_fallback",
        selection_reasons=_merge_reasons(selection_reasons),
        selected_from_items=selected_from_items,
        matched_items=matches,
        requires_manual_period_assessment=bool(final_manual_items) and not any(max_values.values()),
    )
    audit = {
        "candidate_period_clauses": matches,
        "matched_period_clauses": effective_matches,
        "manual_review_items": final_manual_items,
        "period_interval_candidates": interval_candidates,
        "period_selection": {
            "selected_values": max_values,
            "selected_ranges": max_ranges,
            "selected_from_items": selected_from_items,
            "display_clauses": display_clauses,
            "coverage_clauses": coverage_clauses,
            "selected_value_policy": result.selected_value_policy,
            "selection_reasons": result.selection_reasons,
        },
    }
    return result, audit
