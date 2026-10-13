"""Coverage matrix utilities for Ruler Agent.

The matrix is generated from source standards first, then annotated with implemented
rule rows. This keeps coverage honest: every standard clause can be represented even
when automation is not yet available.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DISABILITY_STANDARD = WORKSPACE_ROOT / "files" / "知识库" / "知识库_条例" / "人体损伤致残程度分级.md"
PERIOD_STANDARD = WORKSPACE_ROOT / "files" / "知识库" / "知识库_条例" / "人身损害误工期、护理期、营养期评定.md"
RULE_DIR = Path(__file__).resolve().parent / "rules"
RUN_DIR = WORKSPACE_ROOT / "runs" / "ruler_rule_engine_coverage"


IMPLEMENTED_DISABILITY_ROWS = [
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.8.6.1",
        "branch_id": "5.8.6.1",
        "grade": 8,
        "source_heading": "八级 / 脊柱、骨盆及四肢损伤",
        "source_text": "二椎体压缩性骨折（压缩程度均达1/3）",
        "required_fact_fields": ["forensic_facts.fracture_features.vertebral.count", "forensic_facts.fracture_features.vertebral.compression_degree"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_spine_two_compression_degree_g8"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.8.6.2",
        "branch_id": "5.8.6.2",
        "grade": 8,
        "source_heading": "八级 / 脊柱、骨盆及四肢损伤",
        "source_text": "三个以上椎体骨折，经手术治疗后",
        "required_fact_fields": ["forensic_facts.fracture_features.vertebral.count", "forensic_facts.fracture_features.vertebral.has_spine_surgery"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_spine_three_surgery_g8"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.9.6.1",
        "branch_id": "5.9.6.1",
        "grade": 9,
        "source_heading": "九级 / 脊柱、骨盆及四肢损伤",
        "source_text": "一椎体粉碎性骨折，椎管内骨性占位",
        "required_fact_fields": ["forensic_facts.fracture_features.vertebral.comminuted_fracture", "forensic_facts.fracture_features.vertebral.spinal_canal_bony_occupancy"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_spine_burst_canal_g9"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.9.6.2",
        "branch_id": "5.9.6.2.a",
        "grade": 9,
        "source_heading": "九级 / 脊柱、骨盆及四肢损伤",
        "source_text": "一椎体并相应附件骨折，经手术治疗后",
        "required_fact_fields": ["forensic_facts.fracture_features.vertebral.count", "forensic_facts.fracture_features.vertebral.has_corresponding_appendix_fracture", "forensic_facts.fracture_features.vertebral.has_spine_surgery"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_spine_one_with_appendix_surgery_g9"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.9.6.2",
        "branch_id": "5.9.6.2.b",
        "grade": 9,
        "source_heading": "九级 / 脊柱、骨盆及四肢损伤",
        "source_text": "二椎体压缩性骨折",
        "required_fact_fields": ["forensic_facts.fracture_features.vertebral.count", "forensic_facts.fracture_features.vertebral.compression_fracture"],
        "coverage_status": "covered",
        "fixture_ids": ["golden_001_dingqifeng", "synthetic_spine_two_compression_unknown_degree_g9"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.10.6.2",
        "branch_id": "5.10.6.2.a",
        "grade": 10,
        "source_heading": "十级 / 脊柱、骨盆及四肢损伤",
        "source_text": "一椎体压缩性骨折（压缩程度达1/3）",
        "required_fact_fields": ["forensic_facts.fracture_features.vertebral.count", "forensic_facts.fracture_features.vertebral.compression_degree"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_spine_one_compression_degree_g10"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.10.6.2",
        "branch_id": "5.10.6.2.b",
        "grade": 10,
        "source_heading": "十级 / 脊柱、骨盆及四肢损伤",
        "source_text": "一椎体粉碎性骨折",
        "required_fact_fields": ["forensic_facts.fracture_features.vertebral.comminuted_fracture"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_spine_burst_g10"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.10.6.2",
        "branch_id": "5.10.6.2.c",
        "grade": 10,
        "source_heading": "十级 / 脊柱、骨盆及四肢损伤",
        "source_text": "一椎体骨折经手术治疗后",
        "required_fact_fields": ["forensic_facts.fracture_features.vertebral.count", "forensic_facts.fracture_features.vertebral.has_spine_surgery"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_spine_one_surgery_g10"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.10.6.3",
        "branch_id": "5.10.6.3",
        "grade": 10,
        "source_heading": "十级 / 脊柱、骨盆及四肢损伤",
        "source_text": "四处以上横突、棘突或者椎弓根骨折，影响功能",
        "required_fact_fields": ["forensic_facts.fracture_features.vertebral.transverse_spinous_pedicle_fracture_count", "forensic_facts.fracture_features.vertebral.affects_function"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_spine_posterior_four_g10"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.8.3.6",
        "branch_id": "5.8.3.6",
        "grade": 8,
        "source_heading": "八级 / 颈部及胸部损伤",
        "source_text": "肋骨骨折12根以上并后遗6处畸形愈合",
        "required_fact_fields": ["forensic_facts.fracture_features.rib.count", "forensic_facts.fracture_features.rib.deformity_healing_count"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_rib_twelve_deformity_six_g8"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.9.3.11",
        "branch_id": "5.9.3.11.a",
        "grade": 9,
        "source_heading": "九级 / 颈部及胸部损伤",
        "source_text": "肋骨骨折12根以上，或者肋骨部分缺失4根以上",
        "required_fact_fields": ["forensic_facts.fracture_features.rib.count", "forensic_facts.fracture_features.rib.missing_count"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_rib_twelve_g9"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.9.3.11",
        "branch_id": "5.9.3.11.b",
        "grade": 9,
        "source_heading": "九级 / 颈部及胸部损伤",
        "source_text": "肋骨骨折8根以上并后遗4处畸形愈合",
        "required_fact_fields": ["forensic_facts.fracture_features.rib.count", "forensic_facts.fracture_features.rib.deformity_healing_count"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_rib_eight_deformity_four_g9"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.10.3.7",
        "branch_id": "5.10.3.7.a",
        "grade": 10,
        "source_heading": "十级 / 颈部及胸部损伤",
        "source_text": "肋骨骨折6根以上，或者肋骨部分缺失2根以上",
        "required_fact_fields": ["forensic_facts.fracture_features.rib.count", "forensic_facts.fracture_features.rib.missing_count"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_rib_six_g10"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.10.3.7",
        "branch_id": "5.10.3.7.b",
        "grade": 10,
        "source_heading": "十级 / 颈部及胸部损伤",
        "source_text": "肋骨骨折4根以上并后遗2处畸形愈合",
        "required_fact_fields": ["forensic_facts.fracture_features.rib.count", "forensic_facts.fracture_features.rib.deformity_healing_count"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_rib_four_deformity_two_g10"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.7.6.6/5.8.6.12/5.9.6.14/5.10.6.18",
        "branch_id": "foot_arch_thresholds",
        "grade": 7,
        "source_heading": "脊柱、骨盆及四肢损伤 / 足弓结构破坏阈值组",
        "source_text": "双足完全破坏、完全+部分、双足部分/单足完全、单足部分等足弓阈值",
        "required_fact_fields": ["forensic_facts.rule_features.foot_arch_complete_destroyed_count", "forensic_facts.rule_features.foot_arch_partial_destroyed_count"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_foot_arch_partial_g10", "synthetic_foot_arch_complete_g9"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.7.6.1/5.8.6.6-7/5.9.6.7-8/5.10.6.9-10",
        "branch_id": "limb_length_thresholds",
        "grade": 7,
        "source_heading": "脊柱、骨盆及四肢损伤 / 肢体长度差阈值组",
        "source_text": "双上肢/双下肢长度差阈值",
        "required_fact_fields": ["forensic_facts.rule_features.upper_limb_length_diff_cm", "forensic_facts.rule_features.lower_limb_length_diff_cm"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_lower_limb_diff_g10", "synthetic_lower_limb_diff_g8"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.9.6.3/5.10.6.4",
        "branch_id": "pelvis_deformity_thresholds",
        "grade": 9,
        "source_heading": "九级/十级 / 脊柱、骨盆及四肢损伤",
        "source_text": "骨盆两处以上骨折或者粉碎性骨折，畸形/严重畸形愈合",
        "required_fact_fields": ["forensic_facts.fracture_features.pelvis.two_plus_or_comminuted_deformity", "forensic_facts.fracture_features.pelvis.two_plus_or_comminuted_severe_deformity"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_pelvis_deformity_g10", "synthetic_pelvis_severe_deformity_g9"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.9.6.13/5.10.6.17",
        "branch_id": "calcaneus_deformity_thresholds",
        "grade": 9,
        "source_heading": "九级/十级 / 脊柱、骨盆及四肢损伤",
        "source_text": "单/双足跟骨粉碎性骨折畸形愈合",
        "required_fact_fields": ["forensic_facts.fracture_features.calcaneus.comminuted_deformity_count"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_calcaneus_one_g10", "synthetic_calcaneus_two_g9"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.6.7.1/5.8.7.1/5.9.7.1/5.10.7.2",
        "branch_id": "body_surface_scar_thresholds",
        "grade": 6,
        "source_heading": "体表及其他损伤 / 瘢痕体表面积阈值组",
        "source_text": "皮肤瘢痕形成达体表面积50%、30%、10%、4%",
        "required_fact_fields": ["forensic_facts.rule_features.scar.body_surface_pct"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_body_scar_pct_g10", "synthetic_body_scar_pct_g8"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.9.2.4-6/5.10.2.4-6",
        "branch_id": "face_scar_thresholds",
        "grade": 9,
        "source_heading": "头面部损伤 / 面部瘢痕阈值组",
        "source_text": "面部条状、块状、片状细小瘢痕或色素异常阈值",
        "required_fact_fields": ["forensic_facts.rule_features.scar.face_linear_length_cm", "forensic_facts.rule_features.scar.face_block_area_cm2", "forensic_facts.rule_features.scar.face_patch_area_cm2"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_face_linear_scar_g10", "synthetic_face_block_scar_g9"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.7.2.4/5.9.2.16/5.10.2.9/5.10.1.8/5.9.6.5/5.10.6.13",
        "branch_id": "structured_boolean_or_numeric_high_confidence",
        "grade": 7,
        "source_heading": "多系统高置信结构化特征组",
        "source_text": "眼球缺失、眼眶骨折眼球内陷、开颅术后、关节置换、创伤性关节炎等",
        "required_fact_fields": ["forensic_facts.rule_features.*"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_eye_enophthalmos_g10", "synthetic_joint_replacement_g9", "synthetic_traumatic_arthritis_g10"],
    },

    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.6.6.4/5.7.6.7/5.8.6.13/5.9.6.15/5.10.6.19",
        "branch_id": "hand_foot_function_score_thresholds",
        "grade": 6,
        "source_heading": "脊柱、骨盆及四肢损伤 / 手足功能丧失分值阈值组",
        "source_text": "手或者足功能丧失分值≥90/60/40/25/10分",
        "required_fact_fields": ["forensic_facts.rule_features.hand_or_foot_function_score", "forensic_facts.amputation_features.hand_or_foot_function_score"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_hand_foot_score_g10", "synthetic_hand_foot_score_g8"],
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.8.2.7/5.9.2.11/5.10.2.15/5.8.2.8/5.9.2.13/5.10.2.17",
        "branch_id": "vision_visual_field_numeric_thresholds",
        "grade": 8,
        "source_heading": "头面部损伤 / 视力视野数值阈值组",
        "source_text": "一眼盲目4级/3级、中度视力损害、视野有效值阈值",
        "required_fact_fields": ["forensic_facts.rule_features.blindness_level_one_eye", "forensic_facts.rule_features.moderate_visual_impairment_one_eye", "forensic_facts.rule_features.visual_field_effective_pct_one_eye"],
        "coverage_status": "partially_covered",
        "fixture_ids": ["synthetic_one_eye_blindness_level4_g8", "synthetic_one_eye_moderate_visual_g10", "synthetic_visual_field_g9"],
        "notes": "先覆盖单眼明确数值/分级；双眼组合、年龄和专科复杂情形仍需人工复核。",
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.10.6.7",
        "branch_id": "limb_epiphyseal_involved",
        "grade": 10,
        "source_heading": "十级 / 脊柱、骨盆及四肢损伤",
        "source_text": "青少年四肢长骨骨折累及骨骺",
        "required_fact_fields": ["forensic_facts.fracture_features.limb[].epiphyseal_involved", "subject_birth/age"],
        "coverage_status": "partially_covered",
        "fixture_ids": ["synthetic_limb_epiphyseal_g10"],
        "notes": "结构化识别累及骨骺后可提示命中；青少年年龄条件需人工或结构化年龄字段确认。",
    },
    {
        "standard_id": "human_disability_2016",
        "clause_id": "5.8.6.5",
        "branch_id": "chronic_osteomyelitis_long_nonunion",
        "grade": 8,
        "source_heading": "八级 / 脊柱、骨盆及四肢损伤",
        "source_text": "四肢长骨开放性骨折并发慢性骨髓炎、大块死骨形成，长期不愈（1年以上）",
        "required_fact_fields": ["forensic_facts.fracture_healing_status.chronic_osteomyelitis_or_sequestrum", "forensic_facts.fracture_healing_status.long_term_over_one_year"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_chronic_osteomyelitis_long_nonunion_g8"],
    },
]

IMPLEMENTED_PERIOD_ROWS = [
    {
        "standard_id": "GA_T_1193_2014",
        "clause_id": "9.1.1",
        "title": "脊柱骨折非手术治疗",
        "work_loss_range": [45, 150],
        "nursing_range": [45, 60],
        "nutrition_range": [45, 60],
        "required_fact_fields": ["forensic_facts.fracture_features.vertebral.count", "forensic_facts.fracture_features.vertebral.has_spine_surgery"],
        "coverage_status": "covered",
        "fixture_ids": ["golden_001_dingqifeng", "synthetic_period_spine_non_surgery"],
    },
    {
        "standard_id": "GA_T_1193_2014",
        "clause_id": "9.1.2",
        "title": "脊柱骨折手术治疗",
        "work_loss_range": [120, 180],
        "nursing_range": [60, 90],
        "nutrition_range": [60, 90],
        "required_fact_fields": ["forensic_facts.fracture_features.vertebral.count", "forensic_facts.fracture_features.vertebral.has_spine_surgery"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_period_spine_surgery"],
    },
    {
        "standard_id": "GA_T_1193_2014",
        "clause_id": "7.2.1/7.2.2",
        "title": "肋骨骨折一处/多根多处",
        "work_loss_range": [30, 120],
        "nursing_range": [7, 60],
        "nutrition_range": [15, 60],
        "required_fact_fields": ["forensic_facts.fracture_features.rib.count"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_period_rib_single", "synthetic_period_rib_multiple"],
    },
    {
        "standard_id": "GA_T_1193_2014",
        "clause_id": "9.5.1/9.5.2",
        "title": "骨盆稳定型/不稳定型骨折",
        "work_loss_range": [60, 180],
        "nursing_range": [20, 90],
        "nutrition_range": [30, 90],
        "required_fact_fields": ["forensic_facts.fracture_features.pelvis.unstable"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_period_pelvis_stable", "synthetic_period_pelvis_unstable"],
    },
    {
        "standard_id": "GA_T_1193_2014",
        "clause_id": "10.2.1-10.2.18",
        "title": "四肢骨折高频结构化规则族",
        "work_loss_range": [45, 365],
        "nursing_range": [20, 180],
        "nutrition_range": [20, 180],
        "required_fact_fields": ["forensic_facts.fracture_features.limb[].type", "forensic_facts.fracture_features.limb[].has_surgery"],
        "coverage_status": "partially_covered",
        "fixture_ids": ["synthetic_period_limb_femoral_neck_surgery", "synthetic_period_limb_tibia_fibula_non_surgery"],
        "notes": "已覆盖常见锁骨、肩胛、肱骨、尺桡骨、股骨、胫腓骨、踝、跟距骨等；手足小骨和个别亚型仍需继续细化。",
    },
    {
        "standard_id": "GA_T_1193_2014",
        "clause_id": "10.1.1-10.1.3/8.1.1-8.1.3",
        "title": "软组织擦挫伤、皮肤创长度阈值",
        "work_loss_range": [7, 60],
        "nursing_range": [0, 15],
        "nutrition_range": [1, 30],
        "required_fact_fields": ["forensic_facts.soft_tissue_features.region", "forensic_facts.soft_tissue_features.wound_length_cm"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_period_limb_wound_large", "synthetic_period_abdomen_wound_small"],
    },
    {
        "standard_id": "GA_T_1193_2014",
        "clause_id": "9.4.2/10.7/11.1.4/11.2.2/11.3",
        "title": "标准要求根据临床治疗/恢复情况确定的条款组",
        "work_loss_range": [0, 0],
        "nursing_range": [0, 0],
        "nutrition_range": [0, 0],
        "required_fact_fields": ["forensic_facts.rule_features.clinical_course", "forensic_facts.treatment_events"],
        "coverage_status": "clinical_context_required",
        "fixture_ids": ["synthetic_period_spinal_cord_manual"],
        "notes": "规则引擎只标记 manual_review，不自动编造天数。",
    },

    {
        "standard_id": "GA_T_1193_2014",
        "clause_id": "10.2.1-10.2.18",
        "title": "四肢骨折扩展结构化规则族",
        "work_loss_range": [30, 365],
        "nursing_range": [20, 180],
        "nutrition_range": [20, 180],
        "required_fact_fields": ["forensic_facts.fracture_features.limb[].type", "forensic_facts.fracture_features.limb[].has_surgery", "forensic_facts.fracture_features.limb[].joint_surface_involved"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_period_limb_intertrochanteric_surgery", "synthetic_period_metatarsal_toe_surgery"],
        "notes": "在原常见四肢骨折基础上补锁骨至跖趾骨等10.2主干条款；复杂多发损伤仍按附录A取较长期限。",
    },
    {
        "standard_id": "GA_T_1193_2014",
        "clause_id": "10.4/10.5",
        "title": "四肢大关节韧带损伤与主要肌腱断裂",
        "work_loss_range": [60, 150],
        "nursing_range": [0, 90],
        "nutrition_range": [0, 60],
        "required_fact_fields": ["forensic_facts.ligament_meniscus_features"],
        "coverage_status": "partially_covered",
        "fixture_ids": ["synthetic_period_ligament_meniscus"],
        "notes": "10.4可自动给区间；10.5护理/营养为临床确定，输出人工审查项。",
    },
    {
        "standard_id": "GA_T_1193_2014",
        "clause_id": "10.6/10.7",
        "title": "肢体/指趾离断与再植",
        "work_loss_range": [60, 180],
        "nursing_range": [30, 90],
        "nutrition_range": [30, 90],
        "required_fact_fields": ["forensic_facts.amputation_features.level", "forensic_facts.amputation_features.is_replantation"],
        "coverage_status": "partially_covered",
        "fixture_ids": ["synthetic_period_finger_amputation"],
        "notes": "离断可按10.6自动给区间；再植10.7按标准标记临床恢复情况人工确定。",
    },
    {
        "standard_id": "GA_T_1193_2014",
        "clause_id": "5.1.11/5.1.12",
        "title": "视神经损伤/视觉功能障碍与眼球摘除",
        "work_loss_range": [30, 120],
        "nursing_range": [15, 45],
        "nutrition_range": [15, 45],
        "required_fact_fields": ["forensic_facts.eye_facts", "forensic_facts.rule_features.blindness_level_one_eye"],
        "coverage_status": "covered",
        "fixture_ids": ["synthetic_period_eye_visual_impairment"],
    },
]

MANUAL_REVIEW_ROWS = [
    {
        "category": "伤残等级",
        "rule_family": "精神障碍、智能减退、ADL/护理依赖",
        "standard_refs": "5.1.1-5.10.1 中精神/智能/护理依赖相关条款",
        "why_manual": "需要精神专科评估、日常生活能力分级、护理依赖程度和因果关系判断。",
        "review_evidence": "精神检查记录、神经心理量表、ADL评分、护理依赖评定、原始病历和既往史。",
        "how_to_review": "确认损伤因果关系 → 核对量表/专科诊断 → 按标准护理依赖或活动能力受限等级映射。",
    },
    {
        "category": "伤残等级",
        "rule_family": "视力、视野、听力、张口受限、吞咽/发声功能",
        "standard_refs": "头面部损伤各级条款及附录视觉/听觉/张口受限定义",
        "why_manual": "需要专科客观检查和标准分级，单靠诊断名称不能可靠判断等级。",
        "review_evidence": "视力/视野报告、听力图、张口度测量、喉镜/吞咽功能检查。",
        "how_to_review": "先确认检查时间和稳定性，再按标准附录分级；缺少专科检查时不得自动定级。",
    },
    {
        "category": "伤残等级",
        "rule_family": "瘢痕外观、容貌毁损、颈部瘢痕挛缩",
        "standard_refs": "5.7.2、5.8.2、5.9.2、5.10.2、B.14 等",
        "why_manual": "当前可按面积/长度阈值自动处理一部分，但容貌毁损、中心区宽度、年龄折算、畸形程度需人工测量确认。",
        "review_evidence": "带标尺照片、现场测量记录、年龄、瘢痕部位图、中心区标注。",
        "how_to_review": "复核测量单位和部位 → 按年龄折算 → 区分条状/块状/片状 → 决定是否适用更高容貌毁损条款。",
    },
    {
        "category": "伤残等级",
        "rule_family": "内脏功能损害和术后功能障碍",
        "standard_refs": "胸部、腹部、盆会阴各级功能损害条款",
        "why_manual": "功能损害程度依赖心肺肝肾功能、排尿排便、生殖功能等临床指标。",
        "review_evidence": "手术记录、出院诊断、功能检查、实验室指标、复查记录。",
        "how_to_review": "确认器官损伤与手术方式 → 核对功能分级 → 必要时标记专家复核。",
    },
    {
        "category": "三期",
        "rule_family": "根据临床治疗情况确定的三期条款",
        "standard_refs": "7.10、9.4.2、10.7、11.1.4、11.2.2、11.3 等",
        "why_manual": "标准未给固定区间，需结合住院天数、手术次数、并发症和恢复情况。",
        "review_evidence": "门急诊病历、住院首页、出院记录、手术记录、复查医嘱、误工证明。",
        "how_to_review": "以治疗连续性和恢复节点为主线，说明取值理由；不得由规则引擎凭关键词自动给天数。",
    },
]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_disability_inventory(path: Path = DISABILITY_STANDARD) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    grade = None
    heading = ""
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if not line:
            continue
        m_grade = re.match(r"# 5\.(\d+) ([一二三四五六七八九十]+)级", line)
        if m_grade:
            grade = int(m_grade.group(1))
            continue
        m_heading = re.match(r"# (5\.\d+\.\d+) (.+)", line)
        if m_heading:
            heading = m_heading.group(2)
            continue
        m_item = re.match(r"(\d+)\)\s*(.+)", line)
        if m_item and grade and heading:
            item_no, text = m_item.groups()
            clause_id = f"5.{grade}.{heading_to_section(heading)}.{item_no}"
            rows.append({
                "standard_id": "human_disability_2016",
                "clause_id": clause_id,
                "source_heading": f"{grade}级 / {heading}",
                "source_text": text.rstrip("；。"),
                "coverage_status": "not_started",
            })
    return rows


def heading_to_section(heading: str) -> str:
    sections = ["颅脑", "头面", "颈部", "腹部", "盆部", "脊柱", "体表"]
    for idx, key in enumerate(sections, start=1):
        if key in heading:
            return str(idx)
    return "x"


def parse_period_inventory(path: Path = PERIOD_STANDARD) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    current_title = ""
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if not line:
            continue
        m_title = re.match(r"#?\s*(\d+(?:\.\d+)*)\s+(.+)", line)
        if m_title:
            current_title = m_title.group(2)
        m_clause = re.match(r"(\d+\.\d+(?:\.\d+)?)\s*(.*?)(?:：|:)?\s*误工\s*\$?([\d\s]+)(?:\\sim|~|～|-)([\d\s]+)\$?\s*日", line)
        if m_clause:
            cid, label, left, right = m_clause.groups()
            rows.append({
                "standard_id": "GA_T_1193_2014",
                "clause_id": cid,
                "title": label.strip() or current_title,
                "work_loss_range": [int(left.replace(" ", "")), int(right.replace(" ", ""))],
                "coverage_status": "not_started",
            })
    return rows


def merge_coverage(source_rows: List[Dict[str, Any]], implemented_rows: List[Dict[str, Any]], key_name: str = "clause_id") -> List[Dict[str, Any]]:
    merged = list(source_rows)
    by_clause = {r.get(key_name): r for r in merged}
    used_source_clauses = set()
    for item in implemented_rows:
        clause = item.get(key_name)
        branch = item.get("branch_id") or clause
        existing_key = (clause, branch)
        existing_impl_keys = {(r.get(key_name), r.get("branch_id") or r.get(key_name)) for r in merged if r.get("coverage_status") != "not_started"}
        if clause in by_clause and clause not in used_source_clauses and existing_key not in existing_impl_keys:
            by_clause[clause].update(item)
            used_source_clauses.add(clause)
        elif existing_key not in existing_impl_keys:
            merged.append(item)
    return merged


def write_json_rule_files() -> None:
    RULE_DIR.mkdir(parents=True, exist_ok=True)
    (RULE_DIR / "disability_clauses.json").write_text(json.dumps(IMPLEMENTED_DISABILITY_ROWS, ensure_ascii=False, indent=2), encoding="utf-8")
    (RULE_DIR / "coverage_disability.json").write_text(json.dumps(merge_coverage(parse_disability_inventory(), IMPLEMENTED_DISABILITY_ROWS), ensure_ascii=False, indent=2), encoding="utf-8")
    (RULE_DIR / "coverage_period.json").write_text(json.dumps(merge_coverage(parse_period_inventory(), IMPLEMENTED_PERIOD_ROWS), ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({k for row in rows for k in row.keys()}) or ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items()})


def _write_manual_review_report() -> None:
    _write_csv(RUN_DIR / "manual_review_items.csv", MANUAL_REVIEW_ROWS)
    lines = ["# Ruler manual review guide", ""]
    for row in MANUAL_REVIEW_ROWS:
        lines.extend([
            f"## {row['category']}：{row['rule_family']}",
            f"- 标准范围：{row['standard_refs']}",
            f"- 为什么需要人工审查：{row['why_manual']}",
            f"- 需要证据：{row['review_evidence']}",
            f"- 审查方法：{row['how_to_review']}",
            "",
        ])
    (RUN_DIR / "manual_review_guide.md").write_text("\n".join(lines), encoding="utf-8")


def build_coverage_reports() -> Dict[str, Any]:
    write_json_rule_files()
    disability = json.loads((RULE_DIR / "coverage_disability.json").read_text(encoding="utf-8"))
    period = json.loads((RULE_DIR / "coverage_period.json").read_text(encoding="utf-8"))
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    _write_csv(RUN_DIR / "coverage_disability.csv", disability)
    _write_csv(RUN_DIR / "coverage_period.csv", period)
    _write_manual_review_report()
    summary = coverage_snapshot(disability, period)
    (RUN_DIR / "validation_summary.md").write_text(
        "# Ruler rule coverage summary\n\n"
        f"- 伤残条款矩阵行数：{summary['disability_total']}\n"
        f"- 伤残 covered/partially_covered：{summary['disability_covered']}\n"
        f"- 三期条款矩阵行数：{summary['period_total']}\n"
        f"- 三期 covered/partially_covered：{summary['period_covered']}\n"
        f"- 人工审查规则族：{len(MANUAL_REVIEW_ROWS)}\n"
        "\n详见 `manual_review_guide.md` 和 `manual_review_items.csv`。\n",
        encoding="utf-8",
    )
    return summary


def coverage_snapshot(disability_rows: List[Dict[str, Any]] | None = None, period_rows: List[Dict[str, Any]] | None = None) -> Dict[str, int]:
    try:
        if disability_rows is None:
            disability_rows = json.loads((RULE_DIR / "coverage_disability.json").read_text(encoding="utf-8"))
        if period_rows is None:
            period_rows = json.loads((RULE_DIR / "coverage_period.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        disability_rows = merge_coverage(parse_disability_inventory(), IMPLEMENTED_DISABILITY_ROWS)
        period_rows = merge_coverage(parse_period_inventory(), IMPLEMENTED_PERIOD_ROWS)
    covered_values = {"covered", "partially_covered"}
    return {
        "disability_total": len(disability_rows),
        "disability_covered": sum(1 for r in disability_rows if r.get("coverage_status") in covered_values),
        "period_total": len(period_rows),
        "period_covered": sum(1 for r in period_rows if r.get("coverage_status") in covered_values),
    }


def validate_schema() -> List[str]:
    write_json_rule_files()
    errors: List[str] = []
    for path in [RULE_DIR / "disability_clauses.json", RULE_DIR / "period_clauses.json", RULE_DIR / "coverage_disability.json", RULE_DIR / "coverage_period.json"]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - CLI diagnostic path
            errors.append(f"{path}: JSON读取失败: {exc}")
            continue
        if not isinstance(data, list):
            errors.append(f"{path}: 顶层必须是列表")
    for row in IMPLEMENTED_DISABILITY_ROWS:
        grade = row.get("grade")
        if not isinstance(grade, int) or not (1 <= grade <= 10):
            errors.append(f"伤残条款 {row.get('clause_id')} 等级非法: {grade}")
        if row.get("coverage_status") in {"covered", "partially_covered"} and not row.get("fixture_ids"):
            errors.append(f"伤残条款 {row.get('clause_id')} 缺少 fixture_ids")
    for row in IMPLEMENTED_PERIOD_ROWS:
        for key in ["work_loss_range", "nursing_range", "nutrition_range"]:
            rng = row.get(key, [0, 0])
            if len(rng) != 2 or rng[0] > rng[1] or rng[0] < 0:
                errors.append(f"三期条款 {row.get('clause_id')} 区间非法: {key}={rng}")
        if row.get("coverage_status") in {"covered", "partially_covered"} and not row.get("fixture_ids"):
            errors.append(f"三期条款 {row.get('clause_id')} 缺少 fixture_ids")
    return errors
