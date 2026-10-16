import os
import json
import re
import argparse
import time
import threading
import sys
from collections import Counter
from datetime import date
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# LangChain 相关库
from langchain_core.prompts import ChatPromptTemplate
from agents.common.llm_client import append_local_prompt_if_needed, build_chat_llm
from agents.common.output_paths import resolve_numbered_output_dir
from agents.common.runtime_config import apply_runtime_env, get_runtime_config
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

# 加载项目根目录下的环境变量，避免受当前工作目录影响
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(dotenv_path=ENV_PATH, override=True)

# ========================
# 1. 定义我们希望大模型提取的数据结构 (Pydantic)
# 这些字段是目前参考了《知识库_案例》中法医鉴定报告的"基本情况"、"基本案情"、"资料摘要"部分提炼出的
# ========================
class MedicalRecord(BaseModel):
    source_files: List[str] = Field(description="该条记录对应的原始文件名列表（可合并同院同日同类记录）")
    medical_type: str = Field(description="医疗记录类型：急诊/门诊/住院/影像/证明/康复/其他", default="不详")
    hospital_name: str = Field(description="就诊或住院的医院名称", default="不详")
    visit_date: Optional[str] = Field(description="就诊日期（门诊/影像等）", default="不详")
    admission_date: Optional[str] = Field(description="入院或就诊时间", default="不详")
    discharge_date: Optional[str] = Field(description="出院时间，如果是门诊则填无", default="不详")
    complaint_and_history: Optional[str] = Field(description="主诉与现病史摘要", default="不详")
    diagnosis: str = Field(description="门诊诊断、出院诊断或检查结论", default="不详")
    treatment_summary: Optional[str] = Field(description="诊治经过与主要的修复/手术摘要", default="不详")
    key_exam_findings: Optional[str] = Field(description="关键影像学/查体要点", default="不详")


class BasicSituationInfo(BaseModel):
    client_name: str = Field(description="委托人或原告姓名/单位", default="不详")
    entrustment_items: List[str] = Field(description="委托鉴定事项（程序后处理固定填充）", default_factory=list)
    acceptance_date: str = Field(description="受理日期（程序后处理固定填充）", default="不详")
    appraisal_location: str = Field(description="鉴定地点（程序后处理固定填充）", default="不详")
    present_personnel: List[str] = Field(description="在场人员（默认待补充，由审计人工填写）", default_factory=list)

    subject_name: str = Field(description="被鉴定人(受伤害者)姓名", default="不详")
    subject_gender: str = Field(description="被鉴定人性别", default="不详")
    subject_ethnicity: str = Field(description="被鉴定人民族", default="不详")
    subject_birth: str = Field(description="被鉴定人出生年月", default="不详")
    subject_address: str = Field(description="被鉴定人住址", default="不详")
    subject_id: str = Field(description="被鉴定人身份证号", default="不详")

    case_number: str = Field(description="案件编号（如(2025)浙0108民初5105号）", default="不详")
    material_summary: List[str] = Field(description="鉴定材料摘要，要求 3 条：委托书条目、文书病历条目、实物信息条目", default_factory=list)
    evidence_snippets: List[str] = Field(description="支持以上字段的原文证据片段（每条不超过 60 字）", default_factory=list)
    uncertain_points: List[str] = Field(description="仍不确定或需人工核验的信息点", default_factory=list)


class BasicCaseInfo(BaseModel):
    paragraph_1: str = Field(description="第一段：事故经过与责任认定", default="不详")
    paragraph_2: str = Field(description="第二段：治疗后、起诉及委托鉴定经过", default="不详")
    evidence_snippets: List[str] = Field(description="支持基本案情两段的原文证据片段", default_factory=list)
    uncertain_points: List[str] = Field(description="仍不确定或材料缺失的信息点", default_factory=list)


class MaterialSummaryItem(BaseModel):
    subject_name: str = Field(description="被鉴定人姓名", default="不详")
    record_date: str = Field(description="医疗文书日期（可无）", default="不详")
    hospital_name: str = Field(description="就诊医院名称", default="不详")
    material_name: str = Field(description="医疗材料名称（急诊病历/住院病历/医疗证明书等）", default="不详")
    record_number: str = Field(description="病案号/就诊卡号/住院号等可检索编号", default="不详")
    hospitalization_period: str = Field(description="住院时间与天数", default="不详")
    chief_complaint: str = Field(description="主诉", default="不详")
    present_history: str = Field(description="先病史/现病史", default="不详")
    imaging_findings: str = Field(description="CT/影像及其他资料显示", default="不详")
    physical_exam: str = Field(description="体格检查", default="不详")
    specialist_findings: str = Field(description="专科情况", default="不详")
    treatment_course: str = Field(description="诊疗经过", default="不详")
    post_op_review: str = Field(description="术后复查拍片显示", default="不详")
    discharge_diagnosis: str = Field(description="出院诊断", default="不详")
    treatment_advice: str = Field(description="处理意见", default="不详")
    source_file: str = Field(description="来源文件名", default="不详")


class OtherSummaryItem(BaseModel):
    summary_type: str = Field(description="外部信息类型，如既往司法鉴定意见/重新鉴定材料", default="外部鉴定意见")
    institution: str = Field(description="出具外部信息的机构", default="不详")
    document_name: str = Field(description="材料名称", default="司法鉴定意见书")
    document_no: str = Field(description="文号", default="不详")
    document_date: str = Field(description="出具日期或关键日期", default="不详")
    entrusting_party: str = Field(description="委托人或申请人", default="不详")
    opinion_text: str = Field(description="需要在资料摘要中摘录的外部意见内容", default="不详")
    source_files: List[str] = Field(description="来源文件名", default_factory=list)


class BasicSummaryInfo(BaseModel):
    items: List[MaterialSummaryItem] = Field(description="资料摘要条目，按时间和材料拆分", default_factory=list)
    evidence_snippets: List[str] = Field(description="支持资料摘要的原文证据片段", default_factory=list)
    uncertain_points: List[str] = Field(description="仍不确定或材料缺失的信息点", default_factory=list)

class AppraisalInfo(BaseModel):
    client_name: str = Field(description="委托人或原告姓名/单位", default="不详")
    subject_name: str = Field(description="被鉴定人(受伤害者)姓名", default="不详")
    subject_gender: str = Field(description="被鉴定人性别", default="不详")
    subject_ethnicity: str = Field(description="被鉴定人民族", default="不详")
    subject_birth: str = Field(description="被鉴定人出生年月", default="不详")
    subject_address: str = Field(description="被鉴定人住址", default="不详")
    subject_id: str = Field(description="被鉴定人身份证号", default="不详")

    acceptance_date: str = Field(description="受理日期（可含材料补齐日期）", default="不详")
    appraisal_location: str = Field(description="鉴定地点", default="不详")
    present_personnel: List[str] = Field(description="在场人员", default_factory=list)

    case_number: str = Field(description="案件编号（如(2025)浙0108民初5105号）", default="不详")
    incident_location: str = Field(description="案发地点", default="不详")
    responsibility: str = Field(description="事故责任认定", default="不详")
    defendants: List[str] = Field(description="主要被告/相对方列表", default_factory=list)

    incident_date: str = Field(description="案发或受伤发生的时间(例如：2018年7月1日)", default="不详")
    incident_summary: str = Field(description="基本案情摘要：用1-2句话概括发生什么事故导致当事人受伤。例如'XXX发生交通事故受伤被送往医院'", default="不详")
    diagnosis_overview: str = Field(description="诊疗概况（医院、治疗经过、伤情稳定情况）", default="不详")
    appraisal_purpose: str = Field(description="鉴定目的（如案件审理/赔偿处理依据）", default="不详")

    entrustment_items: List[str] = Field(description="委托鉴定事项（如伤残等级、误工期、护理期、营养期）", default_factory=list)
    material_summary: List[str] = Field(description="送检材料要点（按类别归纳）", default_factory=list)

    injury_summary: str = Field(description="已能认定的损伤事实汇总", default="不详")
    surgery_history: List[str] = Field(description="手术/有创治疗时间线", default_factory=list)
    current_status: str = Field(description="目前伤情及功能状态摘要", default="不详")

    medical_records: List[MedicalRecord] = Field(description="受害者的所有就医、住院与诊断记录", default_factory=list)
    medical_summary_items: List[Dict[str, Any]] = Field(description="第三部分资料摘要的专家式关键病历节点", default_factory=list)
    other_summary_items: List[Dict[str, Any]] = Field(description="既往鉴定/重新鉴定等外部信息摘要", default_factory=list)
    summary_selection_audit: List[Dict[str, Any]] = Field(description="资料摘要关键节点筛选审计", default_factory=list)

    drafting_points: List[str] = Field(description="可直接写入鉴定意见书资料摘要的事实要点", default_factory=list)
    conflicting_or_unclear_points: List[str] = Field(description="材料中互相矛盾或OCR不清项", default_factory=list)

# 初始化解析器
parser = PydanticOutputParser(pydantic_object=AppraisalInfo)
basic_situation_parser = PydanticOutputParser(pydantic_object=BasicSituationInfo)
basic_case_parser = PydanticOutputParser(pydantic_object=BasicCaseInfo)
basic_summary_parser = PydanticOutputParser(pydantic_object=BasicSummaryInfo)


FILE_BLOCK_PATTERN = re.compile(
    r"### 文件：(?P<filename>.+?)\n\n(?P<content>.*?)(?=\n---\n|\Z)",
    re.DOTALL,
)

CASE_KEYWORDS = [
    "委托书", "起诉状", "鉴定申请书", "事故认定书", "鉴定笔录", "风险告知书", "身份证", "委鉴字", "民初",
]

MEDICAL_KEYWORDS = [
    "病历", "出院", "入院", "门诊", "诊断", "报告", "报告单", "病假", "证明书", "导诊", "检查", "住院", "康复", "CT", "MR", "MRI", "磁共振", "X线", "DR", "影像",
]

BASIC_SITUATION_FILENAME_KEYWORDS = [
    "委托", "起诉", "申请", "承诺", "身份证", "委鉴字", "民初", "事故认定", "委托代理", "笔录",
]

BASIC_SITUATION_CONTENT_KEYWORDS = [
    "委托人", "被鉴定人", "受理", "鉴定地点", "在场", "身份证号", "案号", "送检材料", "鉴定事项", "委托鉴定",
]

FIXED_APPRAISAL_LOCATION = "浙江大学司法鉴定中心"

HYBRID_PENDING_TEXT = "未查找到"

LOW_CONFIDENCE_MARKERS = [
    "无法判断",
    "无法确定",
    "难以判断",
    "不确定",
    "待核实",
    "仅供参考",
    "可能",
    "推测",
    "估计",
    "疑似",
    "需进一步",
    "证据不足",
]

BASIC_REQUIREMENT_KEYWORDS = {
    "client_name": ["委托人", "司法鉴定委托书", "人民法院", "委鉴字", "法院"],
    "case_number": ["案号", "民初", "委鉴字", "第", "号"],
    "present_personnel": ["在场人员", "代理人", "代表在场", "陪同", "鉴定笔录"],
    "subject_profile": ["被鉴定人", "原告", "姓名", "性别", "民族", "出生", "住址", "身份证", "公民身份号码"],
    "material_1": ["司法鉴定委托书", "委鉴字", "委托书", "法院"],
    "material_2": ["民事起诉状", "鉴定申请书", "鉴定笔录", "事故认定书", "门诊", "病历", "住院病历", "出院记录", "报告单"],
    "material_3": ["光盘", "U盘", "u盘", "USB", "影像", "影像学", "CT", "MR", "MRI", "磁共振", "X线", "DR", "电子影像", "电子图像", "阅片"],
}

CASE_REQUIREMENT_KEYWORDS = {
    "incident": ["事故时间", "事故地点", "车牌号", "碰撞", "交通事故", "交通事故认定书", "交通事故事实及责任", "受伤", "爆炸", "工伤", "案件记录", "分析报告", "答辩"],
    "responsibility": ["事故认定", "交通事故事实及责任", "认定", "责任", "负全部责任", "交警"],
    "litigation": ["民事起诉状", "诉讼请求", "被告", "具状人", "申请鉴定", "诉至"],
    "entrustment": ["司法鉴定委托书", "人民法院", "委托本中心", "委鉴字", "委托鉴定"],
}

SUMMARY_REQUIREMENT_KEYWORDS = {
    "record_basic": ["病历", "医疗证明", "诊断证明", "病情证明", "出院记录", "入院记录", "急诊", "门诊", "住院"],
    "hospitalization": ["住院时间", "入院", "出院", "住院天", "住院号", "病案号", "住院病案首页"],
    "clinical": ["主诉", "现病史", "先病史", "入院情况", "体格检查", "专科", "诊疗经过", "治疗经过", "处理意见"],
    "treatment_course": ["手术记录", "手术名称", "术中见", "内固定术", "诊疗经过", "治疗经过", "出院诊断", "出院医嘱"],
    "imaging": ["CT", "DR", "MRI", "X线", "影像", "复查", "拍片", "磁共振", "检查报告"],
    "diagnosis": ["出院诊断", "诊断", "医嘱", "建议", "治疗", "治疗终结", "伤情稳定"],
}

SUMMARY_NEGATIVE_FILENAME_KEYWORDS = [
    "司法鉴定委托书", "起诉状", "身份证", "事故认定", "鉴定笔录", "委托代理", "承诺书", "证据目录", "委鉴字", "民初",
]

EXTERNAL_APPRAISAL_KEYWORDS = [
    "司法鉴定意见书", "鉴定意见书", "司法鉴定所", "司法鉴定中心", "司鉴", "六、鉴定意见", "鉴定意见：",
]

SUMMARY_LEGAL_NOISE_KEYWORDS = [
    "司法鉴定委托书", "民事起诉状", "道路交通事故认定书", "身份证", "身份证明", "鉴定申请书", "鉴定笔录", "庭审", "笔录", "证据目录", "授权委托书", "执业证",
]

SUMMARY_MEDICAL_NOISE_KEYWORDS = [
    "婚育史", "月经史", "个人史", "家族史", "过敏史", "传染病史", "预防接种史", "生活史", "职业史",
    "医疗付费", "费用", "中医诊断", "主要中医诊断", "中医治疗", "中医外治", "实验室检查", "检验报告",
]

SUMMARY_TRAUMA_RELEVANT_DIAGNOSIS_KEYWORDS = [
    "骨折", "脱位", "挫伤", "擦伤", "裂伤", "牙折", "牙脱位", "损伤", "压缩", "瘢痕", "关节", "韧带", "肌腱", "内固定",
]

SUMMARY_INCIDENTAL_DIAGNOSIS_KEYWORDS = [
    "脂肪肝", "左肾囊肿", "肾囊肿", "高血压", "糖尿病", "胆囊", "返流", "肺结节", "结节灶", "纤维灶", "骨折病", "气滞血瘀", "筋伤筋断证",
]

SUMMARY_FIELDS = [
    "record_date",
    "hospital_name",
    "record_number",
    "hospitalization_period",
    "chief_complaint",
    "present_history",
    "imaging_findings",
    "physical_exam",
    "specialist_findings",
    "treatment_course",
    "post_op_review",
    "discharge_diagnosis",
    "treatment_advice",
]


def _split_file_blocks(markdown_text: str) -> List[dict]:
    blocks = []
    for match in FILE_BLOCK_PATTERN.finditer(markdown_text):
        filename = match.group("filename").strip()
        content = match.group("content").strip()
        blocks.append({"filename": filename, "content": content})
    return blocks


def _is_case_material(filename: str) -> bool:
    return any(keyword in filename for keyword in CASE_KEYWORDS)


def _is_medical_material(filename: str) -> bool:
    return any(keyword in filename for keyword in MEDICAL_KEYWORDS)


