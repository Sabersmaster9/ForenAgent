import argparse
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog

from dotenv import load_dotenv
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agents.common.llm_client import append_local_prompt_if_needed, build_chat_llm
from agents.common.output_paths import resolve_numbered_output_dir
from agents.common.runtime_config import apply_runtime_env, get_runtime_config

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(dotenv_path=ENV_PATH, override=True)

DEFAULT_CHAT_MODEL = os.getenv("FORENAGENT_LOCAL_CHAT_MODEL", "qwen3.5:4b")

WORD_EXTENSIONS = {".docx"}
TEXT_EXTENSIONS = {".txt", ".md", ".text"}
PREFERRED_SECOND_AGENT_FILE = "structured_summary_rule.md"

TRANSCRIPT_HINT_KEYWORDS = ["对白", "逐字", "转写", "录音", "访谈", "检查室"]

FIXED_SECTION4_METHOD = (
    "参照司法鉴定技术规范《法医临床检验规范》（SF/T 0111-2021）、"
    "《法医临床影像学检验实施规范》（SF/T 0112-2021）等进行法医学检验及核实。"
)

FIXED_SECTION4_TOOLS = (
    "使用人体关节测量器（ZDSJ-A-002）、钢卷尺（ZDSJ-A-003）、"
    "影像学阅片软件等工具进行检验。"
)

JOINT_CONFIG = {
    "膝关节": {
        "directions": ["屈曲", "伸展"],
        "scar_keywords": ["膝前", "膝外侧", "膝内侧", "膝关节", "髌骨"],
    },
    "踝关节": {
        "directions": ["背屈", "跖屈"],
        "scar_keywords": ["内踝", "外踝", "小腿下段", "踝部"],
    },
    "髋关节": {
        "directions": ["前屈", "后伸", "外展", "内收", "内旋", "外旋"],
        "scar_keywords": ["大腿根部", "大腿外侧", "大腿上段", "臀部", "髋部"],
    },
    "肩关节": {
        "directions": ["前屈上举", "后伸", "外展上举", "内收", "水平位内旋", "水平位外旋"],
        "scar_keywords": ["肩部", "肩前", "肩外侧", "肩后"],
    },
    "肘关节": {
        "directions": ["屈曲", "伸展"],
        "scar_keywords": ["肘背侧", "肘外侧", "肘内侧", "肘部"],
    },
    "腕关节": {
        "directions": ["掌屈", "背伸", "桡偏", "尺偏"],
        "scar_keywords": ["腕部", "腕背侧", "腕掌侧", "前臂下段"],
    },
}

SCAR_GENERIC_PATTERN = re.compile(
    r"[^\s。；\n]{2,25}?[见有][^\s。；\n]{0,6}?"
    r"[0-9]+(?:\.[0-9]+)?(?:\s*[~\-—至]\s*[0-9]+(?:\.[0-9]+)?)?\s*(?:cm|厘米|㎝)?"
    r"\s*[×x*\~]\s*"
    r"[（(]?\s*[0-9]+(?:\.[0-9]+)?(?:\s*[~\-—至]\s*[0-9]+(?:\.[0-9]+)?)?\s*[）)]?"
    r"\s*(?:cm|厘米|㎝)?\s*"
    r"[^\s。；]*?"
    r"(?:手术|关节镜|挫伤|创伤)?瘢痕",
    re.IGNORECASE,
)

SCAR_NO_VERB_PATTERN = re.compile(
    r"[0-9]+(?:\.[0-9]+)?(?:\s*[~\-—至]\s*[0-9]+(?:\.[0-9]+)?)?\s*(?:cm|厘米|㎝)?"
    r"\s*[×x*\~]\s*"
    r"[（(]?\s*[0-9]+(?:\.[0-9]+)?(?:\s*[~\-—至]\s*[0-9]+(?:\.[0-9]+)?)?\s*[）)]?"
    r"\s*(?:cm|厘米|㎝)?\s*"
    r"[^\s。；]*?"
    r"(?:手术|关节镜|挫伤|创伤)?瘢痕",
    re.IGNORECASE,
)


class PhysicalExamExtraction(BaseModel):
    subject_name: str = Field(default="XXX")
    companion_description: str = Field(default="其代理人")
    entry_description: str = Field(default="步入检查室")
    insurer_presence: str = Field(default="不详")
    chief_complaint: str = Field(default="不详")
    exam_findings: List[str] = Field(default_factory=list)
    rom_findings: List[str] = Field(default_factory=list)
    evidence_quotes: List[str] = Field(default_factory=list)
    uncertain_points: List[str] = Field(default_factory=list)


class AuxiliaryCheckItem(BaseModel):
    hospital: str = Field(default="不详")
    exam_date: str = Field(default="不详")
    exam_name: str = Field(default="不详")
    exam_no: str = Field(default="不详")
    findings: str = Field(default="不详")


class AuxiliaryCheckExtraction(BaseModel):
    items: List[AuxiliaryCheckItem] = Field(default_factory=list)
    evidence_quotes: List[str] = Field(default_factory=list)
    uncertain_points: List[str] = Field(default_factory=list)


def _detect_injured_joints(text: str) -> List[str]:
    if not text:
        return []
    found = []
    for joint_name in JOINT_CONFIG:
        if joint_name in text:
            found.append(joint_name)
    return found


def _clean_text(text: Optional[str], default: str = "不详") -> str:
    if text is None:
        return default
    value = str(text).strip()
    return value if value else default


def _ensure_cn_sentence(text: str) -> str:
    value = _clean_text(text)
    if value == "不详":
        return value
    if value.endswith(("。", "！", "？")):
        return value
    return f"{value}。"


def _today_cn_date() -> str:
    today = date.today()
    return f"{today.year}年{today.month}月{today.day}日"


def _merge_unique(items: List[str], max_items: int = 20) -> List[str]:
    result: List[str] = []
    for item in items:
        value = _clean_text(item)
        if value == "不详":
            continue
        if value in result:
            continue
        result.append(value)
        if len(result) >= max_items:
            break
    return result


def _split_sentences(text: str) -> List[str]:
    chunks = re.split(r"[\n。；;!?！？]+", (text or "").replace("\r", "\n"))
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _trim_text(text: str, max_chars: int) -> str:
    value = (text or "").strip().strip("，,。；;：:")
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip("，,。；;：:")


def _normalize_number_token(token: str) -> str:
    value = (token or "").strip()
    if not value:
        return value
    value = value.replace("零", "0").replace("〇", "0")
    if re.fullmatch(r"0[0-9]+", value):
        value = f"0.{value[1:]}"
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value


