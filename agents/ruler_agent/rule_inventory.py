"""法规条款全量结构化模块。

把《人体损伤致残程度分级》与《人身损害误工期、护理期、营养期评定规范》
(G A/T 1193-2014) 的全部条款解析为机器可执行对象。

每一条款输出以下字段：

    clause_id            条款号
    grade                伤残等级（仅伤残标准）
    source_heading       所属章节
    source_text          条款原文
    body_region          部位分类（7 类）
    rule_family          规则族（用于归并与证据清单）
    disposition          处置方式，见下
    required_fact_fields 该条款裁决所必需的事实字段路径
    review_evidence      人工复核所需材料（structured_manual 时）
    how_to_review        人工复核方法（structured_manual 时）

disposition 取三个值之一：

    auto_decidable      确定性引擎已实现该条款的自动裁决
    structured_manual   已结构化但需人工判断，携带必需字段与证据清单
    clinical_context    条款本身要求"根据临床治疗情况确定"，不得自动取值

覆盖率口径：auto_decidable + structured_manual + clinical_context = 全量条款，
即每个条款都是机器可执行对象；其中 auto_decidable 为可自动裁决部分。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

RULER_DIR = Path(__file__).resolve().parent
STANDARD_DIR = RULER_DIR.parents[1] / "files" / "知识库" / "知识库_条例"
DISABILITY_STANDARD = STANDARD_DIR / "人体损伤致残程度分级.md"
PERIOD_STANDARD = STANDARD_DIR / "人身损害误工期、护理期、营养期评定.md"

RULES_DIR = RULER_DIR / "rules"
DISABILITY_RULES_PATH = RULES_DIR / "disability_rules_full.json"
PERIOD_RULES_PATH = RULES_DIR / "period_rules_full.json"

DISABILITY_STANDARD_ID = "human_disability_2016"
PERIOD_STANDARD_ID = "GA_T_1193_2014"

# 引擎源码：用于判定哪些条款已实现自动裁决
ENGINE_SOURCES = (
    RULER_DIR / "disability_engine.py",
    RULER_DIR / "ruler_agent_main.py",
    RULER_DIR / "fact_adapter.py",
)

# ---------------------------------------------------------------- 文本清洗

_MATH_NOISE = [
    (re.compile(r"\$"), ""),
    (re.compile(r"\\%"), "%"),
    (re.compile(r"\\sim"), "~"),
    (re.compile(r"\\leqslant|\\le\b"), "≤"),
    (re.compile(r"\\geqslant|\\ge\b"), "≥"),
    (re.compile(r"\\text(?:less|greater)[^}]*\}"), ""),
    (re.compile(r"\\mathrm\s*\{([^}]*)\}"), r"\1"),
    (re.compile(r"\\text\s*\{([^}]*)\}"), r"\1"),
    (re.compile(r"\^ \{\\circ\}"), "°"),
    (re.compile(r"\\circ"), "°"),
    (re.compile(r"\\mathord|\\left|\\right|\\,"), ""),
    (re.compile(r"[{}]"), ""),
    (re.compile(r"\s+"), " "),
]


def clean_math(text: str) -> str:
    """去掉标准 Markdown 中残留的 LaTeX 记号，还原为可读条文。"""
    out = text or ""
    for pattern, repl in _MATH_NOISE:
        out = pattern.sub(repl, out)
    return out.strip()


def _to_int(text: str) -> Optional[int]:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


# ---------------------------------------------------------------- 部位分类

BODY_REGIONS: Sequence[Tuple[str, str]] = (
    ("颅脑", "neuro"),
    ("头面", "head_face"),
    ("颈部及胸部", "neck_chest"),
    ("腹部", "abdomen"),
    ("盆部", "pelvis"),
    ("脊柱", "spine_limb"),
    ("体表", "surface"),
)

REGION_LABEL = {
    "neuro": "颅脑、脊髓及周围神经",
    "head_face": "头面部",
    "neck_chest": "颈部及胸部",
    "abdomen": "腹部",
    "pelvis": "盆部及会阴部",
    "spine_limb": "脊柱、骨盆及四肢",
    "surface": "体表及其他",
}


def detect_region(heading: str) -> str:
    for key, region in BODY_REGIONS:
        if key in heading:
            return region
    return "other"


# ---------------------------------------------------------------- 规则族表
#
# 每条：(匹配关键词元组, 族名, 必需事实字段, 复核证据, 复核方法)
# 关键词按顺序匹配，先命中者生效；因此更具体的族放在前面。

R = "forensic_facts."
FAMILY_RULES: Sequence[Tuple[Tuple[str, ...], str, List[str], str, str]] = (
    # ---- 脊柱、骨盆及四肢 ----
    ((("肋骨",),), "rib_fracture",
     [R + "fracture_features.rib.count", R + "fracture_features.rib.deformity_healing_count",
      R + "fracture_features.rib.missing_count"],
     "胸部影像报告（肋骨三维重建）、手术记录", "按肋骨序号逐根确认骨折、缺失与畸形愈合根数。"),
    ((("椎",),), "vertebral_fracture",
     [R + "fracture_features.vertebral.count", R + "fracture_features.vertebral.compression_degrees",
      R + "fracture_features.vertebral.has_spine_surgery"],
     "脊柱影像报告、压缩程度测量、手术记录", "逐节段确认骨折、压缩程度与是否手术。"),
    ((("足弓",),), "foot_arch",
     [R + "rule_features.foot_arch_break_complete", R + "rule_features.foot_arch_break_partial"],
     "足弓负重位影像、专科查体", "确认足弓完全破坏或部分破坏的侧别与数量。"),
    ((("肢体长度", "下肢长度", "上肢长度", "长度相差"),), "limb_length",
     [R + "rule_features.limb_length_difference_cm"],
     "双下肢全长位影像、体格测量", "以影像测量为准，确认相差厘米数。"),
    ((("大关节", "关节功能丧失", "关节强直", "关节僵直", "前臂旋转"),), "joint_function",
     [R + "rom_measurements", R + "rule_features.major_joint_loss_percent_max"],
     "关节活动度测量记录与量角器照片", "按附录 C 表查功能丧失率，确认关节与侧别。"),
    ((("功能丧失分值", "丧失分值"),), "hand_foot_score",
     [R + "rule_features.hand_or_foot_function_score"],
     "手或足功能评分记录", "按标准附录的手足功能丧失分值表累计。"),
    ((("骨盆", "髋臼", "跟骨"),), "pelvis_heel",
     [R + "fracture_features.limb", R + "imaging_events"],
     "骨盆或跟骨影像、手术记录", "确认畸形愈合及是否影响功能。"),
    ((("缺失", "离断", "截肢"),), "amputation",
     [R + "amputation_features"],
     "手术记录、断肢再植记录", "确认缺失平面与侧别。"),
    ((("创伤性关节炎",),), "traumatic_arthritis",
     [R + "rule_features.traumatic_arthritis"],
     "关节影像、关节镜或专科查体", "确认创伤性关节炎诊断与其与外伤的因果关系。"),
    ((("关节假体置换", "关节置换"),), "joint_replacement",
     [R + "rule_features.major_joint_replacement"],
     "手术记录、内置物合格证", "确认置换关节与侧别。"),
    ((("骨不连", "骨不愈合", "延迟愈合"),), "nonunion",
     [R + "fracture_healing_status"],
     "系列影像、病程记录", "确认骨不连或延迟愈合及持续时间。"),
    ((("骨髓炎",),), "osteomyelitis",
     [R + "fracture_healing_status"],
     "细菌培养、影像、病程记录", "确认慢性骨髓炎或死腔形成及持续时间。"),
    ((("骨骺",),), "epiphyseal",
     [R + "fracture_features.limb"],
     "损伤时年龄证明、影像", "确认损伤时是否为青少年及是否累及骨骺。"),
    ((("髌骨",),), "patella",
     [R + "rule_features.patella_removed_one_side"],
     "手术记录", "确认髌骨切除侧别。"),
    ((("交叉韧带", "半月板", "侧副韧带"),), "ligament",
     [R + "ligament_meniscus_features"],
     "关节镜手术记录、查体", "确认韧带与半月板损伤组合及术后功能影响。"),
    ((("脊柱骨折后遗留", "侧弯", "后凸"),), "spine_deformity",
     [R + "rule_features.spine_deformity_angle_deg"],
     "脊柱全长位影像、Cobb 角测量", "测量侧弯或后凸角度，确认是否达阈值。"),
    ((("手功能", "手指", "拇指", "掌骨", "指骨"),), "hand",
     [R + "amputation_features", R + "fracture_features.limb"],
     "手部影像、专科查体、功能评分", "确认手指缺失或功能丧失分值。"),
    ((("足趾", "趾"),), "foot",
     [R + "amputation_features"],
     "足部影像、专科查体", "确认足趾缺失或功能丧失分值。"),

    # ---- 体表 ----
    ((("皮肤瘢痕形成达体表面积", "体表面积", "手部皮肤瘢痕", "植皮"),), "body_scar_area",
     [R + "scar_burn_facts.scar_bsa_percent"],
     "瘢痕面积测量记录与照片、植皮手术记录",
     "按体表面积百分比或手掌面积比例测量，确认达阈值。"),
    ((("再生障碍性贫血",),), "aplastic_anemia",
     [], "血液科诊断、骨髓穿刺报告", "确认贫血分型（重型或非重型）。"),
    ((("放射性皮肤癌",),), "radiation_skin_cancer",
     [], "病理诊断、放疗记录", "确认病理诊断成立。"),

    # ---- 颅脑、脊髓及周围神经 ----
    ((("精神障碍", "智能减退", "植物生存"),), "mental_intelligence",
     [R + "neuro_facts.adl_score", R + "neuro_facts.objective_neuro_deficit"],
     "精神专科检查、智力量表、ADL 评分、护理依赖评定、既往史",
     "确认损伤因果关系 → 核对量表与专科诊断 → 按护理依赖或活动能力受限等级映射。"),
    ((("日常生活能力", "日常生活有关的活动能力"),), "mental_intelligence",
     [R + "neuro_facts.adl_score"],
     "精神专科检查、ADL 评分、护理依赖评定",
     "确认损伤因果关系与日常生活能力受限等级。"),
    ((("瘫",),), "paralysis",
     [R + "neuro_facts.muscle_grades", R + "neuro_facts.objective_neuro_deficit"],
     "神经系统查体、肌力分级记录、肌电图",
     "按肢体逐级确认肌力等级与瘫痪范围（四肢瘫/三肢瘫/截瘫/偏瘫/单肢瘫）。"),
    ((("肌瘫",),), "paralysis",
     [R + "neuro_facts.muscle_grades"],
     "神经系统查体、肌力分级记录", "确认肌群范围与肌力等级。"),
    ((("重要神经损伤", "肌群肌力", "脑软化灶", "嗅觉"),), "peripheral_nerve",
     [R + "neuro_facts.muscle_grades", R + "neuro_facts.objective_neuro_deficit"],
     "神经系统查体、肌电图、颅脑影像",
     "确认受损神经、遗留肌群肌力等级或脑软化灶与症状体征的对应关系。"),
    ((("失语", "失用", "失写", "失读", "失认"),), "aphasia_apraxia",
     [R + "neuro_facts.language_function"],
     "神经心理评估、语言功能评定、影像",
     "区分完全性与不完全性，确认类型。"),
    ((("面瘫",),), "facial_paralysis",
     [R + "neuro_facts.facial_nerve"],
     "面部查体、面神经功能分级", "确认完全性或部分性面瘫及侧别、遗留症状。"),
    ((("癫痫",),), "epilepsy",
     [R + "neuro_facts.epilepsy"],
     "癫痫专科诊断、脑电图、用药记录", "确认外伤性癫痫及严重程度分级。"),
    ((("尿崩症",),), "diabetes_insipidus",
     [R + "neuro_facts.diabetes_insipidus"],
     "内分泌专科诊断、禁水加压素试验", "确认尿崩症及严重程度。"),
    ((("排尿功能障碍", "排便功能障碍", "排尿", "排便"),), "bowel_bladder",
     [R + "neuro_facts.bowel_bladder_function"],
     "泌尿外科与肛肠科检查、尿动力学、影像", "确认功能障碍程度（重度或非重度）。"),
    ((("勃起障碍",),), "erectile_dysfunction",
     [R + "neuro_facts.erectile_function"],
     "泌尿外科检查、夜间勃起监测", "确认器质性勃起障碍及严重程度。"),
    ((("非肢体瘫运动障碍",),), "non_limb_motor",
     [R + "neuro_facts.objective_neuro_deficit"],
     "神经系统查体、影像", "确认运动障碍程度分级。"),
    ((("颅骨缺损", "开颅", "脑叶"),), "craniotomy",
     [R + "neuro_facts.craniotomy_done"],
     "手术记录、颅骨缺损影像", "确认开颅或颅骨缺损事实与面积。"),

    # ---- 头面部 ----
    ((("容貌毁损",),), "facial_disfigurement",
     [R + "scar_burn_facts.scar_bsa_percent", R + "scar_burn_facts.face_scar_total_length_cm",
      R + "scar_burn_facts.scar_total_area_cm2"],
     "面部照片、容貌毁损项目逐项比对",
     "按标准附录容貌毁损项目逐项核对，确认符合项数。"),
    ((("面部条状瘢痕", "面部中心区条状瘢痕"),), "face_scar_linear",
     [R + "scar_burn_facts.face_scar_total_length_cm",
      R + "scar_burn_facts.face_center_scar_length_cm"],
     "面部瘢痕测量记录与照片（含宽度）",
     "按中心区与非中心区分别累计长度，确认宽度是否达阈值。"),
    ((("面部块状", "面部片状", "色素"),), "face_scar_area",
     [R + "scar_burn_facts.scar_total_area_cm2",
      R + "scar_burn_facts.face_scar_area_cm2"],
     "面部瘢痕面积测量记录与照片", "按面积累计，区分单块与多块。"),
    ((("头皮",),), "scalp",
     [], "头皮照片、专科查体", "确认头皮缺损范围与可否修复。"),
    ((("视野", "偏盲"),), "visual_field",
     [R + "eye_facts.visual_field_effective_pct"],
     "视野计检查报告", "确认视野有效值百分比与直径。"),
    ((("眼球", "盲目", "视力", "视觉", "青光眼", "低眼压", "虹膜", "白内障",
       "晶体", "角膜", "泪器", "溢泪", "复视", "斜视"),), "visual_function",
     [R + "eye_facts.eyeball_missing_or_removed", R + "eye_facts.blindness_level_num",
      R + "eye_facts.moderate_visual_impairment", R + "eye_facts.severe_visual_impairment"],
     "眼科专科检查、视力表、裂隙灯、眼底与电生理",
     "按盲目分级、视力损害分级或眼附属器损伤逐眼确认。"),
    ((("鼻",),), "nose",
     [], "耳鼻喉与整形专科查体、照片", "确认鼻翼、鼻尖缺损范围或畸形程度。"),
    ((("耳廓",),), "ear",
     [], "耳鼻喉专科查体、照片", "确认耳廓缺损或畸形累计范围。"),
    ((("张口受限",),), "mouth_opening",
     [R + "rule_features.mouth_opening_degree"],
     "口腔颌面专科查体、开口度测量", "测量最大开口度并确认受限程度分级。"),
    ((("脑脊液漏",),), "csf_leak",
     [], "耳鼻喉与神经外科检查、影像、手术记录", "确认脑脊液漏事实、手术经过与是否持续不愈。"),
    ((("眼睑",),), "eyelid",
     [], "眼科与整形专科查体、照片", "确认眼睑畸形或下垂程度及是否遮盖瞳孔。"),
    ((("听力",),), "hearing",
     [], "纯音测听、声导抗、听觉诱发电位", "按 dB HL 确认各耳听力障碍程度。"),
    ((("上颌骨", "下颌骨", "颌骨", "面颅骨"),), "jaw",
     [], "颌面影像、专科查体", "确认颌骨缺损范围比例或牙齿缺失枚数。"),
    ((("牙齿",),), "teeth",
     [], "口腔专科检查、全景片", "确认牙齿缺失枚数。"),
    ((("唇",),), "lip",
     [], "面部照片、口腔颌面专科查体", "确认唇缺损或畸形范围。"),
    ((("舌",),), "tongue",
     [], "口腔颌面专科查体", "确认舌缺损范围。"),
    ((("吞咽", "咽"),), "swallowing",
     [], "吞咽功能评估、造影", "确认吞咽功能受限程度（流质/半流质）。"),
    ((("前庭",),), "vestibular",
     [], "前庭功能检查、平衡试验", "确认前庭平衡功能丧失程度。"),
    ((("涎",),), "salivary",
     [], "专科查体", "确认涎漏事实。"),
    ((("颏颈粘连", "颈部瘢痕", "颈前三角区"),), "neck_scar",
     [], "颈部照片、专科查体", "确认粘连程度。"),

    # ---- 颈部及胸部 ----
    ((("心功能", "心律失常", "心脏", "起搏器", "心"),), "cardiac",
     [], "心内科专科检查、心脏超声、心电图、心功能分级",
     "确认心功能分级或心律失常类型与器械植入事实。"),
    ((("肺", "呼吸困难", "胸廓", "气管", "支气管"),), "pulmonary",
     [], "呼吸科专科检查、肺功能、血气分析、影像、手术记录",
     "确认肺功能受损程度、呼吸困难分级、肺切除范围或气道成形术式。"),
    ((("颈总动脉", "颈内动脉", "血管", "冠状动脉", "胸导管"),), "vascular",
     [R + "treatment_events", R + "rule_features.limb_vascular_compromise"],
     "血管超声、造影、手术记录",
     "确认血管损伤、狭窄、支架置入或移植事实及其对功能的影响。"),
    ((("食管",),), "esophagus",
     [], "消化科与胸外科检查、造影", "确认食管狭窄程度或重建术式。"),
    ((("甲状腺", "甲状旁腺"),), "thyroid",
     [], "内分泌专科检查、激素水平", "确认腺体功能损害程度。"),
    ((("乳房", "乳头"),), "breast",
     [], "专科查体、影像", "确认乳房缺失或畸形范围。"),
    ((("喉返神经", "喉", "发声", "声音嘶哑"),), "larynx",
     [], "喉镜、嗓音功能评估", "确认喉返神经损伤与功能影响。"),

    # ---- 腹部 ----
    ((("肝",),), "liver",
     [R + "treatment_events"],
     "手术记录、肝功能与影像", "确认肝切除范围或肝衰竭分期。"),
    ((("肾",),), "kidney",
     [R + "treatment_events"],
     "手术记录、肾功能与核素显像、透析记录", "确认肾切除、肾功能下降程度或移植事实。"),
    ((("胰",),), "pancreas",
     [R + "treatment_events"],
     "手术记录、血糖与消化功能", "确认胰腺切除范围与功能依赖情况。"),
    ((("脾",),), "spleen",
     [R + "treatment_events"],
     "手术记录", "确认脾切除事实与年龄。"),
    ((("小肠", "结肠", "肠", "回肠", "造口", "肠瘘"),), "intestine",
     [R + "treatment_events"],
     "手术记录、营养支持记录、病程记录", "确认肠切除范围、造口事实与营养依赖程度。"),
    ((("胃",),), "stomach",
     [R + "treatment_events"],
     "手术记录", "确认胃切除范围。"),
    ((("肾上腺",),), "adrenal",
     [], "内分泌专科检查、手术记录", "确认肾上腺缺失或功能减退。"),
    ((("膈肌", "膈疝"),), "diaphragm",
     [], "胸腹部影像、手术记录", "确认膈肌修补术后是否遗留功能障碍。"),
    ((("腹壁",),), "abdominal_wall",
     [], "专科查体、影像、手术记录", "确认腹壁缺损范围或腹壁疝可否修补。"),
    ((("胆",),), "biliary",
     [R + "treatment_events"],
     "手术记录、胆道影像", "确认胆道手术与术后并发症。"),

    # ---- 盆部及会阴部 ----
    ((("膀胱", "尿", "输尿管", "尿道"),), "urinary",
     [], "泌尿外科检查、尿动力学、影像", "确认膀胱缺失、造口、尿瘘或尿道狭窄事实。"),
    ((("卵巢", "子宫", "阴道", "输卵管"),), "female_reproductive",
     [], "妇科专科检查、影像、年龄证明", "确认缺失或功能丧失范围及是否未成年人。"),
    ((("睾丸", "阴茎", "输精管", "会阴", "生殖"),), "male_reproductive",
     [], "泌尿外科与男科专科检查、激素水平", "确认缺失、萎缩或功能损害程度。"),
    ((("直肠", "肛门", "肛管"),), "anorectal",
     [], "肛肠科与普外科检查、影像", "确认排便功能障碍、失禁或闭锁事实。"),
)


def _flat_keywords(keywords: Iterable[Any]) -> List[str]:
    """关键字表允许写成嵌套元组，这里统一展平为字符串列表。"""
    out: List[str] = []
    for item in keywords:
        if isinstance(item, str):
            out.append(item)
        else:
            out.extend(_flat_keywords(item))
    return out


def classify(body_region: str, text: str) -> Tuple[str, List[str], str, str]:
    """返回 (族名, 必需字段, 复核证据, 复核方法)。"""
    for keywords, family, fields, evidence, how in FAMILY_RULES:
        if any(k in text for k in _flat_keywords(keywords)):
            return family, list(fields), evidence, how
    return "unclassified_" + body_region, [], "原始病历、检查报告与影像", "按条款原文逐项核对事实后人工判定。"


# ---------------------------------------------------------------- 解析伤残标准

_GRADE_RE = re.compile(r"^#\s*5\.(\d+)\s*[一二三四五六七八九十]+级")
_HEADING_RE = re.compile(r"^#\s*(5\.\d+\.\d+)\s+(.+)")
_ITEM_RE = re.compile(r"^(\d+)\)\s*(.+)")


def _section_number(heading: str) -> str:
    mapping = {"颅脑": "1", "头面": "2", "颈部": "3", "腹部": "4",
               "盆部": "5", "脊柱": "6", "体表": "7"}
    for key, num in mapping.items():
        if key in heading:
            return num
    return "x"


def parse_disability_clauses(path: Path = DISABILITY_STANDARD) -> List[Dict[str, Any]]:
    """解析《人体损伤致残程度分级》全部条款。"""
    rows: List[Dict[str, Any]] = []
    grade: Optional[int] = None
    heading = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        m_grade = _GRADE_RE.match(line)
        if m_grade:
            grade = int(m_grade.group(1))
            continue
        m_heading = _HEADING_RE.match(line)
        if m_heading:
            heading = clean_math(m_heading.group(2))
            continue
        m_item = _ITEM_RE.match(line)
        if m_item and grade and heading:
            item_no, text = m_item.groups()
            clause_id = f"5.{grade}.{_section_number(heading)}.{item_no}"
            rows.append({
                "clause_id": clause_id,
                "grade": grade,
                "source_heading": f"{grade}级 / {heading}",
                "source_text": clean_math(text).rstrip("；。"),
            })
    return rows


# ---------------------------------------------------------------- 解析三期标准

_CLAUSE_RE = re.compile(r"^(\d+\.\d+\.\d+)\s*(.*)$")
_SUBITEM_RE = re.compile(r"^([a-z])\s*[)）]\s*(.*)$")
_NUM = r"[\d\s]+"


def _range(text: str) -> Optional[List[int]]:
    m = re.search(rf"({_NUM})\s*(?:~|～|—|-|至)\s*({_NUM})\s*日", text)
    if m:
        lo, hi = _to_int(m.group(1)), _to_int(m.group(2))
        if lo is not None and hi is not None:
            return [lo, hi]
    m = re.search(rf"({_NUM})\s*日", text)
    if m:
        v = _to_int(m.group(1))
        if v is not None:
            return [v, v]
    return None


def _periods_from_text(text: str) -> Dict[str, Any]:
    """从一段文字中提取误工/护理/营养区间。"""
    t = clean_math(text)
    out: Dict[str, Any] = {"误工": None, "护理": None, "营养": None}
    # 「无需护理和营养」「原则上不考虑护理、营养」等表述按 0 日处理
    if re.search(r"无需护理|不考虑护理|无需.{0,4}护理", t):
        out["护理"] = [0, 0]
    if re.search(r"无需营养|不考虑营养|无需.{0,4}营养", t):
        out["营养"] = [0, 0]
    for key in ("误工", "护理", "营养"):
        m = re.search(rf"{key}[^;；。]{{0,60}}", t)
        if m:
            value = _range(m.group(0))
            if value:
                out[key] = value
    if "根据临床治疗情况确定" in t:
        out["manual"] = True
    return out


def _merge_periods(target: Dict[str, Any], extra: Dict[str, Any]) -> None:
    for key in ("误工", "护理", "营养"):
        if target.get(key) is None and extra.get(key):
            target[key] = extra[key]
    if extra.get("manual"):
        target["manual"] = True


def parse_period_clauses(path: Path = PERIOD_STANDARD) -> List[Dict[str, Any]]:
    """解析 GA/T 1193-2014 全部条款。

    该标准的条款有三种书写形式，均需覆盖：

        4.1.1 头皮下血肿：误工 7~15 日，无需护理和营养。      ← 正文行内区间
        4.1.2 帽状腱膜下血肿/骨膜下血肿：                      ← 正文行 + a)/b) 子项
            a) 一般情况下: 误工 15~30 日, ...
        # 5.1.1 眼睑损伤：                                    ← 标题即为条款
            a) 眼睑血肿: ...
        # 4.9 颅脑损伤并发症及后遗症                          ← 二级标题即条款，
            根据临床治疗情况确定。                               需临床背景
    """
    rows: List[Dict[str, Any]] = []
    section = ""
    subsection = ""
    current: Optional[Dict[str, Any]] = None

    def open_clause(cid: str, rest: str, tentative: bool, source_line: str) -> None:
        nonlocal current
        parts = [subsection, clean_math(rest)]
        title = "｜".join(p for p in parts if p)
        current = {
            "clause_id": cid,
            "section": section,
            "title": title,
            "ranges": _periods_from_text(source_line),
            "sub_items": [],
            "tentative": tentative,
        }

    def flush() -> None:
        nonlocal current
        if current is not None:
            has = any(current["ranges"].get(k) for k in ("误工", "护理", "营养"))
            if (has or current["ranges"].get("manual")) and not (
                current["tentative"] and not has and not current["ranges"].get("manual")
            ):
                current.pop("tentative", None)
                rows.append(current)
        current = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue

        # 三级编号：正文条款或标题条款
        m = _CLAUSE_RE.match(line.lstrip("# "))
        if m and line.lstrip("# ")[:1].isdigit():
            flush()
            open_clause(m.group(1), m.group(2), tentative=False, source_line=line)
            continue

        # 二级及一级标题
        m_sub = re.match(r"^#\s*(\d+\.\d+)\s+(.+)$", line)
        if m_sub:
            flush()
            subsection = clean_math(m_sub.group(2))
            open_clause(m_sub.group(1), m_sub.group(2), tentative=True, source_line=line)
            continue
        m_sec = re.match(r"^#\s*(\d+)\s+(\S.*)$", line)
        if m_sec:
            flush()
            section = f"{m_sec.group(1)} {clean_math(m_sec.group(2))}"
            subsection = ""
            continue
        if line.startswith("#"):
            continue

        # 子项 a) / b） / c)
        m_item = _SUBITEM_RE.match(line)
        if m_item and current is not None:
            letter, rest = m_item.groups()
            pr = _periods_from_text(line)
            current["sub_items"].append({
                "label": letter,
                "text": clean_math(rest).rstrip("；。;"),
                "误工": pr["误工"], "护理": pr["护理"], "营养": pr["营养"],
            })
            _merge_periods(current["ranges"], pr)
            continue

        # 条款的续行（如「根据临床治疗情况确定。」）
        if current is not None:
            _merge_periods(current["ranges"], _periods_from_text(line))
            if not current["title"]:
                current["title"] = clean_math(line)[:40]

    flush()
    # 条款号去重，保留首个
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for row in rows:
        if row["clause_id"] in seen:
            continue
        seen.add(row["clause_id"])
        unique.append(row)
    return unique


# ---------------------------------------------------------------- 已实现条款探测

# 只认「引擎真正会产出的条款」，即裁决结果的构造点与条款索引条目。
# 仅仅出现在注释、人工审查清单或关键词表里的条款号不算自动可裁决，
# 否则会把人工条款误报为自动条款。
_ADJUDICATION_PATTERNS = (
    re.compile(r'DisabilityResult\(\s*"(5\.\d+(?:\.\d+)*)"'),          # 位置参数
    re.compile(r'DisabilityResult\([^)]*?clause\s*=\s*"(5\.\d+(?:\.\d+)*)"', re.S),
    re.compile(r'"clause"\s*:\s*"(5\.\d+(?:\.\d+)*)"'),                # 条款索引条目
    re.compile(r'clause\s*=\s*"(5\.\d+(?:\.\d+)*)"'),
)


def implemented_clause_ids(sources: Iterable[Path] = ENGINE_SOURCES) -> set:
    """扫描引擎源码，取出引擎能够自动裁决输出的伤残条款号。

    以代码为准判定自动覆盖范围，避免文档与实现脱节，也避免把
    仅出现在人工清单中的条款误计为自动条款。
    """
    found: set = set()
    for path in sources:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in _ADJUDICATION_PATTERNS:
            for m in pattern.finditer(text):
                found.add(m.group(1))
    return found


# ---------------------------------------------------------------- 组装

def build_disability_rules(implemented: Optional[set] = None) -> List[Dict[str, Any]]:
    implemented = implemented if implemented is not None else implemented_clause_ids()
    rules: List[Dict[str, Any]] = []
    for row in parse_disability_clauses():
        region = detect_region(row["source_heading"])
        family, fields, evidence, how = classify(region, row["source_text"])
        is_auto = row["clause_id"] in implemented
        rule = {
            "standard_id": DISABILITY_STANDARD_ID,
            "clause_id": row["clause_id"],
            "grade": row["grade"],
            "source_heading": row["source_heading"],
            "source_text": row["source_text"],
            "body_region": region,
            "body_region_label": REGION_LABEL.get(region, "其他"),
            "rule_family": family,
            "disposition": "auto_decidable" if is_auto else "structured_manual",
            "required_fact_fields": fields,
        }
        if not is_auto:
            rule["review_evidence"] = evidence
            rule["how_to_review"] = how
        else:
            rule["required_fact_fields"] = fields
        rules.append(rule)
    return rules


def build_period_rules() -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    for row in parse_period_clauses():
        ranges = row["ranges"]
        manual = bool(ranges.get("manual"))
        rules.append({
            "standard_id": PERIOD_STANDARD_ID,
            "clause_id": row["clause_id"],
            "section": row["section"],
            "title": row["title"],
            "误工": ranges.get("误工"),
            "护理": ranges.get("护理"),
            "营养": ranges.get("营养"),
            "sub_items": row["sub_items"],
            "disposition": "clinical_context" if manual else "auto_decidable",
            "required_fact_fields": [
                R + "period_rule_features.candidate_clauses",
                R + "recognized_injuries",
                R + "treatment_events",
            ],
            "review_evidence": "住院首页、出院记录、手术记录、复查医嘱、误工证明",
            "how_to_review": "以治疗连续性和恢复节点为主线，说明取值理由；不得由规则引擎凭关键词自动给天数。",
        })
    return rules


def coverage_summary(rules: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(rules)
    auto = sum(1 for r in rules if r["disposition"] == "auto_decidable")
    manual = sum(1 for r in rules if r["disposition"] == "structured_manual")
    clinical = sum(1 for r in rules if r["disposition"] == "clinical_context")
    by_family: Dict[str, int] = {}
    for r in rules:
        if "rule_family" in r:
            by_family[r["rule_family"]] = by_family.get(r["rule_family"], 0) + 1
    return {
        "total": total,
        "auto_decidable": auto,
        "structured_manual": manual,
        "clinical_context": clinical,
        "structured_total": auto + manual + clinical,
        "structured_rate": round((auto + manual + clinical) / total, 4) if total else 0.0,
        "auto_rate": round(auto / total, 4) if total else 0.0,
        "families": dict(sorted(by_family.items(), key=lambda kv: -kv[1])),
    }


def write_rule_files() -> Dict[str, Any]:
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    disability = build_disability_rules()
    period = build_period_rules()
    DISABILITY_RULES_PATH.write_text(
        json.dumps(disability, ensure_ascii=False, indent=1), encoding="utf-8")
    PERIOD_RULES_PATH.write_text(
        json.dumps(period, ensure_ascii=False, indent=1), encoding="utf-8")
    return {
        "disability": coverage_summary(disability),
        "period": coverage_summary(period),
    }


if __name__ == "__main__":
    import sys

    summary = write_rule_files()
    for name, stat in summary.items():
        print(f"[{name}] 总条款 {stat['total']}｜自动可裁决 {stat['auto_decidable']}"
              f"｜结构化转人工 {stat['structured_manual']}"
              f"｜需临床背景 {stat['clinical_context']}"
              f"｜结构化率 {stat['structured_rate']:.2%}")