def _contains_any_keyword(text: str, keywords: List[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _select_blocks_for_basic_situation(blocks: List[dict]) -> List[dict]:
    selected = []
    for block in blocks:
        filename = block.get("filename", "")
        content = block.get("content", "")
        if _contains_any_keyword(filename, BASIC_SITUATION_FILENAME_KEYWORDS) or _contains_any_keyword(content, BASIC_SITUATION_CONTENT_KEYWORDS):
            selected.append(block)
    return selected


def _build_markdown_chunks_from_blocks(blocks: List[dict], chunk_max_chars: int = 32000) -> List[str]:
    if not blocks:
        return []

    chunks = []
    current_parts = []
    current_len = 0

    for block in blocks:
        section = f"### 文件：{block['filename']}\n\n{block['content']}\n\n---\n\n"
        if current_parts and current_len + len(section) > chunk_max_chars:
            chunks.append("".join(current_parts).strip())
            current_parts = [section]
            current_len = len(section)
        else:
            current_parts.append(section)
            current_len += len(section)

    if current_parts:
        chunks.append("".join(current_parts).strip())

    return chunks


def _render_blocks_with_char_limit(blocks: List[dict], max_chars: int) -> str:
    if not blocks:
        return ""

    parts = []
    current_len = 0
    for block in blocks:
        section = f"### 文件：{block['filename']}\n\n{block['content']}\n\n---\n\n"
        if current_len + len(section) <= max_chars:
            parts.append(section)
            current_len += len(section)
            continue

        remain = max_chars - current_len
        if remain > 400:
            clipped = section[:remain] + "\n[本字段上下文已截断]"
            parts.append(clipped)
        break

    return "".join(parts).strip()


def _select_blocks_by_field_requirement(blocks: List[dict], keywords: List[str], max_blocks: int = 12) -> List[dict]:
    selected = []
    for block in blocks:
        filename = block.get("filename", "")
        content = block.get("content", "")
        haystack = f"{filename}\n{content}"
        if _contains_any_keyword(haystack, keywords):
            selected.append(block)
    if not selected:
        return blocks[:max_blocks]
    return selected[:max_blocks]


def _build_basic_requirement_contexts(blocks: List[dict], field_max_chars: int) -> tuple[dict, dict]:
    contexts = {}
    selected_files = set()
    requirement_hit_count = {}

    for requirement_name, keywords in BASIC_REQUIREMENT_KEYWORDS.items():
        req_blocks = _select_blocks_by_field_requirement(blocks, keywords, max_blocks=12)
        requirement_hit_count[requirement_name] = len(req_blocks)
        for block in req_blocks:
            selected_files.add(block.get("filename", ""))
        contexts[requirement_name] = _render_blocks_with_char_limit(req_blocks, max_chars=field_max_chars)

    stats = {
        "total_files": len(blocks),
        "selected_files": len(selected_files),
        "selected_file_names": sorted([name for name in selected_files if name]),
        "requirement_count": len(BASIC_REQUIREMENT_KEYWORDS),
        "requirement_hit_count": requirement_hit_count,
    }
    return contexts, stats


def _merge_list_unique(base: List[str], incoming: List[str], max_items: Optional[int] = None) -> List[str]:
    merged = []
    for item in (base or []) + (incoming or []):
        text = _clean_text(item)
        if text == "不详":
            continue
        if text not in merged:
            merged.append(text)
        if max_items is not None and len(merged) >= max_items:
            break
    return merged


def _pick_better_value(old_value: str, new_value: str) -> str:
    old_text = _clean_text(old_value)
    new_text = _clean_text(new_value)
    if old_text == "不详" and new_text != "不详":
        return new_text
    if old_text != "不详" and new_text != "不详" and len(new_text) > len(old_text):
        return new_text
    return old_text


def _merge_basic_situation(base: dict, incoming: dict) -> dict:
    scalar_fields = [
        "client_name",
        "subject_name",
        "subject_gender",
        "subject_ethnicity",
        "subject_birth",
        "subject_address",
        "subject_id",
        "case_number",
    ]
    for field in scalar_fields:
        if field == "subject_name":
            old_name = _clean_subject_name(base.get(field, "不详"))
            new_name = _clean_subject_name(incoming.get(field, "不详"))
            base[field] = new_name if _subject_name_score(new_name) > _subject_name_score(old_name) else old_name
        else:
            base[field] = _pick_better_value(base.get(field, "不详"), incoming.get(field, "不详"))

    base["entrustment_items"] = _merge_list_unique(base.get("entrustment_items", []), incoming.get("entrustment_items", []), max_items=12)
    base["present_personnel"] = _merge_list_unique(base.get("present_personnel", []), incoming.get("present_personnel", []), max_items=12)
    base["material_summary"] = _merge_list_unique(base.get("material_summary", []), incoming.get("material_summary", []), max_items=12)
    base["evidence_snippets"] = _merge_list_unique(base.get("evidence_snippets", []), incoming.get("evidence_snippets", []), max_items=20)
    base["uncertain_points"] = _merge_list_unique(base.get("uncertain_points", []), incoming.get("uncertain_points", []), max_items=8)
    return base


def build_focus_markdown(markdown_text: str, max_chars: int = 180000) -> tuple[str, dict]:
    """优先保留案情与医学相关文件，减少噪声并尽量避免粗暴截断。"""

    blocks = _split_file_blocks(markdown_text)
    if not blocks:
        shortened = markdown_text[:max_chars]
        return shortened, {
            "total_files": 0,
            "selected_files": 0,
            "selected_case_files": 0,
            "selected_medical_files": 0,
            "truncated": len(markdown_text) > max_chars,
        }

    selected_blocks = []
    case_count = 0
    medical_count = 0

    for block in blocks:
        filename = block["filename"]
        if _is_case_material(filename):
            selected_blocks.append(block)
            case_count += 1
        elif _is_medical_material(filename):
            selected_blocks.append(block)
            medical_count += 1

    if not selected_blocks:
        selected_blocks = blocks

    rendered_sections = []
    current_chars = 0
    truncated = False

    for block in selected_blocks:
        content = block["content"]
        section = f"### 文件：{block['filename']}\n\n{content}\n\n---\n\n"

        if current_chars + len(section) <= max_chars:
            rendered_sections.append(section)
            current_chars += len(section)
            continue

        # 到达上限后，尽量保留文件头和前部关键内容，避免直接丢弃该条。
        remain = max_chars - current_chars
        if remain > 600:
            partial_len = max(200, remain - 250)
            clipped_content = content[:partial_len] + "\n[该文件内容过长，后续已截断]"
            clipped_section = f"### 文件：{block['filename']}\n\n{clipped_content}\n\n---\n\n"
            rendered_sections.append(clipped_section)
            current_chars += len(clipped_section)
        truncated = True
        break

    focus_text = "".join(rendered_sections).strip()
    return focus_text, {
        "total_files": len(blocks),
        "selected_files": len(selected_blocks),
        "selected_case_files": case_count,
        "selected_medical_files": medical_count,
        "truncated": truncated,
    }


def _date_sort_key(record: dict) -> str:
    candidate = (
        record.get("visit_date")
        or record.get("admission_date")
        or record.get("discharge_date")
        or "9999-99-99"
    )
    # 仅提取数字方便排序
    digits = "".join(ch for ch in candidate if ch.isdigit())
    return digits if digits else "99999999"


def _normalize_value(value):
    if value is None:
        return "不详"
    if isinstance(value, str):
        text = value.strip()
        return text if text else "不详"
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in value.items()}
    return value


def _clean_text(value: str) -> str:
    if value is None:
        return "不详"
    text = str(value).strip()
    return text if text else "不详"


def _is_unresolved_value(value) -> bool:
    normalized = _clean_text(value).strip("。；;，,！!？?")
    return normalized in {"不详", "待补充", "未查找到", HYBRID_PENDING_TEXT}


def _is_high_confidence_text(value) -> bool:
    text = _clean_text(value)
    if _is_unresolved_value(text):
        return False

    compact = re.sub(r"\s+", "", text)
    if len(compact) < 2:
        return False

    for marker in LOW_CONFIDENCE_MARKERS:
        if marker in text:
            return False

    return True


def _first_usable_text(rule_value, llm_value, llm_validator=None) -> tuple[str, bool]:
    rule_text = _clean_text(rule_value)
    if not _is_unresolved_value(rule_text) and not (llm_validator is not None and not llm_validator(rule_text)):
        return rule_text, False

    llm_text = _clean_text(llm_value)
    if _is_unresolved_value(llm_text):
        return HYBRID_PENDING_TEXT, False

    if not _is_high_confidence_text(llm_text):
        return HYBRID_PENDING_TEXT, False

    if llm_validator is not None and (not llm_validator(llm_text)):
        return HYBRID_PENDING_TEXT, False

    return llm_text, True


def _filter_usable_str_list(values: Optional[List[str]], max_items: Optional[int] = None) -> List[str]:
    result = []
    for item in values or []:
        text = _clean_text(item)
        if _is_unresolved_value(text):
            continue
        if text not in result:
            result.append(text)
        if max_items is not None and len(result) >= max_items:
            break
    return result


def _first_usable_list(
    rule_values: Optional[List[str]],
    llm_values: Optional[List[str]],
    max_items: Optional[int] = None,
    fallback_pending: bool = False,
) -> tuple[List[str], bool]:
    rule_clean = _filter_usable_str_list(rule_values, max_items=max_items)
    if rule_clean:
        return rule_clean, False

    llm_clean = _filter_usable_str_list(llm_values, max_items=max_items)
    if llm_clean:
        return llm_clean, True

    if fallback_pending:
        return [HYBRID_PENDING_TEXT], False
    return [], False


def _summary_item_has_usable_content(item: dict) -> bool:
    if not isinstance(item, dict):
        return False

    key_fields = [
        "record_date",
        "hospital_name",
        "material_name",
        "record_number",
        "hospitalization_period",
        "chief_complaint",
        "present_history",
        "imaging_findings",
        "physical_exam",
        "specialist_findings",
        "treatment_course",
        "post_op_review",
        "discharge_diagnosis",
        "treatment_advice",
    ]
    return any((not _is_unresolved_value(item.get(field))) for field in key_fields)


def _summary_items_have_usable_content(items: Optional[List[dict]]) -> bool:
    return any(_summary_item_has_usable_content(item) for item in (items or []))


def _basic_rule_needs_llm(rule_payload: dict) -> bool:
    scalar_fields = [
        "client_name",
        "case_number",
        "subject_name",
        "subject_gender",
        "subject_ethnicity",
        "subject_birth",
        "subject_address",
        "subject_id",
    ]
    if any(_is_unresolved_value(rule_payload.get(field)) for field in scalar_fields):
        return True

    personnel = _filter_usable_str_list(rule_payload.get("present_personnel", []), max_items=12)
    return len(personnel) == 0


def _case_paragraph_quality_score(text: str) -> int:
    value = _clean_text(text)
    if _is_unresolved_value(value):
        return 0
    compact = re.sub(r"\s+", "", value)
    score = 0
    if len(compact) >= 45:
        score += 1
    if re.search(r"(?:19|20)\d{2}年\d{1,2}月\d{1,2}日|\d{4}[-./]\d{1,2}[-./]\d{1,2}", compact):
        score += 1
    if re.search(r"(?:路|路口|街|道|村|镇|小区|门岗|道闸|停车场|公司|工地)", compact):
        score += 1
    if re.search(r"(?:驾驶|骑|乘坐|行走|电动自行车|小型轿车|车牌|车辆|行人)", compact):
        score += 1
    if re.search(r"(?:碰撞|相撞|撞伤|摔倒|跌倒|压伤|受伤|事故|道闸|电动门)", compact):
        score += 1
    if re.search(r"(?:造成|致|导致)[^。；]{0,20}(?:受伤|损伤|骨折|疼痛)", compact):
        score += 1
    if re.search(r"(?:责任|认定|负|承担)", compact):
        score += 1
    if re.search(r"(?:医院|诊断|治疗|伤情稳定)", compact):
        score += 1
    return score


def _case_paragraph_is_thin(text: str) -> bool:
    value = _clean_text(text)
    if _is_unresolved_value(value):
        return True
    compact = re.sub(r"\s+", "", value)
    if len(compact) < 35:
        return True
    if any(k in compact for k in ["具体经过材料记载不充分", "事故经过不详", "未查找到稳定"]):
        return True
    return _case_paragraph_quality_score(compact) < 4


def _case_rule_needs_llm(rule_payload: dict) -> bool:
    if any(_is_unresolved_value(rule_payload.get(field)) for field in ["paragraph_1", "paragraph_2"]):
        return True
    return _case_paragraph_is_thin(rule_payload.get("paragraph_1"))


def _summary_coverage_profile(items: List[dict], markdown_text: str = "") -> Dict[str, bool]:
    rows = [item for item in items or [] if isinstance(item, dict)]
    item_text = "\n".join(_summary_item_all_text(item) for item in rows)
    source_text = markdown_text or item_text

    def has(pattern: str, text: str = item_text) -> bool:
        return re.search(pattern, text or "") is not None

    source_has = {
        "initial": has(r"急诊|门诊|首诊|初诊", source_text),
        "inpatient": has(r"入院记录|出院记录|住院|住院病案|病程记录", source_text),
        "surgery": has(r"手术|内固定|清创|缝合|治疗经过|诊疗经过", source_text),
        "discharge": has(r"出院诊断|出院医嘱|出院情况|处理意见", source_text),
        "followup": has(r"复查|CT|MRI|MR|DR|X线|影像|报告单", source_text),
        "certificate": has(r"诊断证明|病情证明|治疗终结|伤情稳定", source_text),
    }
    covered = {
        "initial": has(r"急诊|门诊|首诊|初诊"),
        "inpatient": any(_summary_item_kind(item) == "inpatient" for item in rows),
        "surgery": has(r"手术|内固定|清创|缝合|治疗经过|诊疗经过"),
        "discharge": has(r"出院诊断|出院医嘱|出院情况|处理意见"),
        "followup": has(r"复查|CT|MRI|MR|DR|X线|影像|报告单"),
        "certificate": has(r"诊断证明|病情证明|治疗终结|伤情稳定"),
    }
    return {key: (not source_has[key]) or covered[key] for key in source_has}


def _summary_rule_needs_llm(rule_payload: dict, markdown_text: str = "") -> bool:
    items = [item for item in rule_payload.get("items", []) if isinstance(item, dict)]
    if not _summary_items_have_usable_content(items):
        return True
    key_fields = ["chief_complaint", "present_history", "physical_exam", "specialist_findings", "treatment_course", "discharge_diagnosis"]
    rich_items = 0
    for item in items:
        hits = sum(1 for field in key_fields if not _is_unresolved_value(item.get(field)))
        if hits >= 3:
            rich_items += 1
    if rich_items == 0 or len(items) < 2:
        return True
    profile = _summary_coverage_profile(items, markdown_text)
    return any(not ok for ok in profile.values())


def _has_usable_data(value) -> bool:
    if isinstance(value, str):
        return not _is_unresolved_value(value)
    if isinstance(value, list):
        return any(_has_usable_data(item) for item in value)
    if isinstance(value, dict):
        return any(_has_usable_data(v) for v in value.values())
    return value is not None


def _payload_needs_llm(value) -> bool:
    if isinstance(value, str):
        return _is_unresolved_value(value)
    if isinstance(value, list):
        if len(value) == 0:
            return True
        return any(_payload_needs_llm(item) for item in value)
    if isinstance(value, dict):
        return any(_payload_needs_llm(v) for v in value.values())
    return False


def _merge_rule_first_struct(rule_value, llm_value):
    if isinstance(rule_value, dict):
        result = {}
        llm_dict = llm_value if isinstance(llm_value, dict) else {}
        for key, val in rule_value.items():
            result[key] = _merge_rule_first_struct(val, llm_dict.get(key))
        return result

    if isinstance(rule_value, list):
        llm_list = llm_value if isinstance(llm_value, list) else []

        # 列表内做逐项合并，确保 rules 中的"不详"能被 LLM 的高置信字段补上。
        if (rule_value and all(isinstance(item, dict) for item in rule_value)) or (
            (not rule_value) and llm_list and all(isinstance(item, dict) for item in llm_list)
        ):
            if not rule_value and llm_list:
                return llm_list

            merged_list = []
            for idx in range(len(rule_value)):
                r_item = rule_value[idx] if idx < len(rule_value) else {}
                l_item = llm_list[idx] if idx < len(llm_list) and isinstance(llm_list[idx], dict) else {}
                if not isinstance(r_item, dict):
                    r_item = {}
                merged_list.append(_merge_rule_first_struct(r_item, l_item))
            return merged_list

        if _has_usable_data(rule_value):
            return rule_value

        if llm_list and all(isinstance(item, str) for item in llm_list):
            confident_items = [text for text in llm_list if _is_high_confidence_text(text)]
            if confident_items:
                return confident_items

        if _has_usable_data(llm_list):
            return llm_list

        return []

    if isinstance(rule_value, str):
        rule_text = _clean_text(rule_value)
        if not _is_unresolved_value(rule_text):
            return rule_text

        llm_text = _clean_text(llm_value)
        if _is_high_confidence_text(llm_text):
            return llm_text
        return HYBRID_PENDING_TEXT

    if _has_usable_data(rule_value):
        return rule_value
    if isinstance(llm_value, str):
        llm_text = _clean_text(llm_value)
        if _is_high_confidence_text(llm_text):
            return llm_text

    if _has_usable_data(llm_value):
        return llm_value
    if isinstance(rule_value, str) or isinstance(llm_value, str):
        return HYBRID_PENDING_TEXT
    return rule_value if rule_value is not None else llm_value


def _normalize_space_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _safe_group(match, idx: int, default: str = "不详") -> str:
    if not match:
        return default
    try:
        value = match.group(idx)
    except IndexError:
        return default
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _today_cn_date() -> str:
    today = date.today()
    return f"{today.year}年{today.month}月{today.day}日"


def _subject_for_template(subject_name: str) -> str:
    name = _clean_text(subject_name)
    return name if name != "不详" else "XXX"


def _build_fixed_entrustment_items(subject_name: str) -> List[str]:
    target = _subject_for_template(subject_name)
    return [
        f"依照《人体损伤致残程度分级》对{target}的伤残等级进行鉴定",
        f"依照《人身损害误工期、护理期、营养期评定规范》（GA/T 1193-2014）对{target}的误工期、护理期、营养期进行鉴定",
    ]


def _is_imaging_material_text(text: str) -> bool:
    return any(k in text for k in ["光盘", "U盘", "u盘", "USB", "影像", "影像学", "CT", "MR", "MRI", "磁共振", "X线", "DR", "电子图像", "电子影像"])


def _normalize_basic_material_summary(material_summary) -> List[str]:
    if isinstance(material_summary, list):
        candidates = [str(item).strip("；。 ") for item in material_summary if str(item).strip() and str(item).strip() != "不详"]
    elif isinstance(material_summary, str):
        text = material_summary.strip("；。 ")
        candidates = [text] if text and text != "不详" else []
    else:
        candidates = []

    item_1 = ""
    normal_items: List[str] = []
    image_items: List[str] = []

    for text in candidates:
        if (not item_1) and ("委托书" in text or "委鉴字" in text):
            item_1 = text
            continue
        if _is_imaging_material_text(text):
            image_items.append(text)
            continue
        normal_items.append(text)

    if not item_1:
        item_1 = "xxx法院（xxxx年）浙xxxx委鉴字第xxx号《司法鉴定委托书》1份"

    result = [item_1]
    result.extend(_merge_list_unique([], normal_items, max_items=6))
    result.extend(_merge_list_unique([], image_items, max_items=2))
    return result


MATERIAL_ID_PATTERN = re.compile(
    r"(?:病案号|病历号|住院号|门诊号|就诊卡号|就诊号|住院流水号|检查号|报告号|影像号|电子图像号|CT号|CT检查号|MR号|MRI号|磁共振号)[:：]?\s*([A-Za-z0-9\-]{3,40})",
    re.I,
)

# 身份证号标签：OCR 常吞字（“公民身份号码”→“公民份号码”），这里保留常见残缺写法。
# 该模式同时用于取号和切分住址，因此必须在两处保持一致。
SUBJECT_ID_LABEL_PATTERN = (
    r"(?:公民身份号码|公民身份证号码|公民身份证号|公民身份号|公民份号码|"
    r"身份证件号码|身份证号码|身份证号|身份号码)"
)
SUBJECT_ID_LABEL_RE = re.compile(SUBJECT_ID_LABEL_PATTERN + r"[:：]?\s*([0-9Xx]{15,18})")
# 结构合法的 18 位身份证号（第 7-14 位为合法日期），用于在正文里发现候选号。
SUBJECT_ID_STRICT_RE = re.compile(
    r"(?<![0-9Xx])([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9Xx])"
)
SUBJECT_ID_AGENT_CONTEXT_RE = re.compile(r"代理人|律师|被告|法定代表人|负责人|委托人|员工|驾驶员|持证人")
IDENTITY_POSITIVE_FILENAME_KEYWORDS = ["原告", "当事人", "被鉴定人", "身份证", "身份证明", "身份信息"]
IDENTITY_NEGATIVE_FILENAME_KEYWORDS = ["被告", "公司", "法定代表人", "负责人", "员工", "代理人", "驾驶员", "法人"]
IDENTITY_POSITIVE_CONTENT_KEYWORDS = ["姓名", "性别", "民族", "出生", "住址", "公民身份号码", "身份证号码", "身份证号"]
SUBJECT_NAME_BAD_KEYWORDS = [
    "性别", "民族", "出生", "住址", "地址", "号码", "身份", "法院", "医院", "公司", "中心", "委托", "鉴定", "材料", "病历",
    # 案卷里高频出现、且会被“原告/申请人”类标签连带捕获的非姓名词。
    # 典型误抽：文件名「原告证据-门诊病历1.jpg」→ 正则从「原告证据」里截出「证据」，
    # 而它与真实姓名同分，谁先出现谁赢，OCR 一抖动就会顶掉真名。
    "证据", "原告", "被告", "申请人", "被申请人", "第三人", "当事人", "代理人", "诉讼",
    "照片", "报告", "证明", "诊断", "身份证", "户口", "保险", "公安", "交警", "事故",
    "起诉状", "委托书", "笔录", "责任", "认为", "权益", "损伤", "受伤", "碰撞", "受伤",
]
SUBJECT_ADDRESS_POLLUTION_RE = re.compile(
    r"预检|分诊|就诊|门诊号|住院号|病案号|病历号|科室|检查号|影像号|CT|MRI|MR|DR|X线|报告单|打印时间|扫描时间|"
    r"中华人民共和国|居民身份证|签发机关|有效期限|\d{6,}|\d{4}\s*年\s*\d{1,2}\s*月"
)


def _subject_id_birth_date(value: str) -> str:
    """取身份证号第 7-14 位编码的出生日期，返回 'YYYYMMDD'；结构不合法返回空串。"""
    text = re.sub(r"\s+", "", str(value or "")).upper()
    if len(text) != 18 or not re.fullmatch(r"[0-9]{17}[0-9X]", text):
        return ""
    birth = text[6:14]
    if not birth.isdigit():
        return ""
    year, month, day = int(birth[:4]), int(birth[4:6]), int(birth[6:8])
    if not (1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return birth


def _subject_id_gender(value: str) -> str:
    """取身份证号第 17 位奇偶编码的性别。"""
    text = re.sub(r"\s+", "", str(value or "")).upper()
    if len(text) != 18 or not text[16].isdigit():
        return ""
    return "男" if int(text[16]) % 2 == 1 else "女"


def _birth_text_to_compact(value: str) -> str:
    """把“1996年5月2日”归一成 '19960502'。"""
    match = re.search(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", str(value or ""))
    if not match:
        return ""
    return f"{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d}"


def _subject_id_conflicts_with_profile(subject_id: str, subject_birth: str, subject_gender: str) -> bool:
    """身份证号内编码的出生日期/性别是否与被鉴定人已知资料矛盾。

    案卷里常同时出现对方当事人、代理律师的身份证号；号码自带的出生日期
    与已知出生日期不符，就说明它多半不属于被鉴定人。
    """
    encoded_birth = _subject_id_birth_date(subject_id)
    if not encoded_birth:
        return False
    known_birth = _birth_text_to_compact(subject_birth)
    if known_birth and encoded_birth != known_birth:
        return True
    known_gender = _clean_text(subject_gender)
    encoded_gender = _subject_id_gender(subject_id)
    if known_gender in {"男", "女"} and encoded_gender and encoded_gender != known_gender:
        return True
    return False


def _collect_subject_id_candidates(text: str, limit: int = 8) -> List[str]:
    """收集文本中的身份证号候选，标签命中优先，其次为结构合法的号码。"""
    ordered: List[str] = []
    seen = set()

    def add(value: str) -> None:
        candidate = re.sub(r"\s+", "", str(value or "")).upper()
        if len(candidate) == 18 and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)

    for match in SUBJECT_ID_LABEL_RE.finditer(text or ""):
        add(match.group(1))
    for match in SUBJECT_ID_STRICT_RE.finditer(text or ""):
        add(match.group(1))
    return ordered[:limit]


def _reconcile_subject_id(merged: dict, scored: List[tuple]) -> tuple[dict, List[str]]:
    """用身份证号自带的出生日期/性别校验已选号码，宁可置不详也不要写错号。"""
    warnings: List[str] = []
    current = _clean_text(merged.get("subject_id"))
    if _is_unresolved_value(current):
        return merged, warnings
    subject_birth = merged.get("subject_birth")
    subject_gender = merged.get("subject_gender")
    if not _subject_id_conflicts_with_profile(current, subject_birth, subject_gender):
        return merged, warnings

    candidates: List[str] = []
    for score, block in scored[:6]:
        if score <= 0:
            continue
        candidates.extend(_collect_subject_id_candidates(block.get("content", "")))
    for candidate in candidates:
        if candidate != current and not _subject_id_conflicts_with_profile(candidate, subject_birth, subject_gender):
            merged["subject_id"] = candidate
            warnings.append(
                f"已抽取的身份证号 {current} 与被鉴定人出生日期/性别不符，"
                f"已改用材料中与之自洽的 {candidate}；建议人工核对身份证原件。"
            )
            return merged, warnings

    merged["subject_id"] = "不详"
    warnings.append(
        f"已抽取的身份证号 {current} 与被鉴定人出生日期/性别矛盾，且材料中未找到自洽号码；"
        "已置为不详，需人工补录，避免文书写错被鉴定人身份。"
    )
    return merged, warnings


def _is_valid_subject_name(value: str) -> bool:
    text = _clean_text(value)
    if _is_unresolved_value(text):
        return False
    text = re.sub(r"\s+", "", text).strip(" ，,。；;：:")
    if not re.fullmatch(r"[一-龥]{2,5}", text):
        return False
    if any(k in text for k in SUBJECT_NAME_BAD_KEYWORDS):
        return False
    if re.search(r"[男女]民$|^姓名$|^被鉴定人$|^原告$|^患者$", text):
        return False
    # 从句子里截出来的片段通常带这些起首字，不是姓名。
    if text[0] in "的该其本此这为与和及对将把被":
        return False
    return True


def _clean_subject_name(value: str) -> str:
    text = re.sub(r"\s+", "", _clean_text(value)).strip(" ，,。；;：:")
    return text if _is_valid_subject_name(text) else "不详"


def _subject_name_score(value: str) -> int:
    text = _clean_subject_name(value)
    if text == "不详":
        return -100
    score = 20
    if 2 <= len(text) <= 4:
        score += 6
    if len(text) == 5:
        score -= 2
    return score


def _is_polluted_subject_address(value: str) -> bool:
    text = _clean_text(value)
    if _is_unresolved_value(text):
        return False
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 4:
        return True
    return SUBJECT_ADDRESS_POLLUTION_RE.search(compact) is not None or SUBJECT_ID_LABEL_RE.search(compact) is not None


def _clean_subject_address(value: str) -> str:
    text = _clean_text(value)
    if _is_unresolved_value(text):
        return "不详"
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"^(?:住址|地址|户籍地址|住所地)[:：]?", "", text)
    text = re.split(SUBJECT_ID_LABEL_PATTERN + r"|签发机关|有效期限", text)[0]
    text = text.strip(" ，,。；;：:")
    if not text or _is_polluted_subject_address(text):
        return "不详"
    return text[:80]


def _identity_block_score(block: dict) -> int:
    filename = str(block.get("filename", ""))
    content = str(block.get("content", ""))
    score = 0
    subject_owner = any(k in filename for k in ["原告", "当事人", "被鉴定人"])
    negative_owner = any(k in filename for k in IDENTITY_NEGATIVE_FILENAME_KEYWORDS)
    if negative_owner and not subject_owner:
        return -50
    if any(k in filename for k in ["身份证", "身份证明", "身份信息"]):
        score += 18
    if subject_owner:
        score += 10
    if SUBJECT_ID_LABEL_RE.search(content):
        score += 12
    if re.search(r"(?:住址|地址)[:：]", content):
        score += 8
    score += sum(2 for k in IDENTITY_POSITIVE_CONTENT_KEYWORDS if k in content)
    if any(k in content[:300] for k in ["被告", "法定代表人", "代理人", "员工"]):
        score -= 10
    return score


def _extract_identity_fields_from_text(text: str) -> dict:
    content = text or ""
    compact = re.sub(r"[ \t]+", "", content)
    payload = {}
    # 冒号若可选，「原告证据-门诊病历1.jpg」这类文本会被截出「证据」当姓名。
    # 这里要求：要么有冒号，要么后面紧跟姓名语境词（性别/男/女/民族…）。
    name_match = re.search(
        r"(?:姓名|患者姓名|病人姓名|委托人的姓名|被鉴定人|原告|申请人|受检者)\s*[:：]\s*([一-龥]{2,5})",
        compact,
    ) or re.search(
        r"(?:姓名|被鉴定人|原告|申请人|受检者)\s*([一-龥]{2,5})"
        r"(?=\s*(?:性别|男|女|民族|出生|住址|公民身份|身份证|，|,|。|；|;|$))",
        compact,
    )
    gender_match = re.search(r"性别[:：]?\s*(男|女|男性|女性)", compact)
    ethnicity_match = re.search(r"民族[:：]?\s*([一-龥]{1,3})", compact)
    birth_match = re.search(r"出生[:：]?\s*((?:19|20)\d{2}年\d{1,2}月\d{1,2}日)", compact)
    id_match = SUBJECT_ID_LABEL_RE.search(compact)
    address_match = re.search(
        r"(?:住址|地址)[:：]?\s*([\s\S]{2,140}?)(?=(?:" + SUBJECT_ID_LABEL_PATTERN + r"|签发机关|有效期限|\n\s*\n|$))",
        content,
    )
    if name_match:
        name = _clean_subject_name(_safe_group(name_match, 1))
        if name != "不详":
            payload["subject_name"] = name
    if gender_match:
        payload["subject_gender"] = _safe_group(gender_match, 1).replace("女性", "女").replace("男性", "男")
    if ethnicity_match:
        payload["subject_ethnicity"] = _safe_group(ethnicity_match, 1)
    if birth_match:
        payload["subject_birth"] = _safe_group(birth_match, 1)
    if id_match:
        payload["subject_id"] = _safe_group(id_match, 1)
        if "subject_birth" not in payload and len(payload["subject_id"]) >= 14:
            birth = payload["subject_id"][6:14]
            if birth.isdigit():
                payload["subject_birth"] = f"{birth[:4]}年{int(birth[4:6])}月{int(birth[6:8])}日"
    if address_match:
        address = _clean_subject_address(_safe_group(address_match, 1))
        if not _is_unresolved_value(address):
            payload["subject_address"] = address
    return payload


def _extract_subject_name_candidates(blocks: List[dict], joined: str = "") -> List[dict]:
    candidates: List[dict] = []

    def add(value: str, score: int, source: str, reason: str) -> None:
        name = _clean_subject_name(value)
        if name == "不详":
            return
        candidates.append({"name": name, "score": score + _subject_name_score(name), "source": source, "reason": reason})

    for block in blocks or []:
        filename = str(block.get("filename", ""))
        content = str(block.get("content", ""))
        compact = re.sub(r"\s+", "", content)
        block_score = _identity_block_score(block)
        for match in re.finditer(r"(?:姓名|患者姓名|病人姓名)[:：]?([一-龥]{2,5})(?=(?:性别|男|女|民族|出生|住址|公民身份|身份证|$))", compact):
            add(match.group(1), 35 + max(0, block_score), filename, "身份证明/病历姓名标签")
        for match in re.finditer(r"(?:被鉴定人|委托人的姓名|受检者)[:：]?([一-龥]{2,5})(?=(?:，|,|。|；|;|的|男|女|$))", compact):
            add(match.group(1), 28 + max(0, block_score // 2), filename, "委托/鉴定对象标签")
        for match in re.finditer(r"对([一-龥]{2,5})的(?:伤残|残疾|误工|护理期|营养期|三期|医疗费)", compact):
            add(match.group(1), 24, filename, "鉴定要求对象")
        for match in re.finditer(r"(?:原告|申请人)[:：]?([一-龥]{2,5})(?=(?:，|,|。|；|;|男|女|$))", compact):
            add(match.group(1), 18, filename, "诉讼当事人标签")

    counts = Counter()
    for match in re.finditer(r"(?:病人姓名|患者姓名|姓名)[:：]?\s*([一-龥]{2,5})", joined or ""):
        name = _clean_subject_name(match.group(1))
        if name != "不详":
            counts[name] += 1
    for name, count in counts.items():
        if count >= 2:
            add(name, 12 + min(count, 8), "全文医疗姓名频次", "多次出现的患者姓名")

    best_by_name: Dict[str, dict] = {}
    for item in candidates:
        old = best_by_name.get(item["name"])
        if old is None or item["score"] > old["score"]:
            best_by_name[item["name"]] = item
    return sorted(best_by_name.values(), key=lambda x: x["score"], reverse=True)


def _best_subject_name_from_blocks(blocks: List[dict], joined: str = "") -> tuple[str, List[str]]:
    candidates = _extract_subject_name_candidates(blocks, joined)
    if not candidates:
        return "不详", []
    warnings: List[str] = []
    if len(candidates) >= 2 and candidates[0]["name"] != candidates[1]["name"] and candidates[1]["score"] >= candidates[0]["score"] - 8:
        warnings.append(f"被鉴定人姓名存在多个候选：{candidates[0]['name']}、{candidates[1]['name']}，建议核对身份证及委托书。")
    # 身份证/病历标签命中时分数在 70 以上；只有 45 上下说明姓名是从叙述性文字里
    # 猜出来的（多为 OCR 没读到身份证/病历时）。这种情况必须让人来确认，
    # 不能默不作声地把一个猜出来的名字写进正式文书。
    if candidates[0]["score"] < 50:
        warnings.append(
            f"被鉴定人姓名「{candidates[0]['name']}」来自低置信来源（{candidates[0]['source']}，"
            f"依据：{candidates[0]['reason']}），未在身份证/病历上找到姓名标签，请人工核对。"
        )
    return candidates[0]["name"], warnings


def _extract_subject_identity_from_blocks(blocks: List[dict]) -> tuple[dict, List[str]]:
    scored = sorted((( _identity_block_score(block), block) for block in blocks), key=lambda x: x[0], reverse=True)
    merged: dict = {}
    warnings: List[str] = []
    best_name, name_warnings = _best_subject_name_from_blocks(blocks, "\n".join(str(b.get("content", "")) for b in blocks or []))
    warnings.extend(name_warnings)
    if best_name != "不详":
        merged["subject_name"] = best_name
    for score, block in scored[:6]:
        if score <= 0:
            continue
        fields = _extract_identity_fields_from_text(block.get("content", ""))
        if not fields:
            continue
        for key in ["subject_name", "subject_gender", "subject_ethnicity", "subject_birth", "subject_id"]:
            if key not in merged and not _is_unresolved_value(fields.get(key)):
                merged[key] = fields[key]
        if not _is_unresolved_value(fields.get("subject_address")):
            merged["subject_address"] = fields["subject_address"]
            merged["_identity_address_source"] = block.get("filename", "")
            break
    merged, id_warnings = _reconcile_subject_id(merged, scored)
    warnings.extend(id_warnings)
    if not merged.get("subject_address"):
        for score, block in scored[:4]:
            if score > 0 and re.search(r"(?:住址|地址)", block.get("content", "")):
                warnings.append(f"身份证明材料 {block.get('filename', '')} 中住址未能稳定提取，建议人工核对身份证反面。")
                break
    return merged, warnings


def _extract_labeled_identifiers(text: str, max_items: int = 8) -> List[str]:
    """抽取材料和影像摘要中常见编号，保留编号类型。"""
    results: List[str] = []
    seen = set()
    for match in re.finditer(
        r"(病案号|病历号|住院号|门诊号|就诊卡号|就诊号|住院流水号|检查号|报告号|影像号|电子图像号|CT号|CT检查号|MR号|MRI号|磁共振号)[:：]?\s*([A-Za-z0-9\-]{3,40})",
        text or "",
        re.I,
    ):
        label = match.group(1).upper().replace("MRI号", "MR号").replace("磁共振号", "MR号").replace("CT检查号", "CT号")
        raw_label = match.group(1)
        if label in {"CT号", "MR号"}:
            display_label = label
        else:
            display_label = raw_label
        value = match.group(2).strip().strip("。；;，,")
        key = (display_label, value)
        if value and key not in seen:
            seen.add(key)
            results.append(f"{display_label}：{value}")
        if len(results) >= max_items:
            break
    return results


def _extract_material_carrier(blocks: List[dict], joined: str) -> str:
    haystack = "\n".join([b.get("filename", "") + "\n" + b.get("content", "") for b in blocks]) or joined
    if re.search(r"U\s*盘|USB|u\s*盘", haystack, re.I):
        return "U盘1个"
    if "光盘" in haystack:
        return "光盘1张"
    if any(k in haystack for k in ["电子影像", "电子图像", "影像学资料", "影像资料"]):
        return "资料载体待补充"
    return "实物信息待补充"


def _extract_exam_body_text(text: str) -> str:
    haystack = text or ""
    for pattern in [
        r"(胸腰椎|胸腰段脊柱|腰椎|胸椎|颈椎|膝关节|踝关节|肩关节|髋关节|腕关节|肘关节|头颅|胸部|腹部)[^，。；\n]{0,10}(?:CT|MR|MRI|磁共振|DR|X线)",
        r"(?:CT|MR|MRI|磁共振|DR|X线)[^，。；\n]{0,16}(胸腰椎|胸腰段脊柱|腰椎|胸椎|颈椎|膝关节|踝关节|肩关节|髋关节|腕关节|肘关节|头颅|胸部|腹部)",
        r"检查(?:部位|项目)[:：]\s*([^，。；\n]{2,40})",
    ]:
        match = re.search(pattern, haystack, re.I)
        if match:
            value = _collapse_chinese_spaces(match.group(1)).strip("，。；; ")
            value = re.sub(r"(?:螺旋|平扫|增强|检查|项目|部位)", "", value).strip()
            if value and value != "不详":
                return value[:20]
    body = _infer_body_part(haystack)
    return body if body else ""


def _extract_imaging_material_entries(blocks: List[dict], max_items: int = 5) -> List[str]:
    entries: List[str] = []
    seen = set()
    for block in blocks:
        filename = block.get("filename", "")
        content = block.get("content", "")
        haystack = f"{filename}\n{content}"
        if not re.search(r"CT|ＣＴ|MR|MRI|磁共振|DR|X线|电子图像|电子影像|影像学", haystack, re.I):
            continue
        if not any(k in haystack for k in ["检查", "影像", "报告", "CT号", "MR号", "病历号", "病案号", "电子图像", "电子影像"]):
            continue
        hospital = _extract_hospital_name(filename, content)
        date_text = _extract_first_cn_date(content)
        method = _extract_single_imaging_method(haystack)
        body = _extract_exam_body_text(haystack)
        identifiers = _extract_labeled_identifiers(haystack, max_items=4)
        if method == "不详" and not identifiers:
            continue
        prefix_parts = []
        if hospital != "不详":
            prefix_parts.append(hospital)
        if date_text != "不详":
            prefix_parts.append(date_text)
        exam_name = "".join([body, method if method != "不详" else "影像"])
        if not exam_name:
            exam_name = "影像学"
        entry = "".join(prefix_parts) + f"{exam_name}电子图像"
        if identifiers:
            entry += "（" + "；".join(identifiers[:3]) + "）"
        key = re.sub(r"\s+", "", entry)
        if key not in seen:
            seen.add(key)
            entries.append(entry)
        if len(entries) >= max_items:
            break
    return entries


def _build_material_summary_line3(blocks: List[dict], joined: str) -> str:
    carrier = _extract_material_carrier(blocks, joined)
    entries = _extract_imaging_material_entries(blocks)
    if entries:
        return f"影像学{carrier}，其中包括" + "、".join(entries) + "等"
    identifiers = _extract_labeled_identifiers(joined, max_items=5)
    if identifiers:
        return f"影像学{carrier}，其中包括相关影像学电子图像（" + "；".join(identifiers) + "）等"
    if carrier != "实物信息待补充":
        return f"影像学{carrier}，其中包括相关影像学电子图像等"
    return ""


def _count_material_blocks(blocks: List[dict], filename_keywords: List[str], content_keywords: Optional[List[str]] = None) -> int:
    count = 0
    content_keywords = content_keywords or []
    for block in blocks:
        filename = str(block.get("filename") or "")
        content = str(block.get("content") or "")
        if any(k in filename for k in filename_keywords) or (content_keywords and any(k in content[:1200] for k in content_keywords)):
            count += 1
    return count


def _format_material_count(count: int) -> str:
    return f"{count}份" if count > 0 else ""


def _extract_first_identifier_for_labels(text: str, labels: List[str]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{label_pattern})[:：]?\s*([A-Za-z0-9\-]{{3,40}})", text or "", re.I)
    return _safe_group(match, 1, default="")


def _build_legal_medical_material_line(blocks: List[dict], joined: str) -> str:
    parts: List[str] = []

    complaint_count = _count_material_blocks(blocks, ["民事起诉状", "起诉状"])
    reappraisal_apply_count = _count_material_blocks(blocks, ["重新鉴定申请书", "重新鉴定申请", "重鉴申请"])
    lawsuit_items = []
    if complaint_count:
        lawsuit_items.append(f"民事起诉状{_format_material_count(complaint_count)}")
    if reappraisal_apply_count:
        lawsuit_items.append(f"重新鉴定申请书{_format_material_count(reappraisal_apply_count)}")
    if lawsuit_items:
        parts.append("、".join(lawsuit_items))

    for block in blocks:
        filename = str(block.get("filename") or "")
        content = str(block.get("content") or "")
        haystack = f"{filename}\n{content[:1800]}"
        if "交通事故认定书" not in haystack and not ("道路交通事故" in haystack and "认定" in haystack):
            continue
        agency = _safe_group(re.search(r"([一-龥]{2,}(?:公安局|公安机关)交通警察[^\n，。；]{0,25}(?:支队|大队))", haystack), 1, default="")
        prefix = f"{agency}道路交通事故认定书" if agency else "道路交通事故认定书"
        procedure = "（简易程序）" if "简易程序" in haystack else ""
        parts.append(f"{prefix}{procedure}1份")
        break

    external_seen = set()
    for block in blocks:
        filename = str(block.get("filename") or "")
        content = str(block.get("content") or "")
        haystack = f"{filename}\n{content[:2500]}"
        if "司法鉴定意见书" not in haystack and "重新鉴定意见书" not in haystack:
            continue
        if "浙江大学司法鉴定中心" in haystack and "本中心" in haystack[:800]:
            continue
        institution = _safe_group(re.search(r"([一-龥]{2,}司法鉴定(?:中心|所)(?:[一-龥]{0,12}所)?)", haystack), 1, default="")
        doc_no = _safe_group(re.search(r"([一-龥]{0,8}所?\[[^\]]{2,30}\]临鉴字第\d+号|[一-龥]{0,8}所?[（(]\d{4}[）)][^，。；\n]{0,20}临鉴字第\d+号)", haystack), 1, default="")
        key = (institution, doc_no, filename)
        if key in external_seen:
            continue
        external_seen.add(key)
        label = f"{institution}" if institution else "外部司法鉴定机构"
        no_part = f"{{{doc_no}}}" if doc_no else ""
        parts.append(f"{label}{no_part}司法鉴定意见书1份")
        if len(external_seen) >= 3:
            break

    medical_groups: Dict[tuple, Dict[str, Any]] = {}
    for block in blocks:
        filename = str(block.get("filename") or "")
        content = str(block.get("content") or "")
        haystack = f"{filename}\n{content[:2200]}"
        if not any(k in haystack for k in ["病历", "诊断", "报告单", "门诊号", "住院号", "病案号", "检查报告"]):
            continue
        if any(k in filename for k in ["司法鉴定委托书", "民事起诉状", "事故认定", "身份证", "鉴定笔录"]):
            continue
        hospital = _extract_hospital_name(filename, content)
        hospital = "相关医院" if hospital == "不详" else hospital
        if "住院" in haystack or "入院" in haystack or "出院" in haystack:
            kind = "住院病历"
            number = _extract_first_identifier_for_labels(haystack, ["住院号", "病案号", "病历号"])
        elif "门急诊" in haystack or "急诊" in haystack or "门诊病历" in haystack or "门诊" in haystack:
            kind = "门急诊病历"
            number = _extract_first_identifier_for_labels(haystack, ["门诊号", "病历号", "病案号", "就诊号", "就诊卡号"])
        elif "诊断" in haystack and "报告单" not in haystack:
            kind = "门诊诊断"
            number = _extract_first_identifier_for_labels(haystack, ["门诊号", "病历号", "就诊号", "就诊卡号"])
        elif "报告单" in haystack or "检查报告" in haystack:
            kind = "报告单"
            number = ""
        else:
            continue
        key = (hospital, kind, number)
        entry = medical_groups.setdefault(key, {"hospital": hospital, "kind": kind, "number": number, "count": 0})
        entry["count"] += 1

    for entry in medical_groups.values():
        hospital = str(entry["hospital"])
        kind = str(entry["kind"])
        number = str(entry.get("number") or "")
        count = int(entry.get("count") or 1)
        if kind == "报告单":
            parts.append(f"{hospital}报告单若干")
            continue
        number_part = ""
        if number:
            label = "住院号" if kind == "住院病历" else "门诊号"
            number_part = f"（{label}{number}）"
        parts.append(f"{hospital}{kind}{number_part}{_format_material_count(count)}")

    return "；".join(_merge_list_unique([], parts, max_items=18)) + "等" if parts else ""


ACCEPTANCE_DATE_LABEL_RE = re.compile(
    r"(?:受理日期|受理时间|受理日|收案日期)[:：]?\s*"
    r"((?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)"
)


def _normalize_cn_date_text(value: Any) -> str:
    """把日期文本归一成“YYYY年M月D日”；不是完整日期则返回空串。"""
    match = re.search(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", str(value or ""))
    if not match:
        return ""
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year}年{month}月{day}日"


def _extract_acceptance_date(text: str) -> str:
    """从材料中找本案受理日期；找不到返回空串。

    受理日期必须能在材料中找到出处。旧实现直接填运行当天，等于在正式文书上
    写一个错误的受理日期；判不出时应留空转人工。
    """
    compact = re.sub(r"[ \t]+", "", text or "")
    match = ACCEPTANCE_DATE_LABEL_RE.search(compact)
    if not match:
        return ""
    return _normalize_cn_date_text(match.group(1))


def _post_process_basic_situation(payload: dict, source_text: str = "") -> dict:
    items = _filter_usable_str_list(payload.get("entrustment_items", []), max_items=12)
    if items:
        payload["entrustment_items"] = items
        payload.pop("_entrustment_items_fallback", None)
    else:
        payload["entrustment_items"] = []
        payload["_entrustment_items_fallback"] = True
    # 受理日期只采信能在材料中核到出处的值；否则置“不详”，由人工审计补录。
    acceptance = _extract_acceptance_date(source_text)
    if not acceptance:
        candidate = _normalize_cn_date_text(payload.get("acceptance_date"))
        if candidate:
            source_compact = re.sub(r"\s+", "", source_text or "")
            # 手里有原文时必须能核到出处；拿不到原文时沿用上游已抽取的值。
            if not source_compact or candidate in source_compact:
                acceptance = candidate
    payload["acceptance_date"] = acceptance or "不详"
    payload["appraisal_location"] = FIXED_APPRAISAL_LOCATION
    payload["present_personnel"] = []
    payload["material_summary"] = _normalize_basic_material_summary(payload.get("material_summary", []))
    payload["uncertain_points"] = _merge_list_unique([], payload.get("uncertain_points", []), max_items=8)
    return payload


def _rule_based_basic_extraction(markdown_text: str) -> dict:
    blocks = _split_file_blocks(markdown_text)
    joined = markdown_text

    payload = BasicSituationInfo().model_dump()

    court_match = re.search(r"([\u4e00-\u9fa5]{2,}(?:市|区|县)?人民法院)", joined)
    if court_match:
        payload["client_name"] = court_match.group(1)

    case_match = re.search(r"[（(]\d{4}[）)]\s*浙\s*\d+\s*民初\s*\d+\s*号", joined)
    if not case_match:
        case_match = re.search(r"[（(]\d{4}[）)]\s*浙\s*\d+\s*委鉴字第\s*\d+\s*号", joined)
    payload["case_number"] = _normalize_space_text(_safe_group(case_match, 0))

    subject_regex = re.search(
        r"(?:原告|被鉴定人)[:：]\s*([\u4e00-\u9fa5]{2,4})[，,]\s*(男|女)[，,]\s*(\d{4}年\d{1,2}月\d{1,2}日)出生[，,]\s*([\u4e00-\u9fa5]{1,3})族[，,]\s*(?:住址|地址|住)[:：]?\s*([^，。\n]+)[，,]\s*(?:公民身份号码|公民身份证号码|身份证号)[:：]?\s*([0-9Xx]{15,18})",
        joined,
    )
    identity_payload, identity_warnings = _extract_subject_identity_from_blocks(blocks)
    if subject_regex:
        payload["subject_name"] = _clean_subject_name(_safe_group(subject_regex, 1))
        payload["subject_gender"] = _safe_group(subject_regex, 2)
        payload["subject_birth"] = _safe_group(subject_regex, 3)
        payload["subject_ethnicity"] = _safe_group(subject_regex, 4)
        payload["subject_address"] = _clean_subject_address(_safe_group(subject_regex, 5))
        payload["subject_id"] = _safe_group(subject_regex, 6)
    else:
        subject_context = joined
        best_identity_blocks = sorted(blocks, key=_identity_block_score, reverse=True)
        if best_identity_blocks and _identity_block_score(best_identity_blocks[0]) > 0:
            subject_context = best_identity_blocks[0].get("content", "")
        else:
            for marker in ["### 文件：原告-身份证", "### 文件：身份证", "### 文件：原告-门诊病历", "### 文件：原告-住院"]:
                idx = joined.find(marker)
                if idx >= 0:
                    subject_context = joined[idx: idx + 1200]
                    break
        name_match = re.search(r"(?:病人姓名|患者姓名|姓名|原告|被鉴定人|委托人的姓名)[:：]?\s*([一-龥]{2,5})", subject_context)
        gender_match = re.search(r"性别[:：]\s*(男|女|女性|男性)", subject_context)
        ethnicity_match = re.search(r"民族[:：]\s*([\u4e00-\u9fa5]{1,3})", subject_context)
        birth_match = re.search(r"出生[:：]\s*(\d{4}年\d{1,2}月\d{1,2}日)", subject_context)
        addr_match = re.search(r"(?:住址|地址)[:：]\s*([^\n，。]+)", subject_context)
        id_match = SUBJECT_ID_LABEL_RE.search(subject_context)

        payload["subject_name"] = _clean_subject_name(_safe_group(name_match, 1))
        payload["subject_gender"] = _safe_group(gender_match, 1).replace("女性", "女").replace("男性", "男")
        payload["subject_ethnicity"] = _safe_group(ethnicity_match, 1)
        payload["subject_birth"] = _safe_group(birth_match, 1)
        payload["subject_address"] = _clean_subject_address(_safe_group(addr_match, 1))
        payload["subject_id"] = _safe_group(id_match, 1)

    for key, value in identity_payload.items():
        if key.startswith("_"):
            continue
        if key == "subject_address":
            if not _is_unresolved_value(value):
                payload[key] = value
        elif _is_unresolved_value(payload.get(key)) and not _is_unresolved_value(value):
            payload[key] = value

    # 在场人员通常 OCR 无法稳定确认，默认留待审计人工填写，避免误把代理人写入现场在场人员。
    payload["present_personnel"] = []

    commission_no = re.search(r"[（(]\d{4}[）)]\s*浙\s*\d+\s*委鉴字第\s*\d+\s*号", joined)
    commission_text = _normalize_space_text(_safe_group(commission_no, 0, default="（xxxx年）浙xxxx委鉴字第xxx号"))
    court_name = payload.get("client_name", "xxx法院")

    material_summary = [f"{court_name}{commission_text}《司法鉴定委托书》1份"]
    line2 = _build_legal_medical_material_line(blocks, joined)
    if line2:
        material_summary.append(line2)
    line3 = _build_material_summary_line3(blocks, joined)
    if line3:
        material_summary.append(line3)
    payload["material_summary"] = material_summary

    evidence = []
    for line in joined.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(k in s for k in ["司法鉴定委托书", "委鉴字", "民初", "在场人员", "公民身份证号码", "光盘"]):
            evidence.append(s)
        if len(evidence) >= 8:
            break
    payload["evidence_snippets"] = evidence

    uncertain_points = list(identity_warnings)
    if _is_polluted_subject_address(payload.get("subject_address")):
        payload["subject_address"] = "不详"
        uncertain_points.append("被鉴定人住址疑似混入医疗字段，建议核对身份证反面住址。")
    if _clean_text(payload.get("client_name")) == "不详":
        uncertain_points.append("委托人名称未明确识别，建议核对司法鉴定委托书抬头。")
    if _clean_text(payload.get("subject_name")) == "不详":
        uncertain_points.append("被鉴定人身份信息提取不完整，建议核对身份证明及起诉状。")
    if _clean_text(payload.get("case_number")) == "不详":
        uncertain_points.append("案号未明确识别，建议核对委托书编号与法院案号。")
    payload["uncertain_points"] = uncertain_points

    return _post_process_basic_situation(_normalize_value(payload), markdown_text)


def _material_quality_score(items: List[str]) -> int:
    score = 0
    for item in items or []:
        text = _clean_text(item)
        if text == "不详":
            continue
        score += 1
        if re.search(r"\d+", text):
            score += 1
        if "xxx" in text or "x份" in text:
            score -= 1
    return score


def _merge_basic_rule_llm(rule_payload: dict, llm_payload: Optional[dict]) -> dict:
    merged = dict(rule_payload)
    llm_contributed = False

    scalar_fields = [
        "client_name",
        "case_number",
        "subject_name",
        "subject_gender",
        "subject_ethnicity",
        "subject_birth",
        "subject_address",
        "subject_id",
    ]
    for field in scalar_fields:
        validator = (lambda text: not _is_polluted_subject_address(text)) if field == "subject_address" else None
        value, used_llm = _first_usable_text(
            rule_payload.get(field),
            llm_payload.get(field) if llm_payload else None,
            llm_validator=validator,
        )
        merged[field] = _clean_subject_address(value) if field == "subject_address" else value
        llm_contributed = llm_contributed or used_llm

    # 在场人员不再由 OCR/LLM 自动提取，保持为空，由 renderer 显示“待补充”或由审计覆盖。
    merged["present_personnel"] = []

    rule_materials = _filter_usable_str_list(rule_payload.get("material_summary", []), max_items=12)
    llm_materials = _filter_usable_str_list(llm_payload.get("material_summary", []), max_items=12) if llm_payload else []
    if rule_materials:
        merged["material_summary"] = rule_materials
    elif llm_materials:
        merged["material_summary"] = llm_materials
        llm_contributed = True
    else:
        merged["material_summary"] = []

    merged["evidence_snippets"], _ = _first_usable_list(
        rule_payload.get("evidence_snippets", []),
        llm_payload.get("evidence_snippets", []) if llm_payload else [],
        max_items=12,
        fallback_pending=False,
    )

    merged["uncertain_points"] = _merge_list_unique(
        _filter_usable_str_list(rule_payload.get("uncertain_points", []), max_items=8),
        _filter_usable_str_list(llm_payload.get("uncertain_points", []), max_items=8) if llm_payload else [],
        max_items=8,
    )

    merged = _post_process_basic_situation(_normalize_value(merged))

    for field in scalar_fields:
        if _is_unresolved_value(merged.get(field)):
            merged[field] = HYBRID_PENDING_TEXT

    merged["_basic_source_mode"] = "hybrid" if llm_contributed else "rules"
    return merged


def extract_basic_situation_hybrid(
    markdown_text: str,
    request_timeout_seconds: int = 90,
    max_attempts: int = 2,
    chunk_max_chars: int = 32000,
    enable_llm: bool = True,
) -> dict:
    """第一部分采用规则优先；仅规则缺失时才调用 LLM 补全。"""

    blocks = _split_file_blocks(markdown_text)
    if not blocks:
        blocks = [{"filename": "整篇输入", "content": markdown_text}]

    rule_payload = _rule_based_basic_extraction(markdown_text)
    llm_payload = None
    llm_stats = {}
    llm_error = None
    llm_attempted = False

    need_llm = enable_llm and _basic_rule_needs_llm(rule_payload)
    if need_llm:
        llm_attempted = True
        try:
            llm_result = extract_basic_situation_chunked(
                markdown_text,
                request_timeout_seconds=request_timeout_seconds,
                max_attempts=max_attempts,
                chunk_max_chars=chunk_max_chars,
            )
            if llm_result:
                llm_stats = llm_result.pop("_parse_stats", {})
                llm_payload = _normalize_value(llm_result)
        except Exception as e:
            llm_error = str(e)

    merged = _merge_basic_rule_llm(rule_payload, llm_payload)
    selected_names = llm_stats.get("selected_file_names") or [b.get("filename", "") for b in blocks]
    selected_names = [name for name in selected_names if name]
    merged["_parse_stats"] = {
        "total_files": len(blocks),
        "selected_files": llm_stats.get("selected_files", len(blocks)),
        "selected_file_names": selected_names,
        "requirement_count": llm_stats.get("requirement_count", 7),
        "mode": merged.get("_basic_source_mode", "rules"),
        "llm_used": llm_payload is not None,
        "llm_attempted": llm_attempted,
        "llm_error": llm_error,
    }
    return merged


def _normalize_cn_datetime(text: str) -> str:
    normalized = re.sub(r"\s+", "", text or "")
    normalized = normalized.replace("许", "")
    return normalized.strip("，。；;")


def _collapse_chinese_spaces(text: str) -> str:
    cleaned = _clean_text(text)
    if cleaned == "不详":
        return cleaned
    cleaned = re.sub(r"([\u4e00-\u9fa5])\s+([\u4e00-\u9fa5])", r"\1\2", cleaned)
    cleaned = re.sub(r"\s+([（）()])", r"\1", cleaned)
    cleaned = re.sub(r"([（）()])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([，。；：])", r"\1", cleaned)
    cleaned = re.sub(r"([，。；：])\s+", r"\1", cleaned)
    return cleaned.strip()


def _ensure_cn_sentence(text: str) -> str:
    cleaned = _clean_text(text)
    if cleaned == "不详":
        return cleaned
    if cleaned.endswith(("。", "！", "？")):
        return cleaned
    return f"{cleaned}。"


def _build_case_requirement_contexts(blocks: List[dict], field_max_chars: int) -> tuple[dict, dict]:
    contexts = {}
    selected_files = set()
    requirement_hit_count = {}

    for requirement_name, keywords in CASE_REQUIREMENT_KEYWORDS.items():
        req_blocks = _select_blocks_by_field_requirement(blocks, keywords, max_blocks=14)
        requirement_hit_count[requirement_name] = len(req_blocks)
        for block in req_blocks:
            selected_files.add(block.get("filename", ""))
        contexts[requirement_name] = _render_blocks_with_char_limit(req_blocks, max_chars=field_max_chars)

    stats = {
        "total_files": len(blocks),
        "selected_files": len(selected_files),
        "selected_file_names": sorted([name for name in selected_files if name]),
        "requirement_count": len(CASE_REQUIREMENT_KEYWORDS),
        "requirement_hit_count": requirement_hit_count,
    }
    return contexts, stats


def _extract_case_defendants(joined_text: str) -> List[str]:
    raw = re.findall(r"被告[一二三四五六七八九十]?[：:]\s*([^，。\n；;]+)", joined_text)
    defendants = []
    for item in raw:
        name = item.strip()
        if name and name not in defendants:
            defendants.append(name)
        if len(defendants) >= 8:
            break
    return defendants


def _replace_anonymous_name(text: str, real_name: str) -> str:
    cleaned = _clean_text(text)
    if cleaned == "不详":
        return cleaned
    if _clean_text(real_name) == "不详":
        return cleaned
    return re.sub(r"x{2,}", real_name, cleaned, flags=re.IGNORECASE)


def _traffic_accident_blocks(markdown_text: str) -> List[dict]:
    blocks = _split_file_blocks(markdown_text)
    selected = []
    for block in blocks:
        filename = str(block.get("filename") or "")
        content = str(block.get("content") or "")
        haystack = f"{filename}\n{content[:1200]}"
        if "交通事故认定书" in haystack or ("道路交通事故" in haystack and "认定" in haystack):
            selected.append(block)
    return selected


def _incident_source_blocks(markdown_text: str) -> List[dict]:
    blocks = _split_file_blocks(markdown_text)
    scored: List[tuple[int, dict]] = []
    filename_keywords = ["分析报告", "案件记录", "答辩", "事故", "工伤", "警情", "询问", "调查"]
    content_keywords = ["事故发生", "事情发生", "事发", "受伤经过", "事故经过", "摔倒", "摔伤", "砸伤", "爆炸", "工伤", "送往医院", "送至医院"]
    negative_keywords = ["司法鉴定委托书", "民事起诉状", "交通事故认定书", "病历", "报告单", "出院", "门诊", "住院", "发票", "费用"]
    for block in blocks:
        filename = str(block.get("filename") or "")
        content = str(block.get("content") or "")
        haystack = f"{filename}\n{content[:1800]}"
        score = 0
        score += sum(5 for k in filename_keywords if k in filename)
        score += sum(2 for k in content_keywords if k in haystack)
        score -= sum(4 for k in negative_keywords if k in filename)
        if score > 0:
            scored.append((score, block))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [block for _, block in scored[:5]]


def _extract_nontraffic_incident_fact(markdown_text: str, subject_name: str = "") -> Dict[str, str]:
    candidates: List[str] = []
    blocks = _incident_source_blocks(markdown_text)
    if not blocks:
        blocks = _split_file_blocks(markdown_text)[:8]
    patterns = [
        r"(20\d{2}年\s*\d{1,2}月\s*\d{1,2}日[^。；\n]{0,220}(?:摔倒|摔伤|跌倒|坠落|砸伤|压伤|爆炸|烧伤|烫伤|工伤|事故|受伤)[^。；\n]{0,120})",
        r"([^。；\n]{0,80}(?:于|在)20\d{2}年\s*\d{1,2}月\s*\d{1,2}日[^。；\n]{0,220}(?:摔倒|摔伤|跌倒|坠落|砸伤|压伤|爆炸|烧伤|烫伤|工伤|事故|受伤)[^。；\n]{0,120})",
        r"([^。；\n]{0,120}(?:摔倒|摔伤|跌倒|坠落|砸伤|压伤|爆炸|烧伤|烫伤|工伤事故|意外事故)[^。；\n]{0,160}(?:送往|送至|医院|治疗)[^。；\n]{0,80})",
    ]
    for block in blocks:
        filename = str(block.get("filename") or "")
        content = _collapse_chinese_spaces(str(block.get("content") or ""))
        if "交通事故" in f"{filename}\n{content[:600]}" or "道路交通事故" in f"{filename}\n{content[:600]}":
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, content):
                text = _forensic_snippet(match.group(1), max_len=320)
                text = re.split(r"原告为维护|现被告|诉讼请求|特具状|申请鉴定|赔偿", text)[0].strip("。；;，, ")
                if len(text) >= 16:
                    candidates.append(text)
        if candidates:
            break
    if not candidates:
        return {}
    fact = _replace_anonymous_name(candidates[0], subject_name).strip("。；;，, ")
    if not re.search(r"伤后|送往|送至|医院|治疗", fact):
        fact += "，伤后立即送往医院治疗"
    return {"incident_fact_text": fact, "accident_date": _extract_first_cn_date(fact)}


# 表格版式噪声：出现这些说明取到的是勾选项/表头，不是案情叙述。
_FORM_NOISE_FACT_RE = re.compile(
    r"[√□■☑]|交通修正章|财产损失事故|受伤当事人伤势轻微|适用简易程序|交通警察|损害赔偿调解|存档份"
)


def _extract_traffic_fact_responsibility(markdown_text: str) -> Dict[str, str]:
    """从交通事故认定书中提取“交通事故事实及责任”原文板块。"""
    for block in _traffic_accident_blocks(markdown_text):
        content = str(block.get("content") or "")
        lines = content.splitlines()
        for idx, raw in enumerate(lines):
            line = _forensic_snippet(raw, max_len=420)
            compact_line = re.sub(r"\s+", "", line)
            if not ("交通事故事实" in compact_line and "责任" in compact_line):
                continue
            parts: List[str] = []
            # 表格被 OCR 拉平后，表头“交通事故事实及责任”常被插进句子中间，
            # 此时案情在表头【之前】。判断依据：上一行没有以句号收尾，
            # 说明句子被表头截断了。
            preceding: List[str] = []
            for back in range(idx - 1, max(-1, idx - 4), -1):
                prev = _forensic_snippet(lines[back], max_len=420)
                compact_prev = re.sub(r"\s+", "", prev)
                if not prev or prev == "不详" or re.search(r"^(?:事故时间|事故地点|当事人|天气)", compact_prev):
                    break
                # 表格单元格值（车牌号等）不是句子，遇到就停，避免把“杭118280”接在句首。
                if len(compact_prev) < 12:
                    break
                preceding.insert(0, prev)
                if re.search(r"[。；;]$", prev.strip()):
                    break
            if preceding:
                # 句子的收尾（如“故。”）通常跟在表头后面一行，接上它这句才完整。
                head = "".join(_collapse_chinese_spaces(x) for x in preceding)
                for extra in lines[idx + 1: idx + 3]:
                    compact_extra = re.sub(r"\s+", "", _forensic_snippet(extra, max_len=60))
                    if not compact_extra or len(compact_extra) > 8:
                        break
                    if _FORM_NOISE_FACT_RE.search(compact_extra):
                        break
                    if re.match(r"^(?:杭州|交通|修正章|当事人|交通警察)", compact_extra):
                        break
                    head += compact_extra
                    break
                parts.append(head)
                fact = _collapse_chinese_spaces("".join(parts)).strip("。；;，, ")
                if len(fact) >= 20 and not _FORM_NOISE_FACT_RE.search(fact):
                    return {
                        "traffic_accident_fact_responsibility_text": fact,
                        "accident_date": _extract_first_cn_date(fact),
                        "source_file": str(block.get("filename") or ""),
                    }
                parts = []
            inline = re.sub(r".*?交通事故事实及责任[:：]?", "", line).strip()
            if inline and inline != line and inline != "不详":
                parts.append(inline)
            for extra in lines[idx + 1: idx + 16]:
                text = _forensic_snippet(extra, max_len=420)
                if not text or text == "不详":
                    continue
                compact = re.sub(r"\s+", "", text)
                if re.search(r"^(?:损害赔偿调解结果|道路交通事故证据|当事人签名|交通警察|日期|时间|告知事项|送达情况|编号)[:：]?", compact):
                    break
                if compact.startswith("###文件"):
                    break
                # 表头之后的短单元格（“杭州”“交通”“修正章（6）”“√”等）是版式碎片，不是案情。
                if preceding and len(compact) <= 6:
                    break
                parts.append(text)
            fact = _collapse_chinese_spaces("".join(parts)).strip("。；;，, ")
            if len(fact) >= 20 and not _FORM_NOISE_FACT_RE.search(fact):
                return {
                    "traffic_accident_fact_responsibility_text": fact,
                    "accident_date": _extract_first_cn_date(fact),
                    "source_file": str(block.get("filename") or ""),
                }
        text = _collapse_chinese_spaces(content)
        match = re.search(r"(20\d{2}年\d{1,2}月\d{1,2}日[^。]{20,260}(?:责任|无责任|全部责任|主要责任|次要责任|同等责任)[^。]{0,120})", text)
        if match:
            fact = match.group(1).strip("。；;，, ")
            return {"traffic_accident_fact_responsibility_text": fact, "accident_date": _extract_first_cn_date(fact), "source_file": str(block.get("filename") or "")}
    return {}


def _post_process_basic_case(payload: dict) -> dict:
    payload["paragraph_1"] = _ensure_cn_sentence(_collapse_chinese_spaces(payload.get("paragraph_1", "不详")))
    payload["paragraph_2"] = _ensure_cn_sentence(_collapse_chinese_spaces(payload.get("paragraph_2", "不详")))
    payload["evidence_snippets"] = _merge_list_unique([], payload.get("evidence_snippets", []), max_items=14)
    payload["uncertain_points"] = _merge_list_unique([], payload.get("uncertain_points", []), max_items=8)
    return payload


def _rule_based_case_extraction(markdown_text: str) -> dict:
    payload = BasicCaseInfo().model_dump()
    joined = markdown_text
    traffic_payload = _extract_traffic_fact_responsibility(markdown_text)

    plaintiff_match = re.search(r"原告[：:]\s*([\u4e00-\u9fa5]{2,4})", joined)
    plaintiff_name = _safe_group(plaintiff_match, 1)
    nontraffic_payload = _extract_nontraffic_incident_fact(markdown_text, plaintiff_name)

    incident_date_match = re.search(
        r"事故时间[：:]\s*([0-9]{4}年\s*[0-9]{1,2}月\s*[0-9]{1,2}日(?:\s*[0-9]{1,2}时(?:\s*[0-9]{1,2}分)?(?:许)?)?)",
        joined,
    )
    if not incident_date_match:
        incident_date_match = re.search(
            r"([0-9]{4}年\s*[0-9]{1,2}月\s*[0-9]{1,2}日\s*[0-9]{1,2}时\s*[0-9]{1,2}分(?:许)?)[^\n。]{0,180}(?:发生|交通事故|碰撞|撞伤|受伤|车辆损坏|道闸|电动门)",
            joined,
        )
    incident_datetime = _normalize_cn_datetime(_safe_group(incident_date_match, 1))
    incident_datetime = re.sub(r"^(?:025|225|20225)年", "2025年", incident_datetime)

    incident_fact = "不详"
    line_candidates = [line.strip() for line in joined.splitlines() if line.strip()]
    collision_terms = ["碰撞", "相撞", "撞伤", "电动门", "道闸", "受伤", "车辆损坏"]
    movement_terms = ["驾驶", "骑", "电动自行车", "车牌号", "小区", "门岗", "事故"]
    scored_incidents = []
    for line in line_candidates:
        if any(k in line for k in collision_terms) and any(k in line for k in movement_terms):
            score = 0
            if "驾驶" in line or "骑" in line:
                score += 3
            if "道闸" in line or "电动门" in line:
                score += 2
            if "车辆损坏" in line:
                score += 1
            if any(k in line for k in ["事实和理由", "完毕", "原告为维护", "特具状起诉", "法庭"]):
                score -= 4
            if len(line) > 240:
                score -= 2
            scored_incidents.append((score, len(line), line))
    if scored_incidents:
        scored_incidents.sort(key=lambda x: (-x[0], x[1]))
        incident_fact = scored_incidents[0][2]
    if incident_fact == "不详":
        sentence_match = re.search(
            r"([^。\n]*(?:驾驶|骑|电动自行车|车牌号)[^。\n]{0,180}(?:碰撞|相撞|撞伤|受伤|车辆损坏)[^。\n]*[。；]?)",
            joined,
        )
        if sentence_match:
            incident_fact = sentence_match.group(1).strip()
    if incident_fact != "不详":
        incident_fact = re.sub(r"^(?:025|225|20225)年", "2025年", incident_fact)
        event_match = re.search(
            r"(20\d{2}年\s*\d{1,2}月\s*\d{1,2}日\s*\d{1,2}时\s*\d{1,2}分(?:许)?[^。；\n]{0,220}(?:碰撞|相撞|撞伤|受伤|车辆损坏)[^。；\n]*)",
            incident_fact,
        )
        if event_match:
            incident_fact = event_match.group(1).strip()
        incident_fact = re.split(r"原告为维护|现被告|完毕|特具状起诉", incident_fact)[0].strip("。；;，, ")
    incident_fact = _replace_anonymous_name(incident_fact, plaintiff_name)

    location_match = re.search(
        r"在([^，。\n]{2,90}(?:路|街道|街|小区|苑|门岗|道闸|门禁|村|镇|院内|旁)[^，。\n]{0,40})(?:行驶|发生|与|被|时|处)",
        incident_fact if incident_fact != "不详" else joined,
    )
    incident_location = _collapse_chinese_spaces(_safe_group(location_match, 1)).strip("，。；; ")

    authority_match = re.search(r"([\u4e00-\u9fa5]{2,}(?:公安局|公安机关)交通警察[^\n，。；]{0,20}(?:支队|大队))", joined)
    authority = _safe_group(authority_match, 1)

    responsibility_match = re.search(r"认定[：:]\s*([^。\n]*责任[^。\n]*)", joined)
    if not responsibility_match:
        responsibility_match = re.search(r"(当事人[^。\n]*负[^。\n]*责任[^。\n]*)", joined)
    if not responsibility_match:
        # 鉴定笔录里常见写法：“结果为：被告一负事故全部责任，原告无责任。”
        responsibility_match = re.search(r"(?:认定结果|结果)[为是][：:]?\s*([^。\n]{2,60}责任[^。\n]{0,30})", joined)
    if not responsibility_match:
        responsibility_match = re.search(r"((?:原告|被告|申请人|被申请人|驾驶人)[^。\n]{0,40}负[^。\n]{0,12}责任[^。\n]{0,30})", joined)
    responsibility_text = _safe_group(responsibility_match, 1)
    responsibility_text = _replace_anonymous_name(responsibility_text, plaintiff_name)
    # 鉴定笔录常以“原告/被告”指称当事人；被鉴定人在此类案件中即原告，
    # 因此可安全替换为姓名。被告的具体姓名不做推断，留给鉴定人核对。
    if responsibility_text != "不详" and _clean_text(plaintiff_name) != "不详":
        responsibility_text = responsibility_text.replace("原告", _clean_text(plaintiff_name))
    responsibility_text = _collapse_chinese_spaces(responsibility_text)

    if "负全部责任" in responsibility_text and "无责任" not in responsibility_text:
        responsibility_text = re.sub(r"([\u4e00-\u9fa5]{2,4})负责任", r"\1无责任", responsibility_text)

    defendants = _extract_case_defendants(joined)
    if not defendants:
        litigate_match = re.search(r"将\s*([^。\n]{4,180})\s*诉至", joined)
        if litigate_match:
            candidates = re.split(r"[、，,；;]", litigate_match.group(1))
            for item in candidates:
                name = _clean_text(item)
                if name != "不详" and len(name) >= 2 and name not in defendants:
                    defendants.append(name)
                if len(defendants) >= 8:
                    break
    defendants_text = "、".join(defendants) if defendants else ""

    filing_date_match = re.search(r"申请人[:：][^\n]*\n\s*([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)", joined, re.DOTALL)
    if not filing_date_match:
        filing_date_match = re.search(r"具状人[:：][^\n]*\n\s*时间[:：]\s*([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)", joined, re.DOTALL)
    if not filing_date_match:
        filing_date_match = re.search(r"具状人[:：][^\n]*\n\s*([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)", joined, re.DOTALL)
    if not filing_date_match:
        filing_date_match = re.search(r"(?:起诉日期|落款日期|申请日期|时间)[:：]\s*([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)", joined)
    filing_date = _safe_group(filing_date_match, 1)

    court_match = re.search(r"诉至\s*([\u4e00-\u9fa5]{2,}(?:市|区|县)?人民法院)", joined)
    if not court_match:
        court_match = re.search(r"([\u4e00-\u9fa5]{2,}(?:市|区|县)?人民法院)", joined)
    entrusting_court = _safe_group(court_match, 1)

    paragraph_1 = "不详"
    traffic_fact_text = _clean_text(traffic_payload.get("traffic_accident_fact_responsibility_text"))
    if traffic_fact_text != "不详":
        traffic_fact_text = _replace_anonymous_name(traffic_fact_text, plaintiff_name).strip("。；;")
        paragraph_1 = f"据送检的相关鉴定资料记载：{traffic_fact_text}。"
        if traffic_payload.get("accident_date") and incident_datetime == "不详":
            incident_datetime = traffic_payload.get("accident_date")
    elif nontraffic_payload.get("incident_fact_text"):
        incident_fact_text = _clean_text(nontraffic_payload.get("incident_fact_text")).strip("。；;")
        paragraph_1 = f"据委托人提供的鉴定资料记载：{incident_fact_text}。"
        if nontraffic_payload.get("accident_date") and incident_datetime == "不详":
            incident_datetime = nontraffic_payload.get("accident_date")
    elif incident_fact != "不详":
        incident_fact_text = incident_fact.strip("。；;")
        date_prefix = re.match(r"(?:20\d{2}|\d{3})年\s*\d{1,2}月\s*\d{1,2}日\s*\d{1,2}时\s*\d{1,2}分", incident_fact_text)
        if incident_datetime != "不详" and incident_datetime not in incident_fact_text and not date_prefix:
            incident_fact_text = f"{incident_datetime}，{incident_fact_text}"
        if not re.search(r"伤后|送往|送至|医院|治疗", incident_fact_text):
            incident_fact_text += "，伤后立即送往医院治疗"
        paragraph_1 = f"据委托人提供的鉴定资料记载：{incident_fact_text}。"
    elif incident_datetime != "不详":
        paragraph_1 = f"据委托人提供的鉴定资料记载：{incident_datetime}发生事故，伤后立即送往医院治疗。"

    if responsibility_text != "不详" and traffic_fact_text == "不详":
        resp_text = responsibility_text.strip("。；;")
        if authority != "不详":
            resp_sentence = f"该事故据{authority}认定：{resp_text}。"
        else:
            resp_sentence = f"责任认定情况：{resp_text}。"
        if paragraph_1 == "不详":
            paragraph_1 = resp_sentence
        else:
            paragraph_1 += resp_sentence

    subject_for_sentence = plaintiff_name if plaintiff_name != "不详" else "被鉴定人"
    paragraph_2_parts = [f"经治疗后，{subject_for_sentence}伤情稳定。"]

    litigation_subject = plaintiff_name if plaintiff_name != "不详" else "当事人"
    filing_prefix = f"{filing_date}，" if filing_date != "不详" else ""
    if defendants_text:
        if entrusting_court != "不详":
            litigation_sentence = f"{filing_prefix}{litigation_subject}将{defendants_text}诉至{entrusting_court}，诉请相应赔偿并申请鉴定。"
        else:
            litigation_sentence = f"{filing_prefix}{litigation_subject}将{defendants_text}诉请相应赔偿并申请鉴定。"
    else:
        if entrusting_court != "不详":
            litigation_sentence = f"{filing_prefix}{litigation_subject}诉至{entrusting_court}，诉请相应赔偿并申请鉴定。"
        else:
            litigation_sentence = f"{filing_prefix}{litigation_subject}提起诉讼并申请鉴定。"
    paragraph_2_parts.append(litigation_sentence)

    if entrusting_court != "不详":
        paragraph_2_parts.append(f"为案件审理及合理处理赔偿相关事宜，{entrusting_court}委托本中心对上述委托事项进行鉴定。")

    paragraph_2 = "".join(paragraph_2_parts)

    payload["paragraph_1"] = paragraph_1
    payload["paragraph_2"] = paragraph_2
    payload["incident_datetime"] = incident_datetime
    payload["accident_date"] = traffic_payload.get("accident_date") or nontraffic_payload.get("accident_date") or _extract_first_cn_date(paragraph_1)
    if traffic_payload.get("traffic_accident_fact_responsibility_text"):
        payload["traffic_accident_fact_responsibility_text"] = traffic_payload.get("traffic_accident_fact_responsibility_text")
        payload["traffic_accident_source_file"] = traffic_payload.get("source_file", "")
    payload["incident_location"] = incident_location
    payload["lawsuit_date"] = filing_date
    payload["defendants"] = defendants
    payload["entrusting_court"] = entrusting_court
    payload["responsibility"] = responsibility_text

    evidence = []
    for line in line_candidates:
        if any(k in line for k in ["事故时间", "车牌号", "交通事故", "碰撞", "撞伤", "道闸", "认定", "被告", "诉讼请求", "具状人", "委托本中心", "人民法院"]):
            evidence.append(line)
        if len(evidence) >= 14:
            break
    payload["evidence_snippets"] = evidence

    uncertain_points = []
    if incident_fact == "不详":
        uncertain_points.append("未检索到完整事故经过原文，建议核对事故认定书原件。")
    if responsibility_text == "不详":
        uncertain_points.append("未检索到明确责任分配表述，建议核对责任认定条款。")
    if not defendants:
        uncertain_points.append("未检索到完整被诉主体列表，建议核对民事起诉状。")
    if entrusting_court == "不详":
        uncertain_points.append("未检索到明确委托法院名称，建议核对司法鉴定委托书。")
    payload["uncertain_points"] = uncertain_points

    return _post_process_basic_case(_normalize_value(payload))

def _merge_basic_case_rule_llm(rule_payload: dict, llm_payload: Optional[dict]) -> dict:
    merged = dict(rule_payload)
    llm_contributed = False

    for field in ["paragraph_1", "paragraph_2"]:
        rule_value = rule_payload.get(field)
        llm_value = llm_payload.get(field) if llm_payload else None
        value, used_llm = _first_usable_text(
            rule_value,
            llm_value,
            llm_validator=lambda text: ("xxx" not in text.lower()),
        )
        if field == "paragraph_1" and llm_payload and _case_paragraph_is_thin(rule_value):
            llm_text = _clean_text(llm_value)
            if not _is_unresolved_value(llm_text) and _case_paragraph_quality_score(llm_text) > _case_paragraph_quality_score(rule_value):
                value = llm_text
                used_llm = True
        merged[field] = value
        llm_contributed = llm_contributed or used_llm

    merged["evidence_snippets"], _ = _first_usable_list(
        rule_payload.get("evidence_snippets", []),
        llm_payload.get("evidence_snippets", []) if llm_payload else [],
        max_items=14,
        fallback_pending=False,
    )
    merged["uncertain_points"] = _merge_list_unique(
        _filter_usable_str_list(rule_payload.get("uncertain_points", []), max_items=8),
        _filter_usable_str_list(llm_payload.get("uncertain_points", []), max_items=8) if llm_payload else [],
        max_items=8,
    )

    merged = _post_process_basic_case(_normalize_value(merged))
    for field in ["paragraph_1", "paragraph_2"]:
        if _is_unresolved_value(merged.get(field)):
            merged[field] = HYBRID_PENDING_TEXT

    merged["_case_source_mode"] = "hybrid" if llm_contributed else "rules"
    return merged


def extract_basic_case_chunked(
    markdown_text: str,
    request_timeout_seconds: int = 90,
    max_attempts: int = 2,
    chunk_max_chars: int = 32000,
) -> dict:
    """按"基本案情"字段需求定向检索并抽取，输出两段可写入正文的表述。"""

    config = get_runtime_config()
    if config.llm_provider == "off":
        raise RuntimeError("当前模型调用模式为 off。")

    blocks = _split_file_blocks(markdown_text)
    if not blocks:
        blocks = [{"filename": "整篇输入", "content": markdown_text}]

    per_field_max_chars = max(2800, min(10000, max(9000, chunk_max_chars) // 2))
    contexts, stats = _build_case_requirement_contexts(blocks, field_max_chars=per_field_max_chars)

    llm = build_chat_llm(
        config,
        temperature=0.0,
        timeout=request_timeout_seconds,
        max_retries=0,
        max_tokens=1200,
    )

    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            append_local_prompt_if_needed("""你是一位严谨的法医临床鉴定助理。请基于给定上下文输出"二、基本案情"两段话。

要求：
1. 严格依据材料，不得补造未出现事实。
2. paragraph_1 写事故经过。交通事故优先使用“交通事故认定书/交通事故事实及责任”原文事实；其他意外事故按“谁于何时何地发生何事故，伤后送医”归纳。
3. paragraph_2 写治疗后、起诉及法院委托鉴定经过，不要把诉讼请求混入事故经过。
4. 找不到的信息不要硬编，可写"不详"或在 uncertain_points 说明。
5. evidence_snippets 最多 10 条，uncertain_points 最多 6 条。
6. 仅输出 JSON。
{format_instructions}
""", config, "basic_case"),
        ),
        (
            "human",
            """【事故经过上下文】
{ctx_incident}

【责任认定上下文】
{ctx_responsibility}

【起诉与诉请上下文】
{ctx_litigation}

【法院委托上下文】
{ctx_entrustment}

请输出"基本案情"两段内容。""",
        ),
    ])

    chain = prompt | llm | basic_case_parser

    payload_input = {
        "format_instructions": basic_case_parser.get_format_instructions(),
        "ctx_incident": contexts.get("incident", ""),
        "ctx_responsibility": contexts.get("responsibility", ""),
        "ctx_litigation": contexts.get("litigation", ""),
        "ctx_entrustment": contexts.get("entrustment", ""),
    }

    print("[*] 准备进行第二部分「基本案情」定向抽取（按事故/责任/诉讼/委托四组上下文）")
    result = _invoke_chain_with_retry(
        chain=chain,
        payload_input=payload_input,
        request_timeout_seconds=request_timeout_seconds,
        max_attempts=max_attempts,
        call_name="基本案情定向抽取",
    )

    if result is None:
        return None

    payload = _normalize_value(result.model_dump())
    payload = _post_process_basic_case(payload)
    payload["_parse_stats"] = stats
    return payload


def extract_basic_case_hybrid(
    markdown_text: str,
    request_timeout_seconds: int = 90,
    max_attempts: int = 2,
    chunk_max_chars: int = 32000,
    enable_llm: bool = True,
) -> dict:
    """第二部分采用规则优先；仅规则缺失时才调用 LLM 补全。"""

    blocks = _split_file_blocks(markdown_text)
    if not blocks:
        blocks = [{"filename": "整篇输入", "content": markdown_text}]

    rule_payload = _rule_based_case_extraction(markdown_text)
    llm_payload = None
    llm_stats = {}
    llm_error = None
    llm_attempted = False

    need_llm = enable_llm and _case_rule_needs_llm(rule_payload)
    if need_llm:
        llm_attempted = True
        try:
            llm_result = extract_basic_case_chunked(
                markdown_text,
                request_timeout_seconds=request_timeout_seconds,
                max_attempts=max_attempts,
                chunk_max_chars=chunk_max_chars,
            )
            if llm_result:
                llm_stats = llm_result.pop("_parse_stats", {})
                llm_payload = _normalize_value(llm_result)
        except Exception as e:
            llm_error = str(e)

    merged = _merge_basic_case_rule_llm(rule_payload, llm_payload)
    selected_names = llm_stats.get("selected_file_names") or [b.get("filename", "") for b in blocks]
    selected_names = [name for name in selected_names if name]
    merged["_parse_stats"] = {
        "total_files": len(blocks),
        "selected_files": llm_stats.get("selected_files", len(blocks)),
        "selected_file_names": selected_names,
        "requirement_count": llm_stats.get("requirement_count", 4),
        "mode": merged.get("_case_source_mode", "rules"),
        "llm_used": llm_payload is not None,
        "llm_attempted": llm_attempted,
        "llm_error": llm_error,
    }
    return merged


def _build_summary_requirement_contexts(blocks: List[dict], field_max_chars: int) -> tuple[dict, dict]:
    candidate_blocks = [
        b for b in blocks
        if _is_summary_candidate_block(b.get("filename", ""), b.get("content", ""))
    ]
    if not candidate_blocks:
        candidate_blocks = blocks

    contexts = {}
    selected_files = set()
    requirement_hit_count = {}

    for requirement_name, keywords in SUMMARY_REQUIREMENT_KEYWORDS.items():
        req_blocks = _select_summary_blocks_by_requirement(candidate_blocks, keywords, max_blocks=10)
        requirement_hit_count[requirement_name] = len(req_blocks)
        for block in req_blocks:
            selected_files.add(block.get("filename", ""))
        contexts[requirement_name] = _render_blocks_with_char_limit(req_blocks, max_chars=field_max_chars)

    stats = {
        "total_files": len(blocks),
        "selected_files": len(selected_files),
        "selected_file_names": sorted([name for name in selected_files if name]),
        "requirement_count": len(SUMMARY_REQUIREMENT_KEYWORDS),
        "requirement_hit_count": requirement_hit_count,
    }
    return contexts, stats


def _flatten_summary_keywords() -> List[str]:
    values = []
    for items in SUMMARY_REQUIREMENT_KEYWORDS.values():
        values.extend(items)
    return values


def _keyword_hit_score(text: str, keywords: List[str]) -> int:
    return sum(1 for keyword in keywords if keyword and keyword in text)


def _is_external_summary_block(filename: str, content: str) -> bool:
    haystack = f"{filename}\n{content}"
    if not any(keyword in haystack for keyword in EXTERNAL_APPRAISAL_KEYWORDS):
        return False
    if any(k in filename for k in ["病历", "出院", "门诊", "报告单", "发票", "费用"]):
        return False
    return bool(re.search(r"司法鉴定意见书|重新鉴定意见书|司法鉴定所|司法鉴定中心|鉴定意见|司鉴", haystack))


def _is_summary_candidate_block(filename: str, content: str) -> bool:
    haystack = f"{filename}\n{content}"
    if _is_external_summary_block(filename, content):
        return True

    score = 0

    score += _keyword_hit_score(haystack, _flatten_summary_keywords())
    score += _keyword_hit_score(filename, MEDICAL_KEYWORDS) * 2

    if re.search(r"主诉|现病史|入院情况|体格检查|专科|诊疗经过|治疗经过|手术记录|出院诊断|出院医嘱|住院号|病案号|就诊卡号", content):
        score += 5
    if re.search(r"急诊|首诊|入院记录|出院记录|住院病案|手术名称|术中见|内固定术|治疗终结|伤情稳定|复查", haystack):
        score += 4

    if _contains_any_keyword(filename, SUMMARY_NEGATIVE_FILENAME_KEYWORDS):
        score -= 4

    if re.search(r"司法鉴定委托书|民事起诉状|道路交通事故认定书|身份证明|委托代理", content[:2000]):
        score -= 2

    return score > 0


def _select_summary_blocks_by_requirement(blocks: List[dict], keywords: List[str], max_blocks: int = 10) -> List[dict]:
    scored = []
    for block in blocks:
        filename = block.get("filename", "")
        content = block.get("content", "")
        haystack = f"{filename}\n{content}"
        hit = _keyword_hit_score(haystack, keywords)
        if hit > 0:
            scored.append((hit, len(content), block))

    if not scored:
        return blocks[:max_blocks]

    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [item[2] for item in scored[:max_blocks]]


def _summary_text_quality(text: str) -> int:
    cleaned = _clean_text(text)
    if cleaned == "不详":
        return -999

    score = min(len(cleaned), 220)
    if re.search(r"骨折|术后|复查|影像|CT|DR|MRI|X线|住院|诊断|主诉|体格", cleaned):
        score += 20
    if re.search(r"过敏史|婚育史|月经史|个人史|家族史|中医诊断", cleaned):
        score -= 45
    if cleaned in {"无", "同前", "见上", "未见异常"}:
        score -= 30
    return score


def _summary_strip_markdown_noise(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"\|[^\n]*\|", " ", cleaned)
    cleaned = re.sub(r"[-|:]{4,}", " ", cleaned)
    return cleaned


def _summary_remove_noise_segments(text: str) -> str:
    cleaned = _summary_strip_markdown_noise(text)
    if not cleaned.strip():
        return ""

    boundaries = r"主诉|现病史|先病史|入院情况|专科检查|专科情况|体格检查|查体|辅助检查|诊疗经过|治疗经过|处理|出院情况|出院诊断|初步诊断|诊断|处理意见|出院医嘱|医嘱|建议"
    for label in SUMMARY_MEDICAL_NOISE_KEYWORDS:
        cleaned = re.sub(rf"{label}[:：]?[^\n。；]*?(?=(?:{boundaries})[:：]|\n|$)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" ，。；;：:")


def _summary_has_trauma_relevance(text: str) -> bool:
    cleaned = _clean_text(text)
    if cleaned == "不详":
        return False
    return any(k in cleaned for k in SUMMARY_TRAUMA_RELEVANT_DIAGNOSIS_KEYWORDS)


def _summary_clean_diagnosis_text(text: str) -> str:
    cleaned = _summary_remove_noise_segments(text)
    cleaned = re.sub(r"^(?:西医诊断|中医诊断|初步诊断|诊断|出院诊断)[:：]\s*", "", cleaned)
    if _clean_text(cleaned) == "不详":
        return "不详"

    cleaned = re.sub(r"\s+[0-9一二三四五六七八九十]+[\.．、]\s*", "；", cleaned)
    pieces = re.split(r"[；;，,、\n]|\s{2,}", cleaned)
    kept: List[str] = []
    for piece in pieces:
        part = piece.strip(" 。，；;:：0123456789.、")
        if not part:
            continue
        if any(noise in part for noise in SUMMARY_MEDICAL_NOISE_KEYWORDS):
            continue
        if any(noise in part for noise in SUMMARY_INCIDENTAL_DIAGNOSIS_KEYWORDS):
            continue
        if _summary_has_trauma_relevance(part):
            kept.append(part)
        elif len(pieces) <= 1:
            kept.append(part)
        if len(kept) >= 6:
            break
    if not kept:
        return "不详"
    return "；".join(_merge_list_unique([], kept, max_items=6))


def _trim_summary_value_by_labels(text: str, labels: List[str]) -> str:
    cleaned = text
    cut_index = len(cleaned)
    for label in labels:
        idx = cleaned.find(label)
        if idx > 0:
            cut_index = min(cut_index, idx)
    return cleaned[:cut_index].strip("，。；;：:")


def _sanitize_summary_field(field_name: str, text: str) -> str:
    cleaned = _collapse_chinese_spaces(_summary_remove_noise_segments(text))
    cleaned = cleaned.strip("，。；;：:")
    if _clean_text(cleaned) == "不详":
        return "不详"

    stop_labels = {
        "chief_complaint": ["现病史", "先病史", "体格检查", "专科", "诊疗经过", "术后复查", "出院诊断", "处理意见"],
        "present_history": ["既往史", "婚育史", "月经史", "个人史", "家族史", "过敏史", "体格检查", "专科", "诊疗经过", "术后复查", "出院诊断", "处理意见"],
        "imaging_findings": ["体格检查", "专科", "诊疗经过", "术后复查", "出院诊断", "处理意见", "实验室检查"],
        "physical_exam": ["辅助检查", "实验室检查", "专科", "诊疗经过", "诊治经过", "术后复查", "出院诊断", "处理意见"],
        "specialist_findings": ["辅助检查", "实验室检查", "诊疗经过", "术后复查", "出院诊断", "处理意见"],
        "treatment_course": ["辅助检查", "实验室检查", "术后复查", "出院诊断", "处理意见", "出院医嘱"],
        "post_op_review": ["实验室检查", "出院诊断", "处理意见", "出院医嘱"],
        "discharge_diagnosis": ["出院医嘱", "诊疗经过", "辅助检查", "实验室检查"],
        "treatment_advice": ["医师签名", "科室", "时间"],
    }

    if field_name in stop_labels:
        cleaned = _trim_summary_value_by_labels(cleaned, stop_labels[field_name])
        if _clean_text(cleaned) == "不详":
            return "不详"

    if field_name in {"physical_exam", "specialist_findings"} and re.match(r"^(?:婚育史|月经史|个人史|家族史|过敏史|传染病史|预防接种史|用药史|手术史|外伤史)[:：]", cleaned):
        if not re.search(r"压痛|肿胀|畸形|活动受限|肌力|感觉|血运|伤口|擦伤|挫伤|叩击痛|神经", cleaned):
            return "不详"

    if field_name in {"discharge_diagnosis", "treatment_advice"}:
        diagnosis_cleaned = _summary_clean_diagnosis_text(cleaned) if field_name == "discharge_diagnosis" else cleaned
        if _clean_text(diagnosis_cleaned) == "不详":
            return "不详"
        cleaned = diagnosis_cleaned

    if field_name == "post_op_review":
        has_imaging_signal = re.search(r"骨折|内固定|拍片|复查|影像|X线|CT|DR|MRI", cleaned)
        if not has_imaging_signal:
            return "不详"

    return cleaned[:220]


def _extract_first_cn_date(text: str) -> str:
    content = text or ""
    match = re.search(r"([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)", content)
    if match:
        return _safe_group(match, 1)

    dash = re.search(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})", content)
    if dash:
        y, m, d = dash.groups()
        return f"{int(y)}年{int(m)}月{int(d)}日"
    return "不详"


def _summary_item_key(item: dict) -> tuple:
    subject = _clean_text(item.get("subject_name"))
    hospital = _clean_text(item.get("hospital_name"))
    material = _clean_text(item.get("material_name"))
    date_text = _clean_text(item.get("record_date"))
    if date_text == "不详":
        date_text = _extract_first_cn_date(item.get("hospitalization_period", ""))
    record_number = _clean_text(item.get("record_number"))
    source_file = _clean_text(item.get("source_file"))

    base_key = (subject, hospital, material, date_text, record_number)
    if date_text == "不详" and record_number == "不详":
        return base_key + (source_file,)
    return base_key


def _merge_summary_item(base: dict, incoming: dict) -> dict:
    merged = dict(base)
    for field in [
        "subject_name",
        "record_date",
        "hospital_name",
        "material_name",
        "record_number",
        "hospitalization_period",
        "chief_complaint",
        "present_history",
        "imaging_findings",
        "physical_exam",
        "specialist_findings",
        "treatment_course",
        "post_op_review",
        "discharge_diagnosis",
        "treatment_advice",
        "source_file",
    ]:
        merged[field] = _pick_better_value(merged.get(field, "不详"), incoming.get(field, "不详"))

    merged["selection_reason"] = _merge_list_unique(
        merged.get("selection_reason", []) if isinstance(merged.get("selection_reason"), list) else [],
        incoming.get("selection_reason", []) if isinstance(incoming.get("selection_reason"), list) else [],
        max_items=8,
    )
    try:
        merged["selection_score"] = max(int(merged.get("selection_score") or 0), int(incoming.get("selection_score") or 0))
    except (TypeError, ValueError):
        pass

    return merged


def _summary_field_value(text: str, patterns: List[str], max_len: int = 220) -> str:
    candidates = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = _collapse_chinese_spaces(_safe_group(match, 1))
            if value != "不详":
                candidates.append(value[:max_len])
            if len(candidates) >= 8:
                break

    if not candidates:
        return "不详"

    candidates = sorted(candidates, key=_summary_text_quality, reverse=True)
    return candidates[0]


def _summary_item_score(item: dict) -> int:
    score = 0
    for key, value in (item or {}).items():
        if key in {"subject_name", "source_file", "material_name", "hospital_name"}:
            continue
        if _clean_text(value) != "不详":
            score += 1
    return score


def _summary_item_all_text(item: dict) -> str:
    return "\n".join(str(item.get(field, "")) for field in [
        "source_file", "material_name", "hospital_name", "hospitalization_period", "chief_complaint", "present_history",
        "imaging_findings", "physical_exam", "specialist_findings", "treatment_course", "post_op_review",
        "discharge_diagnosis", "treatment_advice",
    ])


def _summary_item_kind(item: dict) -> str:
    haystack = _summary_item_all_text(item)
    material = _clean_text(item.get("material_name"))
    if any(k in haystack for k in EXTERNAL_APPRAISAL_KEYWORDS):
        return "external"
    if "诊断证明" in haystack or ("证明" in material and re.search(r"经诊断|建议|内固定", haystack)):
        return "diagnosis_certificate"
    if any(k in haystack for k in ["住院病历", "住院病案", "住院号", "住院时间", "出院记录", "出院诊断", "手术记录"]):
        return "inpatient"
    if "急诊" in haystack or "入室" in haystack:
        return "emergency"
    if "门诊" in haystack or "就诊" in haystack:
        return "outpatient"
    if any(k in haystack for k in ["CT", "DR", "MRI", "MR", "磁共振", "X线", "影像", "报告单", "检查项目"]):
        return "imaging"
    return "medical"


def _summary_item_year(item: dict) -> Optional[int]:
    text = "\n".join([
        _clean_text(item.get("record_date")),
        _clean_text(item.get("hospitalization_period")),
        _summary_item_all_text(item),
    ])
    match = re.search(r"(20[0-9]{2})年", text)
    if not match:
        match = re.search(r"(20[0-9]{2})[-./]", text)
    if match:
        return int(match.group(1))
    return None


def _summary_item_date_digits(item: dict) -> str:
    for value in [item.get("record_date"), item.get("hospitalization_period")]:
        text = _clean_text(value)
        if text == "不详":
            continue
        match = re.search(r"(20[0-9]{2})年\s*([0-9]{1,2})月\s*([0-9]{1,2})日", text)
        if match:
            y, m, d = match.groups()
            return f"{int(y):04d}{int(m):02d}{int(d):02d}"
    return "99999999"


def _summary_target_year(items: List[dict], incident_date: str = "") -> Optional[int]:
    match = re.search(r"(20[0-9]{2})", incident_date or "")
    if match:
        return int(match.group(1))
    counts: Dict[int, int] = {}
    for item in items:
        year = _summary_item_year(item)
        if year is None:
            continue
        counts[year] = counts.get(year, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))[0][0]