def _rule_refine_chief_complaint(raw_text: str, max_chars: int = 20) -> str:
    value = _clean_text(raw_text)
    if value == "不详":
        return "不详"

    value = re.sub(r"^(主诉|自诉)[:：]\s*", "", value)
    value = re.sub(r"[“”\"'\s]+", "", value)
    value = re.sub(r"(就是|有点|然后|特别的|这个|那个|啊|呀|呢|吧|嘛|呃|嗯|哎|诶)", "", value)

    side_prefix = ""
    for side in ["左", "右"]:
        if side in value:
            side_prefix = side
            break

    matched_joint = None
    for joint in JOINT_CONFIG:
        if joint in value:
            matched_joint = joint
            break

    if matched_joint and ("伸不直" in value or "伸不" in value) and (
        "弯下去" in value or "弯不过去" in value or "弯不" in value
    ):
        base = f"{side_prefix}{matched_joint}伸不直，弯不下去" if side_prefix else f"{matched_joint}伸不直，弯不下去"
        return _trim_text(base, max_chars=max_chars)

    chunks = [x.strip("，,。；;：:") for x in re.split(r"[，,。；;]+", value) if x.strip("，,。；;：:")]
    if not chunks:
        return "不详"

    symptom_keywords = ["痛", "酸", "麻", "胀", "肿", "不直", "不下", "受限", "活动", "无力", "僵硬", "屈", "伸", "背屈", "跖屈"]
    picked: List[str] = []
    for chunk in chunks:
        if any(k in chunk for k in symptom_keywords) or len(chunk) <= 8:
            picked.append(chunk)
        if len(picked) >= 2:
            break

    merged = "，".join(picked if picked else chunks[:2])
    merged = re.sub(r"，{2,}", "，", merged).strip("，,。；;：:")
    merged = _trim_text(merged, max_chars=max_chars)
    return merged or "不详"


def _normalize_exam_source_text(physical: PhysicalExamExtraction) -> str:
    source = "\n".join(list(physical.exam_findings) + list(physical.rom_findings) + list(physical.evidence_quotes))
    source = source.replace("区区", "屈曲")
    source = source.replace("屈区", "屈曲")
    source = source.replace("生展", "伸展")
    source = source.replace("背区", "背屈")
    source = source.replace("直曲", "跖屈")
    source = source.replace("零度", "0度")
    source = source.replace("零", "0")
    source = source.replace("到", "-")
    source = source.replace("~", "-").replace("～", "-").replace("至", "-")
    return source


def _extract_motion_range(source: str, side: str, joint: str, motion: str) -> Tuple[Optional[str], Optional[str]]:
    lines = [x.strip() for x in re.split(r"[\n。；;]", source) if x.strip()]

    # 检测段级侧别（如"左髋关节活动度：..."表示后续值属于左）
    context_side = ""
    for line in lines:
        cs_match = re.search(r"([左右])" + re.escape(joint) + r"关节活动度", line)
        if cs_match:
            context_side = cs_match.group(1)
            break

    for line in lines:
        if motion not in line:
            continue
        if joint not in line and not any(f"{joint}关节" in l for l in lines[:3]):
            continue

        # Pattern A: 屈曲：左0°-100°（右0°-140°） or 屈曲：左：5°-135°
        pat_a = rf"{motion}[^0-9零〇\-]*?{side}[：:]*\s*-?([0-9零〇]+(?:\.[0-9]+)?)\s*(?:度|°)?\s*[-~\-—至]\s*([0-9零〇]+(?:\.[0-9]+)?)\s*(?:度|°)?"
        match = re.search(pat_a, line)
        if match:
            return _normalize_number_token(match.group(1)), _normalize_number_token(match.group(2))

        # Pattern B: 屈曲：左0°-100°、右0°-165° (comma-separated bilateral)
        pat_b = rf"{side}[：:]*\s*-?([0-9零〇]+(?:\.[0-9]+)?)\s*(?:度|°)?\s*[-~\-—至]\s*([0-9零〇]+(?:\.[0-9]+)?)\s*(?:度|°)?"
        match = re.search(pat_b, line)
        if match:
            return _normalize_number_token(match.group(1)), _normalize_number_token(match.group(2))

        # Pattern C: single value like 伸展：左-18° or 伸展：右0°
        pat_c = rf"{motion}[^0-9零〇\-]*?{side}[：:]*\s*-?([0-9零〇]+(?:\.[0-9]+)?)\s*(?:度|°)"
        match = re.search(pat_c, line)
        if match:
            value = _normalize_number_token(match.group(1))
            return value, value

        # Pattern D: 段级侧别格式 - 前屈0°-110°（右0°-130°）
        if context_side == side:
            pat_d = rf"{motion}[^0-9零〇\-]*?([0-9零〇]+(?:\.[0-9]+)?)\s*(?:度|°)?\s*[-~\-—至]\s*([0-9零〇]+(?:\.[0-9]+)?)\s*(?:度|°)?"
            match = re.search(pat_d, line)
            if match:
                return _normalize_number_token(match.group(1)), _normalize_number_token(match.group(2))
        elif context_side:
            opposite = "右" if context_side == "左" else "左"
            if side == opposite:
                pat_e = rf"{motion}[^0-9零〇]*?[（(]{side}[：:]*\s*-?([0-9零〇]+(?:\.[0-9]+)?)\s*(?:度|°)?\s*[-~\-—至]?\s*([0-9零〇]+(?:\.[0-9]+)?)?\s*(?:度|°)?[）)]"
                match = re.search(pat_e, line)
                if match:
                    end = match.group(2) or match.group(1)
                    return _normalize_number_token(match.group(1)), _normalize_number_token(end)
                pat_f = rf"[（(]{side}[：:]*\s*-?([0-9零〇]+(?:\.[0-9]+)?)\s*(?:度|°)?\s*[-~\-—至]?\s*([0-9零〇]+(?:\.[0-9]+)?)?\s*(?:度|°)?[）)]"
                match = re.search(pat_f, line)
                if match:
                    end = match.group(2) or match.group(1)
                    return _normalize_number_token(match.group(1)), _normalize_number_token(end)

    return None, None