def _is_imaging_only_summary_item(item: dict) -> bool:
    kind = _summary_item_kind(item)
    if kind != "imaging":
        return False
    clinical_fields = ["chief_complaint", "present_history", "physical_exam", "specialist_findings", "treatment_course", "discharge_diagnosis", "treatment_advice"]
    return not any(not _is_unresolved_value(item.get(field)) for field in clinical_fields)


def _is_key_imaging_summary_item(item: dict) -> bool:
    if _summary_item_kind(item) != "imaging":
        return False
    text = _summary_item_all_text(item)
    if re.search(r"未见明显外伤性改变|年度复查|纤维灶|结节灶|脂肪肝|囊肿|返流|骨密度|T值", text) and not re.search(r"骨折|脱位|压缩|内固定|损伤|创伤性关节炎|复查", text):
        return False
    return re.search(r"骨折|脱位|压缩|内固定|损伤|创伤性关节炎|骨折愈合|复查", text) is not None


# 病历类文档标题：用于核对“资料摘要”是否漏掉了送检病历。
# 只按“出现过的文档类型”计数，不按次数，避免同一份病历被反复提及而虚高。
MEDICAL_DOCUMENT_HEADING_RE = re.compile(
    r"(急诊出室小结|急诊留观病历|急诊病历|门诊病历|住院病历|出院记录|出院小结|"
    r"入院记录|诊断证明书|病情诊断证明书|手术记录|病程记录|入院证|出院证)"
)


def _detect_medical_document_headings(text: str) -> List[str]:
    """列出材料中出现过的病历类文档标题（去重，保持出现顺序）。"""
    found: List[str] = []
    for match in MEDICAL_DOCUMENT_HEADING_RE.finditer(text or ""):
        name = match.group(1)
        if name not in found:
            found.append(name)
    return found


def _summary_is_low_value_imaging_report(item: dict) -> bool:
    text = _summary_item_all_text(item)
    if _summary_item_kind(item) != "imaging":
        return False
    if re.search(r"未见明显外伤性改变|未见明显骨折|年度复查|纤维灶|结节灶|脂肪肝|囊肿|返流|骨密度|T值", text):
        return True
    clinical_signal = re.search(r"主诉|现病史|专科检查|诊疗经过|出院诊断|手术|内固定|牙折|牙脱位", text)
    return clinical_signal is None


def _summary_is_inpatient_fragment(item: dict) -> bool:
    if _summary_item_kind(item) != "inpatient":
        return False
    text = _summary_item_all_text(item)
    has_period = not _is_unresolved_value(item.get("hospitalization_period"))
    has_course = re.search(r"入院情况|诊疗经过|手术|出院诊断|出院医嘱|住院时间", text) is not None
    if has_period and has_course:
        return False
    if re.search(r"出院情况|切口|要求出院|带药|尿分析|检验报告|病假时间", text) and not has_course:
        return True
    useful_fields = sum(1 for field in ["chief_complaint", "present_history", "specialist_findings", "treatment_course", "discharge_diagnosis", "treatment_advice"] if not _is_unresolved_value(item.get(field)))
    return useful_fields <= 1 and not has_period


def _summary_item_priority_rank(item: dict) -> int:
    text = _summary_item_all_text(item)
    kind = _summary_item_kind(item)
    if kind == "inpatient" and re.search(r"住院时间|入院日期|出院日期|入院情况|诊疗经过|出院诊断|手术", text):
        return 1
    if _summary_item_date_digits(item) != "99999999" and re.search(r"主诉|现病史|疼痛|外伤|车祸|摔伤", text):
        return 2
    if re.search(r"牙折|牙脱位|口腔|颌面", text) and kind != "imaging":
        return 3
    if kind == "diagnosis_certificate" or re.search(r"内固定无需取出|无需取出|治疗终结|伤情稳定", text):
        return 4
    if kind == "outpatient" and re.search(r"MRI|MR|磁共振|胸椎|椎体压缩|创伤性关节炎", text):
        return 5
    return 9


def _summary_selection_score(item: dict, earliest_date: str, target_year: Optional[int]) -> tuple[int, List[str]]:
    kind = _summary_item_kind(item)
    text = _summary_item_all_text(item)
    year = _summary_item_year(item)
    if kind == "external" or ((_is_imaging_only_summary_item(item) and not _is_key_imaging_summary_item(item)) or _summary_is_low_value_imaging_report(item)):
        return -999, []
    if _summary_is_inpatient_fragment(item):
        return -90, []
    if any(k in text for k in SUMMARY_LEGAL_NOISE_KEYWORDS):
        return -120, []
    if target_year and year and (year < target_year or year > target_year + 3):
        return -120, []
    if any(k in text for k in ["开药", "中药", "温服", "甘草", "当归", "运动疗法", "熏蒸"]):
        if not re.search(r"内固定无需取出|牙列缺损|牙折|牙脱位|MRI|MR|磁共振|胸|腰|椎体|创伤性关节炎", text):
            return -60, []

    score = 0
    reasons: List[str] = []
    date_digits = _summary_item_date_digits(item)
    same_initial_day = date_digits != "99999999" and earliest_date != "99999999" and date_digits == earliest_date

    if kind in {"emergency", "outpatient", "medical"} and same_initial_day and re.search(r"主诉|现病史|疼痛|受伤|车祸|外伤|摔伤", text):
        if any(k in text for k in ["入院时间", "住院号", "供史者", "可靠程度", "为求进一步诊治", "收入病房"]):
            score += 24
        else:
            score += 60
            reasons.append("首诊/同日诊疗记录")
    if kind == "emergency":
        score += 18
        if "首诊/同日诊疗记录" not in reasons:
            reasons.append("急诊记录")
    if kind == "inpatient":
        if re.search(r"入院情况|诊疗经过|出院诊断|手术|住院时间|入院日期|出院日期", text):
            score += 76
            reasons.append("主要住院/治疗记录")
        else:
            score += 30
    if kind == "diagnosis_certificate":
        score += 62
        reasons.append("诊断证明/鉴定时机材料")

    if re.search(r"手术名称|手术记录|切开复位|内固定术|术中见|麻醉方式|神经丛阻滞", text):
        score += 30
        if "主要住院/治疗记录" not in reasons:
            reasons.append("包含手术或关键治疗")
    if re.search(r"牙列缺损要求临时修复|普通树脂牙修复|隐形义齿", text):
        return -40, []
    if re.search(r"牙折|牙脱位|口腔|颌面", text):
        score += 45
        reasons.append("覆盖口腔/牙损伤专科")
    if kind in {"outpatient", "imaging", "medical"} and re.search(r"MRI|MR|磁共振|CT|DR|X线|内固定无需取出|无需取出|胸椎|腰椎|创伤性关节炎|复查", text):
        score += 32
        reasons.append("后续复诊/影像补充关键事实")
    if re.search(r"内固定无需取出|无需取出|骨折愈合|伤情稳定|治疗终结|继续康复|关节活动", text):
        score += 28
        if not any("鉴定时机" in r for r in reasons):
            reasons.append("影响恢复状态/后续治疗")
    if kind == "outpatient" and (not same_initial_day) and re.search(r"术后|复查|功能锻炼|开药|中药|治疗意见", text):
        if not re.search(r"MRI|MR|磁共振|内固定无需取出|无需取出|牙折|牙脱位|新诊断|创伤性关节炎|胸椎|椎体压缩", text):
            score -= 38

    for field, add in [
        ("hospitalization_period", 10), ("chief_complaint", 8), ("present_history", 8), ("physical_exam", 6),
        ("specialist_findings", 8), ("treatment_course", 10), ("post_op_review", 6), ("discharge_diagnosis", 12),
        ("treatment_advice", 8),
    ]:
        if not _is_unresolved_value(item.get(field)):
            score += add
    if not _is_unresolved_value(item.get("imaging_findings")) and kind != "imaging":
        score += 5
    if target_year and year == target_year:
        score += 6

    return score, _merge_list_unique([], reasons, max_items=6)


def _summary_selection_key(item: dict) -> tuple:
    kind = _summary_item_kind(item)
    hospital = _clean_text(item.get("hospital_name"))
    record_number = _clean_text(item.get("record_number"))
    date_text = _clean_text(item.get("record_date"))
    if kind == "inpatient":
        period = _clean_text(item.get("hospitalization_period"))
        return (kind, hospital, record_number if record_number != "不详" else period, period if period != "不详" else "")
    if kind == "diagnosis_certificate":
        return (kind, hospital, date_text, _clean_text(item.get("discharge_diagnosis")))
    complaint = _clean_text(item.get("chief_complaint"))[:40]
    return (kind, hospital, date_text, record_number, complaint)


def _select_key_medical_summary_items(items: List[dict], incident_date: str = "") -> tuple[List[dict], List[dict]]:
    normalized = [item for item in items if isinstance(item, dict) and _summary_item_has_usable_content(item)]
    target_year = _summary_target_year(normalized, incident_date)
    date_candidates = []
    for item in normalized:
        if _summary_item_kind(item) == "external" or (_is_imaging_only_summary_item(item) and not _is_key_imaging_summary_item(item)):
            continue
        year = _summary_item_year(item)
        if target_year and year and year != target_year:
            continue
        digits = _summary_item_date_digits(item)
        if digits != "99999999":
            date_candidates.append(digits)
    earliest_date = min(date_candidates) if date_candidates else "99999999"

    scored: List[tuple[int, List[str], dict]] = []
    audit: List[dict] = []
    for item in normalized:
        score, reasons = _summary_selection_score(item, earliest_date, target_year)
        decision = "selected" if score >= 58 and reasons else "skipped"
        audit.append({
            "decision": decision,
            "score": score,
            "reasons": reasons,
            "hospital_name": _clean_text(item.get("hospital_name")),
            "material_name": _clean_text(item.get("material_name")),
            "record_date": _clean_text(item.get("record_date")),
            "record_number": _clean_text(item.get("record_number")),
            "source_file": _clean_text(item.get("source_file")),
        })
        if decision == "selected":
            row = dict(item)
            row["selection_reason"] = reasons
            row["selection_score"] = score
            scored.append((score, reasons, row))

    grouped: Dict[tuple, dict] = {}
    grouped_scores: Dict[tuple, int] = {}
    for score, _reasons, item in scored:
        key = _summary_selection_key(item)
        old = grouped.get(key)
        if old is None:
            grouped[key] = item
            grouped_scores[key] = score
        else:
            merged = _merge_summary_item(old, item)
            merged["selection_reason"] = _merge_list_unique(old.get("selection_reason", []), item.get("selection_reason", []), max_items=8)
            merged["selection_score"] = max(grouped_scores.get(key, 0), score)
            grouped[key] = merged
            grouped_scores[key] = max(grouped_scores.get(key, 0), score)

    selected = list(grouped.values())

    # 专家报告强调证据链覆盖，不按固定条数截断：首诊/主住院/手术/不同专科/鉴定时机节点优先。
    top_by_category: List[dict] = []
    buckets = {
        "initial": lambda x: _summary_item_date_digits(x) == earliest_date and _summary_item_kind(x) in {"emergency", "outpatient", "medical"},
        "inpatient": lambda x: _summary_item_kind(x) == "inpatient" and not re.search(r"手术名称|手术经过|术中见", _summary_item_all_text(x)),
        "surgery": lambda x: re.search(r"手术名称|手术经过|术中见", _summary_item_all_text(x)) is not None,
        "dental": lambda x: re.search(r"牙折|牙脱位|口腔|颌面", _summary_item_all_text(x)) is not None,
        "timing": lambda x: re.search(r"内固定无需取出|无需取出|诊断证明|治疗终结|伤情稳定", _summary_item_all_text(x)) is not None,
        "key_followup": lambda x: re.search(r"MRI|MR|磁共振|CT|DR|X线|胸椎|腰椎|创伤性关节炎|复查", _summary_item_all_text(x)) is not None,
        "diagnosis_certificate": lambda x: _summary_item_kind(x) == "diagnosis_certificate" or re.search(r"病情证明|诊断证明|治疗终结|伤情稳定", _summary_item_all_text(x)) is not None,
    }
    for bucket in buckets.values():
        candidates = [row for row in selected if bucket(row)]
        if not candidates:
            continue
        candidates.sort(key=lambda x: (-int(x.get("selection_score") or 0), _summary_item_date_digits(x)))
        chosen = candidates[0]
        if chosen not in top_by_category:
            top_by_category.append(chosen)

    selected.sort(key=lambda x: (_summary_item_priority_rank(x), -int(x.get("selection_score") or 0), _summary_item_date_digits(x)))
    for row in selected:
        if row not in top_by_category:
            top_by_category.append(row)
        if len(top_by_category) >= 10:
            break
    selected = top_by_category[:10]
    selected.sort(key=lambda x: (_summary_item_date_digits(x), _summary_item_priority_rank(x)))
    return selected, audit


def _extract_summary_record_number(text: str) -> str:
    content = text or ""
    preferred = re.search(
        r"(?:病案号|病历号|住院号|门诊号|就诊卡号|就诊号|住院流水号)[:：]?\s*([A-Za-z0-9\-]{3,40})",
        content,
        re.I,
    )
    if preferred:
        return _safe_group(preferred, 1)

    # 第三部分病历来源优先使用病案/病历/住院/就诊卡号；纯影像号只在影像检查报告本身作为来源时使用。
    if _guess_summary_material_name("", content) == "影像检查报告":
        match = MATERIAL_ID_PATTERN.search(content)
        if match:
            return _safe_group(match, 1)
    return "不详"


def _normalize_summary_date_text(date_text: str) -> str:
    value = _clean_text(date_text)
    if value == "不详":
        return "不详"
    match = re.search(r"([0-9]{4})年\s*([0-9]{1,2})月\s*([0-9]{1,2})日", value)
    if match:
        y, m, d = match.groups()
        return f"{int(y):04d}年{int(m):02d}月{int(d):02d}日"
    match = re.search(r"([0-9]{4})[-./]([0-9]{1,2})[-./]([0-9]{1,2})", value)
    if match:
        y, m, d = match.groups()
        return f"{int(y):04d}年{int(m):02d}月{int(d):02d}日"
    return value


def _extract_summary_record_date(text: str, material_name: str = "") -> str:
    content = text or ""
    date_pattern = r"([0-9]{4}年\s*[0-9]{1,2}月\s*[0-9]{1,2}日|[0-9]{4}[-./][0-9]{1,2}[-./][0-9]{1,2})"

    # 病历文书日期必须来自就诊/入室/入院/出院/记录/证明等业务标签，避免身份证出生日期误入资料摘要。
    label_patterns = [
        rf"(?:就诊时间|就诊日期|门诊时间|门诊日期|入室时间|入室日期|急诊时间|急诊日期)[:：]?\s*{date_pattern}",
        rf"(?:住院时间|入院时间|入院日期|出院时间|出院日期|记录时间|记录日期|诊断日期)[:：]?\s*{date_pattern}",
        rf"(?:曾于|检查时间|检查日期|报告日期|证明日期|时间)[:：]?\s*{date_pattern}",
    ]
    for pattern in label_patterns:
        match = re.search(pattern, content)
        if match:
            return _normalize_summary_date_text(match.group(1))

    if "诊断证明" in material_name:
        match = re.search(rf"曾于[:：]?\s*{date_pattern}", content)
        if match:
            return _normalize_summary_date_text(match.group(1))

    # 兜底时排除个人身份信息、委托/诉讼程序日期等低相关上下文。
    for match in re.finditer(date_pattern, content):
        start = max(0, match.start() - 16)
        end = min(len(content), match.end() + 16)
        ctx = content[start:end]
        if any(k in ctx for k in ["出生", "身份证", "公民身份", "有效期限", "委托日期", "受理日期", "鉴定开始日期", "具状", "起诉"]):
            continue
        return _normalize_summary_date_text(match.group(1))
    return "不详"


def _summary_source_group_name(filename: str) -> str:
    base = os.path.splitext(filename or "")[0]
    base = re.sub(r"[\s_\-]*(?:第)?[0-9一二三四五六七八九十]+$", "", base)
    return base or filename or "外部鉴定意见"


def _extract_external_document_no(text: str) -> str:
    content = _collapse_chinese_spaces(text or "")
    patterns = [
        r"([一-龥]{0,8}司鉴[〔（(]?[0-9]{4}[〕）)]?[^，。\n]{0,40}第\s*[0-9]+\s*号?)",
        r"([一-龥]{1,10}司鉴[^，。\n]{0,60}第\s*[0-9]+)",
        r"(嘉大司鉴[^，。\n]{0,40}第\s*[0-9]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            return _safe_group(match, 1).strip("，。；; ")
    return "不详"


def _extract_external_opinion_text(text: str) -> str:
    content = _collapse_chinese_spaces(text or "")
    match = re.search(r"六、鉴定意见\s*(.*?)(?:七、附件|司法鉴定人|$)", content, re.S)
    if not match:
        match = re.search(r"鉴定意见[:：]?\s*(.*?)(?:七、附件|司法鉴定人|$)", content, re.S)
    if match:
        value = _collapse_chinese_spaces(_safe_group(match, 1)).strip("：:，。；; ")
    else:
        lines = []
        for raw in (text or "").splitlines():
            line = _collapse_chinese_spaces(raw)
            if any(k in line for k in ["评定为", "构成", "鉴定意见", "误工期限", "护理期限", "营养期限"]):
                lines.append(line.strip("，。；; "))
        value = "。".join(lines)
    value = re.sub(r"\s+", "", value)
    return value[:520] if value else "不详"


def _extract_other_summary_items(blocks: List[dict]) -> List[dict]:
    items: List[dict] = []
    seen_keys = set()
    for block in blocks:
        filename = block.get("filename", "")
        content = block.get("content", "")
        if not _is_external_summary_block(filename, content):
            continue
        institution_match = re.search(r"([一-龥]{2,30}(?:司法鉴定所|司法鉴定中心))", content)
        entrust_match = re.search(r"委托人[:：]\s*([^\n，。；;]{2,30})", content)
        date_text = "不详"
        for pattern in [
            r"(?:出具|签发|落款|鉴定意见书)[:：]?\s*([0-9]{4}年\s*[0-9]{1,2}月\s*[0-9]{1,2}日)",
            r"(?:委托日期|受理日期|鉴定开始日期)[:：]?\s*([0-9]{4}年\s*[0-9]{1,2}月\s*[0-9]{1,2}日)",
        ]:
            m = re.search(pattern, content)
            if m:
                date_text = _normalize_summary_date_text(m.group(1))
                break
        opinion = _extract_external_opinion_text(content)
        if _is_unresolved_value(opinion):
            continue
        institution = _safe_group(institution_match, 1)
        document_no = _extract_external_document_no(content)
        key_source = "|".join([institution, document_no, date_text, opinion[:80] if opinion != "不详" else _summary_source_group_name(filename)])
        key = re.sub(r"\s+", "", key_source)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        items.append({
            "summary_type": "外部鉴定意见",
            "institution": institution,
            "document_name": "司法鉴定意见书",
            "document_no": document_no,
            "document_date": date_text,
            "entrusting_party": _safe_group(entrust_match, 1),
            "opinion_text": opinion,
            "source_files": [filename] if filename else [],
        })
    return items[:6]

def _extract_hospitalization_period(text: str) -> str:
    def _dash_to_cn(date_text: str) -> str:
        match = re.match(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})", date_text)
        if not match:
            return date_text
        y, m, d = match.groups()
        return f"{int(y)}年{int(m)}月{int(d)}日"

    date_pattern = r"[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日"
    direct = re.search(
        rf"住院时间[:：]?\s*({date_pattern})\s*(?:至|到|\-|~)\s*({date_pattern})(?:[，,]\s*住院\s*([0-9]{{1,3}})\s*天)?",
        text,
    )
    if direct:
        start_date = _safe_group(direct, 1)
        end_date = _safe_group(direct, 2)
        days = _safe_group(direct, 3)
        if days != "不详":
            return f"{start_date}至{end_date}，住院{days}天"
        return f"{start_date}至{end_date}"

    direct_dash = re.search(
        r"([0-9]{4}-[0-9]{1,2}-[0-9]{1,2})(?:(?:[ T,，]\s*)?[0-9]{1,2}:[0-9]{2})?\s*(?:至|到|\-|~)\s*([0-9]{4}-[0-9]{1,2}-[0-9]{1,2})",
        text,
    )
    if direct_dash:
        start_date = _dash_to_cn(_safe_group(direct_dash, 1))
        end_date = _dash_to_cn(_safe_group(direct_dash, 2))
        days = _safe_group(re.search(r"住院\s*([0-9]{1,3})\s*天", text), 1)
        if days != "不详":
            return f"{start_date}至{end_date}，住院{days}天"
        return f"{start_date}至{end_date}"

    admission = re.search(rf"(?:入院时间|入院日期|入院)[:：]?\s*({date_pattern})", text)
    discharge = re.search(rf"(?:出院时间|出院日期|出院)[:：]?\s*({date_pattern})", text)
    days_match = re.search(r"住院\s*([0-9]{1,3})\s*天", text)

    if admission and discharge:
        start_date = _safe_group(admission, 1)
        end_date = _safe_group(discharge, 1)
        days = _safe_group(days_match, 1)
        if days != "不详":
            return f"{start_date}至{end_date}，住院{days}天"
        return f"{start_date}至{end_date}"
    return "不详"


def _extract_hospital_name(filename: str, text: str) -> str:
    haystack = f"{filename}\n{text}"
    direct = re.search(r"([\u4e00-\u9fa5]{2,}(?:大学|学院|附属|省|市|区|县)?[\u4e00-\u9fa5]{0,20}医院)", haystack)
    value = _safe_group(direct, 1)
    if value != "不详":
        if any(noise in value for noise in ["送到医院", "上级医院", "相关医院", "转至医院", "转院医院"]):
            value = "不详"
        else:
            return value
    if "浙大二院" in haystack:
        return "浙江大学医学院附属第二医院"
    return "不详"


def _extract_single_imaging_method(text: str) -> str:
    haystack = text or ""
    rules = [
        ("CT", r"CT|ＣＴ|ct"),
        ("DR", r"\bDR\b|dr|数字化摄影"),
        ("MRI", r"MRI|核磁|磁共振"),
        ("X线", r"X线|x线|X-ray"),
        ("超声", r"超声|B超"),
    ]
    for method, pattern in rules:
        if re.search(pattern, haystack):
            return method
    return "不详"


def _split_summary_content_sections(content: str) -> List[str]:
    text = content or ""
    if not text.strip():
        return []

    # 住院记录/出院记录经常把入院情况、诊疗经过、辅助检查、出院诊断分布在同一页；
    # 这里不按字段标签硬切，避免把一个完整住院事实链拆成“中医诊断/出院情况/检验报告”等碎片。
    if re.search(r"住院号|入院日期|入院时间|出院日期|出院时间", text) and re.search(r"入院情况|诊疗经过|出院诊断|出院医嘱|手术", text):
        return [text]

    marker_pattern = re.compile(
        r"(?=(?:病案号|病历号|住院号|就诊卡号|姓名[:：]|病人姓名[:：]|入室日期|入室时间|就诊时间|就诊日期|门诊时间|检查时间|记录时间|记录日期|曾于|经诊断|住院时间|入院时间|出院日期|急诊病历|急诊出室小结|门诊病历|住院病历|出院记录|入院记录|诊断证明书|诊断证明|影像所见|影像诊断|检查项目|手术名称|专科检查|专科情况)[:：]?)"
    )
    markers = [m.start() for m in marker_pattern.finditer(text)]
    markers = sorted(set([idx for idx in markers if idx >= 0]))
    if len(markers) < 2:
        return [text]

    sections = []
    if markers[0] > 0:
        prefix = text[:markers[0]].strip()
        if prefix and any(k in prefix for k in ["急诊", "门诊", "主诉", "体格检查", "诊断", "影像"]):
            sections.append(prefix)

    for idx, start in enumerate(markers):
        end = markers[idx + 1] if idx + 1 < len(markers) else len(text)
        section = text[start:end].strip()
        if len(section) < 20 and sections:
            sections[-1] = (sections[-1] + "\n" + section).strip()
            continue
        if section:
            sections.append(section)

    merged = []
    for section in sections:
        if merged and len(section) < 80 and not any(k in section for k in ["主诉", "现病史", "体格检查", "诊断", "影像所见", "检查项目"]):
            merged[-1] = (merged[-1] + "\n" + section).strip()
        else:
            merged.append(section)
    return merged if merged else [text]