def _enforce_complaint_coverage(complaint: str, transcript_text: str) -> str:
    value = _clean_text(complaint)
    if value == "不详":
        return value

    text = transcript_text or ""
    has_ext = bool(re.search(r"伸不直|伸不|伸展[^。；\n]*受限|伸直受限", text))
    has_flex = bool(re.search(r"弯下去|弯不过去|弯不|屈曲[^。；\n]*受限", text))

    if has_ext and has_flex:
        matched_joint = None
        for joint in JOINT_CONFIG:
            if joint in text:
                matched_joint = joint
                break
        side_prefix = "左" if "左" in text[:50] else ("右" if "右" in text[:50] else "")
        joint_label = f"{side_prefix}{matched_joint}" if matched_joint and side_prefix else (matched_joint or "关节")
        base = f"{joint_label}伸不直，弯不下去"
        return _trim_text(base, max_chars=20)
    return _trim_text(value, max_chars=20)


def _format_degree_range(left_start: Optional[str], left_end: Optional[str]) -> str:
    if not left_start and not left_end:
        return "不详"
    if not left_start:
        left_start = left_end
    if not left_end:
        left_end = left_start
    if left_start == left_end:
        return f"{left_start}°"
    return f"{left_start}°-{left_end}°"


def _extract_scar_descriptions(source: str) -> List[str]:
    scars: List[str] = []
    seen: set = set()
    for match in SCAR_GENERIC_PATTERN.finditer(source):
        scar_text = match.group(0).strip()
        sig = re.sub(r"\s+", "", scar_text)
        if scar_text and sig not in seen:
            seen.add(sig)
            scars.append(scar_text)
    for match in SCAR_NO_VERB_PATTERN.finditer(source):
        scar_text = match.group(0).strip()
        sig = re.sub(r"\s+", "", scar_text)
        if scar_text and sig not in seen:
            seen.add(sig)
            scars.append(scar_text)
    return scars[:4]


def _build_injury_description(physical: PhysicalExamExtraction) -> str:
    source = _normalize_exam_source_text(physical)

    scar_list = _extract_scar_descriptions(source)

    detected_joints = _detect_injured_joints(source)
    if not detected_joints:
        detected_joints = _detect_injured_joints(
            "\n".join(list(physical.exam_findings) + list(physical.rom_findings))
        )

    detail_parts: List[str] = []
    injured_side_prefix = ""

    for joint_name in detected_joints:
        config = JOINT_CONFIG[joint_name]
        joint_short = joint_name.replace("关节", "")
        directions = config["directions"]

        has_any = False
        dir_parts: List[str] = []
        for motion in directions:
            left_val = _extract_motion_range(source, "左", joint_short, motion)
            right_val = _extract_motion_range(source, "右", joint_short, motion)
            has_left = left_val[0] is not None
            has_right = right_val[0] is not None

            if has_left or has_right:
                has_any = True
                left_str = _format_degree_range(*left_val) if has_left else "不详"
                right_str = _format_degree_range(*right_val) if has_right else "不详"
                dir_parts.append(f"{motion}：左{left_str}（右{right_str}）")

            if not has_left and not has_right:
                left_val2 = _extract_motion_range(source, "", joint_short, motion)
                if left_val2[0] is not None:
                    has_any = True
                    dir_parts.append(f"{motion}：{_format_degree_range(*left_val2)}")

        if has_any and dir_parts:
            detail_parts.append(f"{joint_name}活动度测量：" + "；".join(dir_parts))
            for side in ["左", "右"]:
                if any(f"{side}{joint_short}" in d for d in dir_parts):
                    injured_side_prefix = side
                    break

    pieces: List[str] = []
    for scar in scar_list:
        pieces.append(_ensure_cn_sentence(scar))

    if detail_parts:
        first_joint = detected_joints[0] if detected_joints else "关节"
        prefix = f"{injured_side_prefix}{first_joint}活动受限，" if injured_side_prefix else ""
        pieces.append(_ensure_cn_sentence(prefix + "；".join(detail_parts)))

    if not pieces:
        fallback_findings: List[str] = []
        for item in _merge_unique(list(physical.exam_findings) + list(physical.rom_findings), max_items=10):
            if any(k in item for k in ["神志清", "对答切题", "查体", "精神状态", "回答问题清楚"]):
                continue
            fallback_findings.append(_ensure_cn_sentence(item))
        if fallback_findings:
            return " ".join(fallback_findings)
        return "待补充。"

    return " ".join(pieces)


def _extract_paragraphs_from_wml(xml_bytes: bytes) -> List[str]:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    out: List[str] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out

    for p_node in root.findall(".//w:p", ns):
        runs = [n.text for n in p_node.findall(".//w:t", ns) if n.text and n.text.strip()]
        if runs:
            out.append("".join(runs).strip())
    return out


def _read_docx(file_path: str) -> Tuple[str, int]:
    texts: List[str] = []
    image_count = 0
    with zipfile.ZipFile(file_path, "r") as archive:
        names = archive.namelist()
        image_count = len([name for name in names if name.startswith("word/media/")])

        xml_names = [
            name
            for name in names
            if name.startswith("word/")
            and name.endswith(".xml")
            and (name == "word/document.xml" or name.startswith("word/header") or name.startswith("word/footer"))
        ]
        for xml_name in sorted(xml_names):
            paragraphs = _extract_paragraphs_from_wml(archive.read(xml_name))
            if paragraphs:
                texts.append("\n".join(paragraphs))

    return "\n\n".join(texts).strip(), image_count


def _read_text(file_path: str) -> str:
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read().strip()


def collect_input_files(input_dir: str) -> Dict[str, List[str]]:
    output = {"docs": [], "others": []}
    for path in Path(input_dir).rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith("~$"):
            continue

        suffix = path.suffix.lower()
        value = str(path)
        if suffix in WORD_EXTENSIONS or suffix in TEXT_EXTENSIONS:
            output["docs"].append(value)
        else:
            output["others"].append(value)

    for key in output:
        output[key].sort()
    return output


def load_doc_entries(doc_paths: List[str]) -> Tuple[List[Dict[str, str]], List[str]]:
    docs: List[Dict[str, str]] = []
    warnings: List[str] = []

    for path in doc_paths:
        ext = Path(path).suffix.lower()
        name = os.path.basename(path)
        try:
            if ext in WORD_EXTENSIONS:
                text, image_count = _read_docx(path)
                if image_count > 0:
                    warnings.append(f"{name} 含 {image_count} 张内嵌图片，当前版本默认不做图片OCR。")
            elif ext in TEXT_EXTENSIONS:
                text = _read_text(path)
            else:
                continue

            docs.append({"file": path, "name": name, "text": text})
        except Exception as exc:
            warnings.append(f"文档读取失败 {name}: {exc}")

    return docs, warnings


def select_doc_paths(doc_paths: List[str]) -> Tuple[List[str], List[str]]:
    warnings: List[str] = []
    preferred = [p for p in doc_paths if os.path.basename(p).lower() == PREFERRED_SECOND_AGENT_FILE.lower()]
    if preferred:
        selected = sorted(preferred)
        if len(selected) > 1:
            warnings.append(f"检测到多个 {PREFERRED_SECOND_AGENT_FILE}，将按文件名排序后使用全部匹配文件。")
        else:
            warnings.append(f"已锁定第二个agent输出：{PREFERRED_SECOND_AGENT_FILE}，其余文档将被忽略。")
        return selected, warnings

    return doc_paths, warnings


def _looks_like_transcript(file_name: str) -> bool:
    lowered = file_name.lower()
    return any(keyword in lowered for keyword in TRANSCRIPT_HINT_KEYWORDS)


def _extract_physical_exam_block(text: str) -> str:
    source = (text or "").replace("\r", "\n")
    if not source.strip():
        return ""

    start_match = re.search(r"（三）体格检查", source)
    if not start_match:
        start_match = re.search(r"\(三\)体格检查", source)
    if not start_match:
        return ""

    rest = source[start_match.end():]
    end_match = re.search(r"（四）辅助检查|\(四\)辅助检查", rest)
    block = rest[: end_match.start()] if end_match else rest
    return block.strip()


def build_transcript_from_docs(doc_entries: List[Dict[str, str]]) -> Tuple[str, List[str]]:
    preferred: List[Dict[str, str]] = []

    for entry in doc_entries:
        if _looks_like_transcript(entry.get("name", "")):
            preferred.append(entry)

    selected = preferred
    fallback_parts: List[str] = []
    parts: List[str] = []
    sources: List[str] = []

    if not selected:
        for entry in doc_entries:
            block = _extract_physical_exam_block(entry.get("text", ""))
            if not block:
                continue
            fallback_parts.append(f"### 来源：{entry['name']}（体格检查段）\n{block}")
            sources.append(f"{entry['name']}（体格检查段）")

        if fallback_parts:
            return "\n\n".join(fallback_parts).strip(), sources

    for entry in selected:
        text = _clean_text(entry.get("text", ""), default="")
        if not text:
            continue
        parts.append(f"### 来源：{entry['name']}\n{text}")
        sources.append(entry["name"])

    return "\n\n".join(parts).strip(), sources


def _runtime_config() -> Any:
    return get_runtime_config()


def _build_llm(chat_model: str, timeout_seconds: int):
    config = _runtime_config()
    return build_chat_llm(
        config,
        model=chat_model or config.llm_model,
        temperature=0.0,
        timeout=timeout_seconds,
        max_retries=0,
        max_tokens=2000,
    )


def _invoke_chain_with_retry(chain, payload_input: dict, timeout_seconds: int, max_attempts: int, call_name: str):
    max_attempts = max(1, max_attempts)
    for attempt in range(1, max_attempts + 1):
        print(f"[*] {call_name} 第{attempt}/{max_attempts}次调用，超时阈值 {timeout_seconds}s")
        try:
            holder = {"result": None, "error": None}

            def _runner():
                try:
                    holder["result"] = chain.invoke(payload_input)
                except Exception as inner_exc:  # pragma: no cover
                    holder["error"] = inner_exc

            worker = threading.Thread(target=_runner, daemon=True)
            worker.start()
            worker.join(timeout=timeout_seconds + 10)

            if worker.is_alive():
                raise TimeoutError(f"模型调用超时（>{timeout_seconds}s）")
            if holder["error"] is not None:
                raise holder["error"]
            return holder["result"]
        except Exception as exc:
            if attempt < max_attempts:
                wait_seconds = attempt * 3
                print(f"[!] {call_name} 第{attempt}次失败: {exc}；{wait_seconds}秒后重试...")
                time.sleep(wait_seconds)
            else:
                print(f"[X] {call_name} 最终失败: {exc}")
    return None


def rule_extract_physical_exam(transcript_text: str, subject_name_hint: Optional[str]) -> PhysicalExamExtraction:
    text = transcript_text or ""
    sentences = _split_sentences(text)

    subject_name = _clean_text(subject_name_hint)
    if subject_name == "不详":
        name_match = re.search(r"(?:被鉴定人|受检者|当事人)[:：]?\s*([\u4e00-\u9fa5]{2,4}|[Xx]{2,4})", text)
        subject_name = _clean_text(name_match.group(1) if name_match else "XXX", default="XXX")

    companion = "其代理人"
    companion_match = re.search(r"在(.{1,30}?陪同下)", text)
    if companion_match:
        companion = _clean_text(companion_match.group(1), default="其代理人")

    entry = "步入检查室"
    entry_match = re.search(r"(拄双拐入检查室|自行步入检查室|步入检查室|进入检查室)", text)
    if entry_match:
        entry = _clean_text(entry_match.group(1), default="步入检查室")

    insurer = "不详"
    insurer_match = re.search(r"([^。；\n]*(?:保险公司|人保|平安)[^。；\n]*(?:在场|到场))", text)
    if insurer_match:
        insurer = _clean_text(insurer_match.group(1), default="不详")

    complaint = "不详"
    complaint_match = re.search(r"(?:主诉|自诉)[:：]\s*([^\n。；]+)", text)
    if complaint_match:
        complaint = _clean_text(complaint_match.group(1), default="不详")

    exam_findings: List[str] = []
    rom_findings: List[str] = []
    evidence_quotes: List[str] = []

    for sentence in sentences:
        if any(k in sentence for k in ["主诉", "自诉"]):
            continue

        if any(k in sentence for k in ["神志清", "对答切题", "瘢痕", "活动受限", "压痛", "肿胀", "查体"]):
            exam_findings.append(sentence)
        if any(k in sentence for k in ["活动度", "屈曲", "伸展", "背屈", "跖屈", "°"]):
            rom_findings.append(sentence)
        if any(k in sentence for k in ["瘢痕", "活动度", "屈曲", "伸展", "背屈", "跖屈", "主诉"]):
            evidence_quotes.append(sentence)

    exam_findings = _merge_unique(exam_findings, max_items=10)
    rom_findings = _merge_unique(rom_findings, max_items=8)
    evidence_quotes = _merge_unique(evidence_quotes, max_items=12)

    uncertain_points: List[str] = []
    if complaint == "不详":
        uncertain_points.append("未从对白中稳定识别主诉内容。")
    if not exam_findings:
        uncertain_points.append("未稳定识别到查体描述，建议核对转写文本。")
    if not rom_findings:
        uncertain_points.append("未稳定识别到关节活动度测量条目。")

    return PhysicalExamExtraction(
        subject_name=subject_name,
        companion_description=companion,
        entry_description=entry,
        insurer_presence=insurer,
        chief_complaint=complaint,
        exam_findings=exam_findings,
        rom_findings=rom_findings,
        evidence_quotes=evidence_quotes,
        uncertain_points=uncertain_points,
    )