def _guess_summary_material_name(filename: str, content: str) -> str:
    haystack = f"{filename}\n{content}"
    filename_text = filename or ""
    if "诊断证明" in filename_text or "诊断证明" in haystack or (re.search(r"(?:病人姓名|曾于|经诊断)[:：]", haystack) and "住院号" not in haystack) or ("休假" in haystack and "建议" in haystack and "门诊" in haystack and "住院号" not in haystack):
        return "诊断证明书"
    if "出院记录" in haystack:
        return "出院记录"
    if "入院记录" in haystack:
        return "入院记录"
    if any(k in haystack for k in ["住院病案", "住院病历", "住院号", "住院时间", "入院时间", "出院日期", "入院诊断", "出院诊断", "诊疗经过", "手术名称"]):
        return "住院病历"
    if "急诊出室小结" in haystack or "出室小结" in haystack:
        return "急诊出室小结"
    if "急诊" in haystack or "入室日期" in haystack or "出室日期" in haystack or "入室时间" in haystack:
        return "急诊病历"
    if "门诊" in haystack or "就诊时间" in haystack or "就诊日期" in haystack:
        return "门诊病历"
    if any(k in haystack for k in ["CT", "DR", "MRI", "MR", "磁共振", "X线", "影像", "报告单", "检查项目"]):
        return "影像检查报告"

    base_name = os.path.splitext(filename)[0].strip()
    return base_name if base_name else "医疗资料"


def _post_process_basic_summary(payload: dict) -> dict:
    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    dedupe = {}
    fingerprint_seen = set()

    for item in raw_items:
        row = _normalize_value(item if isinstance(item, dict) else {})
        for field in SUMMARY_FIELDS:
            row[field] = _sanitize_summary_field(field, row.get(field, "不详"))

        if _clean_text(row.get("record_date")) == "不详":
            row["record_date"] = _extract_first_cn_date(
                "\n".join([
                    row.get("hospitalization_period", ""),
                    row.get("chief_complaint", ""),
                    row.get("present_history", ""),
                    row.get("treatment_course", ""),
                ])
            )

        key = _summary_item_key(row)
        old_item = dedupe.get(key)
        if old_item is None:
            dedupe[key] = row
        else:
            dedupe[key] = _merge_summary_item(old_item, row)

    normalized_items = []
    for item in dedupe.values():
        fingerprint = (
            _clean_text(item.get("hospital_name")),
            _clean_text(item.get("material_name")),
            _clean_text(item.get("record_date")),
            _clean_text(item.get("record_number")),
            _clean_text(item.get("chief_complaint")),
            _clean_text(item.get("present_history")),
            _clean_text(item.get("imaging_findings")),
            _clean_text(item.get("post_op_review")),
            _clean_text(item.get("discharge_diagnosis")),
        )
        if fingerprint in fingerprint_seen:
            continue
        fingerprint_seen.add(fingerprint)
        normalized_items.append(item)

    normalized_items.sort(key=lambda x: _date_sort_key({"visit_date": x.get("record_date", "不详")}))
    normalized_items.sort(key=_summary_item_score, reverse=True)
    normalized_items.sort(key=lambda x: _date_sort_key({"visit_date": x.get("record_date", "不详")}))

    payload["items"] = normalized_items[:18]
    if isinstance(payload.get("raw_medical_summary_items"), list):
        raw_normalized = []
        for raw_item in payload.get("raw_medical_summary_items", [])[:40]:
            row = _normalize_value(raw_item if isinstance(raw_item, dict) else {})
            for field in SUMMARY_FIELDS:
                row[field] = _sanitize_summary_field(field, row.get(field, "不详"))
            if _summary_item_has_usable_content(row):
                raw_normalized.append(row)
        payload["raw_medical_summary_items"] = raw_normalized
    if isinstance(payload.get("other_summary_items"), list):
        payload["other_summary_items"] = payload.get("other_summary_items", [])[:6]
    if isinstance(payload.get("summary_selection_audit"), list):
        payload["summary_selection_audit"] = payload.get("summary_selection_audit", [])[:40]
    payload["evidence_snippets"] = _merge_list_unique([], payload.get("evidence_snippets", []), max_items=16)
    payload["uncertain_points"] = _merge_list_unique([], payload.get("uncertain_points", []), max_items=10)
    return payload


def _rule_based_summary_extraction(markdown_text: str) -> dict:
    payload = BasicSummaryInfo().model_dump()
    blocks = _split_file_blocks(markdown_text)
    if not blocks:
        blocks = [{"filename": "整篇输入", "content": markdown_text}]

    candidate_blocks = [
        b for b in blocks
        if _is_summary_candidate_block(b.get("filename", ""), b.get("content", ""))
    ]
    if not candidate_blocks:
        candidate_blocks = blocks

    subject_match = re.search(r"(?:原告|被鉴定人|病人姓名|姓名)[:：]\s*([\u4e00-\u9fa5]{2,4})", markdown_text)
    subject_name = _safe_group(subject_match, 1)
    other_summary_items = _extract_other_summary_items(blocks)

    selected_blocks = []
    summary_keywords = _flatten_summary_keywords()

    for block in candidate_blocks:
        filename = block.get("filename", "")
        content = block.get("content", "")
        if _is_external_summary_block(filename, content):
            continue
        if _contains_any_keyword(f"{filename}\n{content}", summary_keywords):
            selected_blocks.append(block)

    if not selected_blocks:
        selected_blocks = [
            b for b in candidate_blocks
            if not _is_external_summary_block(b.get("filename", ""), b.get("content", ""))
        ][:20]

    items = []
    evidence = []
    for block in selected_blocks:
        filename = block.get("filename", "")
        content = block.get("content", "")
        if _is_external_summary_block(filename, content):
            continue
        sections = _split_summary_content_sections(content)
        if not sections:
            sections = [content]

        for section in sections:
            item = MaterialSummaryItem().model_dump()

            item["subject_name"] = subject_name
            item["source_file"] = filename if filename else "不详"
            item["hospital_name"] = _extract_hospital_name(filename, section)
            item["material_name"] = _guess_summary_material_name(filename, section)
            if item["hospital_name"] == "不详" and re.search(r"海盐县中医院", content):
                item["hospital_name"] = "海盐县中医院"

            item["record_date"] = _extract_summary_record_date(section, item["material_name"])
            item["record_number"] = _extract_summary_record_number(section)
            item["hospitalization_period"] = _extract_hospitalization_period(section)

            item["chief_complaint"] = _summary_field_value(section, [
                r"主诉[:：]\s*([^\n]+)",
                r"入室情况[:：]\s*患者因[“\"]?([^”\"。\n]+)[”\"]?入室",
            ])
            item["present_history"] = _summary_field_value(section, [r"(?:现病史|先病史)[:：]\s*([^\n]+)"])
            imaging_value = _summary_field_value(
                section,
                [
                    r"(?:CT|DR|MRI|X线|影像(?:学)?检查?)(?:[^\n]{0,12})[:：]\s*([^\n]+)",
                    r"(?:影像(?:学)?(?:提示|示))[:：]\s*([^\n]+)",
                ],
            )
            if _clean_text(imaging_value) != "不详":
                method = _extract_single_imaging_method(section)
                if method != "不详" and (not re.search(r"^(CT|DR|MRI|X线|超声)[示:：]", imaging_value)):
                    imaging_value = f"{method}示：{imaging_value}"
            item["imaging_findings"] = imaging_value
            item["physical_exam"] = _summary_field_value(section, [
                r"(?:体格检查|查体|体检)[:：]\s*([^\n]+)",
                r"入室情况[:：][^\n。]*入室。?查体[:：]?\s*([^\n]+)",
            ])
            item["specialist_findings"] = _summary_field_value(section, [r"(?:专科检查|专科情况|专科查体)[:：]\s*([^\n]+)"])
            item["treatment_course"] = _summary_field_value(section, [
                r"(?:诊疗经过|治疗经过|住院经过|处理内容|处理)[:：]\s*([^\n]+)",
                r"(?:手术名称|手术记录)[:：]\s*([^\n]+)",
            ])
            item["post_op_review"] = _summary_field_value(
                section,
                [
                    r"(?:术后复查(?:拍片)?(?:显示|示)?)[:：]?\s*([^\n]+)",
                    r"(?:复查(?:拍片)?(?:显示|示)?)[:：]?\s*([^\n]+)",
                ],
            )
            diagnosis_patterns = [
                r"(?:出室诊断|入室诊断|出院诊断|出院情况|诊断结果)[:：]\s*([^\n]+)",
                r"西医诊断[:：]\s*([\s\S]{0,260}?)(?=\n\s*(?:入院情况|诊疗经过|辅助检查|实验室检查|出院医嘱|医嘱|中医诊断|$))",
                r"(?:诊断|初步诊断)[:：]\s*([^\n]+)",
            ]
            item["discharge_diagnosis"] = _summary_clean_diagnosis_text(_summary_field_value(section, diagnosis_patterns))
            item["treatment_advice"] = _summary_field_value(section, [r"(?:处理意见|出院医嘱|医嘱|注意事项|建议与注意事项|治疗建议)[:：]\s*([^\n]+)"])

            details = [
                item.get("hospitalization_period"),
                item.get("chief_complaint"),
                item.get("present_history"),
                item.get("imaging_findings"),
                item.get("physical_exam"),
                item.get("specialist_findings"),
                item.get("treatment_course"),
                item.get("post_op_review"),
                item.get("discharge_diagnosis"),
                item.get("treatment_advice"),
            ]
            if _summary_is_low_value_imaging_report(item) or _summary_is_inpatient_fragment(item):
                continue
            if any(_clean_text(v) != "不详" for v in details):
                items.append(item)

        for line in content.splitlines():
            line_text = line.strip()
            if not line_text:
                continue
            if any(k in line_text for k in ["主诉", "现病史", "体格检查", "专科", "诊疗经过", "出院诊断", "医嘱", "CT", "DR", "MRI"]):
                evidence.append(line_text)
            if len(evidence) >= 16:
                break

    selected_items, selection_audit = _select_key_medical_summary_items(items, "")
    payload["detected_medical_documents"] = _detect_medical_document_headings(markdown_text)
    payload["raw_medical_summary_items"] = items
    payload["items"] = selected_items if selected_items else items[:8]
    payload["other_summary_items"] = other_summary_items
    payload["summary_selection_audit"] = selection_audit
    payload["evidence_snippets"] = _merge_list_unique([], evidence, max_items=16)

    uncertain = []
    if not items:
        uncertain.append("未检索到可直接用于资料摘要成文的医疗材料字段，建议核对门急诊/住院病历原文。")
    if _clean_text(subject_name) == "不详":
        uncertain.append("未明确识别被鉴定人姓名，建议核对身份证明或起诉状。")
    payload["uncertain_points"] = uncertain

    return _post_process_basic_summary(_normalize_value(payload))


def _merge_basic_summary_rule_llm(rule_payload: dict, llm_payload: Optional[dict]) -> dict:
    merged = dict(rule_payload)
    llm_contributed = False

    rule_items = [item for item in rule_payload.get("items", []) if isinstance(item, dict)]
    llm_items = [item for item in (llm_payload.get("items", []) if llm_payload else []) if isinstance(item, dict)]

    combined: List[dict] = []
    keyed: Dict[tuple, int] = {}
    for source, item_list in [("rule", rule_items), ("llm", llm_items)]:
        for item in item_list:
            if not _summary_item_has_usable_content(item):
                continue
            normalized = _normalize_value(item)
            key = _summary_item_key(normalized)
            if key in keyed:
                combined[keyed[key]] = _merge_summary_item(combined[keyed[key]], normalized)
                if source == "llm":
                    llm_contributed = True
            else:
                keyed[key] = len(combined)
                combined.append(normalized)
                if source == "llm":
                    llm_contributed = True

    if combined:
        merged["items"] = combined
    else:
        placeholder_item = MaterialSummaryItem().model_dump()
        for key in placeholder_item.keys():
            placeholder_item[key] = HYBRID_PENDING_TEXT
        merged["items"] = [placeholder_item]

    merged["evidence_snippets"], used_llm_for_evidence = _first_usable_list(
        rule_payload.get("evidence_snippets", []),
        llm_payload.get("evidence_snippets", []) if llm_payload else [],
        max_items=16,
        fallback_pending=True,
    )
    llm_contributed = llm_contributed or used_llm_for_evidence

    merged["uncertain_points"] = _merge_list_unique(
        _filter_usable_str_list(rule_payload.get("uncertain_points", []), max_items=10),
        _filter_usable_str_list(llm_payload.get("uncertain_points", []), max_items=10) if llm_payload else [],
        max_items=10,
    )

    if not _summary_items_have_usable_content(rule_items) and (not _summary_items_have_usable_content(llm_items)):
        merged["uncertain_points"] = _merge_list_unique(
            merged.get("uncertain_points", []),
            ["资料摘要未检索到可用字段，需人工补充。"],
            max_items=10,
        )

    merged = _post_process_basic_summary(_normalize_value(merged))
    merged["_summary_source_mode"] = "hybrid" if llm_contributed else "rules"
    return merged


def extract_basic_summary_chunked(
    markdown_text: str,
    request_timeout_seconds: int = 90,
    max_attempts: int = 2,
    chunk_max_chars: int = 32000,
) -> dict:
    """按"资料摘要"需求定向检索并抽取可直接成文的条目列表。"""

    config = get_runtime_config()
    if config.llm_provider == "off":
        raise RuntimeError("当前模型调用模式为 off。")

    blocks = _split_file_blocks(markdown_text)
    if not blocks:
        blocks = [{"filename": "整篇输入", "content": markdown_text}]

    candidate_blocks = [
        b for b in blocks
        if _is_summary_candidate_block(b.get("filename", ""), b.get("content", ""))
    ]
    if not candidate_blocks:
        candidate_blocks = blocks

    per_field_max_chars = max(2200, min(6000, max(7000, chunk_max_chars) // 5))
    contexts, stats = _build_summary_requirement_contexts(candidate_blocks, field_max_chars=per_field_max_chars)
    effective_timeout = max(25, min(60, request_timeout_seconds))

    llm = build_chat_llm(
        config,
        temperature=0.0,
        timeout=effective_timeout,
        max_retries=0,
        max_tokens=1400,
    )

    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            append_local_prompt_if_needed("""你是一位严谨的法医临床鉴定助理。请输出"第三部分 资料摘要"。

要求：
1. 仅依据材料，不得补造事实；找不到就填"不详"或留空字段。
2. 按专家报告摘录关键病史节点：首诊/急诊、主要住院链、手术或治疗经过、出院诊断/医嘱、关键复诊影像、诊断证明/伤情稳定材料都应尽量覆盖；同名材料但不同时间必须分开。
3. 参考模板字段：日期、医院、材料名、病案/门诊/住院/检查号、住院时间、主诉、现病史、CT/影像显示、体格检查、专科情况、诊疗经过、术后复查拍片显示、出院诊断、处理意见。
4. 不要把多个时间点混入同一条；record_date 有则填，没有可不详。
5. evidence_snippets 最多 16 条，uncertain_points 最多 10 条。
6. 仅输出 JSON。
{format_instructions}
""", config, "summary"),
        ),
        (
            "human",
            """【资料基础上下文】
{ctx_record_basic}

【住院时段与编号上下文】
{ctx_hospitalization}

【临床描述上下文】
{ctx_clinical}

【影像资料上下文】
{ctx_imaging}

【诊断与处理上下文】
{ctx_diagnosis}

请输出第三部分"资料摘要"的结构化条目。""",
        ),
    ])

    chain = prompt | llm | basic_summary_parser

    payload_input = {
        "format_instructions": basic_summary_parser.get_format_instructions(),
        "ctx_record_basic": contexts.get("record_basic", ""),
        "ctx_hospitalization": contexts.get("hospitalization", ""),
        "ctx_clinical": contexts.get("clinical", ""),
        "ctx_imaging": contexts.get("imaging", ""),
        "ctx_diagnosis": contexts.get("diagnosis", ""),
    }

    print("[*] 准备进行第三部分「资料摘要」定向抽取（按入院/住院/临床/影像/诊断分组）")
    result = _invoke_chain_with_retry(
        chain=chain,
        payload_input=payload_input,
        request_timeout_seconds=effective_timeout,
        max_attempts=max_attempts,
        call_name="资料摘要定向抽取",
    )

    if result is None:
        return None

    payload = _normalize_value(result.model_dump())
    payload = _post_process_basic_summary(payload)
    payload["_parse_stats"] = stats
    return payload


def extract_basic_summary_hybrid(
    markdown_text: str,
    request_timeout_seconds: int = 90,
    max_attempts: int = 2,
    chunk_max_chars: int = 32000,
    enable_llm: bool = True,
) -> dict:
    """第三部分采用规则优先；仅规则缺失时才调用 LLM 补全。"""

    blocks = _split_file_blocks(markdown_text)
    if not blocks:
        blocks = [{"filename": "整篇输入", "content": markdown_text}]

    candidate_blocks = [
        b for b in blocks
        if _is_summary_candidate_block(b.get("filename", ""), b.get("content", ""))
    ]
    if not candidate_blocks:
        candidate_blocks = blocks

    rule_payload = _rule_based_summary_extraction(markdown_text)
    llm_payload = None
    llm_stats = {}
    llm_error = None
    llm_attempted = False

    need_llm = enable_llm and _summary_rule_needs_llm(rule_payload, markdown_text)
    if need_llm:
        llm_attempted = True
        try:
            llm_result = extract_basic_summary_chunked(
                markdown_text,
                request_timeout_seconds=request_timeout_seconds,
                max_attempts=max_attempts,
                chunk_max_chars=chunk_max_chars,
            )
            if llm_result:
                llm_stats = llm_result.pop("_parse_stats", {})
                llm_payload = _normalize_value(llm_result)
        except Exception as e:
            llm_error = str(e)

    merged = _merge_basic_summary_rule_llm(rule_payload, llm_payload)
    selected_names = llm_stats.get("selected_file_names") or [b.get("filename", "") for b in candidate_blocks]
    selected_names = [name for name in selected_names if name]
    merged["_parse_stats"] = {
        "total_files": len(blocks),
        "selected_files": llm_stats.get("selected_files", len(candidate_blocks)),
        "selected_file_names": selected_names,
        "requirement_count": llm_stats.get("requirement_count", len(SUMMARY_REQUIREMENT_KEYWORDS)),
        "mode": merged.get("_summary_source_mode", "rules"),
        "llm_used": llm_payload is not None,
        "llm_attempted": llm_attempted,
        "llm_error": llm_error,
    }
    return merged



def _infer_medical_type_from_item(item: dict) -> str:
    material_name = _clean_text(item.get("material_name"))
    source_file = _clean_text(item.get("source_file"))
    haystack = f"{material_name} {source_file}"

    if "急诊" in haystack:
        return "急诊"
    if "住院" in haystack or "入院" in haystack or "出院" in haystack:
        return "住院"
    if "门诊" in haystack:
        return "门诊"
    if any(k in haystack for k in ["CT", "DR", "MRI", "X线", "影像", "报告单"]):
        return "影像"
    if "康复" in haystack:
        return "康复"
    if "证明" in haystack:
        return "证明"
    return "其他"


def _summary_item_to_medical_record(item: dict) -> dict:
    record_date = _clean_text(item.get("record_date"))
    hospitalization_period = _clean_text(item.get("hospitalization_period"))

    admission_date = "不详"
    discharge_date = "不详"
    period_match = re.search(
        r"([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)\s*至\s*([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)",
        hospitalization_period,
    )
    if period_match:
        admission_date = _safe_group(period_match, 1)
        discharge_date = _safe_group(period_match, 2)

    visit_date = record_date
    if visit_date == "不详" and admission_date != "不详":
        visit_date = admission_date

    chief = _clean_text(item.get("chief_complaint"))
    history = _clean_text(item.get("present_history"))
    complaint_parts = [part for part in [chief, history] if part != "不详"]
    complaint_and_history = "；".join(complaint_parts) if complaint_parts else "不详"

    diagnosis = _clean_text(item.get("discharge_diagnosis"))
    if diagnosis == "不详":
        diagnosis = _clean_text(item.get("treatment_advice"))

    treatment_summary = _clean_text(item.get("treatment_course"))
    key_exam_findings = _clean_text(item.get("imaging_findings"))
    source_file = _clean_text(item.get("source_file"))

    return {
        "source_files": [source_file] if source_file != "不详" else [],
        "medical_type": _infer_medical_type_from_item(item),
        "hospital_name": _clean_text(item.get("hospital_name")),
        "visit_date": visit_date,
        "admission_date": admission_date,
        "discharge_date": discharge_date,
        "complaint_and_history": complaint_and_history,
        "diagnosis": diagnosis,
        "treatment_summary": treatment_summary,
        "key_exam_findings": key_exam_findings,
    }


FORENSIC_INJURY_KEYWORDS = [
    "骨折", "压缩", "脱位", "半脱位", "韧带", "肌腱", "肩袖", "半月板", "关节炎", "创伤性",
    "蛛网膜", "硬膜", "脑挫", "开颅", "颅骨", "眼", "视力", "视野", "盲目", "矫正视力", "瘢痕", "烧伤", "烫伤", "截瘫",
    "肌力", "排尿", "排便", "护理依赖", "医疗费", "因果", "胸", "腰", "椎", "肋", "骨盆", "髋臼", "跟骨",
    "内固定", "钢板", "螺钉", "髓内钉", "克氏针", "骨不连", "延迟愈合", "不愈合", "截肢", "离断", "断指", "断趾",
]

IMAGING_KEYWORDS = ["CT", "ＣＴ", "MRI", "MR", "DR", "X线", "x线", "影像", "片", "报告"]
TREATMENT_KEYWORDS = ["手术", "内固定", "取出", "复位", "清创", "植皮", "皮瓣", "VSD", "缝合", "保守", "卧床", "腰围", "石膏", "支具", "义眼", "摘除"]

BODY_PART_RULES = [
    ("脊柱", ["椎", "胸腰", "胸椎", "腰椎", "颈椎", "骶椎"]),
    ("肋骨", ["肋骨", "肋"]),
    ("骨盆", ["骨盆", "骶骨", "耻骨", "髋臼", "闭孔"]),
    ("肩关节", ["肩", "肩袖", "肱骨大结节"]),
    ("膝关节", ["膝", "胫骨平台", "半月板", "交叉韧带"]),
    ("髋关节", ["髋", "股骨粗隆", "股骨颈", "股骨头"]),
    ("腕关节", ["腕", "桡骨远端", "尺桡骨"]),
    ("肘关节", ["肘", "尺骨", "鹰嘴"]),
    ("踝关节", ["踝", "外踝", "内踝", "距腓", "三角韧带"]),
    ("足部", ["跟骨", "距骨", "跖骨", "趾骨", "跗骨", "足"]),
    ("颅脑", ["颅", "脑", "蛛网膜", "硬膜", "头颅"]),
    ("眼", ["眼", "视力", "眼球", "角膜", "晶状体", "玻璃体", "HESS"]),
    ("皮肤瘢痕", ["瘢痕", "烧伤", "烫伤", "植皮", "取皮"]),
]

COMMON_INJURY_PATTERNS = [
    ("胫骨平台骨折", "膝关节", "骨折"),
    ("腓骨小头骨折", "膝关节", "骨折"),
    ("半月板损伤", "膝关节", "半月板损伤"),
    ("交叉韧带损伤", "膝关节", "韧带损伤"),
    ("副韧带损伤", "膝关节", "韧带损伤"),
    ("肩袖损伤", "肩关节", "肌腱/肩袖损伤"),
    ("肱骨骨折", "肩/上肢", "骨折"),
    ("股骨粗隆间骨折", "髋关节", "骨折"),
    ("股骨颈骨折", "髋关节", "骨折"),
    ("桡骨远端骨折", "腕关节", "骨折"),
    ("尺骨近端骨折", "肘关节", "骨折"),
    ("外踝骨折", "踝关节", "骨折"),
    ("跟骨骨折", "足部", "骨折"),
    ("髋臼骨折", "骨盆/髋关节", "骨折"),
    ("骨盆骨折", "骨盆", "骨折"),
    ("蛛网膜下腔出血", "颅脑", "颅脑损伤"),
    ("硬膜下血肿", "颅脑", "颅脑损伤"),
    ("硬膜外血肿", "颅脑", "颅脑损伤"),
    ("脑挫裂伤", "颅脑", "颅脑损伤"),
    ("脑挫伤", "颅脑", "颅脑损伤"),
    ("眼球破裂", "眼", "眼损伤"),
    ("眼球摘除", "眼", "眼球缺失"),
    ("外伤性白内障", "眼", "眼损伤"),
    ("视神经损伤", "眼", "视觉功能障碍"),
    ("矫正视力下降", "眼", "视觉功能障碍"),
    ("瘢痕", "皮肤瘢痕", "瘢痕"),
    ("烧伤", "皮肤瘢痕", "烧伤"),
]


LIMB_FRACTURE_TYPE_RULES = [
    ("clavicle", "锁骨骨折", ["锁骨"]),
    ("scapula", "肩胛骨骨折", ["肩胛骨"]),
    ("humerus", "肱骨骨折", ["肱骨", "肱骨大结节", "肱骨近端", "肱骨干", "肱骨髁"]),
    ("olecranon", "尺骨鹰嘴骨折", ["尺骨鹰嘴"]),
    ("ulna_radius", "尺桡骨折", ["尺桡骨", "尺骨", "桡骨", "桡骨远端", "前臂骨折"]),
    ("wrist", "腕骨骨折", ["腕骨"]),
    ("finger_metacarpal", "指、掌骨骨折", ["指骨", "掌骨"]),
    ("femoral_neck", "股骨颈骨折", ["股骨颈"]),
    ("intertrochanteric", "股骨粗隆间骨折", ["股骨粗隆间", "粗隆间"]),
    ("femoral_shaft", "股骨干骨折", ["股骨干"]),
    ("femoral_distal", "股骨远端骨折", ["股骨远端", "股骨髁", "股骨外侧髁", "股骨内侧髁"]),
    ("patella", "髌骨骨折", ["髌骨"]),
    ("tibial_plateau", "胫骨平台骨折", ["胫骨平台"]),
    ("tibia_fibula", "胫腓骨骨折", ["胫腓骨", "胫骨", "腓骨", "胫骨下端", "胫骨远端"]),
    ("ankle", "踝部骨折", ["外踝", "内踝", "双踝", "三踝", "踝部", "踝关节骨折"]),
    ("calcaneus_talus", "跟、距骨骨折", ["跟骨", "距骨"]),
    ("tarsal", "舟、楔骨等跗骨骨折", ["舟骨", "楔骨", "跗骨"]),
    ("metatarsal_toe", "跖、趾骨骨折", ["跖骨", "趾骨"]),
]


LONG_BONE_TYPES = {"humerus", "ulna_radius", "femoral_neck", "intertrochanteric", "femoral_shaft", "femoral_distal", "tibial_plateau", "tibia_fibula"}


def _forensic_snippet(text: str, max_len: int = 220) -> str:
    cleaned = _collapse_chinese_spaces(text)
    if cleaned == "不详":
        return ""
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" |\t")
    return cleaned[:max_len]


def _iter_forensic_lines(markdown_text: str, keywords: Optional[List[str]] = None) -> List[str]:
    keywords = keywords or FORENSIC_INJURY_KEYWORDS
    lines = []
    seen = set()
    for raw_line in (markdown_text or "").splitlines():
        line = _forensic_snippet(raw_line, max_len=420)
        if not line or line in seen:
            continue
        if len(line) < 4:
            continue
        if any(keyword in line for keyword in keywords):
            lines.append(line)
            seen.add(line)
    return lines


def _infer_side(text: str) -> str:
    if re.search(r"双侧|双眼|双耳|双膝|双肩|双踝", text or ""):
        return "双侧"
    match = re.search(r"(左侧|右侧|左|右)", text or "")
    if match:
        value = match.group(1)
        return "左" if "左" in value else "右"
    return ""


def _infer_body_part(text: str) -> str:
    haystack = text or ""
    for body_part, keywords in BODY_PART_RULES:
        if any(keyword in haystack for keyword in keywords):
            return body_part
    return ""


def _cn_num_to_int(value: str) -> Optional[int]:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    table = {
        "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        "十一": 11, "十二": 12,
    }
    return table.get(value)


VALID_VERTEBRA_RANGES = {"C": (1, 7), "T": (1, 12), "L": (1, 5), "S": (1, 5)}


def _valid_vertebra_level(level: str) -> bool:
    match = re.match(r"^([CTLS])(\d{1,2})$", str(level or "").upper())
    if not match:
        return False
    prefix, number = match.groups()
    low, high = VALID_VERTEBRA_RANGES.get(prefix, (0, -1))
    return low <= int(number) <= high


def _normalize_vertebra_level(prefix: str, raw_num: str) -> Optional[str]:
    raw = (raw_num or "").strip().upper().replace("Ｌ", "L").replace("Ｔ", "T")
    prefix = prefix or ""
    level = None

    if raw.startswith("T") and raw[1:].isdigit():
        level = f"T{int(raw[1:])}"
    elif raw.startswith("L") and raw[1:].isdigit():
        # OCR常把“胸11”误成“胸L1”，在胸椎语境下按T11处理。
        if "胸" in prefix:
            level = "T11" if raw == "L1" else f"T{int(raw[1:])}"
        else:
            level = f"L{int(raw[1:])}"
    else:
        num = _cn_num_to_int(raw)
        if num is None:
            return None
        if "胸" in prefix:
            level = f"T{num}"
        elif "腰" in prefix:
            level = f"L{num}"
        elif "颈" in prefix:
            level = f"C{num}"
        elif "骶" in prefix:
            level = f"S{num}"
    return level if _valid_vertebra_level(level or "") else None


def _extract_vertebral_levels(text: str) -> List[str]:
    levels = []
    normalized = (text or "").replace(" ", "")

    for match in re.finditer(r"\b([TtLl])\s*([0-9]{1,2})\b", text or ""):
        level = _normalize_vertebra_level("", f"{match.group(1)}{match.group(2)}")
        if level and level not in levels:
            levels.append(level)

    for match in re.finditer(r"(胸|腰|颈|骶)\s*([0-9一二两三四五六七八九十]{1,3}|[LlＴT][0-9]{1,2})", normalized):
        level = _normalize_vertebra_level(match.group(1), match.group(2))
        if level and level not in levels:
            levels.append(level)

    for match in re.finditer(r"第([一二两三四五六七八九十]{1,3}|[0-9]{1,2})(胸|腰|颈|骶)椎", normalized):
        level = _normalize_vertebra_level(match.group(2), match.group(1))
        if level and level not in levels:
            levels.append(level)

    return levels


def _expand_number_expression(expr: str) -> List[int]:
    if not expr:
        return []
    expr = expr.replace("至", "-").replace("～", "-").replace("~", "-").replace("—", "-").replace("－", "-")
    expr = re.sub(r"[^0-9、,，;；\-\s]", "", expr)
    nums = []
    for part in re.split(r"[、,，;；\s]+", expr):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            if left.isdigit() and right.isdigit():
                start, end = int(left), int(right)
                if 1 <= start <= end <= 12:
                    nums.extend(range(start, end + 1))
                continue
        if part.isdigit():
            value = int(part)
            if 1 <= value <= 12:
                nums.append(value)
    result = []
    for n in nums:
        if n not in result:
            result.append(n)
    return result


def _level_is_disc_only_in_line(level: str, line: str) -> bool:
    """过滤“腰5椎间盘膨出”这类非椎体骨折节段。"""
    normalized = (line or "").replace(" ", "").upper().replace("Ｌ", "L").replace("Ｔ", "T")
    if not level or not level.startswith("L"):
        return False
    num = level[1:]
    disc_patterns = [f"L{num}椎间盘", f"腰{num}椎间盘"]
    vertebra_fracture_patterns = [f"L{num}椎体", f"腰{num}椎体", f"L{num}骨折", f"腰{num}骨折"]
    return any(pattern in normalized for pattern in disc_patterns) and not any(
        pattern in normalized for pattern in vertebra_fracture_patterns
    )



def _compression_degree_from_text(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "")
    if re.search(r"未达(?:到)?1/3|小于1/3|不足1/3|未达(?:到)?三分之一|小于三分之一|不足三分之一", compact):
        return "未达1/3"
    if re.search(r"(?:均)?达(?:到)?1/3|大于1/3|超过1/3|达(?:到)?三分之一|大于三分之一|超过三分之一", compact):
        return "达1/3"
    if re.search(r"约(?:达)?1/3|约三分之一", compact):
        return "约达1/3"
    if "轻度压缩" in compact or "轻度压缩变扁" in compact:
        return "轻度压缩"
    return ""


NEGATED_SURGERY_PATTERNS = [
    r"无.{0,8}手术",
    r"未.{0,8}手术",
    r"没有.{0,8}手术",
    r"没.{0,8}手术",
    r"未行.{0,8}手术",
    r"未予.{0,8}手术",
    r"手术史[:：]?无",
]


def _has_negated_surgery_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return any(re.search(pattern, compact) for pattern in NEGATED_SURGERY_PATTERNS)


def _has_positive_surgery_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if _has_negated_surgery_text(compact):
        return False
    return any(k in compact for k in ["内固定术", "内固定", "切开复位", "椎弓根", "植骨", "手术名称", "术中见", "手术记录"])


def _is_scan_region_only_line(line: str) -> bool:
    compact = re.sub(r"\s+", "", line or "")
    scan_markers = ["检查部位", "检查项目", "扫描范围", "扫描部位", "检查范围", "扫描序列"]
    fracture_terms = ["骨折", "压缩", "楔形", "变扁", "骨质不连", "透亮影"]
    return any(marker in compact for marker in scan_markers) and not any(k in compact for k in fracture_terms)


# 既往伤 / 退行性改变：这些描述说明改变不是本次外伤造成的。
# “行性变/行性改变”是“退行性变”的稳定尾部，OCR 常把它前面的字认错
# （如“箱行性变”），只匹配尾部比匹配全词更稳，也不会误伤其它医学表述。
_PREEXISTING_OR_DEGENERATIVE_RE = re.compile(
    r"陈(?:\d|\s){0,2}旧|既往|退行性|行性变|行性改变|退变|骨赘|骨质增生|血管瘤|已愈合|术后改变|先天"
)


def _is_preexisting_or_degenerative_line(line: str) -> bool:
    """描述既往伤、退行性改变或先天改变的句子，不是本次外伤的伤情证据。

    影像报告里“胸8椎体陈旧压缩性改变。胸椎退行性改变。”这类描述很常见，
    若按骨折计入会给出远高于实际的伤残等级。
    """
    compact = re.sub(r"\s+", "", line or "")
    return bool(_PREEXISTING_OR_DEGENERATIVE_RE.search(compact))


def _line_has_unreliable_spine_context(line: str) -> bool:
    compact = re.sub(r"\s+", "", line or "")
    return (
        _is_scan_region_only_line(compact)
        or _is_negated_injury_line(compact)
        or _is_preexisting_or_degenerative_line(compact)
    )