def llm_extract_physical_exam(
    transcript_text: str,
    subject_name_hint: Optional[str],
    chat_model: str,
    timeout_seconds: int,
    max_attempts: int,
) -> PhysicalExamExtraction:
    parser = PydanticOutputParser(pydantic_object=PhysicalExamExtraction)
    llm = _build_llm(chat_model=chat_model, timeout_seconds=timeout_seconds)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                append_local_prompt_if_needed("""你是法医临床鉴定助理。请从检查室对白中抽取“第四部分（三）体格检查”所需字段。

要求：
1. 严格依据原文，不得臆造。
2. 优先提取检查者/医生说出的客观查体信息；主诉可来自被鉴定人自述。
3. 忽略寒暄、无关交流。
4. 数值、单位、左右侧、角度必须原样保留。
5. 无法确认时填写“不详”并在 uncertain_points 说明。
6. evidence_quotes 给出可追溯原句（最多12条）。
7. 仅输出 JSON。
{format_instructions}
""", _runtime_config(), "judge_physical"),
            ),
            (
                "human",
                """【姓名提示】
{subject_name_hint}

【检查室对白】
{transcript_text}

请抽取结构化结果。""",
            ),
        ]
    )

    chain = prompt | llm | parser
    payload_input = {
        "format_instructions": parser.get_format_instructions(),
        "subject_name_hint": _clean_text(subject_name_hint),
        "transcript_text": transcript_text[:120000],
    }
    result = _invoke_chain_with_retry(
        chain=chain,
        payload_input=payload_input,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        call_name="体格检查抽取",
    )
    if result is None:
        raise RuntimeError("LLM体格检查抽取失败")
    return result


def llm_refine_chief_complaint(
    raw_complaint: str,
    transcript_text: str,
    chat_model: str,
    timeout_seconds: int,
    max_attempts: int,
) -> str:
    cleaned = _rule_refine_chief_complaint(raw_complaint, max_chars=20)
    if cleaned == "不详":
        return "不详"

    parser = StrOutputParser()
    llm = _build_llm(chat_model=chat_model, timeout_seconds=timeout_seconds)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """你是司法鉴定文书写作助手。请将主诉规整为正式书面表达。

要求：
1. 严格依据给定文本，不得添加新事实。
2. 输出一句主诉，不超过20个字。
3. 去口语化，措辞严肃、客观。
4. 仅输出主诉内容，不要前后缀。""",
            ),
            (
                "human",
                """【原始主诉】
{raw_complaint}

【对白节选】
{transcript_text}

请输出规整后的主诉。""",
            ),
        ]
    )

    chain = prompt | llm | parser
    payload_input = {
        "raw_complaint": cleaned,
        "transcript_text": transcript_text[:3000],
    }
    result = _invoke_chain_with_retry(
        chain=chain,
        payload_input=payload_input,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        call_name="主诉规整",
    )
    if result is None:
        raise RuntimeError("LLM主诉规整失败")

    return _rule_refine_chief_complaint(str(result), max_chars=20)


def _extract_numbered_items_from_text(text: str) -> List[str]:
    source = (text or "").replace("\r", "\n")
    if not source.strip():
        return []

    # 支持单段落内的“1. 2. 3.”连续编号，避免仅按换行抓取导致漏项。
    marker_pattern = re.compile(r"(?<!\d)([1-9][0-9]?)\s*[\.、．](?!\d)")
    matches = list(marker_pattern.finditer(source))
    if not matches:
        return []

    items: List[str] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(source)
        chunk = source[start:end].strip()
        if chunk:
            items.append(chunk)
    return items


def _clean_aux_item_text(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return value
    # 去掉文档中可能混入的图片说明尾注，保留真正的检查条目。
    value = re.sub(r"\n\s*20[0-9]{2}临鉴字第[0-9]+号[:：]?.*", "", value)
    value = re.sub(r"\s*示被鉴定人.*?检查照片.*$", "", value)
    return value.strip()


def extract_aux_lines_by_rules(text: str) -> List[str]:
    source = (text or "").replace("\r", "\n")

    # 优先从“（四）辅助检查”分节中抓取编号条目，保证完整性。
    block = ""
    start_match = re.search(r"（四）辅助检查|\(四\)辅助检查", source)
    if start_match:
        rest = source[start_match.end():]
        end_match = re.search(r"（五）|\(五\)|##\s*五", rest)
        block = rest[: end_match.start()] if end_match else rest

    if block.strip():
        numbered_items = [_clean_aux_item_text(x) for x in _extract_numbered_items_from_text(block)]
        numbered_items = [x for x in numbered_items if x]
        if numbered_items:
            # 按Word中的 1.2.3. 编号顺序原样收录，避免CT/X线关键词过滤导致漏项。
            return _merge_unique(numbered_items, max_items=50)

    lines = [line.strip() for line in source.split("\n") if line.strip()]
    candidates: List[str] = []

    numbered_items_all_doc = [_clean_aux_item_text(x) for x in _extract_numbered_items_from_text(source)]
    numbered_items_all_doc = [x for x in numbered_items_all_doc if x]

    supplement_lines: List[str] = []
    for line in lines:
        clean_line = _clean_aux_item_text(line)
        if not clean_line:
            continue
        if re.search(r"临鉴字第[0-9]+号", clean_line) and re.search(r"检查照片", clean_line):
            continue
        has_date = bool(re.search(r"[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日", clean_line))
        has_exam = bool(re.search(r"CT|X线|X片|DR|MRI|影像学|平扫|拍片|检查报告", clean_line, re.IGNORECASE))
        if has_date and has_exam:
            supplement_lines.append(clean_line)

    if numbered_items_all_doc:
        # 若仅识别到极少编号（常见于Word格式解析后只剩“6.”），
        # 则改用文档顺序的日期+影像条目，避免错序与漏项。
        if len(numbered_items_all_doc) < 3 and supplement_lines:
            return _merge_unique(supplement_lines, max_items=50)

        merged = list(numbered_items_all_doc)
        if len(merged) < 6:
            for line in supplement_lines:
                if any(line in existing or existing in line for existing in merged):
                    continue
                merged.append(line)
        return _merge_unique(merged, max_items=50)

    for line in lines:
        if re.search(r"出院后请保存|保存好住院", line):
            continue

        if re.match(r"^[0-9]{1,2}[\.、]\s*", line) and re.search(r"CT|X线|X片|DR|MRI|影像学|平扫|拍片|检查报告|光盘|病历", line, re.IGNORECASE):
            candidates.append(line)
            continue

        has_hospital = bool(re.search(r"医院|院区|医学中心|大学", line))
        has_date = bool(re.search(r"[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日", line))
        has_exam = bool(re.search(r"CT|X线|X片|DR|MRI|影像学|平扫|拍片|检查报告", line, re.IGNORECASE))
        if has_hospital and has_date and has_exam:
            candidates.append(line)

    return _merge_unique(candidates, max_items=20)


def build_aux_items_from_lines(lines: List[str]) -> List[AuxiliaryCheckItem]:
    out: List[AuxiliaryCheckItem] = []
    for line in lines:
        cleaned = re.sub(r"^[0-9]{1,2}[\.、]\s*", "", line).strip()
        if not cleaned:
            continue

        hospital = "不详"
        date_value = "不详"
        exam_no = "不详"

        hospital_match = re.search(r"([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,}(?:医院|院区|医学中心|大学))", cleaned)
        date_match = re.search(r"([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)", cleaned)
        exam_no_match = re.search(r"号\s*([A-Za-z0-9\-]+)", cleaned)

        if hospital_match:
            hospital = _clean_text(hospital_match.group(1))
        if date_match:
            date_value = _clean_text(date_match.group(1))
        if exam_no_match:
            exam_no = _clean_text(exam_no_match.group(1))

        out.append(
            AuxiliaryCheckItem(
                hospital=hospital,
                exam_date=date_value,
                exam_name="不详",
                exam_no=exam_no,
                findings=_clean_text(cleaned),
            )
        )

    return out


def llm_extract_auxiliary_checks(
    source_text: str,
    chat_model: str,
    timeout_seconds: int,
    max_attempts: int,
) -> AuxiliaryCheckExtraction:
    parser = PydanticOutputParser(pydantic_object=AuxiliaryCheckExtraction)
    llm = _build_llm(chat_model=chat_model, timeout_seconds=timeout_seconds)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                append_local_prompt_if_needed("""你是法医临床鉴定助理。请从文书中抽取“第四部分（四）辅助检查”条目。

要求：
1. 仅提取影像学/辅助检查条目。
2. 每条尽量提取：医院、日期、检查项目、编号、结果。
3. 仅依据原文，不得臆造。
4. 保持原有顺序。
5. 仅输出 JSON。
{format_instructions}
""", _runtime_config(), "judge_auxiliary"),
            ),
            (
                "human",
                """【文书内容】
{doc_text}

请抽取辅助检查条目。""",
            ),
        ]
    )

    chain = prompt | llm | parser
    payload_input = {
        "format_instructions": parser.get_format_instructions(),
        "doc_text": source_text[:120000],
    }
    result = _invoke_chain_with_retry(
        chain=chain,
        payload_input=payload_input,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        call_name="辅助检查抽取",
    )
    if result is None:
        raise RuntimeError("LLM辅助检查抽取失败")
    return result


def compose_section4_markdown(report_date: str, physical: PhysicalExamExtraction, aux_items: List[AuxiliaryCheckItem]) -> str:
    subject_name = _clean_text(physical.subject_name, default="XXX")
    complaint = _clean_text(physical.chief_complaint)
    intro = f"{report_date}，被鉴定人{subject_name}在其代理人的陪同下，自行步入检查室。"
    complaint_line = f"主诉：{complaint if complaint != '不详' else '不详'}。"
    exam_line = "查体：神志清，对答切题，自述伤史。"
    injury_line = f"伤残情况：{_build_injury_description(physical)}"

    lines: List[str] = []
    lines.append("# 司法鉴定意见书结构化提要（第四部分：鉴定过程，AI草稿）")
    lines.append("")
    lines.append("## 四、鉴定过程")
    lines.append("（一）检验方法")
    lines.append(FIXED_SECTION4_METHOD)
    lines.append("（二）使用工具")
    lines.append(FIXED_SECTION4_TOOLS)
    lines.append("（三）体格检查")
    lines.append(intro)
    lines.append(complaint_line)
    lines.append(exam_line)
    lines.append(injury_line)
    lines.append("（四）辅助检查")
    lines.append(f"审阅送检的所有{subject_name}影像学资料，其中：")

    if aux_items:
        for idx, item in enumerate(aux_items, 1):
            lines.append(f"{idx}.{_ensure_cn_sentence(item.findings)}")
    else:
        lines.append("1.暂未识别到可成文的辅助检查条目。")

    return "\n".join(lines)


def _resolve_output_dir(input_dir: str, output_dir: Optional[str]) -> str:
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    case_name = os.path.basename(os.path.normpath(input_dir))
    return resolve_numbered_output_dir(input_dir, fallback_name=case_name)


def _resolve_input_dir(cli_input_dir: Optional[str]) -> Optional[str]:
    if cli_input_dir:
        if not os.path.isdir(cli_input_dir):
            raise FileNotFoundError(f"输入目录不存在: {cli_input_dir}")
        return cli_input_dir

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(title="请选择 judge_agent 的输入文件夹（Word/文本）")
    finally:
        root.destroy()

    selected = os.path.normpath(selected) if selected else ""
    return selected or None