def _level_has_positive_fracture_context(level: str, line: str) -> bool:
    compact = re.sub(r"\s+", "", line or "").upper().replace("Ｌ", "L").replace("Ｔ", "T")
    if not level or _line_has_unreliable_spine_context(compact):
        return False
    cn = _level_to_cn_text(level)
    markers = [level, cn, cn.replace("椎", "")]
    fracture_terms = r"骨折|压缩|楔形|变扁|骨质不连|透亮影"
    for marker in markers:
        if not marker:
            continue
        for match in re.finditer(re.escape(marker), compact):
            window = compact[max(0, match.start() - 24): match.end() + 36]
            if re.search(fracture_terms, window) and not re.search(r"未见|无明显|未发现|排除|血管瘤|退变|椎间盘", window):
                return True
    return False


def _level_to_cn_text(level: str) -> str:
    match = re.match(r"^([CTLS])(\d{1,2})$", str(level or "").upper())
    if not match:
        return ""
    prefix, number = match.groups()
    label = {"C": "颈", "T": "胸", "L": "腰", "S": "骶"}.get(prefix, "")
    return f"{label}{int(number)}椎" if label else ""


def _extract_vertebral_features(markdown_text: str) -> Dict[str, Any]:
    candidate_lines = _iter_forensic_lines(markdown_text, ["椎", "锥", "胸", "腰", "压缩", "楔形", "内固定", "脊柱"])
    levels = []
    evidence = []
    compression = False
    has_surgery = False
    hardware_removed = False
    healed = False
    compression_degree = ""
    compression_degrees: List[str] = []
    explicit_count = 0

    fracture_terms = ["骨折", "压缩", "楔形", "变扁", "骨质不连", "透亮影"]
    for line in candidate_lines:
        if not any(k in line for k in ["椎", "锥", "胸", "腰", "压缩", "脊柱"]):
            continue
        is_scan_region_only = _is_scan_region_only_line(line)
        unreliable_context = _line_has_unreliable_spine_context(line)
        has_fracture_signal = any(k in line for k in fracture_terms)
        raw_levels = _extract_vertebral_levels(line) if has_fracture_signal and not is_scan_region_only else []
        found_levels = [
            level for level in raw_levels
            if not _level_is_disc_only_in_line(level, line) and _level_has_positive_fracture_context(level, line)
        ]
        for level in found_levels:
            if level not in levels:
                levels.append(level)
        if found_levels or (has_fracture_signal and not unreliable_context and any(k in line for k in ["椎体压缩性骨折", "脊柱骨折", "胸椎骨折", "腰椎骨折", "颈椎骨折", "骶椎骨折"])):
            evidence.append(line)
        if found_levels and any(k in line for k in ["压缩", "楔形", "变扁"]):
            compression = True
        if _has_negated_surgery_text(line):
            has_surgery = False
        elif _has_positive_surgery_text(line):
            has_surgery = True
        if "取出" in line or "去除" in line:
            hardware_removed = True
        if any(k in line for k in ["愈合", "骨折线未见", "骨折线模糊"]):
            healed = True
        degree_text = _compression_degree_from_text(line)
        if degree_text:
            if degree_text not in compression_degrees:
                compression_degrees.append(degree_text)
            if degree_text == "未达1/3" or not compression_degree:
                compression_degree = degree_text
        if found_levels and len(found_levels) >= 2 and re.search(r"(二|两|2)\s*(?:节|个)?椎体|(?:二|两|2)椎体", line) and any(k in line for k in ["压缩", "楔形", "变扁", "骨折"]):
            explicit_count = max(explicit_count, 2)

    count = max(len(levels), explicit_count if explicit_count <= len(levels) else 0)
    return {
        "levels": levels,
        "count": count,
        "compression_fracture": compression,
        "compression_degree": compression_degree,
        "compression_degrees": compression_degrees,
        "has_spine_surgery": has_surgery,
        "internal_fixation_removed": hardware_removed,
        "healed": healed,
        "evidence": _merge_list_unique([], evidence, max_items=8),
    }



# 骨折描述带不确定限定词：“不全骨折可能”“建议复查除外隐匿性骨折”。
_RIB_UNCERTAIN_RE = re.compile(
    r"(?:骨折|骨皮质皱褶|骨皮质毛糙|骨皮质增厚)[^。；，,]{0,8}(?:可能|可疑|待排|待定|不除外)"
    r"|(?:可能|可疑|待排|待除外|不除外|除外)[^。；，,]{0,12}(?:骨折|骨皮质皱褶)"
)
# OCR 常在“陈旧性”中间插字符（如“陈1旧性”），这里容忍。
_PREEXISTING_NOISY_RE = re.compile(r"陈(?:\d|\s){0,2}旧|既往|老旧")


def _is_stable_rib_fracture_line(line: str) -> bool:
    compact = re.sub(r"\s+", "", line or "")
    if re.search(r"(?:未见|无明显|未发现|未提示|待排|可疑|除外)[^。；，,]{0,30}肋骨?[^。；，,]{0,20}骨折", compact):
        return False
    # “不全骨折可能”“建议复查除外隐匿性骨折”等只是疑似描述，不能作为确诊计数。
    if _RIB_UNCERTAIN_RE.search(compact):
        return False
    if _PREEXISTING_NOISY_RE.search(compact):
        return any(k in compact for k in ["新近", "同次外伤", "本次外伤", "同一时期", "新鲜"])
    if "肋" not in compact or "骨折" not in compact:
        return False
    if re.search(r"第?[0-9一二三四五六七八九十]+(?:[-至、,，][0-9一二三四五六七八九十]+)*肋", compact):
        return True
    if re.search(r"[0-9一二三四五六七八九十]+根(?:多发)?肋骨骨折|肋骨骨折[0-9一二三四五六七八九十]+根|多根肋骨骨折|多处肋骨骨折", compact):
        return True
    return False

def _extract_rib_fracture_features(markdown_text: str) -> Dict[str, Any]:
    candidate_lines = _iter_forensic_lines(markdown_text, ["肋骨", "肋", "胸部CT", "胸廓"])
    items = []
    max_count = 0
    all_numbers = []
    evidence = []

    for line in candidate_lines:
        if not _is_stable_rib_fracture_line(line):
            continue
        side = _infer_side(line)
        numbers = []
        # 允许“第11、12根肋骨”这种把“根”夹在号码与“肋”之间的写法，
        # 否则该行既取不到号码、又会被数量词规则误读成“12 根”。
        for match in re.finditer(r"(?:第)?([0-9][0-9、,，;；\-~～至 ]{0,30})\s*根?\s*肋", line):
            numbers.extend(_expand_number_expression(match.group(1)))
        numbers = sorted(set(numbers))

        count = len(numbers)
        # 数量词必须独立出现。“第11、12根肋骨骨折”说的是第 11 和第 12 两根肋骨，
        # 不能把紧跟顿号/连字符的数字读成“12 根”，否则会凭空触发更高等级的条款。
        count_patterns = [
            r"共\s*([0-9]{1,2})\s*根",
            r"(?<![0-9、,，;；\-~～至])([0-9]{1,2})\s*根(?:多发)?肋骨骨折",
            r"肋骨骨折\s*([0-9]{1,2})\s*根",
        ]
        for pattern in count_patterns:
            match = re.search(pattern, line)
            if match:
                count = max(count, int(match.group(1)))

        if count == 0 and "多根" in line:
            count = 2

        if count or numbers:
            item = {
                "side": side,
                "rib_numbers": numbers,
                "count": count,
                "new_or_old": "新近" if "新近" in line else ("陈旧" if "陈旧" in line else ""),
                "callus_same_period": any(k in line for k in ["骨痂", "同一时期", "同次外伤"]),
                "evidence": line,
            }
            items.append(item)
            max_count = max(max_count, count)
            for n in numbers:
                if n not in all_numbers:
                    all_numbers.append(n)
            evidence.append(line)

    return {
        "items": items[:8],
        "count_confirmed": max_count,
        "stable_evidence": bool(evidence),
        "rib_numbers_all": sorted(all_numbers),
        "six_plus": max_count >= 6,
        "twelve_plus": max_count >= 12,
        "evidence": _merge_list_unique([], evidence, max_items=8),
    }


def _extract_imaging_events(markdown_text: str) -> List[Dict[str, Any]]:
    events = []
    source_lines: List[tuple[str, str]] = []
    blocks = _split_file_blocks(markdown_text)
    if blocks:
        for block in blocks:
            filename = block.get("filename", "")
            for raw_line in block.get("content", "").splitlines():
                line = _forensic_snippet(raw_line, max_len=520)
                if line:
                    source_lines.append((filename, line))
    else:
        source_lines = [("", line) for line in _iter_forensic_lines(markdown_text, IMAGING_KEYWORDS + ["影像诊断", "影像所见", "提示"])]

    seen = set()
    for filename, line in source_lines:
        has_modality = bool(re.search(r"CT|ＣＴ|MRI|MR|磁共振|\bDR\b|X线|x线|X-ray", line, re.I))
        has_imaging_label = any(k in line for k in ["影像诊断", "影像所见", "检查项目", "检查部位", "检查号", "CT号", "MR号", "报告时间"])
        has_medical_finding = any(k in line for k in ["骨折", "压缩", "楔形", "变扁", "椎管", "硬膜囊", "关节", "脱位", "愈合", "骨折线"])
        if not ((has_modality or has_imaging_label) and (has_medical_finding or has_imaging_label)):
            continue
        modality = _extract_single_imaging_method(line)
        body_part = _infer_body_part(line)
        tags = []
        for tag, keywords in {
            "骨折": ["骨折"],
            "压缩性骨折": ["压缩", "楔形", "变扁"],
            "新近损伤": ["新近", "骨髓水肿", "T1", "T2", "锐利骨折线"],
            "术后改变": ["术后", "内固定", "取出"],
            "骨折愈合": ["愈合", "骨折线未见", "骨折线模糊"],
            "关节面不平整": ["关节面不平整", "塌陷", "骨质缺损", "关节间隙变窄"],
            "创伤性关节炎": ["创伤性关节炎", "关节炎"],
            "畸形愈合": ["畸形愈合", "闭孔大小不一", "外侧缘膨隆"],
        }.items():
            if any(k in line for k in keywords):
                tags.append(tag)
        degree = _compression_degree_from_text(line)
        key = re.sub(r"\s+", "", f"{filename}|{line}")
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "date": _extract_first_cn_date(line),
            "hospital": _extract_hospital_name(filename, line),
            "modality": modality if modality != "不详" else "",
            "body_part": body_part,
            "exam_no": _extract_summary_record_number(line),
            "source_file": filename,
            "compression_degree": degree,
            "vertebra_levels": [] if _is_scan_region_only_line(line) else _extract_vertebral_levels(line),
            "phase": "复查" if "复查" in line else ("术后" if "术后" in line else ""),
            "findings_raw": line,
            "normalized_findings_tags": tags,
            "accepted_by_expert": None,
        })
        if len(events) >= 30:
            break
    return events


def _extract_treatment_events(markdown_text: str) -> List[Dict[str, Any]]:
    events = []
    for line in _iter_forensic_lines(markdown_text, TREATMENT_KEYWORDS):
        if not any(k in line for k in TREATMENT_KEYWORDS):
            continue
        treatment_type = "手术" if _has_positive_surgery_text(line) or any(k in line for k in ["清创", "植皮", "缝合", "摘除", "修补"]) else "保守治疗"
        events.append({
            "date": _extract_first_cn_date(line),
            "hospital": _extract_hospital_name("", line),
            "treatment_type": treatment_type,
            "operation_name": line if treatment_type == "手术" else "",
            "target_body_part": _infer_body_part(line),
            "is_fracture_surgery": treatment_type == "手术" and any(k in line for k in ["骨折", "复位", "内固定", "植骨"]),
            "is_soft_tissue_surgery": treatment_type == "手术" and any(k in line for k in ["韧带", "肌腱", "肩袖", "半月板", "清创", "植皮", "皮瓣", "缝合"]),
            "internal_fixation_used": "内固定" in line,
            "internal_fixation_removed": "取出" in line or "去除" in line,
            "internal_fixation_no_need_remove": "无需取出" in line or "不需取出" in line or "永久" in line,
            "evidence": line,
        })
        if len(events) >= 20:
            break
    return events



def _has_surgical_or_fixation_text(text: str) -> bool:
    haystack = text or ""
    if _has_negated_surgery_text(haystack):
        return False
    return any(k in haystack for k in ["内固定", "钢板", "螺钉", "髓内钉", "克氏针", "切开复位", "闭合复位", "修补", "重建", "缝合", "手术名称", "术中见"])


def _narrow_limb_label(label: str, keywords: List[str], line: str) -> str:
    """按实际命中的骨名收窄伤情标签。

    规则表里有些标签把多块骨并在一起（“跟、距骨骨折”“舟、楔骨等跗骨骨折”）。
    若材料只累及其中一块，照搬标签就会在文书里凭空多写一处骨折。
    """
    if "、" not in label:
        return label
    matched = [k for k in keywords if _injury_term_is_current(line, k)]
    if not matched or len(matched) >= len(keywords):
        return label
    return "、".join(dict.fromkeys(matched)) + "骨折"


def _extract_limb_fracture_features(markdown_text: str, treatment_events: List[dict]) -> List[Dict[str, Any]]:
    lines = _iter_forensic_lines(
        markdown_text,
        ["锁骨", "肩胛", "肱骨", "尺骨", "桡骨", "腕骨", "指骨", "掌骨", "股骨", "髌骨", "胫骨", "腓骨", "踝", "跟骨", "距骨", "跖骨", "趾骨", "跗骨", "内固定", "骨折"],
    )
    joined_treatment = "\n".join(_clean_text(e.get("evidence") or e.get("operation_name")) for e in treatment_events if isinstance(e, dict))
    items = []
    seen = set()
    for line in lines:
        if "骨折" not in line:
            continue
        for injury_type, label, keywords in LIMB_FRACTURE_TYPE_RULES:
            # “左膝未见明显骨折”“胫腓骨未见骨折或脱位”等否定描述，
            # 以及“陈旧性肱骨内上骨折”等既往伤，都不是本次外伤造成的伤情。
            # 脊柱抽取早已用 _line_has_unreliable_spine_context 做了这件事，肢体抽取此前漏掉了。
            if not any(_injury_term_is_current(line, k) for k in keywords):
                continue
            side = _infer_side(line)
            key = (injury_type, side)
            if key in seen:
                continue
            local_context = f"{line}\n{joined_treatment}"
            has_surgery = _has_surgical_or_fixation_text(local_context)
            item = {
                "type": injury_type,
                "label": _narrow_limb_label(label, keywords, line),
                "side": side,
                "body_part": _infer_body_part(line),
                "is_long_bone": injury_type in LONG_BONE_TYPES,
                "has_surgery": has_surgery,
                "internal_fixation_used": any(k in local_context for k in ["内固定", "钢板", "螺钉", "髓内钉", "克氏针"]),
                "internal_fixation_removed": any(k in local_context for k in ["内固定物取出", "内固定装置取出", "取出术", "去除术"]),
                "comminuted": "粉碎" in line,
                "open_fracture": "开放" in line,
                "joint_surface_involved": any(k in line for k in ["关节面", "累及关节", "关节间隙", "关节面不平整", "塌陷"]),
                "epiphyseal_involved": any(k in line for k in ["骨骺", "骺板"]),
                "evidence": line,
            }
            items.append(item)
            seen.add(key)
            break
        if len(items) >= 20:
            break
    return items