def load_visual_result(visual_json_path: Optional[str]) -> Tuple[dict, List[str]]:
    """读取 visual_agent 输出，失败时返回空结果和 warning。"""
    if not visual_json_path:
        return {}, []
    if not os.path.isfile(visual_json_path):
        return {}, [f"视觉结果文件不存在，已跳过视觉ROM合并: {visual_json_path}"]
    try:
        with open(visual_json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload, []
    except Exception as exc:
        return {}, [f"视觉结果读取失败，已跳过视觉ROM合并: {exc}"]


def _normalize_rom_direction(joint: str, direction: str) -> str:
    joint = str(joint or "")
    direction = str(direction or "").strip()
    if joint == "踝关节" and direction == "背伸":
        return "背屈"
    if joint == "腕关节" and direction == "背屈":
        return "背伸"
    return direction


def merge_structured_rom(base_rom: dict, visual_rom: dict) -> Tuple[dict, List[str]]:
    """合并文本ROM与已确认视觉ROM；冲突不覆盖，写入人工复核提示。"""
    merged = json.loads(json.dumps(base_rom or {}, ensure_ascii=False))
    warnings: List[str] = []

    for joint, side_map in (visual_rom or {}).items():
        if not isinstance(side_map, dict):
            continue
        for side, direction_map in side_map.items():
            if not isinstance(direction_map, dict):
                continue
            for direction, visual_value in direction_map.items():
                direction = _normalize_rom_direction(str(joint), str(direction))
                try:
                    visual_angle = float(visual_value)
                except (TypeError, ValueError):
                    continue

                existing = merged.get(joint, {}).get(side, {}).get(direction)
                if existing is not None:
                    try:
                        existing_angle = float(existing)
                    except (TypeError, ValueError):
                        existing_angle = None
                    if existing_angle is not None and abs(existing_angle - visual_angle) > 3.0:
                        warnings.append(
                            f"文本ROM与视觉ROM存在差异：{side}{joint}{direction} 文本={existing_angle}°，视觉={visual_angle}°，未自动覆盖。"
                        )
                    continue

                merged.setdefault(joint, {}).setdefault(side, {})[direction] = visual_angle

    return merged, warnings


def visual_measurements_to_rom_findings(visual_payload: dict) -> List[str]:
    """Only confirmed legacy visual ROM lines may enter physical findings automatically."""
    if not isinstance(visual_payload, dict):
        return []
    findings: List[str] = []
    for item in visual_payload.get("visual_measurements", []) or []:
        if item.get("status") != "ok" or item.get("requires_human_confirmation"):
            continue
        line = _clean_text(item.get("rom_finding"), default="")
        if line:
            findings.append(line)
    return _merge_unique(findings, max_items=20)


def visual_candidates_to_review_lines(visual_payload: dict) -> List[str]:
    if not isinstance(visual_payload, dict):
        return []
    if visual_payload.get("confirmed_image_reviews") or (visual_payload.get("meta") or {}).get("human_review_status") == "confirmed":
        return []
    lines: List[str] = []
    for action in visual_payload.get("actions", []) or []:
        text = _clean_text(action.get("evidence_text"), default="")
        if text:
            overlays = "；".join(str(p) for p in action.get("overlay_paths", []) if p)
            lines.append(text + (f" 可视化证据：{overlays}" if overlays else ""))
    meta = visual_payload.get("meta") or {}
    if meta.get("status") == "no_goniometer_detected":
        lines.append("检查图片未检出可采纳量角器读数，视觉模块未向关节活动度结论提供自动数值。")
    return _merge_unique(lines, max_items=20)


def run_pipeline(
    input_dir: str,
    output_dir: Optional[str],
    report_date: Optional[str],
    subject_name_hint: Optional[str],
    chat_model: str,
    timeout_seconds: int,
    max_attempts: int,
    disable_llm: bool,
    visual_json_path: Optional[str] = None,
    llm_provider: Optional[str] = None,
    local_llm_base_url: Optional[str] = None,
    local_chat_model: Optional[str] = None,
    local_llm_device: Optional[str] = None,
    cloud_chat_model: Optional[str] = None,
    allow_cloud: bool = False,
) -> Tuple[str, str]:
    runtime_config = get_runtime_config(
        llm_provider="off" if disable_llm else llm_provider,
        allow_cloud=True if allow_cloud else None,
        local_llm_base_url=local_llm_base_url,
        local_chat_model=local_chat_model,
        local_llm_device=local_llm_device,
        cloud_chat_model=cloud_chat_model,
    )
    apply_runtime_env(runtime_config)
    if chat_model == DEFAULT_CHAT_MODEL:
        chat_model = runtime_config.llm_model
    disable_llm = disable_llm or runtime_config.llm_provider == "off"

    collected = collect_input_files(input_dir)
    selected_doc_paths, selection_warnings = select_doc_paths(collected["docs"])
    doc_entries, doc_warnings = load_doc_entries(selected_doc_paths)

    transcript_doc, transcript_doc_sources = build_transcript_from_docs(doc_entries)

    warnings = []
    warnings.extend(selection_warnings)
    warnings.extend(doc_warnings)

    transcript_text = transcript_doc.strip()
    if not transcript_text:
        warnings.append("未检测到对白类文档或现场检查记录，体格检查将使用占位输出。")

    if not transcript_text:
        physical = rule_extract_physical_exam(transcript_text, subject_name_hint)
    elif disable_llm:
        physical = rule_extract_physical_exam(transcript_text, subject_name_hint)
    else:
        try:
            physical = llm_extract_physical_exam(
                transcript_text=transcript_text,
                subject_name_hint=subject_name_hint,
                chat_model=chat_model,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        except Exception as exc:
            warnings.append(f"LLM体格检查抽取失败，回退规则抽取: {exc}")
            physical = rule_extract_physical_exam(transcript_text, subject_name_hint)

    if transcript_text:
        raw_complaint = _clean_text(physical.chief_complaint)
        if raw_complaint != "不详":
            if disable_llm:
                physical.chief_complaint = _rule_refine_chief_complaint(raw_complaint, max_chars=20)
            else:
                try:
                    physical.chief_complaint = llm_refine_chief_complaint(
                        raw_complaint=raw_complaint,
                        transcript_text=transcript_text,
                        chat_model=chat_model,
                        timeout_seconds=timeout_seconds,
                        max_attempts=max_attempts,
                    )
                except Exception as exc:
                    warnings.append(f"LLM主诉规整失败，回退规则规整: {exc}")
                    physical.chief_complaint = _rule_refine_chief_complaint(raw_complaint, max_chars=20)
            physical.chief_complaint = _enforce_complaint_coverage(physical.chief_complaint, transcript_text)

    doc_text = "\n\n".join([entry.get("text", "") for entry in doc_entries]).strip()
    aux_lines = extract_aux_lines_by_rules(doc_text)
    aux_items = build_aux_items_from_lines(aux_lines)
    aux_evidence = _merge_unique(aux_lines, max_items=12)
    aux_uncertain: List[str] = []

    if not aux_items and not disable_llm:
        try:
            aux_payload = llm_extract_auxiliary_checks(
                source_text=doc_text,
                chat_model=chat_model,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
            if aux_payload.items:
                aux_items = aux_payload.items
                aux_evidence = _merge_unique(aux_payload.evidence_quotes, max_items=12)
                aux_uncertain = _merge_unique(aux_payload.uncertain_points, max_items=10)
        except Exception as exc:
            warnings.append(f"LLM辅助检查抽取失败，保留规则结果: {exc}")

    if not aux_items:
        aux_uncertain = _merge_unique(aux_uncertain + ["未识别到可成文的辅助检查条目，建议补充可检索影像描述。"], max_items=10)

    final_date = _clean_text(report_date)
    if final_date == "不详":
        final_date = _today_cn_date()

    visual_payload, visual_warnings = load_visual_result(visual_json_path)
    warnings.extend(visual_warnings)
    visual_rom_findings = visual_measurements_to_rom_findings(visual_payload)
    if visual_rom_findings:
        physical.rom_findings = _merge_unique(list(physical.rom_findings) + visual_rom_findings, max_items=20)
        physical.evidence_quotes = _merge_unique(
            list(physical.evidence_quotes) + [f"视觉读数：{line}" for line in visual_rom_findings],
            max_items=20,
        )

    section4_md = compose_section4_markdown(final_date, physical, aux_items)

    # 为 ruler_agent 提供结构化 ROM 数据
    structured_rom = {}
    rom_source = transcript_text or ""
    for joint_name in _detect_injured_joints(rom_source):
        joint_rom = {}
        for side in ["左", "右"]:
            side_key = side[0]  # "左" or "右"
            side_data = {}
            for direction in JOINT_CONFIG[joint_name]["directions"]:
                _start, end = _extract_motion_range(rom_source, side_key, joint_name[0], direction)
                if end is not None:
                    side_data[direction] = float(end)
            if side_data:
                joint_rom[side] = side_data
        if joint_rom:
            structured_rom[joint_name] = joint_rom

    visual_review_lines = visual_candidates_to_review_lines(visual_payload)
    if visual_review_lines:
        warnings.extend(visual_review_lines)

    structured_rom, visual_merge_warnings = merge_structured_rom(
        structured_rom,
        visual_payload.get("structured_rom", {}) if isinstance(visual_payload, dict) else {},
    )
    warnings.extend(visual_merge_warnings)

    payload = {
        "meta": {
            "input_dir": os.path.abspath(input_dir),
            "doc_files": [os.path.basename(x) for x in selected_doc_paths],
            "report_date": final_date,
            "chat_model": chat_model,
            "llm_enabled": not disable_llm,
            "llm_provider": runtime_config.llm_provider,
            "cloud_used": runtime_config.llm_provider == "cloud",
        },
        "section4": {
            "physical_exam": physical.model_dump(),
            "auxiliary_checks": {
                "items": [item.model_dump() for item in aux_items],
                "evidence_quotes": aux_evidence,
                "uncertain_points": aux_uncertain,
            },
            "structured_rom": structured_rom,
            "visual_rom_candidates": visual_payload.get("structured_rom_candidates", {}) if isinstance(visual_payload, dict) else {},
            "visual_rom_actions": visual_payload.get("actions", []) if isinstance(visual_payload, dict) else [],
            "visual_review_notes": visual_review_lines,
            "markdown": section4_md,
        },
        "evidence": {
            "transcript_sources_from_docs": transcript_doc_sources,
            "transcript_preview": transcript_text[:8000],
            "visual_result_json": os.path.abspath(visual_json_path) if visual_json_path else "",
            "visual_measurements": visual_payload.get("visual_measurements", []) if isinstance(visual_payload, dict) else [],
            "visual_warnings": visual_payload.get("warnings", []) if isinstance(visual_payload, dict) else [],
        },
        "next_sections": {
            "section5_status": "pending",
            "section6_status": "pending",
            "note": "本版本优先实现第四部分，五六部分将在此基础上接入条款推理。",
        },
        "warnings": _merge_unique(warnings, max_items=40),
    }

    output_root = _resolve_output_dir(input_dir, output_dir)
    json_path = os.path.join(output_root, "section4_result.json")
    md_path = os.path.join(output_root, "section4_report.md")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(section4_md)

    return json_path, md_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="judge_agent（第三个Agent）- 第四部分生成器")
    parser.add_argument("--input-dir", default=None, help="输入文件夹（Word/文本）；不传则弹窗选择")
    parser.add_argument("--output-dir", default=None, help="输出目录，默认 test_result/<案件编号>")
    parser.add_argument("--report-date", default=None, help="报告日期，例如 2025年8月26日")
    parser.add_argument("--subject-name-hint", default=None, help="被鉴定人姓名提示")
    parser.add_argument("--chat-model", default=DEFAULT_CHAT_MODEL, help="LLM模型")
    parser.add_argument("--llm-provider", choices=["local", "cloud", "off"], default=None, help="LLM调用通道，默认读取 .env，默认 local")
    parser.add_argument("--local-llm-base-url", default=None, help="本地 Ollama OpenAI-compatible base URL")
    parser.add_argument("--local-chat-model", default=None, help="本地 LLM 模型名")
    parser.add_argument("--local-llm-device", choices=["auto", "gpu", "cpu"], default=None, help="本地 LLM 设备，默认 auto/GPU优先")
    parser.add_argument("--cloud-chat-model", default=None, help="云端 LLM 模型名")
    parser.add_argument("--allow-cloud", action="store_true", help="明确允许本次使用云端模型处理材料")
    parser.add_argument("--timeout", type=int, default=120, help="LLM超时秒数")
    parser.add_argument("--max-attempts", type=int, default=2, help="LLM最大重试次数")
    parser.add_argument("--disable-llm", action="store_true", help="禁用LLM，仅规则抽取")
    parser.add_argument("--visual-json", default=None, help="visual_agent 输出的 visual_result.json，可选")
    return parser


if __name__ == "__main__":
    print("========== judge_agent 启动（当前实现：第四部分） ==========")
    args = build_arg_parser().parse_args()

    input_dir = _resolve_input_dir(args.input_dir)
    if not input_dir:
        print("您已取消选择。程序退出。")
        raise SystemExit(0)

    json_file, md_file = run_pipeline(
        input_dir=input_dir,
        output_dir=args.output_dir,
        report_date=args.report_date,
        subject_name_hint=args.subject_name_hint,
        chat_model=args.chat_model,
        timeout_seconds=max(30, args.timeout),
        max_attempts=max(1, args.max_attempts),
        disable_llm=args.disable_llm,
        visual_json_path=args.visual_json,
        llm_provider=args.llm_provider,
        local_llm_base_url=args.local_llm_base_url,
        local_chat_model=args.local_chat_model,
        local_llm_device=args.local_llm_device,
        cloud_chat_model=args.cloud_chat_model,
        allow_cloud=args.allow_cloud,
    )

    print(f"[*] JSON输出: {json_file}")
    print(f"[*] Markdown输出: {md_file}")