def _is_negated_injury_line(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return bool(re.search(r"(?:未见|无|未发现|未检出|未提示)[^。；，,]{0,30}(?:骨折|撕裂|断裂|损伤|异常)", compact))


# 否定 / 既往伤的就近判定窗口（关键词之前的字符数）
_INJURY_NEGATION_PREFIX_RE = re.compile(r"(?:未见|未发现|未检出|未提示|无|排除)[^。；，,]{0,12}$")
_INJURY_PREEXISTING_PREFIX_RE = re.compile(r"(?:陈旧性|陈旧|既往|已愈合|骨不连史)[^。；，,]{0,14}$")


def _injury_term_is_current(line: str, keyword: str) -> bool:
    """判断 keyword 在这行里是否属于本次外伤的现行伤情。

    与整行过滤相比，这里按关键词出现位置就近判断，因此同一行里
    “左侧胫骨外侧平台骨折”与“外侧陈旧性骨折考虑”可以分别处理，
    不会因为一处否定或既往描述而丢掉同一行中真实存在的伤情。

    排除两类误判：
    - 否定描述：“未见骨折”“无骨折脱位”；
    - 既往伤：“陈旧性骨折”“既往骨折”。
    """
    compact = re.sub(r"\s+", "", line or "")
    for match in re.finditer(re.escape(keyword), compact):
        prefix = compact[max(0, match.start() - 16):match.start()]
        suffix = compact[match.end():match.end() + 16]
        # 否定写在骨名之前：“未见肱骨骨折”
        if _INJURY_NEGATION_PREFIX_RE.search(prefix):
            continue
        # 既往伤：“陈旧性肱骨内上骨折”
        if _INJURY_PREEXISTING_PREFIX_RE.search(prefix):
            continue
        # 骨名与“骨折/脱位”之间（同一分句内）的片段
        gap_match = re.match(r"^([^。；，,]{0,10})", suffix)
        gap = gap_match.group(1) if gap_match else ""
        # 否定写在骨名之后：“胫腓骨未见明显骨折”
        if re.search(r"(?:未见|未发现|未检出|未提示|无|排除)", gap):
            continue
        # 骨名附近没有骨折描述时不算伤情：“左内踝周围软组织肿胀”里的“内踝”
        if not re.search(r"(?:骨折|脱位|撕裂|断裂)", gap) and not re.search(r"(?:骨折|脱位|撕裂|断裂)[^。；，,]{0,6}$", prefix):
            continue
        return True
    return False


def _extract_ligament_meniscus_features(markdown_text: str, treatment_events: List[dict]) -> Dict[str, Any]:
    lines = [
        line for line in _iter_forensic_lines(markdown_text, ["韧带", "半月板", "肩袖", "肌腱", "交叉韧带", "侧副韧带", "十字韧带"])
        if not _is_negated_injury_line(line)
    ]
    joined = "\n".join(lines)
    treatment_text = "\n".join(_clean_text(e.get("evidence") or e.get("operation_name")) for e in treatment_events if isinstance(e, dict))
    all_text = f"{joined}\n{treatment_text}"
    items = []
    for line in lines[:12]:
        kind = ""
        if "半月板" in line:
            kind = "meniscus"
        elif "交叉韧带" in line or "十字韧带" in line:
            kind = "cruciate_ligament"
        elif "侧副韧带" in line:
            kind = "collateral_ligament"
        elif "肩袖" in line:
            kind = "rotator_cuff"
        elif "肌腱" in line:
            kind = "tendon"
        elif "韧带" in line:
            kind = "ligament"
        items.append({"kind": kind, "side": _infer_side(line), "joint": _infer_body_part(line), "evidence": line})
    knee_complex = any(k in joined for k in ["交叉韧带", "十字韧带"]) and "半月板" in joined and "侧副韧带" in joined
    return {
        "items": items,
        "has_ligament_or_meniscus": bool(items),
        "has_surgery": _has_surgical_or_fixation_text(all_text),
        "affects_function": any(k in all_text for k in ["活动受限", "功能障碍", "功能丧失", "僵硬", "影响功能"]),
        "knee_cruciate_meniscus_collateral_combo": knee_complex,
        "evidence": _merge_list_unique([], lines, max_items=8),
    }


def _extract_amputation_features(markdown_text: str) -> Dict[str, Any]:
    lines = _iter_forensic_lines(markdown_text, ["截肢", "离断", "断指", "断趾", "缺失", "再植", "手功能丧失分值", "足功能丧失分值"])
    joined = "\n".join(lines)
    score_match = re.search(r"(?:手|足)功能丧失分值[^0-9]{0,8}([0-9]{1,3})\s*分", joined)
    return {
        "has_amputation_or_loss": any(k in joined for k in ["截肢", "离断", "断指", "断趾", "完全缺失", "部分缺失"]),
        "is_replantation": "再植" in joined,
        "level": "limb" if "截肢" in joined else ("finger" if any(k in joined for k in ["断指", "手指", "指骨", "中指", "环指", "小指", "拇指"]) else ("toe" if any(k in joined for k in ["断趾", "足趾", "趾骨"]) else "")),
        "hand_or_foot_function_score": int(score_match.group(1)) if score_match else None,
        "evidence": _merge_list_unique([], lines, max_items=8),
    }


def _extract_healing_features(markdown_text: str) -> Dict[str, Any]:
    lines = _iter_forensic_lines(markdown_text, ["延迟愈合", "骨不连", "不愈合", "长期不愈", "慢性骨髓炎", "死骨"])
    joined = "\n".join(lines)
    return {
        "delayed_union": "延迟愈合" in joined,
        "nonunion": any(k in joined for k in ["骨不连", "不愈合"]),
        "chronic_osteomyelitis_or_sequestrum": any(k in joined for k in ["慢性骨髓炎", "死骨"]),
        "long_term_over_one_year": any(k in joined for k in ["1年以上", "一年以上", "超过1年", "长期不愈"]),
        "evidence": _merge_list_unique([], lines, max_items=8),
    }


def _extract_rom_measurements(markdown_text: str) -> List[Dict[str, Any]]:
    rom_lines = _iter_forensic_lines(markdown_text, ["关节活动度", "活动度", "活动功能", "屈曲", "前屈", "后伸", "外展", "内收", "背伸", "掌屈", "桡偏", "尺偏"])
    measurements = []
    for line in rom_lines:
        if not any(k in line for k in ["关节", "屈", "伸", "外展", "内收", "背伸", "掌屈", "桡偏", "尺偏", "活动"]):
            continue
        joint_match = re.search(r"(肩|膝|髋|腕|肘|踝)关节", line)
        joint = f"{joint_match.group(1)}关节" if joint_match else _infer_body_part(line)
        loss_match = re.search(r"(?:丧失|功能障碍)(?:达|约|为)?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%", line)
        ranges = []
        for match in re.finditer(r"(前屈|后伸|外展|内收|内旋|外旋|屈曲|伸展|背伸|跖屈|掌屈|桡偏|尺偏)[^0-9]{0,8}([0-9]{1,3})\s*[°º]?\s*[-－~至]\s*([0-9]{1,3})\s*[°º]?", line):
            ranges.append({
                "direction": match.group(1),
                "start": int(match.group(2)),
                "end": int(match.group(3)),
            })
        measurements.append({
            "joint": joint,
            "injured_side": _infer_side(line),
            "healthy_side": "",
            "directions": ranges,
            "reported_loss_percent": float(loss_match.group(1)) if loss_match else None,
            "threshold_25_met": (float(loss_match.group(1)) >= 25) if loss_match else None,
            "source": "text",
            "evidence": line,
        })
        if len(measurements) >= 20:
            break
    return measurements


def _extract_scar_burn_facts(markdown_text: str) -> Dict[str, Any]:
    lines = _iter_forensic_lines(markdown_text, ["瘢痕", "烧伤", "烫伤", "体表面积", "TBSA", "cm²", "cm2", "容貌毁损"])
    joined = "\n".join(lines)
    tbsa_match = re.search(r"(?:烧伤[^\n]{0,20}|TBSA[^\n]{0,10})([0-9]{1,3}(?:\.[0-9]+)?)\s*%", joined, re.I)
    scar_area_match = re.search(r"([0-9]{1,5}(?:\.[0-9]+)?)\s*(?:cm²|cm2|平方厘米)", joined, re.I)
    scar_percent_match = re.search(r"瘢痕[^\n]{0,30}?([0-9]{1,3}(?:\.[0-9]+)?)\s*%", joined)
    face_length_match = re.search(r"(?:面部|额部|面颊)[^\n]{0,30}?([0-9]{1,3}(?:\.[0-9]+)?)\s*cm", joined, re.I)
    return {
        "burn_total_tbsa_percent": float(tbsa_match.group(1)) if tbsa_match else None,
        "scar_total_area_cm2": float(scar_area_match.group(1)) if scar_area_match else None,
        "scar_bsa_percent": float(scar_percent_match.group(1)) if scar_percent_match else None,
        "face_scar_total_length_cm": float(face_length_match.group(1)) if face_length_match else None,
        "disfigurement_degree": "重度" if "重度容貌毁损" in joined or "重度毁容" in joined else ("中度" if "中度毁容" in joined else ""),
        "evidence": _merge_list_unique([], lines, max_items=8),
    }


def _extract_eye_facts(markdown_text: str) -> Dict[str, Any]:
    lines = _iter_forensic_lines(markdown_text, ["眼", "视力", "光感", "无光感", "义眼", "眼球", "HESS", "复视", "眼压", "瞳孔"])
    joined = "\n".join(lines)
    corrected = []
    for match in re.finditer(r"(左眼|右眼|左|右)?[^\n]{0,12}矫正视力[^0-9光无]{0,8}([0-9]\.[0-9]|光感|无光感)", joined):
        corrected.append({"side": _infer_side(match.group(1) or ""), "value": match.group(2)})
    visual_field_match = re.search(r"视野[^0-9]{0,12}([0-9]{1,3}(?:\.[0-9]+)?)\s*%", joined)
    visual_acuity_values = []
    for item in corrected:
        raw = item.get("value")
        if raw == "光感":
            visual_acuity_values.append(0.01)
        elif raw == "无光感":
            visual_acuity_values.append(0.0)
        else:
            try:
                visual_acuity_values.append(float(raw))
            except (TypeError, ValueError):
                pass
    min_corrected = min(visual_acuity_values) if visual_acuity_values else None
    blindness_level = _safe_group(re.search(r"盲目\s*([0-9一二三四五六七八九十]+级)", joined), 1, "")
    return {
        "eyeball_missing_or_removed": any(k in joined for k in ["眼球摘除", "眼球缺失", "左眼缺失", "右眼缺失"]),
        "prosthesis_in_place": "义眼" in joined,
        "corrected_visual_acuity": corrected[:6],
        "min_corrected_visual_acuity": min_corrected,
        "blindness_level": blindness_level,
        "blindness_level_num": _cn_num_to_int(blindness_level.replace("级", "")) if blindness_level else None,
        "moderate_visual_impairment": "中度视力损害" in joined or (min_corrected is not None and min_corrected <= 0.1),
        "severe_visual_impairment": "重度视力损害" in joined or (min_corrected is not None and min_corrected <= 0.05),
        "visual_field_effective_pct": float(visual_field_match.group(1)) if visual_field_match else None,
        "hess_screen_abnormal": False if "HESS" in joined and any(k in joined for k in ["未见明显异常", "未见异常", "正常"]) else None,
        "evidence": _merge_list_unique([], lines, max_items=8),
    }


def _extract_neuro_facts(markdown_text: str) -> Dict[str, Any]:
    lines = _iter_forensic_lines(markdown_text, ["脑", "颅", "蛛网膜", "硬膜", "开颅", "肌力", "截瘫", "排尿", "排便", "ADL", "护理依赖", "病理征"])
    joined = "\n".join(lines)
    brain_injuries = []
    for name in ["蛛网膜下腔出血", "硬膜下血肿", "硬膜外血肿", "脑挫裂伤", "脑挫伤", "颅骨骨折", "脑震荡"]:
        if name in joined:
            brain_injuries.append(name)
    muscle_grades = re.findall(r"肌力\s*([0-5VⅤⅣⅢⅡⅠ]+)\s*级?", joined)
    adl_match = re.search(r"ADL[^0-9]{0,8}([0-9]{1,3})\s*分", joined, re.I)
    return {
        "brain_injuries": brain_injuries,
        "craniotomy_done": any(k in joined for k in ["开颅", "去骨瓣", "颅骨修补", "血肿清除"]),
        "objective_neuro_deficit": any(k in joined for k in ["截瘫", "肌力4", "肌力Ⅳ", "肌力IV", "病理征阳性", "感觉减退", "排尿功能障碍", "排便功能障碍"]),
        "muscle_strength_grades_raw": muscle_grades[:8],
        "adl_score": int(adl_match.group(1)) if adl_match else None,
        "care_dependency_mentioned": "护理依赖" in joined,
        "clinical_disability_assessable": False if any(k in joined for k in ["无法评定", "无法完成", "不能配合", "呼之不应"])
        else None,
        "evidence": _merge_list_unique([], lines, max_items=8),
    }


def _extract_recognized_injuries(markdown_text: str, vertebral: Dict[str, Any], rib: Dict[str, Any], limb: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    injuries = []
    seen = set()

    def add(name_raw: str, name_normalized: str, body_region: str, injury_type: str, side: str = "", **extra):
        key = (name_normalized, body_region, side, injury_type)
        if key in seen:
            return
        seen.add(key)
        row = {
            "name_raw": name_raw,
            "name_normalized": name_normalized,
            "side": side,
            "body_region": body_region,
            "joint": extra.pop("joint", ""),
            "bone": extra.pop("bone", ""),
            "injury_type": injury_type,
            "is_primary": extra.pop("is_primary", True),
            "causation_accepted": extra.pop("causation_accepted", None),
        }
        row.update(extra)
        injuries.append(row)

    if vertebral.get("levels"):
        for level in vertebral.get("levels", []):
            add(
                name_raw=f"{level}椎体压缩性骨折" if vertebral.get("compression_fracture") else f"{level}椎体骨折",
                name_normalized=f"{level}椎体压缩性骨折" if vertebral.get("compression_fracture") else f"{level}椎体骨折",
                body_region="脊柱",
                injury_type="压缩性骨折" if vertebral.get("compression_fracture") else "骨折",
                bone="椎体",
                vertebra_levels=[level],
            )

    if rib.get("count_confirmed"):
        add(
            name_raw="肋骨骨折",
            name_normalized=f"肋骨骨折{rib.get('count_confirmed')}根",
            body_region="肋骨",
            injury_type="骨折",
            fracture_count=rib.get("count_confirmed"),
            rib_numbers=rib.get("rib_numbers_all", []),
        )

    for item in limb or []:
        add(
            name_raw=item.get("label", "四肢骨折"),
            name_normalized=item.get("label", "四肢骨折"),
            body_region=item.get("body_part", "四肢"),
            side=item.get("side", ""),
            injury_type="骨折",
            bone=item.get("type", ""),
            evidence=item.get("evidence", ""),
        )

    relevant_lines = _iter_forensic_lines(markdown_text)
    joined = "\n".join(relevant_lines)
    for pattern_name, body_region, injury_type in COMMON_INJURY_PATTERNS:
        if pattern_name not in joined:
            continue
        line = next((item for item in relevant_lines if pattern_name in item), pattern_name)
        add(
            name_raw=pattern_name,
            name_normalized=pattern_name,
            body_region=body_region,
            side=_infer_side(line),
            injury_type=injury_type,
            joint=body_region if "关节" in body_region else "",
            evidence=line,
        )

    return injuries[:30]


def _commission_candidate_blocks(markdown_text: str) -> List[dict]:
    """优先从司法鉴定委托书块中读取鉴定要求，避免诉讼金额/病历费用污染委托范围。"""
    blocks = _split_file_blocks(markdown_text)
    if not blocks:
        return [{"filename": "整篇输入", "content": markdown_text or ""}]

    def score(block: dict) -> int:
        filename = str(block.get("filename") or "")
        content = str(block.get("content") or "")[:1200]
        haystack = f"{filename}\n{content}"
        value = 0
        if "已签章" in filename and "司法鉴定委托书" in filename:
            value += 100
        elif "司法鉴定委托书" in filename:
            value += 80
        elif "鉴定委托书" in filename:
            value += 70
        if "司法鉴定委托书" in haystack:
            value += 20
        if any(k in haystack for k in ["鉴定要求", "委托鉴定事项", "鉴定事项", "委托事项"]):
            value += 20
        if any(k in filename for k in ["起诉", "病历", "发票", "费用", "报告单", "出院", "门诊", "住院"]):
            value -= 25
        return value

    ranked = sorted(blocks, key=score, reverse=True)
    selected = [b for b in ranked if score(b) >= 90]
    if not selected:
        selected = [b for b in ranked if score(b) >= 80]
    if not selected:
        selected = [b for b in ranked if score(b) >= 40]
    return selected[:3]


def _extract_commission_requirement_text(content: str) -> List[str]:
    """抽取委托书中“鉴定要求/委托鉴定事项”标题下的语段。"""
    raw_lines = (content or "").splitlines()
    items: List[str] = []
    heading_re = re.compile(r"(?:鉴定要求|委托鉴定事项|鉴定事项|委托事项)\s*[:：]?\s*(.*)")
    stop_re = re.compile(r"^(?:委托人|受理日期|送检材料|鉴定材料|案情摘要|备注|司法鉴定机构|鉴定机构|签名|签章|联系人|联系电话|日期|时间)\s*[:：]?")
    other_heading_re = re.compile(r"^[一二三四五六七八九十]+[、.．]|^\(?[0-9]{1,2}\)?[、.．]")

    idx = 0
    while idx < len(raw_lines):
        line = _forensic_snippet(raw_lines[idx], max_len=420)
        match = heading_re.search(line)
        if not match:
            idx += 1
            continue
        parts = []
        inline = _forensic_snippet(match.group(1), max_len=360)
        if inline and inline != "不详":
            parts.append(inline)
        for extra in raw_lines[idx + 1: idx + 9]:
            text = _forensic_snippet(extra, max_len=360)
            if not text or text == "不详" or text.startswith("### 文件"):
                continue
            if heading_re.search(text) and parts:
                break
            if stop_re.search(text) or (other_heading_re.search(text) and not any(k in text for k in ["伤残", "三期", "误工", "护理", "营养", "医疗费", "合理性"])):
                break
            if any(k in text for k in ["伤残", "残疾", "致残", "三期", "误工", "护理", "营养", "医疗费", "合理性", "鉴定"]):
                parts.append(text)
            elif parts and len("".join(parts)) < 80:
                parts.append(text)
        if parts:
            items.append("；".join(parts))
        idx += 1

    if not items:
        compact = re.sub(r"\s+", "", content or "")
        for pattern in [
            r"鉴定要求[:：]?(.*?)(?:委托人|受理日期|送检材料|鉴定材料|签章|$)",
            r"(?:委托鉴定事项|鉴定事项|委托事项)[:：]?(.*?)(?:委托人|受理日期|送检材料|鉴定材料|签章|$)",
        ]:
            m = re.search(pattern, compact)
            if m:
                text = _forensic_snippet(m.group(1), max_len=520)
                if text and text != "不详":
                    items.append(text)
                break
    return _merge_list_unique([], items, max_items=6)


def _commission_context_lines(markdown_text: str) -> List[str]:
    lines: List[str] = []
    for block in _commission_candidate_blocks(markdown_text):
        filename = str(block.get("filename") or "")
        content = str(block.get("content") or "")
        for item in _extract_commission_requirement_text(content):
            if item and item != "不详":
                lines.append(item)
        if lines and "已签章" in filename and "司法鉴定委托书" in filename:
            break
    return _merge_list_unique([], lines, max_items=8)


def _near_terms(text: str, left_terms: List[str], right_terms: List[str], window: int = 18) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    for left in left_terms:
        for match in re.finditer(re.escape(left), compact):
            span = compact[max(0, match.start() - window): match.end() + window]
            if any(term in span for term in right_terms):
                return True
    return False


def _commission_scope_type(*, disability: bool, period: bool, fee: bool) -> str:
    if disability and period and fee:
        return "disability_period_medical_fee_review"
    if disability and period:
        return "disability_period"
    if disability:
        return "disability_only"
    if period:
        return "period_only"
    return "unresolved"


def _build_commission_facts(markdown_text: str, base_payload: Optional[dict] = None) -> Dict[str, Any]:
    base_payload = base_payload or {}
    raw_items = _commission_context_lines(markdown_text)
    fallback_items = []
    if not raw_items and not base_payload.get("_entrustment_items_fallback"):
        for item in base_payload.get("entrustment_items", []) or []:
            text = _clean_text(item)
            if text != "不详" and any(k in text for k in ["伤残", "三期", "误工", "护理", "营养", "医疗费", "鉴定"]):
                fallback_items.append(text)
    raw_items = _merge_list_unique(raw_items, fallback_items, max_items=8)
    joined = "\n".join(raw_items) if raw_items else ""
    compact = re.sub(r"\s+", "", joined)

    has_period = any(k in compact for k in ["三期", "误工期", "误工期限", "护理期", "护理期限", "营养期", "营养期限"])
    requires_work = has_period and ("三期" in compact or re.search(r"误工(?:期限|时间|期)", compact) is not None)
    requires_nursing = has_period and ("三期" in compact or re.search(r"护理(?:期限|时间|期)", compact) is not None)
    requires_nutrition = has_period and ("三期" in compact or re.search(r"营养(?:期限|时间|期)", compact) is not None)
    if "三期" in compact:
        requires_work = requires_nursing = requires_nutrition = True
    if "护理依赖" in compact and not ("护理期" in compact or "三期" in compact):
        requires_nursing = False

    fee_terms = ["医疗费", "医疗费用", "诊疗费", "诊疗费用", "检查费", "用药", "药费"]
    review_terms = ["合理性", "合理", "审查", "审核", "关联性", "必要性"]
    requires_fee = _near_terms(compact, fee_terms, review_terms, window=18)
    requires_disability = any(k in compact for k in ["伤残", "残疾", "致残程度"])
    period = bool(requires_work or requires_nursing or requires_nutrition)
    scope_type = _commission_scope_type(disability=requires_disability, period=period, fee=bool(requires_fee))

    return {
        "raw_items": raw_items,
        "scope_type": scope_type,
        "requires_disability": bool(requires_disability),
        "requires_work_loss_period": bool(requires_work),
        "requires_nursing_period": bool(requires_nursing),
        "requires_nutrition_period": bool(requires_nutrition),
        "requires_care_dependency": "护理依赖" in compact,
        "requires_medical_fee_review": bool(requires_fee),
        "requires_causation": "因果" in compact or "参与度" in compact,
        "requires_future_treatment_fee": "后续治疗" in compact,
        "is_reappraisal": any(k in compact for k in ["重新鉴定", "重鉴", "重新鉴定意见书"]),
        "scope_source": "raw_commission" if raw_items else "unresolved",
    }

def _build_period_rule_features(markdown_text: str, vertebral: Dict[str, Any], rib: Dict[str, Any], treatment_events: List[dict], limb: Optional[List[Dict[str, Any]]] = None, ligament: Optional[Dict[str, Any]] = None, amputation: Optional[Dict[str, Any]] = None, eye: Optional[Dict[str, Any]] = None, scar_burn: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    joined = markdown_text or ""
    candidate_clauses = []

    def add_clause(clause: str, label: str, work: str, nursing: str, nutrition: str, reason: str):
        candidate_clauses.append({
            "standard": "GA/T1193-2014",
            "clause": clause,
            "label": label,
            "work_loss_period": work,
            "nursing_period": nursing,
            "nutrition_period": nutrition,
            "误工": work,
            "护理": nursing,
            "营养": nutrition,
            "reason": reason,
        })

    stable_spine = bool(vertebral.get("count", 0) > 0 and vertebral.get("levels") and vertebral.get("evidence"))
    if stable_spine:
        if vertebral.get("has_spine_surgery"):
            add_clause("9.1.2", "脊柱骨折手术治疗", "120-180日", "60-90日", "60-90日", "识别到脊柱骨折及手术/内固定治疗")
        else:
            add_clause("9.1.1", "脊柱骨折非手术治疗", "45-150日", "45-60日", "45-60日", "识别到脊柱骨折且未识别到脊柱手术")

    rib_count = rib.get("count_confirmed", 0)
    stable_rib = bool(rib.get("stable_evidence") and rib.get("evidence"))
    if stable_rib and rib_count >= 2:
        add_clause("7.2.2", "多根、多处肋骨骨折", "60-120日", "30-60日", "30-60日", f"识别到肋骨骨折{rib_count}根")
    elif stable_rib and rib_count == 1:
        add_clause("7.2.1", "一处肋骨骨折", "30-45日", "7-15日", "15-30日", "识别到单根肋骨骨折")

    if "胫骨平台" in joined:
        mode = "手术治疗" if any(e.get("is_fracture_surgery") and "膝" in e.get("target_body_part", "") for e in treatment_events) or "内固定" in joined else "非手术治疗"
        add_clause("10.2.13.b" if mode == "手术治疗" else "10.2.13.a", f"胫骨平台骨折{mode}", "120-180日" if mode == "手术治疗" else "90-150日", "60-90日", "30-60日", "识别到胫骨平台骨折")
    if "股骨粗隆间" in joined:
        add_clause("10.2.9.b" if "手术" in joined or "内固定" in joined else "10.2.9.a", "股骨粗隆间骨折", "180-270日", "90-180日", "90-180日", "识别到股骨粗隆间骨折")
    if "跟骨" in joined or "距骨" in joined:
        add_clause("10.2.17.c" if "手术" in joined or "内固定" in joined else "10.2.17.a", "跟/距骨骨折", "90-240日", "60-90日", "60-90日", "识别到跟骨/距骨骨折")
    for item in limb or []:
        if item.get("type") in {"tibial_plateau", "intertrochanteric", "calcaneus_talus"}:
            continue
        clause_map = {
            "clavicle": "10.2.1", "scapula": "10.2.2", "humerus": "10.2.3", "olecranon": "10.2.4",
            "ulna_radius": "10.2.5", "wrist": "10.2.6", "finger_metacarpal": "10.2.7", "femoral_neck": "10.2.8",
            "femoral_shaft": "10.2.10", "femoral_distal": "10.2.11", "patella": "10.2.12", "tibia_fibula": "10.2.14",
            "ankle": "10.2.15", "tarsal": "10.2.16", "metatarsal_toe": "10.2.18",
        }
        clause = clause_map.get(item.get("type"))
        if clause:
            add_clause(f"{clause}.b" if item.get("has_surgery") else f"{clause}.a", item.get("label", "四肢骨折"), "90-180日" if item.get("has_surgery") else "60-150日", "30-90日", "30-90日", "识别到结构化四肢骨折事实")
    if ligament and ligament.get("has_ligament_or_meniscus"):
        add_clause("10.4", "四肢大关节韧带损伤", "60-120日", "60-90日", "30-60日", "识别到韧带/半月板损伤")
    if ligament and ligament.get("has_ligament_or_meniscus") and any(k in joined for k in ["韧带", "肩袖", "肌腱"]):
        add_clause("10.4/10.5", "四肢大关节韧带或主要肌腱损伤", "60-150日", "根据临床情况", "根据临床情况", "识别到韧带/肌腱损伤")
    if amputation and amputation.get("has_amputation_or_loss"):
        add_clause("10.6.2" if amputation.get("level") in {"finger", "toe"} else "10.6.1", "肢体/指趾离断", "60-180日", "30-90日", "30-90日", "识别到肢体或指趾离断/缺失")
    if eye and (eye.get("blindness_level") or eye.get("moderate_visual_impairment") or eye.get("severe_visual_impairment")):
        add_clause("5.1.11", "视神经损伤/视觉功能障碍", "90-120日", "30-45日", "30-45日", "识别到视力或视野损害")
    if any(k in joined for k in ["蛛网膜下腔", "硬膜下", "硬膜外", "脑挫", "颅脑", "开颅"]):
        clause = "4.8" if "开放" in joined else "4.7/4.9"
        add_clause(clause, "颅脑损伤及后遗症", "30-180日或根据临床情况", "根据临床情况", "根据临床情况", "识别到颅脑损伤")
    scar_burn = scar_burn or {}
    burn_evidence = scar_burn.get("evidence") or []
    has_structured_burn = bool(
        scar_burn.get("burn_total_tbsa_percent")
        or scar_burn.get("scar_total_area_cm2")
        or scar_burn.get("scar_bsa_percent")
        or scar_burn.get("face_scar_total_length_cm")
        or any(any(k in str(line) for k in ["瘢痕", "烧伤", "烫伤", "植皮", "清创", "创面", "体表面积"]) for line in burn_evidence)
    )
    if has_structured_burn:
        add_clause("11.1", "烧烫伤/瘢痕相关损伤", "30-120日或根据临床情况", "1-120日", "20-120日", "识别到结构化烧烫伤或瘢痕事实")

    primary = candidate_clauses[0]["label"] if candidate_clauses else ""
    return {
        "primary_injury_type": primary,
        "treatment_mode": "手术治疗" if any(e.get("treatment_type") == "手术" for e in treatment_events) else "非手术/保守或未明示",
        "multiple_injuries": len(candidate_clauses) > 1,
        "candidate_clauses": candidate_clauses[:10],
    }


def _build_rule_features(markdown_text: str, vertebral: dict, rib: dict, rom: List[dict], imaging_events: List[dict], scar_burn: dict, eye: dict, neuro: dict, limb: Optional[List[Dict[str, Any]]] = None, ligament: Optional[Dict[str, Any]] = None, amputation: Optional[Dict[str, Any]] = None, healing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    traumatic_arthritis_evidence = [
        item.get("findings_raw", "") for item in imaging_events
        if "创伤性关节炎" in item.get("normalized_findings_tags", [])
    ]
    max_joint_loss = None
    for row in rom:
        value = row.get("reported_loss_percent")
        if isinstance(value, (int, float)):
            max_joint_loss = value if max_joint_loss is None else max(max_joint_loss, value)
    limb = limb or []
    ligament = ligament or {}
    amputation = amputation or {}
    healing = healing or {}
    hand_foot_score = amputation.get("hand_or_foot_function_score")
    long_bone_internal_fixation = [item for item in limb if item.get("is_long_bone") and item.get("internal_fixation_used")]
    return {
        "two_vertebral_compression_fractures": bool(vertebral.get("compression_fracture") and vertebral.get("count", 0) >= 2),
        "one_vertebral_fracture_with_surgery": bool(vertebral.get("count", 0) == 1 and vertebral.get("has_spine_surgery")),
        "vertebral_count": vertebral.get("count", 0),
        "vertebra_levels": vertebral.get("levels", []),
        "rib_fracture_count": rib.get("count_confirmed", 0),
        "rib_fracture_six_plus": rib.get("six_plus", False),
        "rib_fracture_twelve_plus": rib.get("twelve_plus", False),
        "major_joint_loss_percent_max": max_joint_loss,
        "major_joint_loss_25_plus": bool(max_joint_loss is not None and max_joint_loss >= 25),
        "traumatic_arthritis": bool(traumatic_arthritis_evidence or "创伤性关节炎" in (markdown_text or "")),
        "traumatic_arthritis_evidence": _merge_list_unique([], traumatic_arthritis_evidence, max_items=6),
        "scar_bsa_percent": scar_burn.get("scar_bsa_percent"),
        "body_scar_pct": scar_burn.get("scar_bsa_percent"),
        "face_scar_total_length_cm": scar_burn.get("face_scar_total_length_cm"),
        "face_linear_scar_length_cm": scar_burn.get("face_scar_total_length_cm"),
        "scar": {
            "body_surface_pct": scar_burn.get("scar_bsa_percent"),
            "face_linear_length_cm": scar_burn.get("face_scar_total_length_cm"),
            "scar_total_area_cm2": scar_burn.get("scar_total_area_cm2"),
            "evidence": scar_burn.get("evidence", []),
        },
        "eyeball_missing_or_removed": eye.get("eyeball_missing_or_removed"),
        "blindness_level_one_eye": eye.get("blindness_level_num"),
        "moderate_visual_impairment_one_eye": eye.get("moderate_visual_impairment"),
        "severe_visual_impairment_one_eye": eye.get("severe_visual_impairment"),
        "visual_field_effective_pct_one_eye": eye.get("visual_field_effective_pct"),
        "craniotomy_done": neuro.get("craniotomy_done"),
        "objective_neuro_deficit": neuro.get("objective_neuro_deficit"),
        "adl_score": neuro.get("adl_score"),
        "limb_fractures": limb,
        "long_bone_internal_fixation": bool(long_bone_internal_fixation),
        "long_bone_internal_fixation_items": long_bone_internal_fixation[:8],
        "knee_ligament_meniscus_collateral_surgery_affects_function": bool(ligament.get("knee_cruciate_meniscus_collateral_combo") and ligament.get("has_surgery") and ligament.get("affects_function")),
        "ligament_meniscus_features": ligament,
        "amputation_features": amputation,
        "hand_or_foot_function_score": hand_foot_score,
        "fracture_healing_status": healing,
        "delayed_union_or_nonunion": bool(healing.get("delayed_union") or healing.get("nonunion")),
        "chronic_osteomyelitis_long_nonunion": bool(healing.get("chronic_osteomyelitis_or_sequestrum") and healing.get("long_term_over_one_year")),
    }


def _extract_negative_rule_features(markdown_text: str) -> List[str]:
    negatives = []
    for line in _iter_forensic_lines(markdown_text, ["肌力", "未见", "无", "阴性", "未构成", "未达", "正常", "HESS"]):
        if any(k in line for k in ["肌力V", "肌力Ⅴ", "肌力5", "未见明显异常", "未见异常", "阴性", "未达", "未构成", "正常"]):
            negatives.append(line)
    return _merge_list_unique([], negatives, max_items=12)


def _build_forensic_facts(markdown_text: str, base_payload: Optional[dict] = None) -> Dict[str, Any]:
    """构建面向Judge/Ruler的高召回法医事实层。"""
    base_payload = base_payload or {}
    vertebral = _extract_vertebral_features(markdown_text)
    rib = _extract_rib_fracture_features(markdown_text)
    imaging_events = _extract_imaging_events(markdown_text)
    if not vertebral.get("compression_degree"):
        for event in imaging_events:
            if isinstance(event, dict) and event.get("compression_degree"):
                vertebral["compression_degree"] = event.get("compression_degree")
                break
    existing_degrees = vertebral.get("compression_degrees") if isinstance(vertebral.get("compression_degrees"), list) else []
    for event in imaging_events:
        if isinstance(event, dict) and event.get("compression_degree") and event.get("compression_degree") not in existing_degrees:
            existing_degrees.append(event.get("compression_degree"))
    if existing_degrees:
        vertebral["compression_degrees"] = existing_degrees
    treatment_events = _extract_treatment_events(markdown_text)
    limb = _extract_limb_fracture_features(markdown_text, treatment_events)
    ligament = _extract_ligament_meniscus_features(markdown_text, treatment_events)
    amputation = _extract_amputation_features(markdown_text)
    healing = _extract_healing_features(markdown_text)
    rom = _extract_rom_measurements(markdown_text)
    scar_burn = _extract_scar_burn_facts(markdown_text)
    eye = _extract_eye_facts(markdown_text)
    neuro = _extract_neuro_facts(markdown_text)
    recognized_injuries = _extract_recognized_injuries(markdown_text, vertebral, rib, limb)
    period_features = _build_period_rule_features(markdown_text, vertebral, rib, treatment_events, limb, ligament, amputation, eye, scar_burn)
    rule_features = _build_rule_features(markdown_text, vertebral, rib, rom, imaging_events, scar_burn, eye, neuro, limb, ligament, amputation, healing)

    uncertain_points = []
    if not recognized_injuries:
        uncertain_points.append("未稳定抽取到规则可消费的损伤事实，建议核对OCR或医疗资料。")
    if rule_features.get("two_vertebral_compression_fractures") and not vertebral.get("compression_degree"):
        uncertain_points.append("已识别二椎体压缩性骨折，但未稳定抽取压缩程度。")
    if rule_features.get("major_joint_loss_25_plus") and not rom:
        uncertain_points.append("疑似存在关节功能障碍，但未稳定抽取ROM原始角度。")

    return _normalize_value({
        "schema_version": "forensic_facts_v2",
        "commission": _build_commission_facts(markdown_text, base_payload),
        "recognized_injuries": recognized_injuries,
        "fracture_features": {
            "vertebral": vertebral,
            "rib": rib,
            "limb": limb,
        },
        "imaging_events": imaging_events,
        "treatment_events": treatment_events,
        "rom_measurements": rom,
        "scar_burn_facts": scar_burn,
        "eye_facts": eye,
        "neuro_facts": neuro,
        "ligament_meniscus_features": ligament,
        "amputation_features": amputation,
        "fracture_healing_status": healing,
        "period_rule_features": period_features,
        "rule_features": rule_features,
        "negative_rule_features": _extract_negative_rule_features(markdown_text),
        "uncertain_points": uncertain_points,
    })


def _rule_based_full_extraction(markdown_text: str) -> dict:
    blocks = _split_file_blocks(markdown_text)

    basic_payload = _rule_based_basic_extraction(markdown_text)
    case_payload = _rule_based_case_extraction(markdown_text)
    summary_payload = _rule_based_summary_extraction(markdown_text)

    full_payload = AppraisalInfo(
        incident_summary="不详",
        medical_records=[],
    ).model_dump()

    for field in [
        "client_name",
        "subject_name",
        "subject_gender",
        "subject_ethnicity",
        "subject_birth",
        "subject_address",
        "subject_id",
        "acceptance_date",
        "appraisal_location",
        "present_personnel",
        "case_number",
        "entrustment_items",
        "material_summary",
    ]:
        full_payload[field] = basic_payload.get(field, full_payload.get(field))

    paragraph_1 = _clean_text(case_payload.get("paragraph_1"))
    paragraph_2 = _clean_text(case_payload.get("paragraph_2"))

    full_payload["accident_date"] = case_payload.get("accident_date") or case_payload.get("incident_datetime") or (_extract_first_cn_date(paragraph_1) if paragraph_1 != "不详" else "不详")
    full_payload["incident_date"] = full_payload["accident_date"]
    full_payload["incident_summary"] = paragraph_1 if paragraph_1 != "不详" else "不详"
    if case_payload.get("traffic_accident_fact_responsibility_text"):
        full_payload["traffic_accident_fact_responsibility_text"] = case_payload.get("traffic_accident_fact_responsibility_text")
        full_payload["traffic_accident_source_file"] = case_payload.get("traffic_accident_source_file", "")
    full_payload["diagnosis_overview"] = paragraph_2 if paragraph_2 != "不详" else "不详"
    full_payload["appraisal_purpose"] = "为案件审理及损害赔偿提供鉴定依据"
    full_payload["defendants"] = case_payload.get("defendants") or _extract_case_defendants(markdown_text)
    if case_payload.get("lawsuit_date"):
        full_payload["lawsuit_date"] = case_payload.get("lawsuit_date")
    if case_payload.get("entrusting_court"):
        full_payload["entrusting_court"] = case_payload.get("entrusting_court")

    responsibility_match = re.search(r"([^。；\n]*(?:责任|认定)[^。；\n]*)", paragraph_1)
    full_payload["responsibility"] = case_payload.get("responsibility") or _safe_group(responsibility_match, 1)

    location_match = re.search(r"在([^，。\n]{2,60}(?:路|路口|街|道|村|镇|小区|苑|门岗|道闸)[^，。\n]{0,30})", paragraph_1)
    full_payload["incident_location"] = case_payload.get("incident_location") or _safe_group(location_match, 1)

    raw_summary_items = [item for item in summary_payload.get("raw_medical_summary_items", []) if isinstance(item, dict)]
    summary_items = [item for item in summary_payload.get("items", []) if isinstance(item, dict)]
    target_summary_items, target_audit = _select_key_medical_summary_items(raw_summary_items or summary_items, full_payload.get("incident_date", ""))
    if target_summary_items:
        summary_items = target_summary_items
    subject_name = _clean_text(full_payload.get("subject_name"))
    if subject_name != "不详":
        for item in summary_items:
            item["subject_name"] = subject_name
    full_payload["medical_summary_items"] = summary_items
    # 资料摘要覆盖度自检：材料里有几类病历、系统生成了几条摘要。
    # 第三部分按工作流允许人工补充，但缺了多少必须让鉴定人看得见。
    full_payload["summary_completeness"] = {
        "detected_documents": summary_payload.get("detected_medical_documents") or [],
        "summary_item_count": len(summary_items),
    }
    full_payload["other_summary_items"] = summary_payload.get("other_summary_items", []) or []
    full_payload["summary_selection_audit"] = target_audit or summary_payload.get("summary_selection_audit", []) or []
    full_payload["medical_records"] = [_summary_item_to_medical_record(item) for item in summary_items]

    drafting_points = []
    if paragraph_1 != "不详":
        drafting_points.append(paragraph_1)
    if paragraph_2 != "不详":
        drafting_points.append(paragraph_2)
    for item in summary_items:
        record_date = _clean_text(item.get("record_date"))
        diagnosis = _clean_text(item.get("discharge_diagnosis"))
        if diagnosis != "不详":
            if record_date != "不详":
                drafting_points.append(f"{record_date}：{diagnosis}")
            else:
                drafting_points.append(diagnosis)
        if len(drafting_points) >= 8:
            break
    full_payload["drafting_points"] = _merge_list_unique([], drafting_points, max_items=8)

    full_payload["conflicting_or_unclear_points"] = _merge_list_unique(
        _merge_list_unique([], basic_payload.get("uncertain_points", []), max_items=12),
        _merge_list_unique(
            _merge_list_unique([], case_payload.get("uncertain_points", []), max_items=12),
            _merge_list_unique([], summary_payload.get("uncertain_points", []), max_items=12),
            max_items=12,
        ),
        max_items=12,
    )

    selected_file_names = [b.get("filename", "") for b in blocks if b.get("filename")]
    case_count = sum(1 for b in blocks if _is_case_material(b.get("filename", "")))
    medical_count = sum(1 for b in blocks if _is_medical_material(b.get("filename", "")))

    full_payload["_parse_stats"] = {
        "total_files": len(blocks),
        "selected_files": len(selected_file_names),
        "selected_file_names": selected_file_names,
        "selected_case_files": case_count,
        "selected_medical_files": medical_count,
        "truncated": False,
        "mode": "rules",
        "llm_used": False,
        "llm_error": None,
    }

    return _normalize_value(full_payload)


def render_basic_summary_markdown(extracted_data: dict, stats: dict) -> str:
    data = _normalize_value(extracted_data)

    def _with_cn_tail(text: str) -> str:
        value = _clean_text(text)
        if value == "不详":
            return value
        if value.endswith(("。", "！", "？")):
            return value
        return f"{value}。"

    def _format_record_number(record_number: str) -> str:
        value = _clean_text(record_number)
        if value == "不详":
            return ""
        if re.match(r"^[A-Za-z0-9\-]{4,}$", value):
            return f"（病案号{value}）"
        return f"（{value}）"

    def _compose_item_sentence(item: dict, style: str = "generic") -> str:
        segments = []

        hospitalization = _clean_text(item.get("hospitalization_period"))
        if style == "inpatient":
            # 住院病历固定首句输出住院时间，便于法医快速校核住院区间
            value = hospitalization if hospitalization != "不详" else "不详"
            segments.append(f"住院时间：{_with_cn_tail(value)}")
        elif hospitalization != "不详" and style != "emergency":
            segments.append(f"住院时间：{_with_cn_tail(hospitalization)}")

        if style == "emergency":
            chief_text = _clean_text(item.get("chief_complaint"))
            present_text = _clean_text(item.get("present_history"))
            physical_text = _clean_text(item.get("physical_exam"))
            if "血压" in physical_text:
                if ("活动受限" in chief_text) or ("活动受限" in present_text):
                    item = dict(item)
                    item["physical_exam"] = "左下肢活动受限"

            fields = [
                ("主诉", "chief_complaint"),
                ("体格检查", "physical_exam"),
                ("诊断", "discharge_diagnosis"),
            ]
        else:
            fields = [
                ("主诉", "chief_complaint"),
                ("现病史", "present_history"),
                ("体格检查", "physical_exam"),
                ("专科检查", "specialist_findings"),
                ("诊疗经过", "treatment_course"),
                ("术后复查拍片示", "post_op_review"),
                ("出院诊断", "discharge_diagnosis"),
                ("处理意见", "treatment_advice"),
            ]

        def _detect_imaging_method(row: dict) -> str:
            haystack = "\n".join([
                _clean_text(row.get("imaging_findings")),
                _clean_text(row.get("present_history")),
                _clean_text(row.get("chief_complaint")),
                _clean_text(row.get("post_op_review")),
                _clean_text(row.get("treatment_course")),
                _clean_text(row.get("material_name")),
                _clean_text(row.get("source_file")),
            ])
            rules = [
                ("CT", r"CT|ＣＴ|ct"),
                ("DR", r"\bDR\b|dr|数字化摄影"),
                ("MRI", r"MRI|核磁|磁共振"),
                ("X线", r"X线|x线|X-ray"),
                ("超声", r"超声|B超"),
            ]
            for method, pattern in rules:
                if re.search(pattern, haystack):
                    return method
            return "影像"

        imaging = _clean_text(item.get("imaging_findings"))
        if imaging != "不详" and style != "emergency":
            if re.search(r"^(CT|DR|MRI|X线|超声)[示:：]", imaging):
                segments.append(_with_cn_tail(imaging))
            else:
                method = _detect_imaging_method(item)
                segments.append(f"{method}示：{_with_cn_tail(imaging)}")

        for label, field_name in fields:
            value = _clean_text(item.get(field_name))
            if value != "不详":
                segments.append(f"{label}：{_with_cn_tail(value)}")

        return "".join(segments)

    def _pick_primary(items: List[dict], material_keyword: str) -> Optional[dict]:
        candidates = [it for it in items if material_keyword in _clean_text(it.get("material_name"))]
        if not candidates:
            return None

        if material_keyword == "急诊":
            def _emergency_quality(row: dict) -> int:
                score = 0
                chief = _clean_text(row.get("chief_complaint"))
                physical = _clean_text(row.get("physical_exam"))
                diagnosis = _clean_text(row.get("discharge_diagnosis"))
                present = _clean_text(row.get("present_history"))
                treatment = _clean_text(row.get("treatment_course"))

                if chief != "不详":
                    score += 3
                    if len(chief) <= 40:
                        score += 1
                if physical != "不详":
                    score += 2
                if diagnosis != "不详":
                    score += 4
                if re.search(r"下肢疼痛|骨折", diagnosis):
                    score += 2

                if present != "不详" and len(present) > 120:
                    score -= 2
                if treatment != "不详":
                    score -= 2
                if re.search(r"入院|住院|术后", f"{present} {treatment}"):
                    score -= 2
                return score

            candidates.sort(
                key=lambda x: (
                    -_emergency_quality(x),
                    _date_sort_key({"visit_date": x.get("record_date", "不详")}),
                )
            )
            return candidates[0]

        candidates.sort(key=_summary_item_score, reverse=True)
        candidates.sort(key=lambda x: _date_sort_key({"visit_date": x.get("record_date", "不详")}))
        return candidates[0]

    def _pick_emergency_items(items: List[dict]) -> List[dict]:
        candidates = [it for it in items if "急诊" in _clean_text(it.get("material_name"))]
        if not candidates:
            return []

        date_by_record = {}
        for row in candidates:
            record_number = _clean_text(row.get("record_number"))
            record_date = _clean_text(row.get("record_date"))
            if record_number != "不详" and record_date != "不详":
                date_by_record[record_number] = record_date

        grouped = {}
        for row in candidates:
            date_text = _clean_text(row.get("record_date"))
            record_number = _clean_text(row.get("record_number"))
            if date_text == "不详" and record_number in date_by_record:
                date_text = date_by_record[record_number]
            if date_text == "不详":
                date_text = _extract_first_cn_date("\n".join([
                    row.get("hospitalization_period", ""),
                    row.get("chief_complaint", ""),
                    row.get("present_history", ""),
                    row.get("treatment_course", ""),
                ]))
            hospital_name = _clean_text(row.get("hospital_name"))
            if date_text != "不详" and record_number != "不详":
                key = (date_text, record_number)
            elif date_text != "不详":
                key = (date_text, hospital_name)
            elif record_number != "不详":
                key = (record_number, hospital_name)
            else:
                key = (date_text, record_number, hospital_name, _clean_text(row.get("source_file")))

            old = grouped.get(key)
            grouped[key] = _merge_summary_item(old, row) if old else row

        result = list(grouped.values())
        result.sort(key=lambda x: _date_sort_key({"visit_date": x.get("record_date", "不详")}))
        return result[:3]

    def _pick_inpatient_items(items: List[dict]) -> List[dict]:
        candidates = [it for it in items if "住院" in _clean_text(it.get("material_name"))]
        if not candidates:
            return []

        unique = {}
        for row in candidates:
            record_number = _clean_text(row.get("record_number"))
            record_date = _clean_text(row.get("record_date"))
            hospitalization = _clean_text(row.get("hospitalization_period"))

            year = "不详"
            for value in [record_date, hospitalization]:
                match = re.search(r"([0-9]{4})年", value)
                hit = _safe_group(match, 1)
                if hit != "不详":
                    year = hit
                    break

            if record_number != "不详" and year != "不详":
                key = (record_number, year)
            elif record_number != "不详":
                key = (record_number, record_date)
            else:
                key = (
                    record_date,
                    _clean_text(row.get("hospital_name")),
                    _clean_text(row.get("source_file")),
                )

            old = unique.get(key)
            unique[key] = _merge_summary_item(old, row) if old else row

        def _item_date(row: dict) -> str:
            date_text = _clean_text(row.get("record_date"))
            if date_text != "不详":
                return date_text
            return _extract_first_cn_date(row.get("hospitalization_period", ""))

        result = list(unique.values())
        anchored_record_numbers = set()
        for row in result:
            record_number = _clean_text(row.get("record_number"))
            has_date = _clean_text(row.get("record_date")) != "不详"
            has_period = _clean_text(row.get("hospitalization_period")) != "不详"
            if record_number != "不详" and (has_date or has_period):
                anchored_record_numbers.add(record_number)

        filtered = []
        for row in result:
            record_number = _clean_text(row.get("record_number"))
            has_date = _clean_text(row.get("record_date")) != "不详"
            has_period = _clean_text(row.get("hospitalization_period")) != "不详"
            if record_number in anchored_record_numbers and (not has_date) and (not has_period):
                continue
            filtered.append(row)

        result = filtered
        result.sort(key=lambda x: _date_sort_key({"visit_date": _item_date(x)}))
        if len(result) <= 2:
            return result

        by_year = {}
        for row in result:
            date_text = _item_date(row)
            year_match = re.search(r"([0-9]{4})年", date_text)
            year_key = _safe_group(year_match, 1)
            old = by_year.get(year_key)
            if old is None or _summary_item_score(row) > _summary_item_score(old):
                by_year[year_key] = row

        selected = list(by_year.values())
        selected.sort(key=lambda x: _date_sort_key({"visit_date": _item_date(x)}))
        return selected[:3]

    def _borrow_meta(items: List[dict], record_number: str, field_name: str) -> str:
        key_number = _clean_text(record_number)
        candidates = []
        for row in items:
            if key_number != "不详" and _clean_text(row.get("record_number")) != key_number:
                continue
            value = _clean_text(row.get(field_name))
            if value != "不详":
                candidates.append(row)
        if not candidates:
            return "不详"
        candidates.sort(key=_summary_item_score, reverse=True)
        return _clean_text(candidates[0].get(field_name))

    def _is_plausible_hospital(name: str) -> bool:
        value = _clean_text(name)
        if value == "不详":
            return False
        if "医院" not in value:
            return False
        return len(value) >= 6

    lines = []
    lines.append("# 司法鉴定意见书结构化提要（资料摘要，AI草稿）")
    lines.append("")
    lines.append("## 三、资料摘要")

    items = [item for item in data.get("items", []) if isinstance(item, dict)]
    if items:
        emergency_rows = _pick_emergency_items(items)
        inpatient_rows = _pick_inpatient_items(items)
        line_index = 1

        if emergency_rows:
            for em_idx, emergency in enumerate(emergency_rows, 1):
                subject_name = _clean_text(emergency.get("subject_name"))
                if subject_name == "不详":
                    subject_name = "xxx"
                record_date = _clean_text(emergency.get("record_date"))
                hospital_name = _clean_text(emergency.get("hospital_name"))
                record_number = emergency.get("record_number")
                if record_date == "不详":
                    record_date = _borrow_meta(items, record_number, "record_date")
                if hospital_name == "不详":
                    hospital_name = _borrow_meta(items, record_number, "hospital_name")
                material_name = _clean_text(emergency.get("material_name"))
                if material_name == "不详":
                    material_name = "急诊病历"

                title = f"{line_index}."
                if len(emergency_rows) > 1:
                    title += f"（{em_idx}）"
                title += f"据{subject_name}的"
                if record_date != "不详":
                    title += record_date
                if hospital_name != "不详":
                    title += hospital_name
                title += material_name
                title += _format_record_number(record_number)
                title += "记载："
                lines.append(f"{title}{_compose_item_sentence(emergency, style='emergency')}")
                line_index += 1

        if inpatient_rows:
            emergency_hospital = "不详"
            if emergency_rows:
                emergency_hospital = _clean_text(emergency_rows[0].get("hospital_name"))

            for row in inpatient_rows:
                row = dict(row)
                record_number = row.get("record_number")

                if _clean_text(row.get("hospitalization_period")) == "不详":
                    borrowed_hosp_period = _borrow_meta(items, record_number, "hospitalization_period")
                    if borrowed_hosp_period != "不详":
                        row["hospitalization_period"] = borrowed_hosp_period

                if _clean_text(row.get("hospitalization_period")) == "不详":
                    inferred = _extract_hospitalization_period("\n".join([
                        row.get("treatment_course", ""),
                        row.get("treatment_advice", ""),
                        row.get("present_history", ""),
                    ]))
                    if inferred != "不详":
                        row["hospitalization_period"] = inferred

                subject_name = _clean_text(row.get("subject_name"))
                if subject_name == "不详":
                    subject_name = "xxx"

                record_date = _clean_text(row.get("record_date"))
                if record_date == "不详":
                    record_date = _extract_first_cn_date(row.get("hospitalization_period", ""))

                hospital_name = _clean_text(row.get("hospital_name"))
                if (not _is_plausible_hospital(hospital_name)) and _is_plausible_hospital(emergency_hospital):
                    hospital_name = emergency_hospital
                if not _is_plausible_hospital(hospital_name):
                    borrowed = _borrow_meta(items, record_number, "hospital_name")
                    if _is_plausible_hospital(borrowed):
                        hospital_name = borrowed

                material_name = _clean_text(row.get("material_name"))
                if material_name == "不详":
                    material_name = "住院病历"

                title = f"{line_index}.据{subject_name}的"
                if record_date != "不详":
                    title += record_date
                if hospital_name != "不详":
                    title += hospital_name
                title += material_name
                title += _format_record_number(record_number)
                title += "记载："

                detail = _compose_item_sentence(row, style="inpatient")
                lines.append(f"{title}{detail}")
                line_index += 1

        if (not emergency_rows) and (not inpatient_rows):
            fallback_rows = list(items)
            fallback_rows.sort(key=_summary_item_score, reverse=True)
            fallback_rows = fallback_rows[:4]
            for row in fallback_rows:
                subject_name = _clean_text(row.get("subject_name"))
                if subject_name == "不详":
                    subject_name = "xxx"
                record_date = _clean_text(row.get("record_date"))
                hospital_name = _clean_text(row.get("hospital_name"))
                material_name = _clean_text(row.get("material_name"))
                if material_name == "不详":
                    material_name = "相关医疗材料"

                title = f"{line_index}.据{subject_name}的"
                if record_date != "不详":
                    title += record_date
                if hospital_name != "不详":
                    title += hospital_name
                title += material_name
                title += _format_record_number(row.get("record_number"))
                title += "记载："
                lines.append(f"{title}{_compose_item_sentence(row)}")
                line_index += 1
    else:
        lines.append("1.未检索到可用于第三部分成文的医疗资料。")

    source_files = [name for name in stats.get("selected_file_names", []) if name]
    lines.append("")
    lines.append("## 信息来源")
    lines.append(
        f"- 定向解析：按 {stats.get('requirement_count', 0)} 项需求逐项抽取；覆盖文件 {stats.get('selected_files', 0)}/{stats.get('total_files', 0)}"
    )
    _append_hybrid_trace_line(lines, stats)
    if source_files:
        for name in source_files:
            lines.append(f"- {name}")
    else:
        lines.append("- 未记录来源文件名")

    uncertain = data.get("uncertain_points", [])
    if uncertain:
        lines.append("")
        lines.append("## 待核信息")
        for item in uncertain:
            lines.append(f"- {item}")

    return "\n".join(lines)


def _append_hybrid_trace_line(lines: List[str], stats: dict) -> None:
    if "llm_attempted" not in stats:
        return

    llm_attempted = bool(stats.get("llm_attempted"))
    llm_used = bool(stats.get("llm_used"))
    lines.append(
        f"- 混合补全：LLM调用={'是' if llm_attempted else '否'}；有效补全={'是' if llm_used else '否'}"
    )


def render_basic_case_markdown(extracted_data: dict, stats: dict) -> str:
    data = _normalize_value(extracted_data)
    lines = []
    lines.append("# 司法鉴定意见书结构化提要（基本案情，AI草稿）")
    lines.append("")
    lines.append("## 二、基本案情")

    paragraph_1 = _clean_text(data.get("paragraph_1"))
    paragraph_2 = _clean_text(data.get("paragraph_2"))

    lines.append(f"1.{paragraph_1 if paragraph_1 != '不详' else '据委托人提供的鉴定资料记载：不详。'}")
    lines.append(f"2.{paragraph_2 if paragraph_2 != '不详' else '经治疗后及诉讼委托经过：不详。'}")

    source_files = [name for name in stats.get("selected_file_names", []) if name]
    lines.append("")
    lines.append("## 信息来源")
    lines.append(
        f"- 定向解析：按 {stats.get('requirement_count', 0)} 项需求逐项抽取；覆盖文件 {stats.get('selected_files', 0)}/{stats.get('total_files', 0)}"
    )
    _append_hybrid_trace_line(lines, stats)
    if source_files:
        for name in source_files:
            lines.append(f"- {name}")
    else:
        lines.append("- 未记录来源文件名")

    uncertain = data.get("uncertain_points", [])
    if uncertain:
        lines.append("")
        lines.append("## 待核信息")
        for item in uncertain:
            lines.append(f"- {item}")

    return "\n".join(lines)


def render_structured_markdown(extracted_data: dict, stats: dict) -> str:
    """输出严格对齐司法鉴定意见书前三部分结构的结构化摘要。"""

    data = _normalize_value(extracted_data)

    medical_records = data.get("medical_records", [])
    if isinstance(medical_records, list):
        medical_records = sorted(medical_records, key=_date_sort_key)

    lines = []
    lines.append("# 司法鉴定意见书结构化提要（第1-3部分，AI草稿，供法医复核）")
    lines.append("")

    lines.append("## 一、基本情况")
    lines.append(f"- 委托人：{_clean_text(data.get('client_name'))}")

    entrustment_items = data.get("entrustment_items", [])
    if entrustment_items:
        lines.append(f"- 委托鉴定事项：{'；'.join(entrustment_items)}")
    else:
        lines.append("- 委托鉴定事项：不详")

    lines.append(f"- 受理日期：{_clean_text(data.get('acceptance_date'))}")
    lines.append(f"- 鉴定地点：{_clean_text(data.get('appraisal_location'))}")

    present_personnel = data.get("present_personnel", [])
    lines.append(f"- 在场人员：{'；'.join(present_personnel) if present_personnel else '不详'}")

    lines.append(
        f"- 被鉴定人：{_clean_text(data.get('subject_name'))}，{_clean_text(data.get('subject_gender'))}，"
        f"民族：{_clean_text(data.get('subject_ethnicity'))}，出生：{_clean_text(data.get('subject_birth'))}，"
        f"住址：{_clean_text(data.get('subject_address'))}，身份证号：{_clean_text(data.get('subject_id'))}"
    )

    lines.append(f"- 案号：{_clean_text(data.get('case_number'))}")

    material_summary = data.get("material_summary", [])
    if material_summary:
        for item in material_summary:
            lines.append(f"- 鉴定材料：{item}")
    else:
        lines.append("- 鉴定材料：不详")

    lines.append(
        f"- 解析覆盖率：{stats.get('selected_files', 0)}/{stats.get('total_files', 0)} "
        f"（案情类：{stats.get('selected_case_files', 0)}，医学类：{stats.get('selected_medical_files', 0)}，"
        f"截断：{'是' if stats.get('truncated') else '否'}）"
    )
    _append_hybrid_trace_line(lines, stats)
    lines.append("")

    lines.append("## 二、基本案情")
    lines.append(f"- 事故经过：{_clean_text(data.get('incident_summary'))}")
    lines.append(f"- 事故时间：{_clean_text(data.get('incident_date'))}")
    lines.append(f"- 事故地点：{_clean_text(data.get('incident_location'))}")
    lines.append(f"- 责任认定：{_clean_text(data.get('responsibility'))}")

    defendants = data.get("defendants", [])
    if defendants:
        lines.append(f"- 涉及人员/单位：{'；'.join(defendants)}")

    lines.append(f"- 诊疗概况：{_clean_text(data.get('diagnosis_overview'))}")
    lines.append(f"- 鉴定目的：{_clean_text(data.get('appraisal_purpose'))}")
    lines.append("")

    lines.append("## 三、资料摘要")

    for idx, med in enumerate(medical_records, 1):
        source_files = med.get("source_files", [])
        source_text = "；".join(source_files) if source_files else "不详"
        lines.append(f"### 病历摘要 {idx}（{_clean_text(med.get('medical_type'))}）")
        lines.append(f"- 来源文件：{source_text}")
        lines.append(f"- 医疗机构：{_clean_text(med.get('hospital_name'))}")
        lines.append(
            f"- 时间：就诊{_clean_text(med.get('visit_date'))}；入院{_clean_text(med.get('admission_date'))}；出院{_clean_text(med.get('discharge_date'))}"
        )
        lines.append(f"- 主诉/现病史：{_clean_text(med.get('complaint_and_history'))}")
        lines.append(f"- 诊断/结论：{_clean_text(med.get('diagnosis'))}")
        lines.append(f"- 诊疗经过：{_clean_text(med.get('treatment_summary'))}")
        lines.append(f"- 关键检查要点：{_clean_text(med.get('key_exam_findings'))}")
        lines.append("")

    unclear_points = data.get("conflicting_or_unclear_points", [])
    if unclear_points:
        lines.append("")
        lines.append("## 附注：材料冲突与不清项")
        for point in unclear_points:
            lines.append(f"- {point}")

    return "\n".join(lines)


def render_basic_situation_markdown(extracted_data: dict, stats: dict) -> str:
    data = _normalize_value(extracted_data)
    lines = []
    lines.append("# 司法鉴定意见书结构化提要（基本情况，AI草稿）")
    lines.append("")
    lines.append("## 一、基本情况")
    lines.append(f"- 委托人：{_clean_text(data.get('client_name'))}")

    entrustment_items = data.get("entrustment_items", [])
    if len(entrustment_items) >= 2:
        lines.append(f"- 委托事项：1.{entrustment_items[0]}；2.{entrustment_items[1]}。")
    elif entrustment_items:
        lines.append(f"- 委托事项：{entrustment_items[0]}")
    else:
        lines.append("- 委托事项：不详")

    lines.append(f"- 受理日期：{_clean_text(data.get('acceptance_date'))}")
    lines.append(f"- 鉴定地点：{_clean_text(data.get('appraisal_location'))}")

    present_personnel = data.get("present_personnel", [])
    lines.append(f"- 在场人员：{'；'.join(present_personnel) if present_personnel else '不详'}")

    lines.append(
        f"- 被鉴定人：{_clean_text(data.get('subject_name'))}，{_clean_text(data.get('subject_gender'))}，"
        f"民族：{_clean_text(data.get('subject_ethnicity'))}，出生：{_clean_text(data.get('subject_birth'))}，"
        f"住址：{_clean_text(data.get('subject_address'))}，身份证号：{_clean_text(data.get('subject_id'))}"
    )
    lines.append(f"- 案号：{_clean_text(data.get('case_number'))}")

    material_summary = data.get("material_summary", [])
    if material_summary:
        lines.append("- 鉴定材料：")
        for idx, item in enumerate(material_summary, 1):
            lines.append(f"  {idx}. {item}")
    else:
        lines.append("- 鉴定材料：不详")

    source_files = [name for name in stats.get("selected_file_names", []) if name]
    lines.append("")
    lines.append("## 信息来源")
    lines.append(
        f"- 定向解析：按 {stats.get('requirement_count', 0)} 项需求逐项抽取；覆盖文件 {stats.get('selected_files', 0)}/{stats.get('total_files', 0)}"
    )
    _append_hybrid_trace_line(lines, stats)
    if source_files:
        for name in source_files:
            lines.append(f"- {name}")
    else:
        lines.append("- 未记录来源文件名")

    uncertain = data.get("uncertain_points", [])
    if uncertain:
        lines.append("")
        lines.append("## 待核信息")
        for item in uncertain:
            lines.append(f"- {item}")

    return "\n".join(lines)


def _invoke_chain_with_retry(chain, payload_input: dict, request_timeout_seconds: int, max_attempts: int, call_name: str):
    max_attempts = max(1, max_attempts)
    for attempt in range(1, max_attempts + 1):
        print(f"[*] {call_name} 第{attempt}/{max_attempts}次调用，超时阈值 {request_timeout_seconds}s")
        try:
            result_holder = {"result": None, "error": None}

            def _runner():
                try:
                    result_holder["result"] = chain.invoke(payload_input)
                except Exception as e:  # pragma: no cover
                    result_holder["error"] = e

            worker = threading.Thread(target=_runner, daemon=True)
            worker.start()
            worker.join(timeout=request_timeout_seconds + 10)

            if worker.is_alive():
                raise TimeoutError(f"模型调用超时（>{request_timeout_seconds}s）")

            if result_holder["error"] is not None:
                raise result_holder["error"]

            return result_holder["result"]
        except Exception as e:
            if attempt < max_attempts:
                wait_seconds = attempt * 3
                print(f"[!] {call_name} 第{attempt}次调用失败: {e}；{wait_seconds}秒后重试...")
                time.sleep(wait_seconds)
            else:
                print(f"[X] {call_name} 处理失败: {e}")
    return None


def extract_basic_situation_chunked(
    markdown_text: str,
    request_timeout_seconds: int = 90,
    max_attempts: int = 2,
    chunk_max_chars: int = 32000,
) -> dict:
    """按"基本情况"各字段需求定向检索并抽取，避免通用信息泛化。"""

    config = get_runtime_config()
    if config.llm_provider == "off":
        raise RuntimeError("当前模型调用模式为 off。")

    blocks = _split_file_blocks(markdown_text)
    if not blocks:
        blocks = [{"filename": "整篇输入", "content": markdown_text}]

    per_field_max_chars = max(2500, min(9000, max(8000, chunk_max_chars) // 2))
    contexts, stats = _build_basic_requirement_contexts(blocks, field_max_chars=per_field_max_chars)

    llm = build_chat_llm(
        config,
        temperature=0.0,
        timeout=request_timeout_seconds,
        max_retries=0,
        max_tokens=1400,
    )

    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            append_local_prompt_if_needed("""你是一位严谨的法医临床鉴定助理。请按"基本情况"字段逐项提取信息。

你将收到按字段定向检索后的 7 组上下文（每组对应一个需求），请严格按对应关系抽取：
1) 委托人 -> 使用"委托人上下文"
2) 案号 -> 使用"案号上下文"
3) 在场人员 -> 使用"在场人员上下文"
4) 被鉴定人信息 -> 使用"被鉴定人上下文"
5) 鉴定材料第1条（委托书）-> 使用"鉴定材料-委托书上下文"
6) 鉴定材料第2条（诉状/申请/笔录/病历）-> 使用"鉴定材料-文书病历上下文"
7) 鉴定材料第3条（实物/影像）-> 使用"鉴定材料-实物影像上下文"

规则：
1. 只能根据对应上下文提取，不要跨上下文臆测。
2. 无法确认的字段写"不详"。
3. present_personnel 按"姓名（身份）"输出。
4. material_summary 必须输出 3 条，分别对应上述第5-7项。
5. entrustment_items、acceptance_date、appraisal_location 会由程序固定覆盖，可保持默认值。
6. evidence_snippets 最多 8 条。
7. 仅输出 JSON。
{format_instructions}
""", config, "basic_situation"),
        ),
        (
            "human",
            """【委托人上下文】
{ctx_client_name}

【案号上下文】
{ctx_case_number}

【在场人员上下文】
{ctx_present_personnel}

【被鉴定人上下文】
{ctx_subject_profile}

【鉴定材料-委托书上下文】
{ctx_material_1}

【鉴定材料-文书病历上下文】
{ctx_material_2}

【鉴定材料-实物影像上下文】
{ctx_material_3}

请输出"基本情况"字段。""",
        ),
    ])

    chain = prompt | llm | basic_situation_parser

    payload_input = {
        "format_instructions": basic_situation_parser.get_format_instructions(),
        "ctx_client_name": contexts.get("client_name", ""),
        "ctx_case_number": contexts.get("case_number", ""),
        "ctx_present_personnel": contexts.get("present_personnel", ""),
        "ctx_subject_profile": contexts.get("subject_profile", ""),
        "ctx_material_1": contexts.get("material_1", ""),
        "ctx_material_2": contexts.get("material_2", ""),
        "ctx_material_3": contexts.get("material_3", ""),
    }

    print("[*] 准备进行第一部分「基本情况」定向提取（按字段需求映射上下文）或统一提取")
    result = _invoke_chain_with_retry(
        chain=chain,
        payload_input=payload_input,
        request_timeout_seconds=request_timeout_seconds,
        max_attempts=max_attempts,
        call_name="基本情况定向抽取",
    )

    if result is None:
        return None

    payload = _normalize_value(result.model_dump())
    payload = _post_process_basic_situation(payload)
    payload["_parse_stats"] = stats
    return payload

# ========================
# 2. 核心大模型处理流程
# ========================
def extract_key_information(markdown_text: str, request_timeout_seconds: int = 90, max_attempts: int = 2) -> dict:
    """按当前 LLM provider 从 Markdown 中提炼结构化字段。"""

    config = get_runtime_config()
    if config.llm_provider == "off":
        raise RuntimeError("当前模型调用模式为 off。")

    focus_markdown, stats = build_focus_markdown(markdown_text)
    llm = build_chat_llm(
        config,
        temperature=0.0,
        timeout=request_timeout_seconds,
        max_retries=0,
        max_tokens=2200,
    )

    # 设计系统提示词
    prompt = ChatPromptTemplate.from_messages([
        ("system", append_local_prompt_if_needed("""你是一位严谨的法医临床鉴定助理。你的任务是阅读OCR提取出的案卷材料，输出可直接用于起草《司法鉴定意见书》前三部分（基本情况、基本案情、资料摘要）的结构化事实信息。

要求：
1. 你的回答必须完全基于提供的文本。如果某些信息在文本中没有提及，请标记为"不详"。
2. 对医学资料要"全面覆盖"：不得只提住院记录，应覆盖门急诊、住院、影像、病假/诊断证明等关键医疗信息。
3. 相同医院同一日期且信息重复的材料可合并，但必须在 source_files 中列出所有来源文件名。
4. 允许基于多处一致证据进行OCR纠错（如明显错位日期/错字），但不得臆造未出现事实。
5. 只提取事实信息，不进行鉴定过程、分析说明或鉴定意见的推理与输出（这些由后续Agent处理）。
6. 请严格遵循以下JSON格式进行输出。不要输出任何JSON之外的内容。
7. 必须完整输出所有顶层字段；其中列表字段在无信息时返回 []，特别是 medical_records 必须存在（可为空数组）。
8. 为控制时延，请遵守精简规则：
    - medical_records 合并同类项后最多输出 10 条；
    - drafting_points 最多 8 条，material_summary 最多 12 条；
    - 单条文本尽量控制在 120 字以内，避免冗长复述。
{format_instructions}
""", config, "full")),
        (
            "human",
            """【案卷材料内容如下】：
{markdown_text}

【材料统计】
- 总文件数：{total_files}
- 本次纳入解析文件数：{selected_files}
- 其中案情类：{selected_case_files}
- 其中医学类：{selected_medical_files}

请提取用于法医临床鉴定意见书写作的关键信息，并确保医学资料归纳完整。""",
        ),
    ])

    # 组合 Chain
    chain = prompt | llm | parser

    # 执行提取
    print("[*] 正在请求大模型进行信息提取与归纳，这可能需要几十秒，请耐心等待...")
    payload_input = {
        "format_instructions": parser.get_format_instructions(),
        "markdown_text": focus_markdown,
        "total_files": stats["total_files"],
        "selected_files": stats["selected_files"],
        "selected_case_files": stats["selected_case_files"],
        "selected_medical_files": stats["selected_medical_files"],
    }

    result = _invoke_chain_with_retry(
        chain=chain,
        payload_input=payload_input,
        request_timeout_seconds=request_timeout_seconds,
        max_attempts=max_attempts,
        call_name="全量结构化抽取",
    )
    if result is None:
        return None

    payload = result.model_dump()
    payload["_parse_stats"] = stats
    return payload


def _resolve_input_file(cli_input: Optional[str]) -> Optional[str]:
    if cli_input:
        if not os.path.exists(cli_input):
            raise FileNotFoundError(f"输入文件不存在: {cli_input}")
        return cli_input

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return filedialog.askopenfilename(
        title="请选择 Extractor Agent 生成的 Markdown 文件",
        filetypes=[("Markdown Files", "*.md"), ("All Files", "*.*")],
    )


def _normalize_mode_value(raw_mode: Optional[str]) -> Optional[str]:
    if raw_mode is None:
        return None
    mode = str(raw_mode).strip().lower()
    if mode in {"rule", "rules"}:
        return "rules"
    if mode in {"llm", "hybrid"}:
        return mode
    return None


def _mode_label_for_filename(raw_mode: Optional[str]) -> str:
    normalized = _normalize_mode_value(raw_mode)
    if normalized == "rules":
        return "rule"
    if normalized in {"llm", "hybrid"}:
        return normalized
    return "rule"


def _prompt_mode_for_gui(section_name: str, default_mode: str) -> Optional[str]:
    """仅在无命令行参数运行（点击运行按钮）时弹窗选择模式。"""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    normalized_default = _mode_label_for_filename(default_mode)
    prompt = (
        f"当前分节：{section_name}\n"
        "请选择运行模式：rule / llm / hybrid\n"
        f"直接回车默认：{normalized_default}"
    )

    while True:
        answer = simpledialog.askstring(
            "选择运行模式",
            prompt,
            initialvalue=normalized_default,
            parent=root,
        )
        if answer is None:
            root.destroy()
            return None

        text = answer.strip().lower()
        if not text:
            root.destroy()
            return _normalize_mode_value(normalized_default)

        normalized = _normalize_mode_value(text)
        if normalized:
            root.destroy()
            return normalized

        messagebox.showwarning("输入无效", "请输入 rule、llm 或 hybrid。", parent=root)

# ========================
# 3. 运行与文件交互
# ========================
if __name__ == "__main__":
    print("========== Structuring Agent 启动 ==========")

    arg_parser = argparse.ArgumentParser(description="Structuring Agent for forensic case materials")
    arg_parser.add_argument("--input", dest="input_file", help="Extractor Agent 输出的 Markdown 文件路径")
    arg_parser.add_argument("--output-dir", dest="output_dir", default=None, help="输出目录，默认 test_result/<案件编号>")
    arg_parser.add_argument("--timeout", dest="timeout_seconds", type=int, default=90, help="单次模型请求超时时间（秒）")
    arg_parser.add_argument("--max-attempts", dest="max_attempts", type=int, default=2, help="模型请求最大重试次数")
    arg_parser.add_argument(
        "--section",
        dest="section",
        choices=["full", "basic", "case", "summary"],
        default="full",
        help="提取范围：full=第1-3部分全量；basic=仅第一部分「基本情况」；case=仅第二部分「基本案情」；summary=仅第三部分「资料摘要」",
    )
    arg_parser.add_argument(
        "--chunk-max-chars",
        dest="chunk_max_chars",
        type=int,
        default=32000,
        help="basic/case/summary 模式下每个分块的最大字符数",
    )
    arg_parser.add_argument(
        "--basic-mode",
        dest="basic_mode",
        choices=["hybrid", "llm", "rules", "rule"],
        default="hybrid",
        help="basic 模式策略：hybrid=规则优先并在缺失时用LLM补全，llm=仅LLM，rules=仅规则",
    )
    arg_parser.add_argument(
        "--case-mode",
        dest="case_mode",
        choices=["hybrid", "llm", "rules", "rule"],
        default="hybrid",
        help="case 模式策略：hybrid=规则优先并在缺失时用LLM补全，llm=仅LLM，rules=仅规则",
    )
    arg_parser.add_argument(
        "--summary-mode",
        dest="summary_mode",
        choices=["hybrid", "llm", "rules", "rule"],
        default="hybrid",
        help="summary 模式策略：hybrid=规则优先并在缺失时用LLM补全，llm=仅LLM，rules=仅规则",
    )
    arg_parser.add_argument(
        "--full-mode",
        dest="full_mode",
        choices=["hybrid", "llm", "rules", "rule"],
        default="hybrid",
        help="full 模式策略：hybrid=规则优先并用LLM补缺，llm=仅LLM，rules=仅规则",
    )
    arg_parser.add_argument("--llm-provider", choices=["local", "cloud", "off"], default=None, help="LLM调用通道，默认读取 .env，默认 local")
    arg_parser.add_argument("--local-llm-base-url", default=None, help="本地 Ollama OpenAI-compatible base URL")
    arg_parser.add_argument("--local-chat-model", default=None, help="本地 LLM 模型名")
    arg_parser.add_argument("--local-llm-device", choices=["auto", "gpu", "cpu"], default=None, help="本地 LLM 设备，默认 auto/GPU优先")
    arg_parser.add_argument("--cloud-chat-model", default=None, help="云端 LLM 模型名")
    arg_parser.add_argument("--allow-cloud", action="store_true", help="明确允许本次使用云端模型处理材料")
    args = arg_parser.parse_args()

    runtime_config = get_runtime_config(
        llm_provider=args.llm_provider,
        allow_cloud=True if args.allow_cloud else None,
        local_llm_base_url=args.local_llm_base_url,
        local_chat_model=args.local_chat_model,
        local_llm_device=args.local_llm_device,
        cloud_chat_model=args.cloud_chat_model,
    )
    apply_runtime_env(runtime_config)
    if runtime_config.llm_provider == "off":
        args.basic_mode = "rules"
        args.case_mode = "rules"
        args.summary_mode = "rules"
        args.full_mode = "rules"
    if runtime_config.llm_provider == "local" and args.chunk_max_chars == 32000:
        args.chunk_max_chars = min(args.chunk_max_chars, runtime_config.local_llm_context_chars)

    args.basic_mode = _normalize_mode_value(args.basic_mode) or "hybrid"
    args.case_mode = _normalize_mode_value(args.case_mode) or "hybrid"
    args.summary_mode = _normalize_mode_value(args.summary_mode) or "hybrid"
    args.full_mode = _normalize_mode_value(args.full_mode) or "hybrid"

    if not args.input_file:
        if args.section == "basic":
            selected_mode = _prompt_mode_for_gui("basic", args.basic_mode)
            if selected_mode is None:
                print("您已取消模式选择。程序退出。")
                raise SystemExit(0)
            args.basic_mode = selected_mode
        elif args.section == "case":
            selected_mode = _prompt_mode_for_gui("case", args.case_mode)
            if selected_mode is None:
                print("您已取消模式选择。程序退出。")
                raise SystemExit(0)
            args.case_mode = selected_mode
        elif args.section == "summary":
            selected_mode = _prompt_mode_for_gui("summary", args.summary_mode)
            if selected_mode is None:
                print("您已取消模式选择。程序退出。")
                raise SystemExit(0)
            args.summary_mode = selected_mode
        elif args.section == "full":
            selected_mode = _prompt_mode_for_gui("full", args.full_mode)
            if selected_mode is None:
                print("您已取消模式选择。程序退出。")
                raise SystemExit(0)
            args.full_mode = selected_mode

    input_file = _resolve_input_file(args.input_file)

    if not input_file:
        print("您已取消选择。程序退出。")
    else:
        print(f"[*] 已选择文件: {input_file}")

        with open(input_file, 'r', encoding='utf-8') as f:
            raw_markdown = f.read()

        print(f"[*] 成功读取文件，字数：{len(raw_markdown)} 字")
        output_mode_tag = "rule"

        # 提取信息
        if args.section == "basic":
            output_mode_tag = _mode_label_for_filename(args.basic_mode)
            if args.basic_mode == "llm":
                extracted_data = extract_basic_situation_chunked(
                    raw_markdown,
                    request_timeout_seconds=max(30, args.timeout_seconds),
                    max_attempts=max(1, args.max_attempts),
                    chunk_max_chars=max(8000, args.chunk_max_chars),
                )
            elif args.basic_mode == "rules":
                _all_blocks = _split_file_blocks(raw_markdown)
                extracted_data = _rule_based_basic_extraction(raw_markdown)
                extracted_data["_parse_stats"] = {
                    "total_files": len(_all_blocks),
                    "selected_files": len(_all_blocks),
                    "selected_file_names": [b.get("filename", "") for b in _all_blocks if b.get("filename")],
                    "requirement_count": 7,
                    "mode": "rules",
                    "llm_used": False,
                    "llm_error": None,
                }
            else:
                extracted_data = extract_basic_situation_hybrid(
                    raw_markdown,
                    request_timeout_seconds=max(30, args.timeout_seconds),
                    max_attempts=max(1, args.max_attempts),
                    chunk_max_chars=max(8000, args.chunk_max_chars),
                    enable_llm=True,
                )
        elif args.section == "case":
            output_mode_tag = _mode_label_for_filename(args.case_mode)
            if args.case_mode == "llm":
                extracted_data = extract_basic_case_chunked(
                    raw_markdown,
                    request_timeout_seconds=max(30, args.timeout_seconds),
                    max_attempts=max(1, args.max_attempts),
                    chunk_max_chars=max(8000, args.chunk_max_chars),
                )
            elif args.case_mode == "rules":
                _all_blocks = _split_file_blocks(raw_markdown)
                extracted_data = _rule_based_case_extraction(raw_markdown)
                extracted_data["_parse_stats"] = {
                    "total_files": len(_all_blocks),
                    "selected_files": len(_all_blocks),
                    "selected_file_names": [b.get("filename", "") for b in _all_blocks if b.get("filename")],
                    "requirement_count": 4,
                    "mode": "rules",
                    "llm_used": False,
                    "llm_error": None,
                }
            else:
                extracted_data = extract_basic_case_hybrid(
                    raw_markdown,
                    request_timeout_seconds=max(30, args.timeout_seconds),
                    max_attempts=max(1, args.max_attempts),
                    chunk_max_chars=max(8000, args.chunk_max_chars),
                    enable_llm=True,
                )
        elif args.section == "summary":
            output_mode_tag = _mode_label_for_filename(args.summary_mode)
            if args.summary_mode == "llm":
                extracted_data = extract_basic_summary_chunked(
                    raw_markdown,
                    request_timeout_seconds=max(30, args.timeout_seconds),
                    max_attempts=max(1, args.max_attempts),
                    chunk_max_chars=max(8000, args.chunk_max_chars),
                )
            elif args.summary_mode == "rules":
                _all_blocks = _split_file_blocks(raw_markdown)
                extracted_data = _rule_based_summary_extraction(raw_markdown)
                extracted_data["_parse_stats"] = {
                    "total_files": len(_all_blocks),
                    "selected_files": len(_all_blocks),
                    "selected_file_names": [b.get("filename", "") for b in _all_blocks if b.get("filename")],
                    "requirement_count": len(SUMMARY_REQUIREMENT_KEYWORDS),
                    "mode": "rules",
                    "llm_used": False,
                    "llm_error": None,
                }
            else:
                extracted_data = extract_basic_summary_hybrid(
                    raw_markdown,
                    request_timeout_seconds=max(30, args.timeout_seconds),
                    max_attempts=max(1, args.max_attempts),
                    chunk_max_chars=max(8000, args.chunk_max_chars),
                    enable_llm=True,
                )
        else:
            output_mode_tag = _mode_label_for_filename(args.full_mode)
            full_timeout_seconds = max(240, args.timeout_seconds)
            if args.full_mode == "llm":
                extracted_data = extract_key_information(
                    raw_markdown,
                    request_timeout_seconds=full_timeout_seconds,
                    max_attempts=max(1, args.max_attempts),
                )
                if extracted_data is not None:
                    extracted_data.setdefault("_parse_stats", {})
                    extracted_data["_parse_stats"]["mode"] = "llm"
            elif args.full_mode == "rules":
                extracted_data = _rule_based_full_extraction(raw_markdown)
            else:
                rule_data = _rule_based_full_extraction(raw_markdown)
                llm_data = None
                llm_error = None
                need_llm = _payload_needs_llm(rule_data)

                if need_llm:
                    try:
                        llm_data = extract_key_information(
                            raw_markdown,
                            request_timeout_seconds=full_timeout_seconds,
                            max_attempts=max(1, args.max_attempts),
                        )
                    except Exception as e:
                        llm_error = str(e)
                else:
                    print("[*] full 模式规则抽取已覆盖有效信息，跳过 LLM 调用。")

                if llm_data:
                    rule_stats = rule_data.pop("_parse_stats", {})
                    llm_stats = llm_data.pop("_parse_stats", {})
                    extracted_data = _merge_rule_first_struct(_normalize_value(rule_data), _normalize_value(llm_data))
                    selected_file_names = llm_stats.get("selected_file_names") or rule_stats.get("selected_file_names", [])
                    extracted_data["_parse_stats"] = {
                        "total_files": rule_stats.get("total_files", llm_stats.get("total_files", 0)),
                        "selected_files": rule_stats.get("selected_files", llm_stats.get("selected_files", 0)),
                        "selected_file_names": selected_file_names,
                        "selected_case_files": rule_stats.get("selected_case_files", llm_stats.get("selected_case_files", 0)),
                        "selected_medical_files": rule_stats.get("selected_medical_files", llm_stats.get("selected_medical_files", 0)),
                        "truncated": llm_stats.get("truncated", rule_stats.get("truncated", False)),
                        "mode": "hybrid",
                        "llm_used": True,
                        "llm_attempted": True,
                        "llm_error": llm_error,
                    }
                else:
                    if llm_error:
                        print(f"[!] full 模式 LLM 补缺失败，使用规则结果：{llm_error}")
                    elif need_llm:
                        print("[!] full 模式未获得可用 LLM 补缺结果，使用规则结果。")
                    rule_stats = rule_data.pop("_parse_stats", {})
                    extracted_data = _merge_rule_first_struct(_normalize_value(rule_data), None)
                    extracted_data["_parse_stats"] = {
                        **rule_stats,
                        "mode": "hybrid",
                        "llm_used": False,
                        "llm_attempted": need_llm,
                        "llm_error": llm_error,
                    }

        if extracted_data:
            stats = extracted_data.pop("_parse_stats", {})
            stats.update({
                "llm_provider": runtime_config.llm_provider,
                "llm_model": runtime_config.llm_model if runtime_config.llm_provider != "off" else "off",
                "cloud_used": runtime_config.llm_provider == "cloud",
                "cloud_allowed": runtime_config.allow_cloud,
            })
            if args.section == "full":
                extracted_data["forensic_facts"] = _build_forensic_facts(raw_markdown, extracted_data)
            extracted_data = _normalize_value(extracted_data)

            # 决定输出路径：优先写入 test_result/<案件编号>，Boss 会显式传入统一输出目录。
            parent_dir = resolve_numbered_output_dir(input_file, args.output_dir, fallback_name=os.path.basename(os.path.dirname(input_file)))
            if args.section == "basic":
                output_json_name = f"structured_basic_info_{output_mode_tag}.json"
            elif args.section == "case":
                output_json_name = f"structured_case_info_{output_mode_tag}.json"
            elif args.section == "summary":
                output_json_name = f"structured_part3_info_{output_mode_tag}.json"
            else:
                output_json_name = f"structured_info_{output_mode_tag}.json"
            output_json_path = os.path.join(
                parent_dir,
                output_json_name,
            )

            with open(output_json_path, 'w', encoding='utf-8') as f:
                json.dump(extracted_data, f, ensure_ascii=False, indent=4)

            print(f"[*] 关键信息提取成功！已保存至 {output_json_path}")

            # 同时生成一个更贴近司法鉴定写作习惯的 Markdown 摘要
            if args.section == "basic":
                output_md_name = f"structured_basic_summary_{output_mode_tag}.md"
            elif args.section == "case":
                output_md_name = f"structured_case_summary_{output_mode_tag}.md"
            elif args.section == "summary":
                output_md_name = f"structured_part3_summary_{output_mode_tag}.md"
            else:
                output_md_name = f"structured_summary_{output_mode_tag}.md"
            output_md_path = os.path.join(
                parent_dir,
                output_md_name,
            )
            if args.section == "basic":
                md_content = render_basic_situation_markdown(extracted_data, stats)
            elif args.section == "case":
                md_content = render_basic_case_markdown(extracted_data, stats)
            elif args.section == "summary":
                md_content = render_basic_summary_markdown(extracted_data, stats)
            else:
                md_content = render_structured_markdown(extracted_data, stats)

            with open(output_md_path, 'w', encoding='utf-8') as f:
                f.write(md_content)

            print(f"[*] 人类可读的 Markdown 摘要保存至 {output_md_path}")

            # 命令行模式不弹窗，避免阻塞；仅在文件对话框模式下弹窗。
            if args.input_file:
                print("[*] 已完成（命令行模式，无弹窗）。")
            else:
                messagebox.showinfo(
                    "信息提取成功", 
                    f"已从杂乱病历中提取结构化报告所需信息！\n\n数据 JSON 路径：{output_json_path}\n可视化 MD 路径：{output_md_path}"
                )
        else:
            if args.input_file:
                print("[X] 大模型未能成功返回解析结果！")
            else:
                messagebox.showerror("错误", "大模型未能成功返回解析结果！")
