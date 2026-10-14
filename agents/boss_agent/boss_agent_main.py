"""
boss_agent — 司法鉴定意见书总控Agent

一键运行4个子agent流水线，输出完整鉴定报告 + AI审查意见。
"""

import argparse
import copy
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from difflib import SequenceMatcher
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# 项目根目录
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agents.boss_agent.consistency_gate import validate_report_consistency
from agents.common.llm_client import append_local_prompt_if_needed, call_chat_json
from agents.common.output_paths import DEFAULT_TEST_RESULT_DIR, extract_case_number, resolve_numbered_output_dir
from agents.common.runtime_config import (
    DEFAULT_PADDLE_PDX_CACHE_HOME as DEFAULT_PADDLE_CACHE_DIR,
    RuntimeConfig,
    apply_runtime_env,
    get_runtime_config,
)

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(dotenv_path=ENV_PATH, override=True)

# 项目历史运行环境。Python 3.14/mingw 环境缺少 pydantic-core 预编译轮子，
# 会导致云端 OCR/Structuring 依赖无法安装；若用户误用该 .venv 启动 Boss，
# 自动重启到已配置的 forenagent conda 解释器，保证端到端命令可直接运行。
def _resolve_preferred_python() -> str:
    """定位用于运行本程序的解释器。

    优先级：FORENAGENT_PYTHON 环境变量 → 开发机历史环境 → 当前解释器。
    部署到鉴定中心的机器时，下面那个 D:\\ 路径必然不存在；若不做回退，
    Boss 会尝试重启到不存在的解释器而直接崩掉。
    """
    env_python = os.getenv("FORENAGENT_PYTHON")
    if env_python and os.path.isfile(env_python):
        return env_python
    legacy = "D:/CondaData/envs/forenagent/python.exe"
    if os.path.isfile(legacy):
        return legacy
    return sys.executable


PREFERRED_PYTHON = _resolve_preferred_python()
AUTO_REEXEC_ENV = "FORENAGENT_BOSS_REEXECED"

# SiliconFlow LLM 配置
LLM_BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
LLM_CHAT_MODEL = os.getenv("SILICONFLOW_CHAT_MODEL", "deepseek-ai/DeepSeek-V3.2")
DISABILITY_STANDARD_NAME = "《人体损伤致残程度分级》"
PERIOD_STANDARD_NAME = "《人身损害误工期、护理期、营养期评定规范》（GA/T1193-2014）"

APPRAISER_OPTIONS = [
    {"name": "柯  燕", "license_no": "330105001094"},
    {"name": "钱晓妹", "license_no": "330105001046"},
    {"name": "骆  啸", "license_no": "330105001008"},
    {"name": "叶灵芝", "license_no": "330105001103"},
    {"name": "黄洪溪", "license_no": "330105001032"},
]
APPRAISER_CHOICE_LABELS = [f"{item['name']}｜{item['license_no']}" for item in APPRAISER_OPTIONS] + ["其他/手动"]


def _appraiser_license_by_label(label: str) -> str:
    text = str(label or "")
    for item in APPRAISER_OPTIONS:
        if item["name"].replace(" ", "") in text.replace(" ", "") or item["license_no"] in text:
            return item["license_no"]
    return ""


def _appraiser_name_by_label(label: str) -> str:
    text = str(label or "")
    for item in APPRAISER_OPTIONS:
        if item["name"].replace(" ", "") in text.replace(" ", "") or item["license_no"] in text:
            return item["name"]
    return ""


# ============================================================
# 模块1：案件名提取与路径工具
# ============================================================

DEFAULT_DATABASE_DIR = os.path.join(PROJECT_ROOT, "database")
DEFAULT_CASE_OUTPUT_FOLDER = "4 ai生成报告"

REFERENCE_FOLDER_KEYWORDS = ["鉴定意见书", "专家报告", "参考报告", "样例输出"]
OUTPUT_FOLDER_KEYWORDS = ["ai生成报告", "AI生成报告", "输出", "result", "output"]
MATERIAL_FOLDER_KEYWORD_GROUPS = [["鉴定材料"], ["证据材料"], ["移交材料"], ["案卷材料"]]
PHOTO_FOLDER_KEYWORD_GROUPS = [["检查照片"], ["活动度照片"], ["量角器"], ["照片"]]
EXAM_DOC_FOLDER_KEYWORD_GROUPS = [["损伤情况"], ["检查资料"], ["检查材料"], ["检查记录"], ["检验记录"], ["检验材料"]]
REFERENCE_FOLDER_KEYWORD_GROUPS = [[keyword] for keyword in REFERENCE_FOLDER_KEYWORDS]
OUTPUT_FOLDER_KEYWORD_GROUPS = [[keyword] for keyword in OUTPUT_FOLDER_KEYWORDS]


JUDGE_INPUT_EXTENSIONS = {
    ".doc", ".docx", ".txt", ".md",
}

STAGE_WORK_DIR = "_work"

# ------------------------------------------------------------
# 交付物：直接选文件夹模式（GUI 两步上传）下，鉴定人只看这三个文件。
# 过程文件全部收在 <交付目录>/_work/，界面不展示，但保留以支持重跑与人工回填重放。
# ------------------------------------------------------------
DELIVERABLE_DIR_NAME = "输出"
DELIVERABLE_REPORT_MD = "鉴定意见书.md"
DELIVERABLE_REPORT_DOCX = "鉴定意见书.docx"
DELIVERABLE_AUDIT_MD = "AI审计报告.md"
DELIVERABLE_FILES = (DELIVERABLE_REPORT_MD, DELIVERABLE_REPORT_DOCX, DELIVERABLE_AUDIT_MD)

# 材料夹里这些子目录是运行产物，不参与通道识别（其余按关键词识别检查记录/专家报告）。
EXCLUDED_CASE_CHILD_DIRS = {DELIVERABLE_DIR_NAME, STAGE_WORK_DIR, "visual_overlays"}
STAGE_EXTRACTOR_MD = "01_extractor.md"
STAGE_STRUCTURING_JSON = "02_structuring.json"
STAGE_STRUCTURING_MD = "02_structuring.md"
STAGE_VISUAL_JSON = "03_visual.json"
STAGE_JUDGE_JSON = "04_judge.json"
STAGE_JUDGE_MD = "04_judge.md"
STAGE_RULER_JSON = "05_ruler.json"
STAGE_RULER_MD = "05_ruler.md"
FINAL_DRAFT_MD = "06_boss_draft.md"
FINAL_REVIEWED_MD = "06_boss_reviewed.md"
FINAL_REPORT_MD = FINAL_REVIEWED_MD
FINAL_AUDIT_MD = "06_boss_audit.md"
PERSISTED_HUMAN_OVERRIDES_JSON = "07_human_overrides.json"
HUMAN_REVIEW_REQUESTS_JSON = "human_review_requests.json"
HUMAN_OVERRIDES_DRAFT_JSON = "human_overrides_draft.json"
HUMAN_AUDIT_ANSWERS_JSON = "human_audit_answers.json"
HUMAN_CHANGE_LOG_JSON = "human_change_log.json"
VISUAL_PRE_REVIEW_SESSION_JSON = "visual_rom_pre_review_session.json"
VISUAL_PRE_REVIEW_OVERRIDES_JSON = "visual_rom_pre_review_overrides.json"
STAGE_STRUCTURING_HUMAN_JSON = "02_structuring_human.json"
STAGE_JUDGE_HUMAN_JSON = "04_judge_human.json"
PUBLIC_OUTPUT_FILES = {
    STAGE_EXTRACTOR_MD,
    STAGE_STRUCTURING_JSON,
    STAGE_VISUAL_JSON,
    STAGE_JUDGE_JSON,
    STAGE_RULER_JSON,
    FINAL_DRAFT_MD,
    FINAL_REVIEWED_MD,
    FINAL_AUDIT_MD,
    PERSISTED_HUMAN_OVERRIDES_JSON,
}
TRANSIENT_OUTPUT_NAMES = {STAGE_WORK_DIR, "visual_overlays"}
MATERIAL_FILE_EXCLUDE_PREFIXES = ("~$", ".")
HUMAN_TIMING_META_KEY = "_forenagent_timing"
HUMAN_ACTIVE_IDLE_CAP_SECONDS = 300.0


class ActiveInteractionTimer:
    """估算人工窗口的实际操作耗时；窗口打开但未交互的等待不计入。"""

    def __init__(self, idle_cap_seconds: float = HUMAN_ACTIVE_IDLE_CAP_SECONDS) -> None:
        self.idle_cap_seconds = idle_cap_seconds
        self._elapsed = 0.0
        self._last_event: Optional[float] = None

    def mark(self, event: Any = None) -> None:
        del event
        now = time.perf_counter()
        if self._last_event is not None:
            self._elapsed += min(max(0.0, now - self._last_event), self.idle_cap_seconds)
        self._last_event = now

    def elapsed(self) -> float:
        if self._last_event is None:
            return self._elapsed
        now = time.perf_counter()
        return self._elapsed + min(max(0.0, now - self._last_event), self.idle_cap_seconds)

    def attach(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        updated = copy.deepcopy(payload)
        meta = updated.get(HUMAN_TIMING_META_KEY)
        if not isinstance(meta, dict):
            meta = {}
        meta["active_elapsed_seconds"] = self.elapsed()
        meta["idle_cap_seconds"] = self.idle_cap_seconds
        updated[HUMAN_TIMING_META_KEY] = meta
        return updated


def _count_material_files(folder: Optional[str]) -> int:
    if not folder or not os.path.isdir(folder):
        return 0
    count = 0
    for path in Path(folder).rglob("*"):
        if path.is_file() and not path.name.startswith(MATERIAL_FILE_EXCLUDE_PREFIXES):
            count += 1
    return count


def _format_elapsed(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 60:
        minutes = int(value // 60)
        remain = value - minutes * 60
        return f"{minutes}分{remain:.1f}秒"
    return f"{value:.1f}秒"


def _sum_stage_elapsed(stages: List[Dict[str, Any]]) -> float:
    total = 0.0
    for item in stages or []:
        if not isinstance(item, dict):
            continue
        try:
            total += float(item.get("elapsed_seconds") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def _callback_active_elapsed(payload: Any, fallback_seconds: Optional[float] = 0.0) -> Optional[float]:
    if isinstance(payload, dict):
        meta = payload.get(HUMAN_TIMING_META_KEY)
        if isinstance(meta, dict):
            try:
                return max(0.0, float(meta.get("active_elapsed_seconds") or 0.0))
            except (TypeError, ValueError):
                return 0.0
    if isinstance(payload, (list, tuple)):
        for item in payload:
            elapsed = _callback_active_elapsed(item, None)
            if elapsed is not None:
                return elapsed
    if fallback_seconds is None:
        return None
    return max(0.0, fallback_seconds)


def _bind_active_interaction_timer(dialog: Any, timer: ActiveInteractionTimer) -> None:
    def bind_widget(widget: Any) -> None:
        for sequence in ("<KeyPress>", "<ButtonPress>", "<MouseWheel>", "<<ComboboxSelected>>"):
            try:
                widget.bind(sequence, timer.mark, add="+")
            except Exception:
                pass
        try:
            children = widget.winfo_children()
        except Exception:
            children = []
        for child in children:
            bind_widget(child)

    bind_widget(dialog)


def _missing_modules(module_names: List[str]) -> List[str]:
    """检查当前 Python 解释器可导入的运行依赖。"""
    missing = []
    for name in module_names:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ModuleNotFoundError):
            missing.append(name)
    return missing


def _dependency_install_hint() -> str:
    return f"请在当前运行解释器中安装依赖：{sys.executable} -m pip install -r requirements.txt"


def _maybe_reexec_with_preferred_python(required_modules: Optional[List[str]] = None) -> None:
    """当前解释器缺关键依赖时，自动切到项目配置的 conda 环境重启 Boss。"""
    if os.environ.get(AUTO_REEXEC_ENV) == "1":
        return
    preferred = os.path.abspath(PREFERRED_PYTHON)
    current = os.path.abspath(sys.executable)
    if os.path.normcase(preferred) == os.path.normcase(current):
        return
    if not os.path.isfile(preferred):
        return
    if required_modules and not _missing_modules(required_modules):
        return

    env = os.environ.copy()
    env[AUTO_REEXEC_ENV] = "1"
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    print(f"[env] Missing runtime dependencies; switching Python to: {preferred}", flush=True)
    rc = subprocess.call([preferred, *sys.argv], cwd=PROJECT_ROOT, env=env)
    raise SystemExit(rc)


def _require_modules(module_names: List[str], purpose: str) -> None:
    missing = _missing_modules(module_names)
    if missing:
        raise RuntimeError(
            f"{purpose}依赖缺失：{', '.join(missing)}。当前解释器：{sys.executable}。"
            f"{_dependency_install_hint()}"
        )


def _child_dirs(folder: str) -> List[str]:
    return [
        os.path.join(folder, name)
        for name in sorted(os.listdir(folder))
        if os.path.isdir(os.path.join(folder, name))
    ]


def _matches_keywords(name: str, keywords: List[str]) -> bool:
    return all(keyword.lower() in name.lower() for keyword in keywords)


def _pick_folder(
    children: List[str],
    keyword_groups: List[List[str]],
    excluded: Optional[List[str]] = None,
) -> Optional[str]:
    excluded_norm = {os.path.normcase(os.path.abspath(path)) for path in (excluded or []) if path}
    for keywords in keyword_groups:
        for child in children:
            child_norm = os.path.normcase(os.path.abspath(child))
            if child_norm in excluded_norm:
                continue
            name = os.path.basename(child)
            if _matches_keywords(name, keywords):
                return child
    return None


def _folder_name_matches_any(path: str, keywords: List[str]) -> bool:
    name = os.path.basename(path)
    return any(keyword.lower() in name.lower() for keyword in keywords)


def extract_case_name(folder_path: str) -> str:
    """从文件夹路径中提取案件名。

    优先匹配含"鉴字"的目录名，否则取最末层目录名。
    """
    parts = os.path.normpath(folder_path).split(os.sep)
    # 优先匹配含"鉴字"的目录
    for p in reversed(parts):
        if "鉴字" in p:
            return p
    # 否则取最末层
    return parts[-1] if parts else "unknown_case"


def _safe_folder_name(value: str) -> str:
    value = (value or "case").strip()
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", "_", value)
    return value or "case"


def get_test_result_output_dir(case_folder: str, override: Optional[str] = None) -> str:
    """database 案件统一输出到 test_result/<编号>/。"""
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    case_number = extract_case_number(case_folder)
    if not case_number:
        raise RuntimeError(f"无法从案件目录名提取三位编号，不能映射到 test_result: {case_folder}")
    return resolve_numbered_output_dir(case_folder)


def get_output_dir(transfer_folder: str, override: Optional[str] = None, batch_label: Optional[str] = None) -> str:
    """确定兼容旧双目录模式的输出目录；能识别编号时也统一写入 test_result/<编号>/。"""
    if override:
        d = override
    else:
        case_number = extract_case_number(transfer_folder)
        if case_number:
            d = resolve_numbered_output_dir(transfer_folder)
        else:
            case_name = extract_case_name(transfer_folder)
            label = batch_label or time.strftime("batch_%Y%m%d_%H%M%S")
            d = os.path.join(DEFAULT_TEST_RESULT_DIR, _safe_folder_name(case_name), label)
    os.makedirs(d, exist_ok=True)
    return d


def resolve_case_channels(case_folder: str) -> Dict[str, Optional[str]]:
    """从 database 案件根目录识别各输入/参考/输出通道。

    当前 database 保持原样：
    - 1 鉴定材料：进入 Extractor/Structuring；
    - 2 鉴定意见书：专家参考，只供后续评价，不进入生成；
    - 3 检查照片：进入 Visual；
    - test_result/<编号>：本次运行输出目录。
    """
    if not case_folder or not os.path.isdir(case_folder):
        raise FileNotFoundError(f"案件根目录不存在: {case_folder}")

    children = _child_dirs(case_folder)
    reference = _pick_folder(children, REFERENCE_FOLDER_KEYWORD_GROUPS)
    output_base = _pick_folder(children, OUTPUT_FOLDER_KEYWORD_GROUPS)
    materials = _pick_folder(children, MATERIAL_FOLDER_KEYWORD_GROUPS, excluded=[reference, output_base])
    photos = _pick_folder(children, PHOTO_FOLDER_KEYWORD_GROUPS, excluded=[materials, reference, output_base])
    exam_doc = _pick_folder(children, EXAM_DOC_FOLDER_KEYWORD_GROUPS, excluded=[materials, reference, output_base, photos])

    if not materials:
        raise RuntimeError("未在案件根目录下找到 `1 鉴定材料` 或包含“鉴定材料/证据材料/移交材料”的子目录。")

    return {
        "case_folder": case_folder,
        "case_number": extract_case_number(case_folder),
        "materials_folder": materials,
        "exam_doc_folder": exam_doc,
        "photo_folder": photos,
        "reference_report_folder": reference,
        "legacy_output_base_folder": output_base or os.path.join(case_folder, DEFAULT_CASE_OUTPUT_FOLDER),
        "output_base_folder": DEFAULT_TEST_RESULT_DIR,
        "output_folder": get_test_result_output_dir(case_folder),
    }


def resolve_direct_channels(
    materials_folder: str,
    photo_folder: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """直接选文件夹模式：所选的案件材料夹即案件材料。

    第 4 部分「检验过程」的检查记录单独读取，取材料夹内按关键词命中的子目录
    （如 `检查记录` / `损伤情况`）。目录不存在时返回 None，由 Judge 走结构化摘要，
    而不是把整夹案件证据当成检查记录。
    """
    if not materials_folder or not os.path.isdir(materials_folder):
        raise FileNotFoundError(f"案件材料文件夹不存在: {materials_folder}")

    children = [
        child for child in _child_dirs(materials_folder)
        # 交付目录与过程目录是上一轮的产物，不是案件材料。
        if os.path.basename(child) not in EXCLUDED_CASE_CHILD_DIRS
    ]
    exam_doc = _pick_folder(children, EXAM_DOC_FOLDER_KEYWORD_GROUPS)

    return {
        "case_folder": materials_folder,
        "case_number": extract_case_number(materials_folder),
        "materials_folder": materials_folder,
        "exam_doc_folder": exam_doc,
        "photo_folder": photo_folder,
        "reference_report_folder": _pick_folder(children, REFERENCE_FOLDER_KEYWORD_GROUPS),
    }


def get_case_output_dir(
    case_folder: str,
    override: Optional[str] = None,
    batch_label: Optional[str] = None,
) -> str:
    """确定 database 案件的一次运行输出目录。"""
    if override:
        os.makedirs(override, exist_ok=True)
        return override

    return get_test_result_output_dir(case_folder)


def resolve_case_folders(case_folder: str) -> Tuple[str, str]:
    """兼容旧接口：返回材料目录与检查/照片目录。"""
    channels = resolve_case_channels(case_folder)
    check = channels.get("exam_doc_folder") or channels.get("photo_folder")
    if not check:
        raise RuntimeError("未在案件根目录下找到检查记录或检查照片目录。")
    return channels["materials_folder"], check


# ============================================================
# 模块2：subprocess封装
# ============================================================

def _safe_emit_log(log_callback: Optional[Callable[[str], None]], text: str) -> None:
    """输出日志；Windows GBK 控制台遇到替换字符时降级为可打印文本。"""
    try:
        if log_callback:
            log_callback(text)
        else:
            print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        if log_callback:
            log_callback(safe_text)
        else:
            print(safe_text)


def run_agent_subprocess(
    cmd: list,
    cwd: str = PROJECT_ROOT,
    timeout: int = 600,
    log_callback: Optional[Callable[[str], None]] = None,
) -> int:
    """运行子agent，实时输出stdout到日志回调。返回returncode。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        for line in proc.stdout:
            text = line.rstrip()
            _safe_emit_log(log_callback, text)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        msg = f"Agent超时（{timeout}秒），已终止。"
        if log_callback:
            log_callback(f"[ERROR] {msg}")
        raise RuntimeError(msg)

    return proc.returncode


def _promote_file(src: str, dst: str) -> str:
    """将阶段产物改成统一短文件名。"""
    if os.path.abspath(src) == os.path.abspath(dst):
        return dst
    if os.path.exists(dst):
        os.remove(dst)
    os.replace(src, dst)
    return dst


def _delete_files(*paths: Optional[str]) -> None:
    for path in paths:
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _cleanup_transient_output_artifacts(output_dir: str) -> None:
    """成功后删除过程目录，避免 visual/work 文件污染公开输出。"""
    if not output_dir or not os.path.isdir(output_dir):
        return
    for name in TRANSIENT_OUTPUT_NAMES:
        path = os.path.join(output_dir, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def _cleanup_public_output_dir(output_dir: str, protected_names: Optional[set] = None) -> None:
    """成功后只保留公开 06 文件和必要阶段 JSON。

    若用户通过 --output-dir 指向已有目录，避免删除运行前已经存在的非过程文件/文件夹；
    `_work`、`visual_overlays` 等过程目录始终清理。
    """
    if not output_dir or not os.path.isdir(output_dir):
        return
    protected_names = protected_names or set()
    for name in os.listdir(output_dir):
        path = os.path.join(output_dir, name)
        if name in TRANSIENT_OUTPUT_NAMES:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            continue
        if name in PUBLIC_OUTPUT_FILES or name in protected_names:
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def resolve_deliverable_dir(materials_folder: str) -> str:
    """直接选文件夹模式下的交付目录：<材料文件夹>/输出/。"""
    if not materials_folder:
        raise ValueError("请先选择案件材料文件夹。")
    target = os.path.join(materials_folder, DELIVERABLE_DIR_NAME)
    os.makedirs(target, exist_ok=True)
    return target


def _consolidate_work_files(deliverable_dir: str) -> None:
    """把交付目录顶层的过程文件收进 _work/，只留三个交付物。

    流水线各阶段把文件直接写在输出目录下，这里统一归拢，鉴定人打开"输出"
    文件夹时看到的就只有意见书和审计报告。
    """
    work_dir = os.path.join(deliverable_dir, STAGE_WORK_DIR)
    os.makedirs(work_dir, exist_ok=True)
    for name in os.listdir(deliverable_dir):
        if name in DELIVERABLE_FILES or name == STAGE_WORK_DIR:
            continue
        src = os.path.join(deliverable_dir, name)
        dst = os.path.join(work_dir, name)
        if os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
        except OSError:
            # 单个过程文件搬不动不该让交付失败。
            pass


def write_deliverables(
    deliverable_dir: str,
    reviewed_md: str,
    audit_md: str,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    """把最终成果写成鉴定人实际要用的三个文件。

    过程文件（OCR 文本、各阶段 JSON、人工回填记录）留在 <交付目录>/_work/，
    这里只产出交付物，不搬动过程文件。
    """
    if log is None:
        log = print
    os.makedirs(deliverable_dir, exist_ok=True)

    # 先把过程文件收进 _work/，避免它们和交付物混在交付目录顶层。
    _consolidate_work_files(deliverable_dir)

    report_md_path = os.path.join(deliverable_dir, DELIVERABLE_REPORT_MD)
    audit_md_path = os.path.join(deliverable_dir, DELIVERABLE_AUDIT_MD)
    docx_path = os.path.join(deliverable_dir, DELIVERABLE_REPORT_DOCX)

    with open(report_md_path, "w", encoding="utf-8") as f:
        f.write(reviewed_md)
    with open(audit_md_path, "w", encoding="utf-8") as f:
        f.write(audit_md)

    # Word 版式渲染失败不应让整条流水线失败：报告正文已经落地，docx 只是排版。
    try:
        from agents.boss_agent.docx_renderer import render_report_docx

        render_report_docx(reviewed_md, docx_path)
    except Exception as exc:  # noqa: BLE001
        log(f"[WARN] Word 版式生成失败（正文 Markdown 已保存）：{type(exc).__name__}: {exc}")
        docx_path = ""

    return {"report_md": report_md_path, "audit_md": audit_md_path, "docx": docx_path}


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, data: dict) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def _json_equal(left: Any, right: Any) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(right, ensure_ascii=False, sort_keys=True, default=str)


def _short_json_value(value: Any, max_chars: int = 180) -> str:
    if value is None:
        return "未填写"
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "空"
    return text if len(text) <= max_chars else text[:max_chars - 1] + "…"


def _source_agent_for_target(target_root: str, target: str, source: str = "") -> str:
    source_text = str(source or "")
    if "visual" in source_text or target.startswith(("section4.structured_rom", "structured_rom", "rom_review")):
        return "Visual/Judge Agent"
    if target_root == "judge" or target.startswith("section4"):
        return "Judge Agent"
    if target.startswith("forensic_facts.period_rule_features"):
        return "Ruler Agent"
    if target.startswith("forensic_facts"):
        return "Structuring Agent"
    if target.startswith("final_metadata"):
        return "Boss Agent"
    return target_root or "Boss Agent"


# 会被 report_renderer 直接读取、或经 Ruler/Structuring 重新裁决后影响正文的字段前缀。
RENDERER_CONSUMED_TARGET_PREFIXES = (
    "present_personnel",
    "injury_summary",
    "section4.physical_exam.manual_findings",
    "section4.physical_exam.rom_findings_manual",
    "section4.auxiliary_checks.manual_review_opinion",
    "section4.structured_rom",
    "forensic_facts.structured_rom",
    # 人工对正文具体行的替换文字（按行锚点定位），渲染时替换回正文。
    "forensic_facts.human_text_patches",
    "forensic_facts.case_unclear_manual_notes",
    "forensic_facts.human_confirmed_injuries",
    "forensic_facts.human_disability_confirmation",
    "forensic_facts.period_rule_features.confirmed_period_result",
    "forensic_facts.rule_features",
    "forensic_facts.fracture_features",
    "forensic_facts.commission",
    "forensic_facts.recognized_injuries",
    # 报告抬头文号与第七部分落款（_report_document_no_line / _section7_footer 会读取）。
    # 注意 final_metadata.footer_manual_note 是历史死字段，已在 AUDIT_ONLY 中单独登记，
    # 而 _target_reaches_report 先查 AUDIT_ONLY，因此这里放行 final_metadata 是安全的。
    "final_metadata",
)

# 明确只做留痕、不进入报告正文的字段：人工填写后由 06_boss_audit.md 呈现。
# 这些字段写入 02/04 JSON 但 renderer 不读，必须显式登记，否则操作员会误以为改动已生效。
AUDIT_ONLY_TARGET_PREFIXES = (
    # 历史字段：新流程已改用 forensic_facts.human_text_patches（见 _placeholder_fields_for_line），
    # 这里保留兼容旧 overrides 文件，避免旧数据被误判为“未登记字段”。
    "forensic_facts.report_placeholder_notes",
    "final_metadata.footer_manual_note",
    "forensic_facts.rom_review_confirmed",
    "forensic_facts.human_override_evidence",
    "section4.visual_manual_review",
    # 三期条款全文：正文模板用 clause + sub_label 生成条款引用，
    # 已与专家文书写法一致，clause_text 仅作留痕，不再重复渲染。
    "forensic_facts.period_rule_features.confirmed_period_result.clause_text",
)


def _target_reaches_report(target: str) -> bool:
    """判断人工回填的字段是否会被报告正文采用。

    先匹配只留痕字段（前缀更长、更具体），再匹配会被正文消费的字段。
    """
    text = str(target or "")
    if text.startswith(AUDIT_ONLY_TARGET_PREFIXES):
        return False
    return text.startswith(RENDERER_CONSUMED_TARGET_PREFIXES)


def _set_nested_value(data: dict, dotted_path: str, value: Any) -> None:
    """按点分路径写入结构化事实，用于人工补充事实回填。"""
    parts = [p for p in (dotted_path or "").split(".") if p]
    if not parts:
        return
    cur: Any = data
    for part in parts[:-1]:
        if not isinstance(cur, dict):
            return
        if part not in cur or not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    if isinstance(cur, dict):
        cur[parts[-1]] = value


def _get_nested_value(data: dict, dotted_path: str, default: Any = None) -> Any:
    parts = [p for p in (dotted_path or "").split(".") if p]
    cur: Any = data
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur.get(part)
    return cur


def _deep_merge_dict(target: dict, patch: dict) -> dict:
    """递归合并人工确认的结构化事实，避免覆盖已有 ROM 方向。"""
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge_dict(target[key], value)
        else:
            target[key] = value
    return target


def _merge_nested_value(data: dict, dotted_path: str, patch: dict) -> None:
    parts = [p for p in (dotted_path or "").split(".") if p]
    if not parts:
        return
    cur: Any = data
    for part in parts:
        if not isinstance(cur, dict):
            return
        if part not in cur or not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    if isinstance(cur, dict):
        _deep_merge_dict(cur, patch)


def _append_nested_value(data: dict, dotted_path: str, value: Any) -> None:
    parts = [p for p in (dotted_path or "").split(".") if p]
    if not parts:
        return
    cur: Any = data
    for part in parts[:-1]:
        if not isinstance(cur, dict):
            return
        if part not in cur or not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    if not isinstance(cur, dict):
        return
    key = parts[-1]
    if key not in cur or not isinstance(cur.get(key), list):
        cur[key] = []
    # 幂等：同一份人工补充被重放（如重跑时自动回放累计文件）时不再重复追加。
    if not any(_json_equal(existing, value) for existing in cur[key]):
        cur[key].append(value)


def _review_targets_for_text(text: str) -> List[Dict[str, str]]:
    """把 Ruler 审计提示映射成人工应补充的事实字段候选。"""
    t = text or ""
    targets: List[Dict[str, str]] = []
    if any(k in t for k in ["瘢痕", "疤痕", "创口"]):
        targets.extend([
            {"target": "forensic_facts.rule_features.scar_length_cm", "value_type": "number", "label": "瘢痕/创口长度 cm"},
            {"target": "forensic_facts.rule_features.scar_area_cm2", "value_type": "number", "label": "瘢痕面积 cm²"},
            {"target": "forensic_facts.rule_features.body_surface_scar_pct", "value_type": "number", "label": "体表瘢痕面积百分比 %"},
        ])
    if any(k in t for k in ["活动度", "ROM", "关节功能", "影响功能"]):
        targets.append({"target": "section4.structured_rom", "value_type": "object", "label": "关节活动度结构化数值"})
    if any(k in t for k in ["视力", "视野", "盲目"]):
        targets.extend([
            {"target": "forensic_facts.rule_features.blindness_level_one_eye", "value_type": "number", "label": "单眼盲目等级"},
            {"target": "forensic_facts.rule_features.moderate_visual_impairment_one_eye", "value_type": "boolean", "label": "单眼中度视力损害"},
            {"target": "forensic_facts.rule_features.visual_field_effective_pct_one_eye", "value_type": "number", "label": "单眼视野有效值 %"},
        ])
    if any(k in t for k in ["骨不连", "延迟愈合", "不愈合"]):
        targets.extend([
            {"target": "forensic_facts.rule_features.delayed_union_or_nonunion", "value_type": "boolean", "label": "是否延迟愈合/骨不连"},
            {"target": "forensic_facts.rule_features.chronic_osteomyelitis_long_nonunion", "value_type": "boolean", "label": "是否慢性骨髓炎伴长期不愈"},
        ])
    if any(k in t for k in ["关节面", "畸形愈合", "跟骨", "距骨", "骨盆", "髋臼"]):
        targets.extend([
            {"target": "forensic_facts.rule_features.joint_surface_involved", "value_type": "boolean", "label": "是否累及关节面"},
            {"target": "forensic_facts.rule_features.deformity_healing", "value_type": "boolean", "label": "是否畸形愈合"},
        ])
    if any(k in t for k in ["肋骨"]):
        targets.extend([
            {"target": "forensic_facts.rule_features.rib_fracture_count", "value_type": "number", "label": "肋骨骨折根数"},
            {"target": "forensic_facts.rule_features.rib_deformity_healing_count", "value_type": "number", "label": "肋骨畸形愈合处数"},
        ])
    if any(k in t for k in ["椎体", "压缩", "脊柱", "胸椎", "腰椎"]):
        targets.extend([
            {"target": "forensic_facts.fracture_features.vertebral.levels", "value_type": "list", "label": "有效椎体节段（排除扫描范围、退变、血管瘤、陈旧性改变）"},
            {"target": "forensic_facts.fracture_features.vertebral.compression_fracture", "value_type": "boolean", "label": "是否存在本次外伤相关椎体压缩性骨折"},
            {"target": "forensic_facts.fracture_features.vertebral.compression_degree", "value_type": "number", "label": "椎体压缩程度"},
            {"target": "forensic_facts.fracture_features.vertebral.affects_function", "value_type": "boolean", "label": "脊柱后方附件骨折是否影响功能"},
        ])
    if any(k in t for k in ["ADL", "护理依赖", "日常生活"]):
        targets.extend([
            {"target": "forensic_facts.rule_features.adl_score", "value_type": "number", "label": "ADL/Barthel 评分"},
            {"target": "forensic_facts.commission.requires_care_dependency", "value_type": "boolean", "label": "是否需护理依赖人工评估"},
        ])
    if any(k in t for k in ["因果", "参与度", "原因力"]):
        targets.append({"target": "forensic_facts.commission.requires_causation", "value_type": "boolean", "label": "是否涉及因果关系/参与度审查"})
    if any(k in t for k in ["误工", "护理", "营养", "临床治疗"]):
        targets.append({"target": "forensic_facts.period_rule_features.manual_period_assessment", "value_type": "object", "label": "人工三期评估依据"})

    seen = set()
    unique = []
    for item in targets:
        key = item["target"]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _request_from_audit_item(prefix: str, index: int, item: dict, default_category: str) -> dict:
    text = "；".join(str(item.get(k, "")) for k in ["category", "label", "text", "reason", "clause", "branch_id"] if item.get(k))
    targets = _review_targets_for_text(text)
    return {
        "id": f"{prefix}_{index:03d}",
        "category": item.get("category") or item.get("label") or default_category,
        "question": f"请人工复核并补充：{item.get('label') or item.get('category') or item.get('text') or default_category}",
        "reason": item.get("reason") or item.get("text") or "Ruler 审计提示该项不能直接自动裁决。",
        "source": item.get("source") or "ruler_audit",
        "clause": item.get("clause", ""),
        "branch_id": item.get("branch_id", ""),
        "target_fields": targets,
        "evidence": item.get("facts") or item.get("details") or item,
    }


def build_human_review_requests(ruler_payload: dict) -> dict:
    """从 Ruler 输出生成 Boss 可展示/落盘的人工审查请求。"""
    section5 = ruler_payload.get("section5", {}) or {}
    audit = ruler_payload.get("audit", {}) or {}
    validation = ruler_payload.get("validation", {}) or {}
    detail = section5.get("disability_conclusion_detail", {}) or {}

    requests: List[dict] = []
    for idx, item in enumerate(audit.get("manual_review_items", []) or [], start=1):
        if isinstance(item, dict):
            requests.append(_request_from_audit_item("manual_period", idx, item, "三期人工评估"))
    for idx, item in enumerate(detail.get("manual_review_markers", []) or [], start=1):
        if isinstance(item, dict):
            requests.append(_request_from_audit_item("manual_disability", idx, item, "伤残人工审查"))
    for idx, item in enumerate(audit.get("rejected_disability_clauses", []) or [], start=1):
        if isinstance(item, dict):
            targets = _review_targets_for_text("；".join(str(item.get(k, "")) for k in ["text", "reason", "clause", "branch_id"]))
            if targets:
                requests.append(_request_from_audit_item("missing_fact", idx, item, "缺少可自动裁决事实"))
    for idx, warning in enumerate(validation.get("warnings", []) or [], start=1):
        if isinstance(warning, str) and warning.strip():
            item = {"category": "上游存疑点", "reason": warning, "source": "validation.warnings"}
            requests.append(_request_from_audit_item("warning", idx, item, "上游存疑点"))

    draft_overrides = []
    for req in requests:
        targets = req.get("target_fields") or []
        if not targets:
            draft_overrides.append({
                "request_id": req["id"],
                "target": "",
                "value": None,
                "value_type": "string",
                "evidence_note": "",
                "source": "manual_exam",
                "operator": "",
                "apply": False,
            })
            continue
        for target in targets:
            draft_overrides.append({
                "request_id": req["id"],
                "target": target["target"],
                "label": target.get("label", target["target"]),
                "value": None,
                "value_type": target.get("value_type", "string"),
                "evidence_note": "",
                "source": "manual_exam",
                "operator": "",
                "apply": False,
            })

    return {
        "schema_version": "forenagent_human_review_v1",
        "conclusion_status": section5.get("disability_conclusion_status", ""),
        "conclusion_label": section5.get("disability_conclusion_label", ""),
        "summary": {
            "request_count": len(requests),
            "manual_review_count": detail.get("manual_review_count", 0),
            "positive_count": detail.get("positive_count", 0),
            "negative_evidence_count": detail.get("negative_evidence_count", 0),
        },
        "policy": "人工只补充检查事实和证据说明，不直接填写伤残等级或三期最终结论；补充后应重新运行 Ruler。",
        "requests": requests,
        "draft_overrides": {
            "schema_version": "forenagent_human_overrides_v1",
            "instructions": "将需要采纳的条目 apply 改为 true，并填写 value/evidence_note；Boss 会回填事实字段后重新运行 Ruler。",
            "fact_overrides": draft_overrides,
        },
    }


def write_human_review_files(ruler_json: str, output_dir: str, log: Callable[[str], None]) -> Tuple[dict, str, str]:
    """生成审计报告所需的人工补充信息；不再额外落盘 JSON，保持输出目录固定 06 文件展示。"""
    review = build_human_review_requests(_read_json(ruler_json))
    log(f"  人工审查清单将汇入 {FINAL_AUDIT_MD}，不额外生成 {HUMAN_REVIEW_REQUESTS_JSON}/{HUMAN_OVERRIDES_DRAFT_JSON}")
    return review, "", ""


def _flatten_json_strings(value: Any, limit: int = 80) -> List[str]:
    """从嵌套 JSON 中收集少量文本，用于审计问题触发。"""
    results: List[str] = []

    def walk(node: Any) -> None:
        if len(results) >= limit:
            return
        if isinstance(node, str):
            text = node.strip()
            if text:
                results.append(text[:240])
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return results


def _is_unresolved_text(text: Any) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    return any(k in value for k in ["不详", "待补充", "未读取到", "无法判断", "需人工", "不稳定"])


def _commission_payload(structuring_payload: dict) -> dict:
    forensic = structuring_payload.get("forensic_facts") or {}
    commission = forensic.get("commission") or {}
    return commission if isinstance(commission, dict) else {}


def _commission_text(structuring_payload: dict, include_fallback: bool = True) -> str:
    commission = _commission_payload(structuring_payload)
    raw_items = commission.get("raw_items") or []
    if raw_items:
        return "\n".join(str(item) for item in raw_items if item)
    if not include_fallback or commission.get("scope_source") == "unresolved":
        return ""
    items = structuring_payload.get("entrustment_items") or []
    return "\n".join(str(item) for item in items if item)


def _has_explicit_commission(commission: dict) -> bool:
    if not isinstance(commission, dict):
        return False
    scope_type = str(commission.get("scope_type") or "").strip()
    if scope_type and scope_type != "unresolved":
        return True
    return bool(commission.get("raw_items") or commission.get("scope_source") == "raw_commission")


def _commission_requests_period(structuring_payload: dict) -> bool:
    commission = _commission_payload(structuring_payload)
    scope_type = str(commission.get("scope_type") or "").strip()
    if scope_type == "disability_only":
        return False
    if scope_type in {"period_only", "disability_period", "disability_period_medical_fee_review"}:
        return True
    text = _commission_text(structuring_payload, include_fallback=False)
    text_mentions_period = any(k in text for k in ["误工期", "护理期", "营养期", "三期"])
    if _has_explicit_commission(commission):
        return bool(
            commission.get("requires_work_loss_period")
            or commission.get("requires_nursing_period")
            or commission.get("requires_nutrition_period")
            or text_mentions_period
        )
    text = _commission_text(structuring_payload, include_fallback=True)
    if text:
        return any(k in text for k in ["误工期", "护理期", "营养期", "三期"])
    return False


def _commission_requests_disability(structuring_payload: dict) -> bool:
    commission = _commission_payload(structuring_payload)
    scope_type = str(commission.get("scope_type") or "").strip()
    if scope_type == "period_only":
        return False
    if scope_type in {"disability_only", "disability_period", "disability_period_medical_fee_review"}:
        return True
    if _has_explicit_commission(commission):
        return bool(commission.get("requires_disability"))
    text = _commission_text(structuring_payload)
    if text:
        return any(k in text for k in ["伤残", "残疾"])
    return True


def _audit_question(
    qid: str,
    source_agent: str,
    group: str,
    category: str,
    priority: str,
    question: str,
    reason: str,
    input_type: str = "text",
    options: Optional[List[str]] = None,
    fields: Optional[List[dict]] = None,
    evidence_hint: str = "",
    risk_if_unresolved: str = "",
    current_value: Any = None,
    report_context: str = "",
    uncertainty: str = "",
    needs_confirmation: str = "",
    suggested_answer: str = "",
    attachments: Optional[List[dict]] = None,
) -> dict:
    return {
        "id": qid,
        "source_agent": source_agent,
        "group": group,
        "category": category,
        "priority": priority,
        "question": question,
        "reason": reason,
        "input_type": input_type,
        "options": options or [],
        "fields": fields or [],
        "evidence_hint": evidence_hint,
        "risk_if_unresolved": risk_if_unresolved,
        "current_value": current_value,
        "report_context": report_context,
        "uncertainty": uncertainty,
        "needs_confirmation": needs_confirmation,
        "suggested_answer": suggested_answer,
        "attachments": attachments or [],
        "status": "pending",
    }


def _dedupe_audit_questions(questions: List[dict]) -> List[dict]:
    seen = set()
    unique: List[dict] = []
    priority_order = {"required": 0, "recommended": 1, "risk": 2}
    for q in sorted(questions, key=lambda item: (priority_order.get(item.get("priority", "risk"), 9), item.get("group", ""), item.get("id", ""))):
        key = (q.get("group"), q.get("category"), q.get("question"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(q)
    return unique


def _report_context(lines: List[str], line_no: int, radius: int = 1) -> str:
    start = max(1, line_no - radius)
    end = min(len(lines), line_no + radius)
    return "\n".join(f"第{i}行：{lines[i - 1]}" for i in range(start, end + 1) if lines[i - 1].strip())


FOOTER_LINE_MARKERS = ["七、附件", "附被鉴定人", "检查照片", "影像学扫描照片", "司法鉴定人", "执业证", "浙江大学司法鉴定中心"]
AUX_PLACEHOLDER_MARKERS = ["辅助检查复核", "人工审片", "审片", "阅片", "送检影像学检查资料", "未读取到稳定", "人工补充事实复核"]


def _is_footer_line(line: str) -> bool:
    return any(marker in str(line or "") for marker in FOOTER_LINE_MARKERS)


def _placeholder_fields_for_line(line: str) -> List[dict]:
    """把报告中的可读占位行映射到真正会被 renderer 采用的字段。

    无法归类到结构化字段的行，统一写入 ``forensic_facts.human_text_patches``，
    并以该行的文本作为锚点。renderer 在渲染时按锚点把人工文字替换回正文，
    因此重跑/重渲染不会丢失人工修改（行号会漂移，锚点不会）。
    """
    if _is_footer_line(line):
        return [{
            "target_json": "structuring",
            "target": "forensic_facts.human_text_patches",
            "label": "附件与落款处理说明",
            "value_type": "text",
            "required": False,
            "line_anchor": line,
        }]
    if "在场人员" in line:
        return [{"target_json": "structuring", "target": "present_personnel", "label": "在场人员", "value_type": "text", "required": True}]
    if "体格检查" in line or "现场体格检查" in line:
        return [{"target_json": "judge", "target": "section4.physical_exam.manual_findings", "label": "体格检查正文", "value_type": "text", "required": True}]
    if any(marker in line for marker in AUX_PLACEHOLDER_MARKERS) and not _is_footer_line(line):
        return [{"target_json": "judge", "target": "section4.auxiliary_checks.manual_review_opinion", "label": "辅助检查/审片正文", "value_type": "text", "required": True}]
    return [{
        "target_json": "structuring",
        "target": "forensic_facts.human_text_patches",
        "label": "正文待补充处理说明",
        "value_type": "text",
        "required": False,
        "line_anchor": line,
    }]


def _placeholder_uncertainty_for_line(line: str) -> str:
    if "待补充" in line:
        return "当前正式报告位置仍是“待补充”，如果不处理会直接形成不完整文书。"
    if "未读取到稳定" in line or "人工补充事实复核" in line:
        return "当前文字是系统运行状态说明，不是司法鉴定意见书中应保留的最终表述。"
    if "不详" in line:
        return "当前报告内容为“不详”，需要确认是否确实无法查明，或补充明确事实。"
    if "需结合" in line or "需人工" in line:
        return "当前报告把关键判断留给后续人工处理，需要在最终报告前补充明确结论或处理说明。"
    return "当前报告存在不确定表述，需要人工确认是否采纳、修改或删除。"


ROM_JOINT_DIRECTIONS = {
    "肩关节": ["前屈上举", "后伸", "外展上举", "内收", "水平位内旋", "水平位外旋"],
    "肘关节": ["屈曲", "伸展"],
    "腕关节": ["掌屈", "背伸", "桡偏", "尺偏"],
    "髋关节": ["前屈", "后伸", "外展", "内收", "内旋", "外旋"],
    "膝关节": ["屈曲", "伸展"],
    "踝关节": ["背屈", "跖屈"],
}


def _grade_cn_from_value(value: Any) -> str:
    mapping = {1: "一级", 2: "二级", 3: "三级", 4: "四级", 5: "五级", 6: "六级", 7: "七级", 8: "八级", 9: "九级", 10: "十级"}
    try:
        grade = int(value)
    except Exception:
        return str(value or "").strip()
    return mapping.get(grade, str(value))


def _normalize_rom_joint(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for joint in ROM_JOINT_DIRECTIONS:
        if text == joint or text == joint.replace("关节", "") or joint.replace("关节", "") in text:
            return joint
    return text


def _normalize_rom_side(value: Any) -> str:
    text = str(value or "").strip()
    if "左" in text:
        return "左"
    if "右" in text:
        return "右"
    return text


def _normalize_rom_direction(joint: str, direction: Any) -> str:
    text = str(direction or "").strip()
    if joint == "踝关节" and text == "背伸":
        return "背屈"
    if joint == "腕关节" and text == "背屈":
        return "背伸"
    return text


def _parse_rom_angle(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return round(float(match.group(0)), 3)
    except Exception:
        return None


def _rom_complement_angle(value: Any) -> Optional[float]:
    angle = _parse_rom_angle(value)
    if angle is None or angle < 0 or angle > 180:
        return None
    return round(180 - angle, 3)


def _format_rom_angle(value: Any) -> str:
    angle = _parse_rom_angle(value)
    if angle is None:
        return str(value or "").strip()
    return f"{angle:g}"


def _rom_display_side_joint(joint: str, side: str) -> str:
    joint_text = _normalize_rom_joint(joint)
    side_text = _normalize_rom_side(side)
    return f"{side_text}{joint_text}" if side_text else joint_text


def _rom_pre_audit_lines_from_structured(rom: dict) -> List[str]:
    lines: List[str] = []
    if not isinstance(rom, dict):
        return lines
    for joint, side_map in rom.items():
        joint_name = _normalize_rom_joint(joint)
        if not isinstance(side_map, dict) or not joint_name:
            continue
        for side, direction_map in side_map.items():
            side_name = _normalize_rom_side(side)
            if not isinstance(direction_map, dict):
                continue
            parts: List[str] = []
            for direction, angle in direction_map.items():
                direction_name = _normalize_rom_direction(joint_name, direction)
                angle_text = _format_rom_angle(angle)
                if direction_name and angle_text:
                    parts.append(f"{direction_name}：0°-{angle_text}°")
            if parts:
                lines.append(f"{_rom_display_side_joint(joint_name, side_name)}活动度：{'；'.join(parts)}。")
    return lines


def _collect_confirmed_rom_for_prefill(structuring_payload: dict, visual_payload: dict, judge_payload: dict) -> dict:
    merged: dict = {}
    for rom in [
        _get_nested_value(judge_payload, "section4.structured_rom", {}),
        _get_nested_value(judge_payload, "structured_rom", {}),
        _get_nested_value(visual_payload, "structured_rom", {}),
        _get_nested_value(structuring_payload, "forensic_facts.structured_rom", {}),
    ]:
        if isinstance(rom, dict):
            _deep_merge_dict(merged, copy.deepcopy(rom))
    return merged


def _append_missing_rom_lines(base_text: str, rom_lines: List[str]) -> str:
    text = str(base_text or "").strip()
    additions: List[str] = []
    for line in rom_lines:
        clean_line = str(line or "").strip()
        if not clean_line:
            continue
        head = clean_line.split("活动度", 1)[0]
        if head and head in text and "活动度" in text:
            continue
        if clean_line not in text:
            additions.append(clean_line)
    if not text:
        return "\n".join(additions)
    if additions:
        return text.rstrip("\n") + "\n" + "\n".join(additions)
    return text


def _rom_relevant_text(*payloads: Any) -> bool:
    parts: List[str] = []
    for payload in payloads:
        if isinstance(payload, dict):
            parts.extend(str(x) for x in _flatten_json_strings(payload, limit=200))
    text = "\n".join(parts)
    return bool(re.search(r"关节活动度|ROM|屈曲|伸展|背屈|跖屈|掌屈|背伸|桡偏|尺偏|活动受限|关节功能", text, re.I))


def _rom_joint_options_for_case(*payloads: Any) -> List[str]:
    parts: List[str] = []
    for payload in payloads:
        if isinstance(payload, dict):
            parts.extend(str(x) for x in _flatten_json_strings(payload, limit=300))
    text = "\n".join(parts)
    scored: List[Tuple[int, str]] = []
    aliases = {
        "肩关节": ["肩关节", "肩部", "肩"],
        "肘关节": ["肘关节", "肘部", "肘"],
        "腕关节": ["腕关节", "腕部", "腕"],
        "髋关节": ["髋关节", "髋部", "髋"],
        "膝关节": ["膝关节", "膝部", "膝", "胫骨平台", "半月板", "交叉韧带", "侧副韧带", "创伤性关节炎"],
        "踝关节": ["踝关节", "踝部", "踝", "跟骨", "距骨"],
    }
    for joint, words in aliases.items():
        score = sum(text.count(word) for word in words)
        if score:
            scored.append((score, joint))
    ordered = [joint for score, joint in sorted(scored, key=lambda item: (-item[0], item[1])) if score]
    for joint in ROM_JOINT_DIRECTIONS:
        if joint not in ordered:
            ordered.append(joint)
    return ordered


def _rom_joint_direction_hint(joints: List[str]) -> str:
    lines = []
    for joint in joints[:6]:
        directions = ROM_JOINT_DIRECTIONS.get(joint) or []
        if directions:
            lines.append(f"{joint}：" + "、".join(directions))
    return "\n".join(lines)


def _rom_action_attachments(action: dict) -> List[dict]:
    attachments: List[dict] = []
    measurements = action.get("measurements") or []
    if isinstance(measurements, list):
        for idx, item in enumerate(measurements, start=1):
            if not isinstance(item, dict):
                continue
            preview_path = item.get("overlay_path") or item.get("image_path")
            if not preview_path:
                continue
            angle = item.get("angle") if item.get("angle") not in (None, "") else item.get("baseline_angle")
            status = str(item.get("status") or "")
            label = f"{item.get('image_name') or f'量角器图片{idx}'}"
            if angle not in (None, ""):
                label += f"｜{float(angle):.1f}°"
            if status:
                label += f"｜{status}"
            attachments.append({
                "type": "image",
                "label": label,
                "path": str(preview_path),
                "source_image_path": str(item.get("image_path") or ""),
                "overlay_path": str(item.get("overlay_path") or ""),
                "angle": angle,
                "status": status,
            })
    if attachments:
        return attachments
    for idx, path in enumerate((action.get("preview_paths") or action.get("overlay_paths") or action.get("image_paths") or []), start=1):
        attachments.append({"type": "image", "label": f"量角器图片{idx}", "path": str(path)})
    return attachments


def _rom_action_report_context(action: dict) -> str:
    lines = []
    if action.get("evidence_text"):
        lines.append(str(action.get("evidence_text")))
    angle = action.get("selected_angle")
    if angle not in (None, ""):
        rng = action.get("angle_range") or {}
        lines.append(f"候选角度：{angle}°；范围：{rng.get('min', '-')}°—{rng.get('max', '-')}°。")
    measurements = action.get("measurements") or []
    if measurements:
        lines.append("本项只展示检测到量角器/疑似量角器的图片，司法人员可逐张预览 overlay 后决定采纳哪张或手工改角度。")
        for item in measurements[:8]:
            if not isinstance(item, dict):
                continue
            item_angle = item.get("angle") if item.get("angle") not in (None, "") else item.get("baseline_angle")
            angle_text = f"，角度约{float(item_angle):.1f}°" if item_angle not in (None, "") else ""
            overlay_text = f"，overlay：{item.get('overlay_path')}" if item.get("overlay_path") else ""
            lines.append(f"- {item.get('image_name')}：status={item.get('status')}{angle_text}{overlay_text}")
    else:
        images = action.get("images") or []
        if images:
            lines.append("图片：" + "；".join(str(x) for x in images[:6]))
        overlays = action.get("overlay_paths") or []
        if overlays:
            lines.append("可视化证据：" + "；".join(str(x) for x in overlays[:6]))
    return "\n".join(lines)


def _positive_disability_results(section5: dict, audit: dict) -> List[dict]:
    """提取 Ruler 已命中的阳性伤残条款，供 GUI 用可读文字确认。"""
    results: List[dict] = []
    for item in section5.get("disability_results", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            grade = int(item.get("grade") or 0)
        except Exception:
            grade = 0
        if grade > 0:
            copied = dict(item)
            copied["grade"] = grade
            copied["grade_cn"] = copied.get("grade_cn") or _grade_cn_from_value(grade)
            results.append(copied)
    if not results:
        for item in audit.get("matched_disability_clauses", []) or []:
            if isinstance(item, dict):
                copied = dict(item)
                copied["grade_cn"] = copied.get("grade_cn") or _grade_cn_from_value(copied.get("grade"))
                results.append(copied)
    return sorted(results, key=lambda item: int(item.get("grade") or 99))


def _format_disability_clause_for_human(item: dict) -> str:
    clause = str(item.get("clause") or item.get("clause_id") or "-").strip()
    text = str(item.get("text") or item.get("clause_text") or item.get("source_text") or item.get("label") or "相关伤残条款").strip()
    grade_cn = str(item.get("grade_cn") or _grade_cn_from_value(item.get("grade")) or "相应等级").strip()
    grade_text = grade_cn if grade_cn.endswith("伤残") else f"{grade_cn}伤残"
    standard = str(item.get("standard_name") or DISABILITY_STANDARD_NAME).strip()
    return f"依据{standard}标准之{clause}条“{text}”之规定，当前建议评定为{grade_text}"


def _joined_unique_values(items: List[dict], *keys: str, limit: int = 3) -> str:
    values: List[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for key in keys:
            text = str(item.get(key) or "").strip()
            if text and text not in values:
                values.append(text)
                break
        if len(values) >= limit:
            break
    return "、".join(values)


def _build_disability_audit_text(section5: dict, audit: dict, subject: str) -> dict:
    positives = _positive_disability_results(section5, audit)
    status = section5.get("disability_conclusion_status") or ""
    label = section5.get("disability_conclusion_label") or ""
    rejected = [item for item in (audit.get("rejected_disability_clauses") or []) if isinstance(item, dict)]
    if positives:
        main = positives[0]
        clause_summary = "；".join(_format_disability_clause_for_human(item) for item in positives[:3])
        main_clause = _format_disability_clause_for_human(main)
        grade_cn = str(main.get("grade_cn") or _grade_cn_from_value(main.get("grade")) or "相应等级")
        grade_text = grade_cn if grade_cn.endswith("伤残") else f"{grade_cn}伤残"
        report_context = "\n".join([
            f"第五部分“伤残等级”预计写入：经检验，{subject}因本次外伤致{main.get('text') or '相关损伤'}。因此，{main_clause}。",
            f"第六部分“鉴定意见”预计写入：{subject}因本次损伤，依据{DISABILITY_STANDARD_NAME}标准之相关规定，已构成{grade_text}。",
        ])
        uncertainty_parts = ["请重点复核条款适用事实是否充分，例如损伤部位、椎体数量、压缩程度、功能影响及与本次外伤的关联性。"]
        if rejected:
            rejected_text = "；".join(str(item.get("reason") or item.get("text") or item.get("clause") or "") for item in rejected[:3] if item)
            if rejected_text:
                uncertainty_parts.append(f"Ruler 同时记录了未采纳或需补充事实的更高/其他条款：{rejected_text}")
        return {
            "summary": clause_summary,
            "conclusion_type": "positive_grade",
            "grade_number": str(main.get("grade") or ""),
            "standard_name": str(main.get("standard_name") or DISABILITY_STANDARD_NAME).strip(),
            "clause": _joined_unique_values(positives, "clause", "clause_id"),
            "clause_text": _joined_unique_values(positives, "text", "clause_text", "source_text", "label"),
            "question": f"请确认伤残等级建议：{clause_summary}。是否可以采纳？",
            "reason": "Ruler 根据结构化损伤事实匹配到具体伤残条款；司法人员需确认条款事实和证据是否充分。",
            "report_context": report_context,
            "uncertainty": "\n".join(uncertainty_parts),
            "needs_confirmation": "请确认上述条款事实、条款依据和等级数字是否成立；如不采纳，请填写需要补充或更正的客观事实、依据材料和调整理由，系统不会让 LLM/RAG 直接改判伤残等级。",
            "current_value": {"status": status, "label": label, "matched_clauses": positives, "rejected_or_missing_clauses": rejected[:5]},
        }
    fallback = label or status or "未形成明确伤残等级建议"
    negative_like = any(k in fallback for k in ["不构成", "未构成", "阴性伤残结论"])
    report_context = (
        "第五部分“伤残等级”预计写入：其损伤及后遗症尚未构成伤残等级。\n第六部分“鉴定意见”预计写入：其损伤及后遗症尚未构成伤残等级。"
        if negative_like
        else "第五部分“伤残等级”和第六部分“鉴定意见”目前无法稳定写入明确伤残等级；需先补充条款适用事实后再由 Ruler 重跑。"
    )
    return {
        "summary": "不构成伤残" if negative_like else fallback,
        "conclusion_type": "no_disability" if negative_like else "unconfirmed",
        "grade_number": "0" if negative_like else "",
        "standard_name": DISABILITY_STANDARD_NAME,
        "clause": "",
        # 判不出伤残时，绝不能把“事实不足，暂不能自动判断伤残”这类状态提示预填进
        # “条款对应内容”——鉴定人一旦确认等级，它就会被当成条款原文写进正式文书，
        # 变成“…之‘事实不足，暂不能自动判断伤残’之规定”。此处留空，由鉴定人填写。
        "clause_text": "其损伤及后遗症尚未构成伤残等级" if negative_like else "",
        "question": f"请确认当前伤残等级审查状态：{fallback}。",
        "reason": "Ruler 未形成阳性伤残条款；如司法人员确认，可选择“不构成伤残”，否则需补充条款适用事实。" if negative_like else "Ruler 未形成可直接条款化展示的阳性伤残结论，需要司法人员核对事实是否足够。",
        "report_context": report_context,
        "uncertainty": "当前为阴性伤残结论，仍需司法人员确认是否存在漏抽的功能损害、影像事实或其他可构成伤残的条款事实。" if negative_like else "伤残等级核心事实不足或仍有人工审查项，不能仅凭 AI 文字直接形成最终等级。",
        "needs_confirmation": "如采纳，请选择“确认不构成伤残”；如不采纳，请补充会影响伤残等级的客观事实和依据材料。" if negative_like else "请补充或确认损伤事实、影像/体格检查依据和条款适用事实；系统会回填事实后重新运行 Ruler。",
        "current_value": {"status": status, "label": label, "matched_clauses": positives, "rejected_or_missing_clauses": rejected[:5]},
    }


def _rule_review_priority(req: dict) -> str:
    category = str(req.get("category") or req.get("question") or "")
    priority = str(req.get("priority") or "").strip()
    if priority in {"required", "recommended", "risk"}:
        return priority
    if "神经功能" in category or "ADL" in category.upper():
        return "recommended"
    return "required"


def _is_disability_fact_review(req: dict) -> bool:
    text_parts = [str(req.get(key) or "") for key in ["category", "question", "reason", "clause", "branch_id"]]
    for target in req.get("target_fields") or []:
        if isinstance(target, dict):
            text_parts.append(str(target.get("label") or ""))
            text_parts.append(str(target.get("target") or ""))
    text = "；".join(text_parts)
    if any(k in text for k in ["三期", "误工", "护理期", "营养"]):
        return False
    return any(k in text for k in ["伤残", "椎体", "压缩", "骨折", "条款"])


def _rule_review_report_context(req: dict) -> str:
    category = str(req.get("category") or "规则人工复核")
    targets = req.get("target_fields") or []
    labels = [str(item.get("label") or "").strip() for item in targets if isinstance(item, dict) and str(item.get("label") or "").strip()]
    label_text = "、".join(labels[:4]) if labels else "相关客观检查事实"
    if "神经功能" in category or "ADL" in category.upper():
        return "报告位置/预计写法：仅当本案存在明确神经功能缺损、护理依赖或 ADL/Barthel 评分材料时，相关事实才会进入第五部分伤残等级分析；普通骨折案件可选择“不涉及”或填写“未见相关材料”。"
    if "压缩" in category or any("压缩" in label for label in labels):
        return f"报告位置/预计写法：第五部分“伤残等级”会使用“{label_text}”判断椎体压缩性骨折条款是否成立；第四部分辅助检查/审片也应尽量写明压缩程度或影像依据。"
    if "三期" in category or "误工" in category or "护理" in category or "营养" in category:
        return f"报告位置/预计写法：第五部分和第六部分的误工期、护理期、营养期会根据“{label_text}”及治疗恢复情况确定。"
    return f"报告位置/预计写法：第五部分“分析说明”会使用“{label_text}”作为规则条款适用事实；如补充后影响结论，第六部分“鉴定意见”会随 Ruler 重跑结果更新。"


def build_pre_audit_supplement_session(structuring_payload: dict, visual_payload: dict, judge_payload: dict) -> dict:
    """构造主审计前的体格检查/辅助检查人工补充入口。"""
    subject = structuring_payload.get("subject_name") or "被鉴定人"
    section4 = judge_payload.get("section4", {}) or {}
    physical = section4.get("physical_exam", {}) or {}
    aux = section4.get("auxiliary_checks") or section4.get("auxiliary_check") or {}
    physical_base = physical.get("manual_findings") or physical.get("chief_complaint") or "；".join(str(x) for x in (physical.get("exam_findings") or [])[:3])
    confirmed_rom = _collect_confirmed_rom_for_prefill(structuring_payload, visual_payload, judge_payload)
    physical_current = _append_missing_rom_lines(physical_base, _rom_pre_audit_lines_from_structured(confirmed_rom))
    aux_current = aux.get("manual_review_opinion") or "；".join(str(x) for x in (aux.get("items") or aux.get("findings") or [])[:3])
    return {
        "schema_version": "forenagent_pre_audit_supplement_v1",
        "title": "审计前信息补充（体格检查 / 辅助检查）",
        "subject_name": subject,
        "intro": "请补充可直接写入报告第四部分的文字；没有相关内容可留空继续。",
        "fields": [
            {
                "target_json": "judge",
                "target": "section4.physical_exam.manual_findings",
                "label": "四、鉴定过程（三）体格检查正文",
                "value_type": "text",
                "current_value": physical_current,
                "guidance": "填写现场体格检查主要所见，如压痛、活动度、肌力、感觉、瘢痕、步态等；如未作检查，请填写可入文的规范说明。"
            },
            {
                "target_json": "judge",
                "target": "section4.auxiliary_checks.manual_review_opinion",
                "label": "四、鉴定过程（四）辅助检查/审片正文",
                "value_type": "text",
                "current_value": aux_current,
                "guidance": "填写影像或辅助检查资料、日期、部位、主要所见，以及是否支持本次外伤。"
            },
            {
                "target_json": "judge",
                "target": "section4.auxiliary_checks.manual_review_file",
                "label": "审片/辅助检查文件（可选）",
                "value_type": "file",
                "current_value": aux.get("manual_review_file") or "",
                "guidance": "可选；正式报告以文字审片结论为准。"
            },
        ],
        "field_values": {},
        "status": "pending",
    }


def pre_audit_supplement_to_overrides(session: dict) -> dict:
    overrides: List[dict] = []
    values = session.get("field_values") or {}
    for field in session.get("fields", []) or []:
        if not isinstance(field, dict):
            continue
        target = field.get("target") or ""
        value = str(values.get(target) or "").strip()
        if not target or not value:
            continue
        overrides.append({
            "request_id": "pre_audit_supplement",
            "target": target,
            "target_json": field.get("target_json") or "judge",
            "label": field.get("label", target),
            "value": value,
            "value_type": field.get("value_type", "text"),
            "evidence_note": "GUI审计前信息补充",
            "source": "gui_pre_audit_supplement",
            "operator": "GUI用户",
            "apply": True,
        })
    return {
        "schema_version": "forenagent_human_overrides_v1",
        "instructions": "GUI 审计前信息补充自动生成；用于补充报告第四部分相关事实。",
        "fact_overrides": overrides,
    }


def _rom_angle_candidate(item: dict) -> Optional[float]:
    angle = item.get("angle") if isinstance(item, dict) else None
    if angle in (None, "") and isinstance(item, dict):
        angle = item.get("baseline_angle")
    return _parse_rom_angle(angle)


def _rom_finding_line(joint: str, side: str, direction: str, angle: float) -> str:
    return f"{side}{joint.replace('关节', '')}{direction}0度到{angle:g}度"


def _rom_review_override_items(
    *,
    request_id: str,
    joint: str,
    side: str,
    direction: str,
    angle: float,
    reason: str = "",
    source: str = "gui_rom_audit",
    operator: str = "GUI用户",
    evidence_note: str = "",
    extra_record: Optional[dict] = None,
) -> List[dict]:
    rom_patch = {joint: {side: {direction: angle}}}
    rom_line = _rom_finding_line(joint, side, direction, angle)
    common_meta = {
        "request_id": request_id,
        "evidence_note": evidence_note or reason,
        "source": source,
        "operator": operator,
        "apply": True,
    }
    record = {
        "joint": joint,
        "side": side,
        "direction": direction,
        "angle": angle,
        "finding_text": rom_line,
        "reason": reason,
        "source_question_id": request_id,
    }
    if extra_record:
        record.update({k: v for k, v in extra_record.items() if v not in (None, "")})
    return [
        {
            **common_meta,
            "target": "section4.structured_rom",
            "target_json": "judge",
            "label": "确认关节活动度结构化数值",
            "value": rom_patch,
            "value_type": "object_merge",
        },
        {
            **common_meta,
            "target": "forensic_facts.structured_rom",
            "target_json": "structuring",
            "label": "确认关节活动度结构化数值（Structuring同步）",
            "value": rom_patch,
            "value_type": "object_merge",
        },
        {
            **common_meta,
            "target": "forensic_facts.rom_review_confirmed",
            "target_json": "structuring",
            "label": "关节活动度复核记录",
            "value": record,
            "value_type": "list_append",
        },
        {
            **common_meta,
            "target": "section4.physical_exam.rom_findings_manual",
            "target_json": "judge",
            "label": "关节活动度入文说明",
            "value": rom_line,
            "value_type": "text",
        },
    ]


def build_visual_pre_review_session(structuring_payload: dict, visual_payload: dict) -> dict:
    """把 Visual action 展开为逐图预审项目；只有采纳图片才会进入后续 ROM。"""
    if not isinstance(visual_payload, dict):
        visual_payload = {}
    joint_choices = _rom_joint_options_for_case(structuring_payload, visual_payload)
    items: List[dict] = []
    seen: set = set()

    def add_measurement(measurement: dict, action: Optional[dict] = None) -> None:
        if not isinstance(measurement, dict):
            return
        status = str(measurement.get("status") or "")
        if status not in {"ok", "reject"}:
            return
        angle = _rom_angle_candidate(measurement)
        if angle is None:
            return
        action = action or {}
        image_path = str(measurement.get("image_path") or "")
        image_name = str(measurement.get("image_name") or os.path.basename(image_path) or f"量角器图片{len(items) + 1}")
        if image_path:
            dedupe_key = (image_path, str(action.get("action_id") or measurement.get("action_id") or ""))
            if dedupe_key in seen:
                return
            seen.add(dedupe_key)
        preview_path = str(measurement.get("overlay_path") or measurement.get("image_path") or "")
        joint = _normalize_rom_joint(measurement.get("joint") or action.get("joint") or (joint_choices[0] if joint_choices else ""))
        side = _normalize_rom_side(measurement.get("side") or action.get("side") or "")
        direction = _normalize_rom_direction(joint, measurement.get("direction") or action.get("direction") or "")
        if direction and direction not in ROM_JOINT_DIRECTIONS.get(joint, []) and ROM_JOINT_DIRECTIONS.get(joint):
            direction = ""
        complement_angle = _rom_complement_angle(angle)
        default_angle = complement_angle if complement_angle is not None else angle
        default_angle_mode = "complement" if complement_angle is not None else "machine"
        items.append({
            "review_id": f"visual_rom_img_{len(items) + 1:03d}",
            "action_id": str(action.get("action_id") or measurement.get("action_id") or ""),
            "image_name": image_name,
            "source_image_path": image_path,
            "overlay_path": str(measurement.get("overlay_path") or ""),
            "preview_path": preview_path,
            "machine_angle": angle,
            "complement_angle": complement_angle,
            "default_angle": default_angle,
            "default_angle_mode": default_angle_mode,
            "angle_candidates": {"machine": angle, "complement": complement_angle},
            "machine_status": status,
            "quality_scores": measurement.get("quality_scores") or {},
            "reasons": measurement.get("reasons") or [],
            "joint": joint,
            "side": side,
            "direction": direction,
            "joint_choices": joint_choices,
            "directions_by_joint": ROM_JOINT_DIRECTIONS,
            "adopt": "pending",
            "reason": "",
        })

    actions = visual_payload.get("actions") or []
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            for measurement in action.get("measurements") or []:
                add_measurement(measurement, action)
    if not items:
        for measurement in visual_payload.get("visual_measurements") or []:
            add_measurement(measurement, None)

    return {
        "schema_version": "forenagent_visual_pre_review_v1",
        "title": "关节活动度｜量角器图片预审",
        "intro": "请逐张查看检测到量角器/疑似量角器的图片。只有选择“采纳”的图片会写入关节活动度事实并进入后续判断。",
        "items": items,
        "answers": {},
        "summary": {
            "candidate_image_count": len(items),
            "adopted_image_count": 0,
            "rejected_image_count": 0,
        },
        "status": "pending" if items else "skipped",
    }


def _adopted_visual_review_records(session: dict) -> Tuple[List[dict], int]:
    answers = session.get("answers") or {}
    if isinstance(answers, list):
        answers = {str(item.get("review_id") or ""): item for item in answers if isinstance(item, dict)}
    records: List[dict] = []
    rejected_count = 0
    for item in session.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        review_id = str(item.get("review_id") or "")
        answer = answers.get(review_id) if isinstance(answers, dict) else None
        if not isinstance(answer, dict):
            continue
        adopt_value = str(answer.get("adopt") or "").strip()
        if adopt_value == "reject":
            rejected_count += 1
            continue
        if adopt_value != "adopt":
            continue
        joint = _normalize_rom_joint(answer.get("joint") or item.get("joint"))
        side = _normalize_rom_side(answer.get("side") or item.get("side"))
        direction = _normalize_rom_direction(joint, answer.get("direction") or item.get("direction"))
        angle = _parse_rom_angle(answer.get("angle") if answer.get("angle") not in (None, "") else item.get("default_angle"))
        if not (joint and side and direction and angle is not None):
            continue
        machine_angle = _parse_rom_angle(item.get("machine_angle"))
        complement_angle = _parse_rom_angle(item.get("complement_angle"))
        mode = str(answer.get("angle_mode") or item.get("default_angle_mode") or "").strip()
        if mode not in {"machine", "complement", "manual"}:
            if machine_angle is not None and abs(angle - machine_angle) <= 0.001:
                mode = "machine"
            elif complement_angle is not None and abs(angle - complement_angle) <= 0.001:
                mode = "complement"
            else:
                mode = "manual"
        records.append({
            "review_id": review_id,
            "action_id": item.get("action_id") or answer.get("action_id") or "",
            "image_name": item.get("image_name") or "",
            "source_image_path": item.get("source_image_path") or "",
            "overlay_path": item.get("overlay_path") or item.get("preview_path") or "",
            "joint": joint,
            "side": side,
            "direction": direction,
            "angle": angle,
            "angle_mode": mode,
            "machine_angle": machine_angle,
            "complement_angle": complement_angle,
            "machine_status": item.get("machine_status"),
            "reason": str(answer.get("reason") or "").strip(),
        })
    return records, rejected_count


def apply_visual_pre_review_to_visual_payload(visual_payload: dict, session: dict) -> dict:
    updated = copy.deepcopy(visual_payload or {})
    records, rejected_count = _adopted_visual_review_records(session)
    structured_rom = updated.setdefault("structured_rom", {})
    if not isinstance(structured_rom, dict):
        structured_rom = {}
        updated["structured_rom"] = structured_rom
    for record in records:
        _deep_merge_dict(structured_rom, {record["joint"]: {record["side"]: {record["direction"]: record["angle"]}}})
    updated["confirmed_image_reviews"] = records
    meta = updated.setdefault("meta", {})
    if not isinstance(meta, dict):
        meta = {}
        updated["meta"] = meta
    if session.get("items"):
        if session.get("status") == "skipped":
            meta["human_review_status"] = "skipped"
        else:
            meta["human_review_status"] = "confirmed"
        meta["adopted_image_count"] = len(records)
        meta["rejected_image_count_by_human"] = rejected_count
    return updated


def visual_pre_review_to_overrides(session: dict) -> dict:
    records, _ = _adopted_visual_review_records(session)
    overrides: List[dict] = []
    for record in records:
        overrides.extend(_rom_review_override_items(
            request_id=record.get("review_id") or "visual_rom_pre_review",
            joint=record["joint"],
            side=record["side"],
            direction=record["direction"],
            angle=record["angle"],
            reason=record.get("reason", ""),
            source="gui_visual_pre_review",
            operator="GUI用户",
            evidence_note=record.get("image_name", ""),
            extra_record=record,
        ))
    return {
        "schema_version": "forenagent_human_overrides_v1",
        "instructions": "GUI 量角器图片预审自动生成；仅采纳图片写入 ROM 事实，Ruler 仍根据规则裁决。",
        "fact_overrides": overrides,
    }


def _first_answer_value(answer: dict) -> str:
    field_values = answer.get("field_values") or {}
    for value in field_values.values():
        text = str(value or "").strip()
        if text:
            return text
    text = str(answer.get("answer_text") or "").strip()
    if text:
        return text
    choice = str(answer.get("choice") or "").strip()
    if choice and not choice.startswith("确认"):
        return choice
    return ""


def _replace_unresolved_placeholders_in_line(line: str, replacement: str) -> Tuple[str, bool]:
    """仅替换正文中的占位短语/系统提示，保留同一行已识别出的其他事实。"""
    text = str(line or "")
    repl = str(replacement or "").strip()
    if not text or not repl:
        return text, False
    phrase_patterns = [
        r"影像学影像学载体待补充",
        r"影像学(?:资料)?载体待补充",
        r"影像学资料载体不详",
        r"实物信息待补充",
        r"实物信息不详",
        r"起诉时间待补充",
        r"在场人员待补充",
        r"照片待补充",
        r"扫描照片待补充",
        r"[一-龥A-Za-z0-9（）()、]{2,18}(?:待补充|不详|缺失)",
    ]
    for pattern in phrase_patterns:
        if re.search(pattern, text):
            return re.sub(pattern, repl, text, count=1), True
    long_pattern = r"(未读取到稳定[^。；;，,]*|需结合[^。；;，,]*|需人工[^。；;，,]*|人工补充[^。；;，,]*)"
    if re.search(long_pattern, text):
        return re.sub(long_pattern, repl, text, count=1), True
    for token in ["不详", "待补充", "缺失"]:
        if token in text:
            return text.replace(token, repl, 1), True
    return text, False


def apply_audit_answers_to_report(report_md: str, audit_session: Optional[dict]) -> str:
    """把 GUI 审计中针对正文占位/不确定句的回答落实到预览报告文本。

    两处关键约束：

    1. **幂等**。render_report 已能按锚点把人工正文替换写回
       （``forensic_facts.human_text_patches``）。若此处再按行号应用一次，
       同一处会被替换两遍，在报告里留下一行重复文本。因此替换文字若已出现在
       正文中就跳过——两个机制由此变成互补而非重复。
    2. **按原文定位，而非行号**。行号来自更早的草稿，重渲染后必然漂移；
       一旦指错行，就会把其它行的内容整行覆盖掉，产生无标签的孤立行。
    """
    if not audit_session:
        return report_md
    lines = report_md.splitlines()
    answers_by_id = {
        (answer.get("question", {}) or {}).get("id") or answer.get("question_id"): answer
        for answer in audit_session.get("answers", []) or []
        if isinstance(answer, dict) and answer.get("status") == "answered"
    }
    for q in audit_session.get("questions", []) or []:
        qid = q.get("id")
        answer = answers_by_id.get(qid)
        if not answer:
            continue
        replacement = _first_answer_value(answer)
        if not replacement:
            continue
        # 已经落地（渲染器按锚点写回，或本函数此前已应用过）则不再重复处理。
        if any(replacement in line for line in lines):
            continue
        line_no = q.get("report_line_no")
        original_line = (q.get("report_line") or "").strip()
        if not line_no and not original_line and qid == "physical_exam_004":
            original_line = "未读取到稳定的现场体格检查记录"
        if not line_no and not original_line:
            continue
        # 先按原文精确/包含匹配定位，匹配不到才退回行号。
        idx = -1
        if original_line:
            idx = next((i for i, line in enumerate(lines) if line.strip() == original_line), -1)
            if idx < 0:
                idx = next((i for i, line in enumerate(lines) if original_line in line), -1)
        if idx < 0 and (isinstance(line_no, int) or str(line_no).isdigit()):
            candidate = int(line_no) - 1
            if 0 <= candidate < len(lines):
                idx = candidate
        if idx < 0:
            continue
        replaced_line, changed = _replace_unresolved_placeholders_in_line(lines[idx], replacement)
        if changed:
            lines[idx] = replaced_line
        elif "在场人员" in lines[idx]:
            lines[idx] = f"在场人员：{replacement}"
        else:
            prefix = re.match(r"^(\s*(?:\d+[.、]|[（(][一二三四五六七八九十]+[）)]))", lines[idx])
            if prefix and not replacement.startswith(prefix.group(1)):
                lines[idx] = f"{prefix.group(1)}{replacement}"
            else:
                lines[idx] = replacement
    return "\n".join(lines).strip() + "\n"


def build_audit_questions(
    structuring_payload: dict,
    visual_payload: dict,
    judge_payload: dict,
    ruler_payload: dict,
    draft_report: str,
    human_review: Optional[dict] = None,
    report_date: str = "",
    include_repeated_validation: bool = False,
) -> dict:
    """汇总面向司法人员的问答式人工审计事项。"""
    questions: List[dict] = []
    draft_lines = draft_report.splitlines()
    seq = 1

    def next_id(prefix: str) -> str:
        nonlocal seq
        value = f"{prefix}_{seq:03d}"
        seq += 1
        return value

    subject = structuring_payload.get("subject_name") or "被鉴定人"
    wants_disability = _commission_requests_disability(structuring_payload)
    wants_period = _commission_requests_period(structuring_payload)
    conclusion_scope = "伤残等级和三期判断" if wants_period else "伤残等级判断"
    injury_texts: List[str] = []
    facts = structuring_payload.get("forensic_facts", {}) or {}
    for item in facts.get("recognized_injuries", []) or facts.get("injuries", []) or []:
        if isinstance(item, dict):
            name = item.get("name_normalized") or item.get("name_raw") or item.get("diagnosis") or ""
            if name and not _is_unresolved_text(name):
                injury_texts.append(str(name))
        elif item and not _is_unresolved_text(item):
            injury_texts.append(str(item))
    if not injury_texts:
        for key in ["injury_diagnosis", "diagnosis", "damage_summary"]:
            value = structuring_payload.get(key)
            if isinstance(value, str) and value.strip() and not _is_unresolved_text(value):
                injury_texts.append(value.strip())
    if include_repeated_validation:
        if not injury_texts or any(_is_unresolved_text(item) for item in injury_texts):
            questions.append(_audit_question(
                next_id("case_injury"), "structuring_agent", "案件事实确认", "受伤情况确认", "required",
                f"请确认{subject}本次事故造成的主要损伤。",
                "系统未稳定识别本案主要受伤情况，需先明确裁决对象。",
                input_type="text",
                fields=[{"target_json": "structuring", "target": "forensic_facts.human_confirmed_injuries", "label": "请填写本次事故造成的主要损伤", "value_type": "text", "required": True}],
                evidence_hint="可依据病历诊断、影像资料、起诉状和事故材料填写。",
                risk_if_unresolved=f"主损伤不明确会影响资料摘要、辅助检查和{conclusion_scope}。",
                report_context=f"报告位置/预计写法：基本案情、第五部分分析说明和第六部分鉴定意见都会引用本次事故主要损伤；当前需先确认{subject}的主要损伤事实。",
                uncertainty="系统没有稳定抽取到可用于规则判断的主要损伤名称。",
                needs_confirmation="请补充明确诊断名称，例如“胸11、腰1椎体压缩性骨折”。",
            ))
        elif len("；".join(injury_texts)) > 0:
            current_injuries = "；".join(injury_texts[:4])
            questions.append(_audit_question(
                next_id("case_injury"), "structuring_agent", "案件事实确认", "受伤情况确认", "recommended",
                f"请确认系统识别的本次事故主要损伤是否准确：{current_injuries}",
                "受伤情况是后续规则判断的基础，建议由司法人员确认。",
                input_type="choice_text",
                options=["确认", "部分调整", "无法确认"],
                fields=[{"target_json": "structuring", "target": "forensic_facts.human_confirmed_injuries", "label": "如需调整，请填写最终确认的损伤范围", "value_type": "text", "required": False}],
                evidence_hint="如选择部分调整，请填写调整后的损伤范围和证据。",
                risk_if_unresolved=f"若损伤范围有误，后续{conclusion_scope}可能偏离。",
                current_value=current_injuries,
                report_context=f"报告位置/预计写法：基本案情会写入“诊断为{current_injuries}”；第五部分分析说明和第六部分鉴定意见会把“{current_injuries}”作为{conclusion_scope}的基础事实。",
                uncertainty=f"如果损伤范围、部位或诊断名称有误，后续{conclusion_scope}会被带偏。",
                needs_confirmation="请确认该损伤事实是否准确；如不准确，请填写最终应进入报告的损伤事实。",
            ))

        for item in structuring_payload.get("conflicting_or_unclear_points", []) or []:
            if item:
                item_text = str(item)
                questions.append(_audit_question(
                    next_id("struct_unclear"), "structuring_agent", "材料与案情", "结构化存疑", "recommended",
                    item_text, "Structuring Agent 标记该信息不明确。", input_type="text",
                    fields=[{"target_json": "structuring", "target": "forensic_facts.case_unclear_manual_notes", "label": "请填写核对结果或处理意见", "value_type": "text", "required": False}],
                    risk_if_unresolved="该问题未处理时会保留在审计报告中。",
                    report_context="报告位置/预计写法：该存疑点通常对应一、基本情况的鉴定材料、二、基本案情的事故/责任/关联性描述，或五、分析说明中的事实基础；请按原始材料补成可直接入文的事实说明。",
                    uncertainty=item_text,
                    needs_confirmation="请核对原始材料后填写明确事实；如材料确实没有，请填写“未见相关材料/无法确认”及原因。",
                ))

    # 第四部分（四）辅助检查需要人工审片/复核信息；如审计前已补充，则不再重复弹主审计项。
    section4_payload = judge_payload.get("section4", {}) or {}
    aux_payload = section4_payload.get("auxiliary_checks") or section4_payload.get("auxiliary_check") or {}
    manual_aux = str((aux_payload or {}).get("manual_review_opinion") or "").strip()
    judge_text = "\n".join(_flatten_json_strings(judge_payload, limit=120))
    aux_missing = _is_unresolved_text(judge_text) or not re.search(r"辅助检查|影像|CT|MR|MRI|DR|X线|审片|阅片", judge_text, re.I)
    if not manual_aux:
        questions.append(_audit_question(
            next_id("aux_review"), "judge_agent", "辅助检查/审片", "审片信息补充", "required" if aux_missing else "recommended",
            "请上传或填写本案辅助检查/审片结论，用于第四部分（四）辅助检查和损伤事实复核。",
            "第四部分辅助检查需要司法人员或专家提供审片/辅助检查复核信息，不能只依赖 OCR 摘录。",
            input_type="file_or_text",
            fields=[
                {"target_json": "judge", "target": "section4.auxiliary_checks.manual_review_opinion", "label": "审片/辅助检查结论", "value_type": "text", "required": True},
                {"target_json": "judge", "target": "section4.auxiliary_checks.manual_review_file", "label": "审片信息文件", "value_type": "file", "required": False},
            ],
            evidence_hint="可上传审片意见、检查记录，或直接填写检查日期、部位、影像结论和是否支持本次外伤。",
            risk_if_unresolved="辅助检查缺少人工复核依据，可能影响损伤事实和伤残等级判断。",
            report_context="报告位置/预计写法：四、鉴定过程（四）辅助检查将写入“阅读送检的影像学及辅助检查资料，其中：……”；如填写人工审片结论，会追加为可直接入文的辅助检查复核意见，并作为第五部分损伤事实分析依据。",
            uncertainty="辅助检查目前主要来自 OCR/结构化摘要，仍需要司法人员或专家确认审片结论是否准确、完整。",
            needs_confirmation="请填写可直接进入报告的辅助检查/审片结论；至少说明检查资料、部位、主要影像所见，以及是否支持本次外伤。",
        ))

    physical_exam = section4_payload.get("physical_exam", {}) or {}
    manual_physical = str(physical_exam.get("manual_findings") or "").strip()
    if not manual_physical and _is_unresolved_text("\n".join(_flatten_json_strings(physical_exam, limit=30))):
        questions.append(_audit_question(
            next_id("physical_exam"), "judge_agent", "体格检查", "体格检查补充", "required",
            f"请补充或确认{subject}的现场体格检查关键事实。",
            "Judge Agent 未读取到稳定的现场体格检查记录。",
            input_type="text",
            fields=[{"target_json": "judge", "target": "section4.physical_exam.manual_findings", "label": "体格检查关键事实", "value_type": "text", "required": True}],
            evidence_hint="可填写关节活动、肌力、感觉、瘢痕、压痛、畸形等与本案损伤相关内容。",
            risk_if_unresolved="体格检查缺失会影响第四部分完整性和伤残等级依据。",
            report_context="报告位置/预计写法：四、鉴定过程（三）体格检查目前缺少可入文内容；如不补充，报告会提示“本次未能从材料中形成稳定的现场体格检查摘录，建议结合原始检查记录或现场检验记录复核”。",
            uncertainty="这句话是系统状态说明，不能作为最终司法鉴定意见书正文保留。",
            needs_confirmation="请补充可直接进入报告的体格检查文字；如本案确无现场体格检查，也请填写规范化说明。",
        ))

    if include_repeated_validation:
        visual_meta = visual_payload.get("meta", {}) or {}
        visual_warnings = visual_payload.get("warnings", []) or []
        rejected_images = visual_payload.get("rejected_images", []) or []
        visual_actions = visual_payload.get("actions", []) or []
        visual_status = str(visual_meta.get("status") or "")
        image_count = int(visual_meta.get("image_count") or len(visual_payload.get("visual_measurements", []) or []) or 0)
        rom_relevant = _rom_relevant_text(structuring_payload, judge_payload, ruler_payload) or bool(visual_actions)
        rom_joint_options = _rom_joint_options_for_case(structuring_payload, judge_payload, ruler_payload)
        rom_direction_hint = _rom_joint_direction_hint(rom_joint_options)
        for action in visual_actions[:12]:
            if not isinstance(action, dict):
                continue
            angle_default = "" if action.get("selected_angle") in (None, "") else str(action.get("selected_angle"))
            default_joint = str(action.get("joint") or (rom_joint_options[0] if rom_joint_options else ""))
            default_direction = str(action.get("direction") or "")
            question_text = "请对照下方量角器图片和 overlay，选择该图片对应的关节、侧别、活动方向，并确认最终采纳角度。"
            questions.append(_audit_question(
                next_id("rom_confirm"), "visual_agent", "关节活动度", "量角器读数确认", "required" if (rom_relevant or image_count) else "recommended",
                question_text,
                "只要检查照片中检出量角器/疑似量角器，就应进入关节活动度审计。Visual Agent 只读取角度和画出 overlay，不能可靠判断关节动作；关节、侧别、动作和是否采纳必须由司法人员确认。",
                input_type="fixed_fields",
                options=["确认采纳该读数", "需要人工审计：调整角度/动作", "需要人工审计：不采纳该视觉读数"],
                fields=[
                    {"target_json": "judge", "target": "rom_review.joint", "label": "关节（建议：" + "、".join(rom_joint_options[:3]) + "）", "value_type": "text", "required": True, "default": default_joint, "choices": rom_joint_options},
                    {"target_json": "judge", "target": "rom_review.side", "label": "侧别（左/右）", "value_type": "text", "required": True, "default": str(action.get("side") or "")},
                    {"target_json": "judge", "target": "rom_review.direction", "label": "活动方向（按SOP选项填写）", "value_type": "text", "required": True, "default": default_direction, "choices_by_joint": ROM_JOINT_DIRECTIONS},
                    {"target_json": "judge", "target": "rom_review.angle", "label": "确认角度（度，可改机器读数）", "value_type": "number", "required": True, "default": angle_default},
                    {"target_json": "judge", "target": "rom_review.reason", "label": "采取/调整/不采纳说明", "value_type": "text", "required": False, "default": ""},
                ],
                evidence_hint="请逐张预览已检出量角器的图片：overlay 已标出 fixed_arm、moving_arm、pivot、拟合线和角度。若同一动作有多张图，请按司法人员判断选择采纳哪张或直接修改最终角度。\n" + rom_direction_hint,
                risk_if_unresolved="检测到量角器图片但未确认关节活动度时，该读数不会进入结构化事实、Ruler 计算和正式报告。",
                current_value=action.get("evidence_text"),
                report_context=_rom_action_report_context(action),
                uncertainty="视觉读数只能作为候选证据；关节、侧别、动作和最终角度仍需人工确认。",
                needs_confirmation="确认后系统会同时回填 02_structuring.json 和 04_judge.json 的 ROM 结构化事实，并重新运行 Ruler 计算功能丧失百分比。",
                attachments=_rom_action_attachments(action),
            ))
        if rom_relevant and visual_status in {"no_goniometer_detected", "inference_unavailable"}:
            unavailable = visual_status == "inference_unavailable"
            questions.append(_audit_question(
                next_id("rom_manual"), "visual_agent", "关节活动度", "视觉不可用/未检出量角器时的人工补充" if unavailable else "未检出量角器时的人工补充", "required",
                "本案可能涉及关节活动度，但视觉推理未成功运行；请人工查看检查照片并补充需进入报告的活动度事实。" if unavailable else "本案可能涉及关节活动度，但图片未检出可采纳量角器读数；请人工补充需进入报告的活动度事实。",
                "检查照片存在但视觉依赖/模型不可用时，系统无法判断是否存在量角器；如照片中有量角器，应由司法人员人工确认角度并回填 ROM。" if unavailable else "没有稳定量角器证据时，Visual Agent 不提供自动结论，活动度数值需由司法人员人工确认。",
                input_type="fixed_fields",
                options=["不涉及关节活动度", "需要人工审计：人工补充ROM", "需要人工审计：暂不确认"],
                fields=[
                    {"target_json": "judge", "target": "rom_review.joint", "label": "关节（建议：" + "、".join(rom_joint_options[:3]) + "）", "value_type": "text", "required": False, "default": rom_joint_options[0] if rom_joint_options else "", "choices": rom_joint_options},
                    {"target_json": "judge", "target": "rom_review.side", "label": "侧别（左/右）", "value_type": "text", "required": False, "default": ""},
                    {"target_json": "judge", "target": "rom_review.direction", "label": "活动方向（按SOP选项填写）", "value_type": "text", "required": False, "default": "", "choices_by_joint": ROM_JOINT_DIRECTIONS},
                    {"target_json": "judge", "target": "rom_review.angle", "label": "确认角度（度）", "value_type": "number", "required": False, "default": ""},
                    {"target_json": "judge", "target": "rom_review.reason", "label": "人工补充说明/依据", "value_type": "text", "required": False, "default": ""},
                ],
                evidence_hint="如不涉及关节活动度，可选择“不涉及”；如涉及，请按SOP选择关节、侧别、活动方向，并填写一个需进入报告和规则计算的动作角度。\n" + rom_direction_hint,
                risk_if_unresolved="涉及关节功能案件中，缺少活动度事实会影响伤残判断。",
                current_value=visual_meta.get("summary_for_judicial_user") or visual_meta.get("summary"),
                report_context=("检查照片存在，但视觉依赖/模型不可用，系统无法判断哪些图片有量角器；请人工打开检查照片目录复核。" if unavailable else "图片未检出可采纳量角器读数，视觉模块不写入关节活动度结论；人工补充后才进入第四、第五、第六部分。"),
                uncertainty="视觉推理未运行成功，不能据此排除量角器图片。" if unavailable else "无量角器证据时不能采纳视觉自动读数。",
                needs_confirmation="请确认是否不涉及，或填写需进入报告的关节、侧别、方向和角度。",
            ))
        if visual_meta.get("status") in {"skipped", "inference_unavailable"} or visual_warnings or rejected_images:
            questions.append(_audit_question(
                next_id("visual"), "visual_agent", "检查图片/ROM", "视觉读数复核", "recommended",
                "请确认是否需要补充 ROM、瘢痕测量或检查图片人工标注信息。",
                "Visual Agent 跳过、失败或存在图片质量/读数风险。",
                input_type="choice_text",
                options=["不涉及", "补充ROM", "补充瘢痕测量", "需要人工标注", "无法确认"],
                fields=[{"target_json": "judge", "target": "section4.visual_manual_review", "label": "检查图片/ROM人工复核说明", "value_type": "text", "required": False}],
                evidence_hint="如涉及关节功能或瘢痕，请补充角度、长度/面积、左右侧和证据来源。",
                risk_if_unresolved="涉及关节功能或瘢痕案件中，缺少稳定测量会影响伤残判断。",
                current_value="；".join(str(x) for x in visual_warnings[:3]),
                report_context="报告位置/预计写法：仅在本案涉及 ROM、瘢痕或检查照片测量时，相关人工读数才会写入四、鉴定过程（三）体格检查，并进入第五部分伤残等级分析；普通脊柱骨折案件可选择“不涉及”。",
                uncertainty="Visual Agent 当前未提供稳定可采纳的测量结果。",
                needs_confirmation="请确认本案是否涉及 ROM、瘢痕或检查照片人工标注；如涉及，请填写可用于报告和规则判断的测量事实。",
            ))

    # Ruler 既有人工审查项转为统一问题；伤残核心事实已合并到“受伤情况确认/伤残等级确认”两项中，避免重复审计。
    for req in (human_review or {}).get("requests", []) or []:
        if _is_disability_fact_review(req):
            continue
        req_reason = req.get("reason") or "Ruler 审计提示该项不能直接自动裁决。"
        priority = _rule_review_priority(req)
        questions.append(_audit_question(
            req.get("id") or next_id("ruler_manual"), "ruler_agent", "规则判别", req.get("category") or "规则人工复核", priority,
            req.get("question") or "请复核 Ruler 标记的关键事实。",
            req_reason,
            input_type="text",
            fields=[dict(target, target_json=("judge" if str(target.get("target", "")).startswith(("section4", "structured_rom")) else "structuring")) for target in (req.get("target_fields") or [])],
            evidence_hint="请根据检查记录、影像资料、专家审片或其他材料填写客观事实，不直接填写伤残等级。",
            risk_if_unresolved="Ruler 标记的关键事实未确认，可能导致结论需要人工复核。" if priority == "required" else "该项为辅助复核提示；普通案件如不涉及，可在审计中说明“不涉及/未见相关材料”。",
            current_value=req.get("evidence"),
            report_context=_rule_review_report_context(req),
            uncertainty=req_reason,
            needs_confirmation="请补充规则需要的客观检查事实和证据来源；不要直接填写“几级伤残”，Ruler 会根据补充事实重新裁决。",
        ))

    section5 = ruler_payload.get("section5", {}) or {}
    audit_payload = ruler_payload.get("audit", {}) or {}
    disability_label = section5.get("disability_conclusion_label") or section5.get("disability_level") or ""
    disability_status = section5.get("disability_conclusion_status") or ""
    if wants_disability and (disability_label or disability_status or section5.get("disability_results")):
        disability_text = _build_disability_audit_text(section5, audit_payload, subject)
        questions.append(_audit_question(
            next_id("disability_confirm"), "ruler_agent", "伤残等级确认", "伤残等级判别确认", "required",
            disability_text["question"],
            disability_text["reason"],
            input_type="fixed_fields",
            options=["确认系统建议", "确认不构成伤残", "需要人工审计：调整伤残等级数字", "需要人工审计：暂不确认，需补充事实"],
            fields=[
                {"target_json": "structuring", "target": "forensic_facts.human_disability_confirmation.conclusion_type", "label": "伤残结论类型", "value_type": "text", "required": False, "default": str(disability_text.get("conclusion_type") or ""), "choices": ["positive_grade", "no_disability", "unconfirmed"]},
                {"target_json": "structuring", "target": "forensic_facts.human_disability_confirmation.grade_number", "label": "确认/调整后的伤残等级数字（1-10；不构成伤残填0）", "value_type": "text", "required": False, "default": str(disability_text.get("grade_number") or "")},
                {"target_json": "structuring", "target": "forensic_facts.human_disability_confirmation.standard_name", "label": "所依照的标准/规范名称", "value_type": "text", "required": False, "default": str(disability_text.get("standard_name") or DISABILITY_STANDARD_NAME)},
                {"target_json": "structuring", "target": "forensic_facts.human_disability_confirmation.clause", "label": "所依照的条款", "value_type": "text", "required": False, "default": str(disability_text.get("clause") or "")},
                {"target_json": "structuring", "target": "forensic_facts.human_disability_confirmation.clause_text", "label": "条款对应内容/依据表述", "value_type": "text", "required": False, "default": str(disability_text.get("clause_text") or "")},
                {"target_json": "structuring", "target": "forensic_facts.human_disability_confirmation.reason", "label": "调整理由（确认系统建议可留空）", "value_type": "text", "required": False, "default": ""},
            ],
            evidence_hint="如调整，请在同一界面确认伤残等级数字、所依照条款和简要理由；系统不会把该项作为 LLM 自动裁决。",
            risk_if_unresolved="核心伤残结论未确认。",
            current_value=disability_text["current_value"],
            report_context=disability_text["report_context"],
            uncertainty=disability_text["uncertainty"],
            needs_confirmation=disability_text["needs_confirmation"],
            suggested_answer=disability_text["summary"],
        ))

    period = section5.get("period_result", {}) or {}
    if wants_period:
        ranges = period.get("ranges") or {}
        has_period_values = bool(period.get("误工") or period.get("护理") or period.get("营养"))
        current = f"误工{period.get('误工', '-')}日、护理{period.get('护理', '-')}日、营养{period.get('营养', '-')}日" if has_period_values else "系统暂未形成稳定三期取值"
        questions.append(_audit_question(
            next_id("period_confirm"), "ruler_agent", "三期确认", "三期区间与取值确认", "required",
            f"请确认系统建议的三期取值：{current}。" if has_period_values else "请补充或确认本案三期取值。",
            f"Ruler 根据 {period.get('clause', '-') or '-'}｜{period.get('sub_label', '-') or '-'} 给出区间内取值。" if has_period_values else "委托事项包含三期鉴定，但 Ruler 暂未形成稳定三期取值，需要司法人员复核治疗恢复事实或直接确认三期数字。",
            input_type="fixed_fields",
            options=["确认系统建议", "需要人工审计：调整三期数字", "需要人工审计：暂不确认，需补充治疗恢复情况"],
            fields=[
                {"target_json": "structuring", "target": "forensic_facts.period_rule_features.confirmed_period_result.work_loss_days", "label": "误工期（日）", "value_type": "text", "required": False, "default": str(period.get("误工") or "")},
                {"target_json": "structuring", "target": "forensic_facts.period_rule_features.confirmed_period_result.nursing_days", "label": "护理期（日）", "value_type": "text", "required": False, "default": str(period.get("护理") or "")},
                {"target_json": "structuring", "target": "forensic_facts.period_rule_features.confirmed_period_result.nutrition_days", "label": "营养期（日）", "value_type": "text", "required": False, "default": str(period.get("营养") or "")},
                {"target_json": "structuring", "target": "forensic_facts.period_rule_features.confirmed_period_result.standard_name", "label": "所依照的标准/规范名称", "value_type": "text", "required": False, "default": PERIOD_STANDARD_NAME},
                {"target_json": "structuring", "target": "forensic_facts.period_rule_features.confirmed_period_result.clause", "label": "所依照的条款", "value_type": "text", "required": False, "default": str(period.get("clause") or "")},
                {"target_json": "structuring", "target": "forensic_facts.period_rule_features.confirmed_period_result.sub_label", "label": "条款项目/损伤类型", "value_type": "text", "required": False, "default": str(period.get("sub_label") or "")},
                {"target_json": "structuring", "target": "forensic_facts.period_rule_features.confirmed_period_result.clause_text", "label": "条款对应内容/依据表述", "value_type": "text", "required": False, "default": str(period.get("clause_text") or "")},
                {"target_json": "structuring", "target": "forensic_facts.period_rule_features.confirmed_period_result.reason", "label": "调整理由（确认系统建议可留空）", "value_type": "text", "required": False, "default": ""},
            ],
            evidence_hint="如调整，请在同一界面确认误工/护理/营养天数、所依照条款和简要理由。",
            risk_if_unresolved="三期核心结论未确认。",
            current_value={"selected": current, "ranges": ranges, "policy": period.get("selected_value_policy")},
            report_context=f"报告第五部分和第六部分当前会写入：{current}。",
            uncertainty=f"Ruler 已给出适用区间 {json.dumps(ranges, ensure_ascii=False)}，但区间内取值仍需结合实际治疗恢复情况复核。",
            needs_confirmation="请确认系统建议天数是否可采纳；如需调整，请写明三期天数、调整理由和依据材料。",
        ))

    final_metadata = structuring_payload.get("final_metadata") or {}
    final_metadata = final_metadata if isinstance(final_metadata, dict) else {}
    footer_fields = [
        ("final_metadata.report_doc_year", "标题下临鉴字号年份", "text", True, str(final_metadata.get("report_doc_year") or "")),
        ("final_metadata.report_doc_serial", "标题下临鉴字号数", "text", True, str(final_metadata.get("report_doc_serial") or "")),
        ("final_metadata.exam_photo_count", "检查照片张数", "text", True, str(final_metadata.get("exam_photo_count") or "")),
        ("final_metadata.imaging_photo_count", "影像学扫描照片张数", "text", True, str(final_metadata.get("imaging_photo_count") or "")),
        ("final_metadata.appraiser1_name", "第一名司法鉴定人签名", "appraiser_select", True, str(final_metadata.get("appraiser1_name") or "")),
        ("final_metadata.appraiser1_license_no", "第一名《司法鉴定人执业证》证号", "text", True, str(final_metadata.get("appraiser1_license_no") or "")),
        ("final_metadata.appraiser2_name", "第二名司法鉴定人签名", "appraiser_select", True, str(final_metadata.get("appraiser2_name") or "")),
        ("final_metadata.appraiser2_license_no", "第二名《司法鉴定人执业证》证号", "text", True, str(final_metadata.get("appraiser2_license_no") or "")),
        ("final_metadata.report_date", "报告日期", "text", True, str(final_metadata.get("report_date") or report_date or "")),
    ]
    missing_structured_footer = any(
        field[3] and not str(field[4]).strip()
        for field in footer_fields
        if field[0] != "final_metadata.report_date"
    )
    footer_block = str(final_metadata.get("footer_block") or "").strip()
    if missing_structured_footer and not footer_block:
        footer_date = report_date or "____年__月__日"
        footer_template = (
            "浙江大学司法鉴定中心\n"
            "司法鉴定意见书\n"
            "浙大中心[____]临鉴字第____号\n\n"
            f"七、附件\n"
            f"附被鉴定人{subject}检查照片_张，影像学扫描照片_张。\n\n"
            "司法鉴定人签名：___\n"
            "《司法鉴定人执业证》证号：___\n"
            "司法鉴定人签名：___\n"
            "《司法鉴定人执业证》证号：___\n\n"
            "浙江大学司法鉴定中心\n"
            f"                             {footer_date}"
        )
        questions.append(_audit_question(
            next_id("footer_info"), "boss_agent", "报告字号、附件与落款", "文书字号、附件和司法鉴定人信息补充", "required",
            "请填写正式报告标题下的临鉴字号，以及第七部分的附件照片数量、司法鉴定人签名和执业证号。",
            "正式司法鉴定意见书开头需要临鉴字号，结尾格式固定，只需补齐对应关键信息。",
            input_type="fixed_fields",
            fields=[
                {
                    "target_json": "structuring",
                    "target": target,
                    "label": label,
                    "value_type": value_type,
                    "required": required,
                    "default": default,
                    **({"choices": APPRAISER_CHOICE_LABELS} if value_type == "appraiser_select" else {}),
                }
                for target, label, value_type, required, default in footer_fields
            ],
            evidence_hint="临鉴字号按专家报告格式填写年份和序号；附件、出具人和日期按案卷附件及实际出具信息填写。",
            risk_if_unresolved="临鉴字号、附件照片数量、司法鉴定人签名或执业证号缺失会导致正式报告头部或尾部不完整。",
            current_value=footer_template,
            report_context=f"报告位置/预计写法：\n{footer_template}",
            uncertainty="当前系统没有稳定的正式报告临鉴字号、附件和落款信息，需要司法人员补齐。",
            needs_confirmation="请在下方固定字段中填写临鉴字号年份、序号、张数、姓名、证号和日期；系统会自动生成标题字号和第七部分，不需要手打一整段。",
        ))

    in_footer_section = False
    for line_no, line in enumerate(draft_lines, start=1):
        clean_line = line.strip()
        if clean_line.startswith("七、附件"):
            in_footer_section = True
        elif re.match(r"^[一二三四五六八九十]+、", clean_line):
            in_footer_section = False
        if in_footer_section or _is_footer_line(clean_line):
            continue
        if any(k in line for k in ["不详", "待补充", "缺失", "需结合", "需人工", "人工补充", "未读取到稳定"]):
            q = _audit_question(
                next_id("report_placeholder"), "boss_agent", "报告正文风险", "正文待补充", "required" if any(k in line for k in ["待补充", "缺失", "未读取到稳定", "人工补充"]) else "recommended",
                f"请处理报告正文中的不确定内容：{clean_line[:80]}",
                "当前报告正文包含不能直接作为最终文书保留的占位或系统提示。",
                input_type="text",
                fields=_placeholder_fields_for_line(clean_line),
                risk_if_unresolved="该待补充/系统提示内容如果不处理，会进入最终报告或保留为审计风险。",
                report_context=_report_context(draft_lines, line_no, radius=1),
                uncertainty=_placeholder_uncertainty_for_line(clean_line),
                needs_confirmation="请填写最终报告中应替换该句的正式文字；如果确实无法确认，也请填写规范化说明，而不是保留系统提示。",
                current_value=clean_line,
            )
            q["report_line_no"] = line_no
            q["report_line"] = clean_line
            questions.append(q)
            if len([q for q in questions if q.get("category") == "正文待补充"]) >= 12:
                break

    questions = _dedupe_audit_questions(questions)
    return {
        "schema_version": "forenagent_audit_questions_v1",
        "summary": {
            "total": len(questions),
            "required": sum(1 for q in questions if q.get("priority") == "required"),
            "recommended": sum(1 for q in questions if q.get("priority") == "recommended"),
            "risk": sum(1 for q in questions if q.get("priority") == "risk"),
        },
        "policy": "司法人员确认/补充事实和理由；系统据结构化事实重新运行 Ruler/Boss，LLM 不直接裁决伤残等级或三期。",
        "questions": questions,
        "answers": [],
        "unresolved_risks": [],
    }


def audit_answers_to_overrides(audit_session: dict) -> dict:
    """把 GUI 问答结果转换为 human overrides。"""
    overrides: List[dict] = []
    for answer in audit_session.get("answers", []) or []:
        if not isinstance(answer, dict):
            continue
        if answer.get("status") in {"skipped", "unresolved"}:
            continue
        question = answer.get("question") or {}
        fields = question.get("fields") or []
        field_values = answer.get("field_values") or {}
        free_text = str(answer.get("answer_text") or "").strip()
        evidence_note = answer.get("evidence_note") or answer.get("choice") or ""
        choice_text = str(answer.get("choice") or "")
        if str(question.get("id") or "").startswith("disability_confirm") and "不构成伤残" in choice_text:
            field_values = dict(field_values)
            field_values["forensic_facts.human_disability_confirmation.conclusion_type"] = "no_disability"
            field_values["forensic_facts.human_disability_confirmation.grade_number"] = "0"
            field_values.setdefault("forensic_facts.human_disability_confirmation.standard_name", DISABILITY_STANDARD_NAME)
            field_values.setdefault("forensic_facts.human_disability_confirmation.clause_text", "其损伤及后遗症尚未构成伤残等级")
            existing_targets = {field.get("target") for field in fields if isinstance(field, dict)}
            for target, label in [
                ("forensic_facts.human_disability_confirmation.conclusion_type", "伤残结论类型"),
                ("forensic_facts.human_disability_confirmation.grade_number", "伤残等级数字"),
                ("forensic_facts.human_disability_confirmation.clause_text", "不构成伤残说明"),
            ]:
                if target not in existing_targets:
                    fields.append({"target": target, "value_type": "text", "label": label})
                    existing_targets.add(target)
        if field_values.get("forensic_facts.human_disability_confirmation.conclusion_type") == "no_disability":
            field_values = dict(field_values)
            field_values["forensic_facts.human_disability_confirmation.grade_number"] = "0"
        if any(field.get("value_type") == "appraiser_select" for field in fields if isinstance(field, dict)):
            field_values = dict(field_values)
            for prefix in ["appraiser1", "appraiser2"]:
                name_target = f"final_metadata.{prefix}_name"
                license_target = f"final_metadata.{prefix}_license_no"
                selected = str(field_values.get(name_target) or "")
                if selected and selected != "其他/手动":
                    appraiser_name = _appraiser_name_by_label(selected)
                    appraiser_license = _appraiser_license_by_label(selected)
                    if appraiser_name:
                        field_values[name_target] = appraiser_name
                    if appraiser_license:
                        field_values[license_target] = appraiser_license
        if str(question.get("id") or "").startswith(("rom_confirm", "rom_manual")):
            joint = _normalize_rom_joint(field_values.get("rom_review.joint"))
            side = _normalize_rom_side(field_values.get("rom_review.side"))
            direction = _normalize_rom_direction(joint, field_values.get("rom_review.direction"))
            angle = _parse_rom_angle(field_values.get("rom_review.angle"))
            reason = str(field_values.get("rom_review.reason") or free_text or "").strip()
            choice_text = str(answer.get("choice") or "")
            if joint and side and direction and angle is not None and "不采纳" not in choice_text and not choice_text.startswith("不涉及"):
                overrides.extend(_rom_review_override_items(
                    request_id=question.get("id", ""),
                    joint=joint,
                    side=side,
                    direction=direction,
                    angle=angle,
                    reason=reason,
                    source="gui_rom_audit",
                    operator=answer.get("operator", ""),
                    evidence_note=evidence_note or reason,
                ))
            elif reason:
                overrides.append({
                    "request_id": question.get("id", ""),
                    "target": "section4.visual_manual_review",
                    "target_json": "judge",
                    "label": "关节活动度视觉读数处理记录",
                    "value": reason,
                    "value_type": "text",
                    "evidence_note": evidence_note,
                    "source": "gui_rom_audit",
                    "operator": answer.get("operator", ""),
                    "apply": True,
                })
            continue
        for field in fields:
            target = field.get("target") or ""
            if not target:
                continue
            value = field_values.get(target)
            if value in (None, "") and free_text and field.get("value_type") != "file":
                value = free_text
            if value in (None, ""):
                continue
            override = {
                "request_id": question.get("id", ""),
                "target": target,
                "target_json": field.get("target_json") or ("judge" if target.startswith(("section4", "structured_rom")) else "structuring"),
                "label": field.get("label", target),
                "value": value,
                "value_type": field.get("value_type", "text"),
                "evidence_note": evidence_note,
                "source": "gui_audit",
                "operator": answer.get("operator", ""),
                "apply": True,
            }
            # 正文行的替换文字按锚点写入 human_text_patches，重渲染后可定位回原行。
            if field.get("line_anchor"):
                override["line_anchor"] = field["line_anchor"]
            overrides.append(override)
    return {
        "schema_version": "forenagent_human_overrides_v1",
        "instructions": "GUI 审计问答自动生成；只回填事实/确认说明，Ruler 仍根据规则裁决。",
        "fact_overrides": overrides,
    }


def _append_human_change_log(output_dir: str, source_file: str, changes: List[dict]) -> str:
    if not changes:
        return ""
    work_dir = os.path.join(output_dir, STAGE_WORK_DIR)
    os.makedirs(work_dir, exist_ok=True)
    path = os.path.join(work_dir, HUMAN_CHANGE_LOG_JSON)
    if os.path.isfile(path):
        try:
            payload = _read_json(path)
        except Exception:
            payload = {}
    else:
        payload = {}
    entries = payload.get("changes") if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        entries = []
    for idx, change in enumerate(changes, start=len(entries) + 1):
        item = dict(change)
        item["change_id"] = f"change_{idx:03d}"
        item["source_file"] = os.path.basename(source_file or "")
        entries.append(item)
    payload = {
        "schema_version": "forenagent_human_change_log_v1",
        "summary": {
            "change_count": len(entries),
            "source_agents": sorted({str(item.get("source_agent") or "未知") for item in entries}),
        },
        "changes": entries,
    }
    _write_json(path, payload)
    return path


def apply_human_overrides(
    structuring_json: str,
    judge_json: str,
    overrides_path: str,
    output_dir: str,
    log: Callable[[str], None],
) -> Tuple[str, str, str]:
    """将人工补充事实回填到运行副本，保持原始上游输出不变。"""
    if not overrides_path:
        return structuring_json, judge_json, ""
    overrides = _read_json(overrides_path)
    fact_overrides = overrides.get("fact_overrides", []) or []
    if not isinstance(fact_overrides, list):
        raise ValueError("human overrides JSON 中 fact_overrides 必须是列表。")

    structuring_data = copy.deepcopy(_read_json(structuring_json))
    judge_data = copy.deepcopy(_read_json(judge_json))
    applied: List[dict] = []
    for item in fact_overrides:
        if not isinstance(item, dict) or not item.get("apply"):
            continue
        target = item.get("target") or item.get("field") or ""
        if not target:
            continue
        value = item.get("value")
        if target == "section4.auxiliary_checks.manual_review_opinion" and _is_footer_line(str(value or "")):
            continue
        target_root = item.get("target_json") or ("judge" if target.startswith("section4") or target.startswith("structured_rom") else "structuring")
        target_data = judge_data if target_root == "judge" else structuring_data
        # 正文行替换：按锚点写入 human_text_patches，renderer 渲染时替换回正文。
        line_anchor = str(item.get("line_anchor") or "").strip()
        if line_anchor:
            forensic = structuring_data.setdefault("forensic_facts", {})
            patches = forensic.get("human_text_patches")
            if not isinstance(patches, dict):
                patches = {}
                forensic["human_text_patches"] = patches
            before_value = copy.deepcopy(patches.get(line_anchor))
            patches[line_anchor] = value
            after_value = copy.deepcopy(patches.get(line_anchor))
            if not _json_equal(before_value, after_value):
                applied.append({
                    "request_id": item.get("request_id", ""),
                    "target_json": "structuring",
                    "target": f"forensic_facts.human_text_patches[{line_anchor[:40]}]",
                    "label": item.get("label") or "正文行替换",
                    "source_agent": "Boss Agent",
                    "value_type": "text",
                    "before": before_value,
                    "after": after_value,
                    "value": value,
                    "source": item.get("source", "manual_exam"),
                    "operator": item.get("operator", ""),
                    "evidence_note": item.get("evidence_note", ""),
                    "reaches_report": True,
                })
            continue
        before_value = copy.deepcopy(_get_nested_value(target_data, target))
        if target_root == "judge":
            if item.get("value_type") == "object_merge" and isinstance(value, dict):
                _merge_nested_value(judge_data, target, value)
            elif item.get("value_type") == "list_append":
                _append_nested_value(judge_data, target, value)
            else:
                _set_nested_value(judge_data, target, value)
        else:
            if item.get("value_type") == "object_merge" and isinstance(value, dict):
                _merge_nested_value(structuring_data, target, value)
            elif item.get("value_type") == "list_append":
                _append_nested_value(structuring_data, target, value)
            else:
                _set_nested_value(structuring_data, target, value)
        after_value = copy.deepcopy(_get_nested_value(target_data, target))
        if _json_equal(before_value, after_value):
            continue
        applied.append({
            "request_id": item.get("request_id", ""),
            "target_json": target_root,
            "target": target,
            "label": item.get("label") or target,
            "source_agent": _source_agent_for_target(target_root, target, item.get("source", "")),
            "value_type": item.get("value_type", "text"),
            "before": before_value,
            "after": after_value,
            "value": value,
            "source": item.get("source", "manual_exam"),
            "operator": item.get("operator", ""),
            "evidence_note": item.get("evidence_note", ""),
            # 该字段是否会被报告正文采用；False 表示只留痕，不进正文。
            "reaches_report": _target_reaches_report(target),
        })

    if not applied:
        log("  [人工补充] 未发现 apply=true 的补充项，继续使用原始事实。")
        return structuring_json, judge_json, ""

    forensic_facts = structuring_data.setdefault("forensic_facts", {})
    forensic_facts.setdefault("human_override_evidence", []).extend(applied)
    if forensic_facts.get("human_confirmed_injuries"):
        structuring_data["injury_summary"] = forensic_facts.get("human_confirmed_injuries")
    _write_json(structuring_json, structuring_data)
    _write_json(judge_json, judge_data)
    change_log_path = _append_human_change_log(output_dir, overrides_path, applied)
    log(f"  [人工补充] 已回填 {len(applied)} 个事实字段到 02/04 阶段文件，Ruler 将使用补充后的事实。")
    # 契约校验：未登记且不会被正文采用的字段，说明前端与 renderer 的字段约定已经脱节，
    # 必须显式告警，避免“改了但报告里没有”被静默吞掉。
    unregistered = [
        item for item in applied
        if not item.get("reaches_report") and not str(item.get("target") or "").startswith(AUDIT_ONLY_TARGET_PREFIXES)
    ]
    if unregistered:
        names = "、".join(sorted({str(item.get("target")) for item in unregistered}))
        log(
            f"  [人工补充][警告] {len(unregistered)} 个字段既不被报告正文采用、也未登记为留痕字段：{names}。"
            "这些改动只会写入 02/04 JSON，不会出现在 06_boss_reviewed.md，请检查字段契约。"
        )
    audit_only_count = sum(1 for item in applied if not item.get("reaches_report"))
    if audit_only_count:
        log(f"  [人工补充] 其中 {audit_only_count} 个字段为留痕项，仅进入 06_boss_audit.md，不改变报告正文。")
    return structuring_json, judge_json, change_log_path


def _override_identity(item: dict) -> str:
    """人工回填项的去重键：同一目标（含正文锚点）只保留最后一次填写。"""
    target = str(item.get("target") or item.get("field") or "")
    anchor = str(item.get("line_anchor") or "")
    return f"{item.get('target_json') or ''}|{target}|{anchor}"


def _merge_override_payloads(*payloads: Optional[dict]) -> dict:
    merged: "dict[str, dict]" = {}
    for payload in payloads:
        for item in (payload or {}).get("fact_overrides") or []:
            if isinstance(item, dict):
                merged[_override_identity(item)] = item
    return {
        "schema_version": "forenagent_human_overrides_v1",
        "instructions": "本案累计的人工回填事实；重跑时会自动重放，避免人工审计成果丢失。",
        "fact_overrides": list(merged.values()),
    }


def persist_human_overrides(output_dir: str, overrides: Optional[dict]) -> str:
    """把本次人工回填并入本案累计文件（07_human_overrides.json）。

    人工审计结果此前只“烧进”02/04 阶段 JSON，而阶段文件在每次重跑时都会重建，
    导致鉴定人审计过的内容被静默清空。这里另存一份累计文件，作为可重放的来源。
    """
    if not output_dir or not (overrides or {}).get("fact_overrides"):
        return ""
    path = os.path.join(output_dir, PERSISTED_HUMAN_OVERRIDES_JSON)
    existing: dict = {}
    if os.path.isfile(path):
        try:
            existing = _read_json(path)
        except (OSError, ValueError):
            existing = {}
    try:
        _write_json(path, _merge_override_payloads(existing, overrides))
    except OSError:
        return ""
    return path


def load_persisted_human_overrides(output_dir: str) -> Optional[str]:
    """返回本案累计人工回填文件的路径；没有则返回 None。"""
    if not output_dir:
        return None
    path = os.path.join(output_dir, PERSISTED_HUMAN_OVERRIDES_JSON)
    if not os.path.isfile(path):
        return None
    try:
        payload = _read_json(path)
    except (OSError, ValueError):
        return None
    if not (payload or {}).get("fact_overrides"):
        return None
    return path


# ============================================================
# 模块3：Pipeline编排
# ============================================================

def run_extractor(
    transfer_folder: str,
    output_dir: str,
    log: Callable[[str], None],
    runtime_config: Optional[RuntimeConfig] = None,
) -> str:
    """Step 1: Extractor Agent → 01_extractor.md"""
    config = runtime_config or get_runtime_config()
    log(f"[Step 1/4] 运行 Extractor Agent (OCR: {config.ocr_provider})...")
    # 直接调用 extractor 核心逻辑；默认本地 OCR，云端仅在显式确认时启用。
    if config.ocr_provider == "cloud":
        config.require_cloud_allowed("使用云端 OCR（LlamaParse）")
        _require_modules(["llama_parse"], "云端 OCR（LlamaParse）")
        if not os.getenv("LLAMA_CLOUD_API_KEY"):
            raise RuntimeError("云端 OCR（LlamaParse）未找到 LLAMA_CLOUD_API_KEY，请检查项目 .env。")
    elif config.ocr_provider == "local":
        _require_modules(["paddleocr"], "本地 OCR（PaddleOCR）")

    from agents.extractor_agent.extractor_agent_main import (
        _normalize_folder_for_case_scan,
        _resolve_output_path,
        build_extractor_graph,
    )

    # 构建state
    selected_folder = _normalize_folder_for_case_scan(transfer_folder)
    output_path = os.path.join(output_dir, STAGE_EXTRACTOR_MD)

    # 运行LangGraph
    graph = build_extractor_graph()
    initial_state = {
        "input_folder": selected_folder,
        "extracted_texts": [],
        "final_markdown": "",
        "output_path": output_path,
        "total_files": 0,
        "success_files": 0,
        "failed_files": 0,
        "runtime_config": config,
        "warnings": [],
    }
    result = graph.invoke(initial_state)
    output_path = result.get("output_path", output_path)

    total = result.get("total_files", 0)
    success = result.get("success_files", 0)
    failed = result.get("failed_files", 0)
    log(f"  提取完成: 共{total}个文件, 成功{success}, 失败{failed}")

    if not os.path.isfile(output_path):
        raise RuntimeError(f"Extractor输出文件不存在: {output_path}")

    return output_path


def run_structuring(
    extracted_md: str,
    mode: str,
    output_dir: str,
    log: Callable[[str], None],
    runtime_config: Optional[RuntimeConfig] = None,
) -> Tuple[str, str]:
    """Step 2: Structuring Agent → 02_structuring.json；MD仅临时用于报告组装。"""
    config = runtime_config or get_runtime_config()
    log(f"[Step 2/4] 运行 Structuring Agent (模式: {mode}, LLM: {config.llm_provider})...")
    _require_modules(["pydantic", "langchain_openai", "langchain_core", "openai"], "Structuring Agent")
    cmd = [
        sys.executable, "-m", "agents.structuring_agent.structuring_agent_main",
        "--input", extracted_md,
        "--output-dir", output_dir,
        "--section", "full",
        "--full-mode", mode,
        "--llm-provider", config.llm_provider,
        "--local-llm-base-url", config.local_llm_base_url,
        "--local-chat-model", config.local_chat_model,
        "--local-llm-device", config.local_llm_device,
        "--cloud-chat-model", config.cloud_chat_model,
    ]
    if config.allow_cloud:
        cmd.append("--allow-cloud")
    rc = run_agent_subprocess(cmd, timeout=600, log_callback=log)
    if rc != 0:
        if mode != "rule":
            log(f"  Structuring Agent失败（模式{mode}），尝试rule模式回退...")
            fallback_cmd = [
                sys.executable, "-m", "agents.structuring_agent.structuring_agent_main",
                "--input", extracted_md,
                "--output-dir", output_dir,
                "--section", "full",
                "--full-mode", "rule",
                "--llm-provider", "off",
            ]
            rc = run_agent_subprocess(fallback_cmd, timeout=600, log_callback=log)
            mode = "rule"
        if rc != 0:
            raise RuntimeError("Structuring Agent运行失败")

    # 查找输出文件
    json_path = os.path.join(output_dir, f"structured_info_{mode}.json")
    md_path = os.path.join(output_dir, f"structured_summary_{mode}.md")

    # 兼容：也可能是 rule 模式输出的
    if not os.path.isfile(json_path):
        json_path = os.path.join(output_dir, "structured_info_rule.json")
    if not os.path.isfile(md_path):
        md_path = os.path.join(output_dir, "structured_summary_rule.md")

    if not os.path.isfile(json_path):
        # 搜索目录下所有structured_info文件
        for f in os.listdir(output_dir):
            if f.startswith("structured_info_") and f.endswith(".json"):
                json_path = os.path.join(output_dir, f)
                break
    if not os.path.isfile(md_path):
        for f in os.listdir(output_dir):
            if f.startswith("structured_summary_") and f.endswith(".md"):
                md_path = os.path.join(output_dir, f)
                break

    if not os.path.isfile(json_path):
        raise RuntimeError(f"找不到structuring输出JSON: {output_dir}")
    if not os.path.isfile(md_path):
        raise RuntimeError(f"找不到structuring输出MD: {output_dir}")

    json_path = _promote_file(json_path, os.path.join(output_dir, STAGE_STRUCTURING_JSON))
    md_path = _promote_file(md_path, os.path.join(output_dir, STAGE_STRUCTURING_MD))
    log(f"  输出: {os.path.basename(json_path)}")
    return json_path, md_path


def _has_visual_images(folder: Optional[str]) -> bool:
    if not folder or not os.path.isdir(folder):
        return False
    image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
    for walk_item in os.walk(folder):
        files = walk_item[2]
        for name in files:
            if os.path.splitext(name)[1].lower() in image_exts:
                return True
    return False


def run_visual(
    photo_folder: Optional[str],
    output_dir: str,
    log: Callable[[str], None],
    skip_visual: bool = False,
) -> str:
    """Optional Visual Agent → 03_visual.json；未运行时写入占位 JSON，保持输出固定 7 个文件。"""
    json_path = os.path.join(output_dir, STAGE_VISUAL_JSON)

    def write_placeholder(reason: str, message: str) -> str:
        log(message)
        return _write_json(json_path, {
            "schema_version": "visual_rom_v1",
            "meta": {
                "input_dir": os.path.abspath(photo_folder) if photo_folder else "",
                "status": "skipped",
                "skip_reason": reason,
                "image_count": 0,
                "accepted_count": 0,
                "needs_goniometer": False,
                "summary": "未运行视觉读数",
                "summary_for_judicial_user": "未运行视觉读数",
                "conclusion_policy": "visual_reads_angles_only_never_grades",
            },
            "actions": [],
            "structured_rom_candidates": {},
            "structured_rom": {},
            "visual_measurements": [],
            "rejected_images": [],
            "no_goniometer_images": [],
            "warnings": [message],
            "judicial_explanation": {"plain_language": "视觉模块未运行，不参与关节活动度结论。"},
        })

    if skip_visual:
        return write_placeholder("skip_visual", "[视觉] 已按参数跳过 Visual Agent。")
    if not photo_folder:
        return write_placeholder("no_photo_folder", "[视觉] 未发现检查照片目录，跳过 Visual Agent。")
    if not _has_visual_images(photo_folder):
        return write_placeholder("no_images", "[视觉] 检查照片目录未发现图片，跳过 Visual Agent。")

    log(f"[视觉] 运行 Visual Agent，读取: {photo_folder}")
    from agents.visual_agent.visual_agent_main import run_pipeline as visual_run

    json_path, csv_path, md_path = visual_run(
        input_dir=photo_folder,
        output_dir=output_dir,
        output_json_name=STAGE_VISUAL_JSON,
        write_aux_outputs=False,
        write_visual_evidence=True,
    )
    _delete_files(csv_path, md_path)
    log(f"  输出: {os.path.basename(json_path)}")
    return json_path


def _has_judge_input_files(folder: Optional[str]) -> bool:
    if not folder or not os.path.isdir(folder):
        return False
    for path in Path(folder).rglob("*"):
        if path.is_file() and path.suffix.lower() in JUDGE_INPUT_EXTENSIONS:
            return True
    return False


def prepare_judge_input_dir(
    output_dir: str,
    structuring_md: str,
    log: Callable[[str], None],
    exam_doc_folder: Optional[str] = None,
) -> str:
    """在输出目录下准备 Judge 输入，避免向 database 源目录写入中间文件。"""
    staging_dir = os.path.join(output_dir, STAGE_WORK_DIR, "judge_input")
    if os.path.isdir(staging_dir):
        shutil.rmtree(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)

    target_md = os.path.join(staging_dir, "structured_summary_for_judge.md")
    shutil.copy2(structuring_md, target_md)
    log("  Judge 输入已在输出目录 staging 中准备，不写回 database 源目录。")

    if exam_doc_folder and os.path.isdir(exam_doc_folder):
        copied_count = 0
        for src in Path(exam_doc_folder).rglob("*"):
            if not src.is_file() or src.name.startswith("~$"):
                continue
            if src.suffix.lower() not in JUDGE_INPUT_EXTENSIONS:
                continue
            rel = src.relative_to(exam_doc_folder)
            dst = Path(staging_dir) / "exam_docs" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            copied_count += 1
        log(f"  已复制检查记录/文本材料 {copied_count} 个文件到 staging。")

    return staging_dir


def run_judge(
    structuring_md: str,
    output_dir: str,
    report_date: str,
    log: Callable[[str], None],
    visual_json: Optional[str] = None,
    exam_doc_folder: Optional[str] = None,
    runtime_config: Optional[RuntimeConfig] = None,
) -> Tuple[str, str]:
    """Step 3: Judge Agent → 04_judge.json；MD仅临时用于报告组装。"""
    config = runtime_config or get_runtime_config()
    log(f"[Step 3/4] 运行 Judge Agent (LLM: {config.llm_provider})...")

    judge_input_dir = prepare_judge_input_dir(output_dir, structuring_md, log, exam_doc_folder=exam_doc_folder)
    if not _has_judge_input_files(judge_input_dir):
        log("  [WARN] Judge staging 中未发现检查记录文本，仅使用前序结构化摘要与视觉结果生成第四部分。")

    cmd = [
        sys.executable, "-m", "agents.judge_agent.judge_agent_main",
        "--input-dir", judge_input_dir,
        "--output-dir", output_dir,
        "--report-date", report_date,
        "--subject-name-hint", "XXX",
        "--llm-provider", config.llm_provider,
        "--local-llm-base-url", config.local_llm_base_url,
        "--local-chat-model", config.local_chat_model,
        "--local-llm-device", config.local_llm_device,
        "--cloud-chat-model", config.cloud_chat_model,
    ]
    if config.allow_cloud:
        cmd.append("--allow-cloud")
    if config.llm_provider == "off":
        cmd.append("--disable-llm")
    if visual_json:
        cmd.extend(["--visual-json", visual_json])
    rc = run_agent_subprocess(cmd, timeout=600, log_callback=log)

    if rc != 0:
        raise RuntimeError("Judge Agent运行失败")

    json_path = os.path.join(output_dir, "section4_result.json")
    md_path = os.path.join(output_dir, "section4_report.md")

    if not os.path.isfile(json_path):
        raise RuntimeError(f"找不到judge输出JSON: {json_path}")

    json_path = _promote_file(json_path, os.path.join(output_dir, STAGE_JUDGE_JSON))
    md_path = _promote_file(md_path, os.path.join(output_dir, STAGE_JUDGE_MD)) if os.path.isfile(md_path) else md_path
    log(f"  输出: {os.path.basename(json_path)}")
    return json_path, md_path


def run_ruler(
    structuring_json: str,
    judge_json: str,
    output_dir: str,
    log: Callable[[str], None],
) -> Tuple[str, str]:
    """Step 4: Ruler Agent → 05_ruler.json；MD仅临时用于报告组装。"""
    log("[Step 4/4] 运行 Ruler Agent...")
    from agents.ruler_agent.ruler_agent_main import run_pipeline as ruler_run

    json_path, md_path = ruler_run(
        structuring_json_path=structuring_json,
        judge_json_path=judge_json,
        output_dir=output_dir,
    )
    json_path = _promote_file(json_path, os.path.join(output_dir, STAGE_RULER_JSON))
    md_path = _promote_file(md_path, os.path.join(output_dir, STAGE_RULER_MD)) if os.path.isfile(md_path) else md_path
    log(f"  输出: {os.path.basename(json_path)}")
    return json_path, md_path


# ============================================================
# 模块4：报告组装
# ============================================================

def _strip_agent_header(text: str) -> str:
    """去掉各agent添加的统一标题行，保留正文。"""
    lines = text.split("\n")
    cleaned = []
    skip_patterns = [
        "# 司法鉴定意见书结构化提要",
        "# 司法鉴定意见书（AI草稿）",
    ]
    for line in lines:
        if any(p in line for p in skip_patterns):
            continue
        cleaned.append(line)
    # 去掉开头空行
    while cleaned and cleaned[0].strip() == "":
        cleaned.pop(0)
    return "\n".join(cleaned)


def _extract_sections_123(text: str) -> str:
    """从structuring_agent输出中仅提取第1-3部分（一～三），丢弃4-7的占位内容。"""
    lines = text.split("\n")
    result = []
    in_first_3 = True
    for line in lines:
        # 检测第四部分开始标记
        if re.match(r'^##\s*四[、.．]', line) or re.match(r'^##\s*四、鉴定过程', line):
            in_first_3 = False
        # 也检测"七、附件"等后续章节（属于structuring占位输出）
        if not in_first_3:
            continue
        result.append(line)
    # 去掉末尾空行
    while result and result[-1].strip() == "":
        result.pop()
    return "\n".join(result)


def assemble_report(
    structuring_md_path: str,
    judge_md_path: str,
    ruler_md_path: str,
    structuring_json_path: Optional[str] = None,
    judge_json_path: Optional[str] = None,
    ruler_json_path: Optional[str] = None,
    report_date: str = "",
) -> str:
    """渲染正式报告；优先使用专家文书渲染器，失败时回退旧拼接逻辑。"""
    if structuring_json_path and judge_json_path and ruler_json_path:
        try:
            from agents.boss_agent.report_renderer import render_report

            return render_report(
                structuring=_read_json(structuring_json_path),
                judge=_read_json(judge_json_path),
                ruler=_read_json(ruler_json_path),
                report_date=report_date,
            )
        except Exception as exc:
            print(f"[WARN] 专家文书渲染器失败，回退旧拼接报告: {exc}")

    with open(structuring_md_path, "r", encoding="utf-8") as f:
        raw_123 = _strip_agent_header(f.read())
    sections_123 = _extract_sections_123(raw_123)

    with open(judge_md_path, "r", encoding="utf-8") as f:
        section_4 = _strip_agent_header(f.read())
    with open(ruler_md_path, "r", encoding="utf-8") as f:
        sections_56 = _strip_agent_header(f.read())

    full_report = f"# 司法鉴定意见书（AI草稿）\n\n{sections_123}\n\n{section_4}\n\n{sections_56}\n"
    return full_report


# ============================================================
# 模块5：格式规范化（纯规则）
# ============================================================

def normalize_format(text: str) -> str:
    """规则化的格式统一，不涉及内容修改。"""
    # 1. 多余空行压缩（连续3行以上→2行）
    text = re.sub(r'\n{4,}', '\n\n\n', text)

    # 2. 数字与百分号/度号间空格统一
    text = re.sub(r'(\d\.?\d*)\s+%', r'\1%', text)
    text = re.sub(r'(\d)\s+°', r'\1°', text)

    # 3. 角度"度"统一为"°"（在数字后）
    text = re.sub(r'(\d)度', r'\1°', text)

    # 4. 中文标点后的多余空格（仅删除空格/制表符，保留换行）
    text = re.sub(r'([，。；：！？、）】」》])[ \t]+', r'\1', text)

    # 5. 确保法规引用中《》配对
    # （仅做基本检查，不修改内容）

    return text


# ============================================================
# 模块6：LLM审查
# ============================================================

def _call_llm(
    prompt: str,
    timeout: int = 120,
    runtime_config: Optional[RuntimeConfig] = None,
    model: Optional[str] = None,
    max_retries: Optional[int] = None,
    retry_without_json_mode: bool = True,
) -> str:
    """按当前本地/云端配置调用 OpenAI-compatible LLM；默认不静默切云端。"""
    config = runtime_config or get_runtime_config()
    if config.llm_provider == "off":
        return ""
    try:
        return call_chat_json(
            prompt,
            config=config,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            retry_without_json_mode=retry_without_json_mode,
        )
    except Exception:
        return ""


def _parse_json_from_response(text: str) -> dict:
    """从LLM响应中提取JSON。"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试提取```json ... ```块
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试找第一个{...}块
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def collect_uncertainties(output_dir: str) -> List[dict]:
    """从各 agent 输出 JSON 中收集存疑信息，统一进入 Boss 审计报告。"""
    results = []

    structuring_path = os.path.join(output_dir, STAGE_STRUCTURING_JSON)
    if os.path.isfile(structuring_path):
        with open(structuring_path, "r", encoding="utf-8") as f:
            structuring = json.load(f)
        for item in structuring.get("conflicting_or_unclear_points", []) or []:
            if item:
                results.append({"source": "Structuring Agent", "text": item})
        for item in (structuring.get("forensic_facts", {}) or {}).get("uncertain_points", []) or []:
            if item:
                results.append({"source": "Structuring forensic_facts", "text": item})
        completeness = structuring.get("summary_completeness") or {}
        headings = completeness.get("detected_documents") or []
        produced = completeness.get("summary_item_count")
        if headings and isinstance(produced, int) and len(headings) > produced:
            results.append({
                "source": "Structuring 资料摘要",
                "text": (
                    f"材料中出现 {len(headings)} 类病历（{'、'.join(headings)}），"
                    f"资料摘要仅生成 {produced} 条，请人工核对第三部分是否遗漏。"
                ),
            })

    s4_path = os.path.join(output_dir, STAGE_JUDGE_JSON)
    if os.path.isfile(s4_path):
        with open(s4_path, "r", encoding="utf-8") as f:
            s4 = json.load(f)
        for item in s4.get("section4", {}).get("physical_exam", {}).get("uncertain_points", []) or []:
            results.append({"source": "体格检查", "text": item})
        for item in s4.get("warnings", []) or []:
            results.append({"source": "Judge Agent", "text": item})

    s56_path = os.path.join(output_dir, STAGE_RULER_JSON)
    if os.path.isfile(s56_path):
        with open(s56_path, "r", encoding="utf-8") as f:
            s56 = json.load(f)
        for r in s56.get("section5", {}).get("disability_results", []) or []:
            if not r.get("clause"):
                results.append({"source": "条款匹配", "text": r.get("text", "未匹配条款")})
        for item in (s56.get("validation", {}) or {}).get("warnings", []) or []:
            results.append({"source": "Ruler validation", "text": item})

    return results


def collect_report_placeholders(full_report: str) -> List[dict]:
    """把正式报告中的占位/不确定正文定位进审计报告。"""
    results: List[dict] = []
    markers = _placeholder_markers()
    for line_no, line in enumerate(full_report.splitlines(), start=1):
        text = line.strip()
        if not text or not any(marker in text for marker in markers):
            continue
        results.append({"source": f"正式报告第{line_no}行", "text": f"存在占位或不确定字段：{text}"})
    return results


def llm_review(full_report: str, uncertainties: List[dict], log: Callable[[str], None], runtime_config: Optional[RuntimeConfig] = None) -> str:
    """LLM审查报告，仅输出审查意见，不修改原文。"""
    config = runtime_config or get_runtime_config()
    log(f"[审查] 运行LLM审查（LLM: {config.llm_provider}）...")

    uncertainties_text = json.dumps(uncertainties, ensure_ascii=False, indent=2) if uncertainties else "无"

    # 截取报告全文（避免超出token限制）
    report_text = full_report
    if len(report_text) > 8000:
        report_text = report_text[:8000] + "\n...(报告过长，已截断)"

    prompt = f"""你是一位资深司法鉴定审查员。请审查以下AI生成的司法鉴定意见书草稿。

审查要求：
1. 格式规范性：检查标点、标准/规范引用方式、明显错别字、异常用语、句式不顺等文书质量问题；不要把不同章节采用不同编号体系本身列为问题。
2. 完整性：是否有缺失的关键信息？尤其关注“待补充”“不详”“未能形成稳定摘录”等不能直接进入正式文书的内容。
3. 存疑点：是否有逻辑矛盾、数据异常、提取错误或需要人工复核之处？

已知存疑信息（来自各agent标记）：
{uncertainties_text}

请严格按以下JSON格式输出审查意见：
{{
  "format_issues": ["格式/文书质量问题1", "格式/文书质量问题2"],
  "missing_info": ["缺失信息1"],
  "uncertain_points": ["存疑点1"],
  "overall_assessment": "整体评估（一句话）"
}}

重要：你只能指出问题，绝不能修改或重写报告内容；只输出上述 JSON 字段，不要新增其他审查章节。

以下是报告全文：
{report_text}"""

    try:
        response = _call_llm(prompt, timeout=120, runtime_config=config)
        if not response:
            log("[审查] LLM调用失败，跳过审查")
            return "## AI一致性审查\n\n（LLM审查跳过：API调用失败）\n"

        review_data = _parse_json_from_response(response)
        if not review_data:
            log(f"[审查] LLM审查结果解析失败，原始返回前200字：{response[:200]}")
        return _format_review_report(review_data)
    except Exception as e:
        log(f"[审查] LLM审查异常: {e}")
        return f"## AI一致性审查\n\n（LLM审查跳过：{e}）\n"


def _as_review_items(value: Any) -> List[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _line_for_span(report_md: str, span: str) -> int:
    span = str(span or "").strip()
    if not span:
        return 0
    for line_no, line in enumerate(report_md.splitlines(), start=1):
        if span in line:
            return line_no
    return 0


def _short_span_from_text(report_md: str, text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    for line in report_md.splitlines():
        clean = line.strip()
        if clean and (clean in text or text in clean):
            return clean[:80]
    m = re.search(r"[一二三四五六七八九十、，。；：：《》\w\s]{8,80}", text)
    return m.group(0).strip() if m else text[:80]


REPORT_PLACEHOLDER_MARKERS = [
    "待补充", "未查找到", "缺失", "无法判断", "不稳定", "未能形成稳定", "未读取到稳定",
    "暂未识别", "暂未形成", "人工补充", "需人工", "[OCR不清晰]", "[WARN]", "____", "___", "XXX",
]
REPORT_SYSTEM_TEXT_MARKERS = ["本次未能从材料中形成稳定", "建议结合原始材料", "系统提示"]
REPEATED_PUNCT_RE = re.compile(r"[，。；：、！？,.;:!?]{2,}")
REPEATED_WORD_RE = re.compile(r"([一-龥]{2,8})\1+")
REPEATED_CHAR_RE = re.compile(r"([一-龥])\1{2,}")
REPEATED_LATIN_RE = re.compile(r"([A-Za-z]{2,})\1+", re.IGNORECASE)
OCR_ARTIFACT_RE = re.compile(r"[�□■]{1,}|[=*_~·•]{4,}|\[(?:OCR不清晰|WARN)\]")
DATE_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*(?:年|[./-])\s*\d{1,2}\s*(?:月|[./-])\s*\d{1,2}\s*日?(?!\d)")
CN_ID_RE = re.compile(r"(?<!\d)(?:\d[ -]?){17}[0-9Xx](?![0-9Xx])")
KEY_IDENTIFIER_LABEL_RE = re.compile(
    r"(?P<label>公民身份号码|身份证(?:号|号码)?|身份号码|病案号|病历号|住院号|门诊号|就诊卡号|检查号|影像号|报告编号|报告号|委托编号|委托号|案号|车牌号|司法鉴定人执业证号|执业证号|许可证号|申请单号|登记号)"
    r"\s*[:：]?\s*(?P<value>[^，。；;、\s）)】》:：]{2,48})"
)
CLAUSE_NUMBER_RE = re.compile(r"(?<!\d)(?:[1-9]|10)\.\d+(?:\.\d+){1,2}(?!\d)")
PERIOD_NUMBER_RE = re.compile(r"(?:误工|护理|营养)(?:期|期限)?[^0-9一二三四五六七八九十]{0,8}[0-9]{1,3}\s*(?:日|天)")
GRADE_NUMBER_RE = re.compile(r"(?:[一二三四五六七八九十]|[1-9]|10)级伤残")
VERTEBRA_LEVEL_RE = re.compile(r"(?<![A-Za-z0-9])(?:[TLCSlcs][0-9]{1,2}|(?:胸|腰|颈|骶)[0-9]{1,2})(?![A-Za-z0-9])")
MEASURE_NUMBER_RE = re.compile(r"(?<!\d)(?:\d+(?:\.\d+)?\s*(?:%|°|度|cm²|cm2|mm²|mm2|cm|mm|厘米|毫米|平方厘米)|(?:1/3|三分之一|二分之一|四分之一))(?!\d)")
ADDRESS_POLLUTION_RE = re.compile(r"预检|分诊|就诊|门诊号|住院号|病案号|病历号|科室|检查号|影像号|CT|MRI|MR|DR|X线|报告单|打印时间|扫描时间|\d{4}\s*年\s*\d{1,2}\s*月")
HOSPITAL_RE = re.compile(r"[一-龥A-Za-z0-9（）()·]{2,40}(?:医院|卫生院|医学院附属医院|中医院|人民医院|中心医院|门诊部|诊所|卫生服务中心|司法鉴定中心)")
PERSON_NAME_STOPWORDS = {
    "被鉴定人", "司法鉴定", "鉴定中心", "浙江大学", "人民法院", "人民医院", "中心医院", "材料显示", "病历摘要", "基本案情", "鉴定意见",
}


def _placeholder_markers() -> List[str]:
    return REPORT_PLACEHOLDER_MARKERS + REPORT_SYSTEM_TEXT_MARKERS


def _line_text(report_md: str, line_no: int) -> str:
    lines = report_md.splitlines()
    if line_no <= 0 or line_no > len(lines):
        return ""
    return lines[line_no - 1]


def _find_repeated_punctuation(text: str) -> Optional[re.Match[str]]:
    for match in REPEATED_PUNCT_RE.finditer(text or ""):
        token = match.group(0)
        if token in {"……", "——"}:
            continue
        return match
    return None


def _narrow_format_span(report_md: str, span: str, line_no: int, message: str) -> tuple[str, int, int]:
    line = _line_text(report_md, line_no) if line_no else ""
    search_text = line or span or message
    punct = _find_repeated_punctuation(search_text)
    if punct:
        return punct.group(0), punct.start(), punct.end()
    word = REPEATED_WORD_RE.search(search_text or "")
    if word:
        return word.group(0), word.start(), word.end()
    if line and span and span in line and len(span) <= 20:
        start = line.find(span)
        return span, start, start + len(span)
    return span, -1, -1


def _annotation_from_item(report_md: str, item: Any, category: str, index: int) -> Optional[dict]:
    if item in (None, "", [], {}):
        return None
    span_start_col = -1
    span_end_col = -1
    if isinstance(item, dict):
        message = str(item.get("message") or item.get("问题") or item.get("text") or item.get("说明") or "").strip()
        span = str(item.get("span_text") or item.get("原文") or item.get("excerpt") or item.get("定位文字") or "").strip()
        note = str(item.get("suggested_note") or item.get("建议") or item.get("suggested_action") or "").strip()
        try:
            line_no = int(item.get("line_no") or item.get("line") or item.get("行号") or 0)
        except (TypeError, ValueError):
            line_no = 0
        try:
            span_start_col = int(item.get("span_start_col", -1))
            span_end_col = int(item.get("span_end_col", -1))
        except (TypeError, ValueError):
            span_start_col = -1
            span_end_col = -1
        severity = str(item.get("severity") or item.get("级别") or ("critical" if category == "missing" else "warning")).strip()
    else:
        message = str(item).strip()
        span = _short_span_from_text(report_md, message)
        note = ""
        line_no = _line_for_span(report_md, span)
        severity = "critical" if category == "missing" else "warning"
    if not message and not span:
        return None
    if not span:
        span = _short_span_from_text(report_md, message)
    if not line_no:
        line_no = _line_for_span(report_md, span)
    if category == "format":
        narrowed, start_col, end_col = _narrow_format_span(report_md, span, line_no, message)
        if narrowed:
            span = narrowed
            if start_col >= 0 and end_col >= 0:
                span_start_col = start_col
                span_end_col = end_col
    result = {
        "id": f"ai_ann_{index:03d}",
        "category": category,
        "severity": severity if severity in {"info", "warning", "critical"} else ("critical" if category == "missing" else "warning"),
        "line_no": line_no,
        "span_text": span,
        "message": message or note or span,
        "suggested_note": note,
        "source": "ai_preview_audit",
    }
    if span_start_col >= 0 and span_end_col >= span_start_col:
        result["span_start_col"] = span_start_col
        result["span_end_col"] = span_end_col
    return result


def _summary_section_annotation(report_md: str) -> Optional[dict]:
    lines = report_md.splitlines()
    for line_no, line in enumerate(lines, start=1):
        if line.strip().startswith("三、资料摘要"):
            return {
                "id": "rule_summary_audit_001",
                "category": "uncertain",
                "severity": "warning",
                "line_no": line_no,
                "span_text": "三、资料摘要",
                "message": "资料摘要为默认人工复核区，请核对病历、影像及材料摘要是否准确。",
                "suggested_note": "请逐项核对资料摘要是否与原始材料一致。",
                "source": "boss_summary_default_audit",
            }
    return None


def _make_rule_annotation(category: str, severity: str, line_no: int, span: str, start: int, end: int, message: str, note: str, source: str) -> dict:
    ann = {
        "id": "",
        "category": category,
        "severity": severity,
        "line_no": line_no,
        "span_text": span,
        "message": message,
        "suggested_note": note,
        "source": source,
    }
    if start >= 0 and end >= start:
        ann["span_start_col"] = start
        ann["span_end_col"] = end
    return ann


def _scan_placeholder_annotations(line_no: int, line: str) -> List[dict]:
    clean = line.strip()
    if not clean:
        return []
    anns: List[dict] = []
    for marker in _placeholder_markers():
        pos = line.find(marker)
        if pos < 0:
            continue
        anns.append(_make_rule_annotation(
            "missing", "critical", line_no, marker, pos, pos + len(marker),
            "该处仍有待补充、系统提示或 OCR 不确定文字，需在出具前处理。",
            "请补充正式文字、核对原始材料或删除系统提示。",
            "boss_placeholder_scan",
        ))
        break
    return anns


def _scan_repetition_annotations(line_no: int, line: str) -> List[dict]:
    anns: List[dict] = []
    for match in REPEATED_PUNCT_RE.finditer(line):
        token = match.group(0)
        if token in {"……", "——"}:
            continue
        anns.append(_make_rule_annotation(
            "format", "info", line_no, token, match.start(), match.end(),
            "此处可能存在重复标点。", "请检查该标点是否需要删改。", "boss_format_scan",
        ))
    for regex, label in [(REPEATED_WORD_RE, "重复词语"), (REPEATED_CHAR_RE, "单字异常重复"), (REPEATED_LATIN_RE, "英文/OCR片段重复")]:
        for match in regex.finditer(line):
            token = match.group(0)
            if len(token.strip()) < 2:
                continue
            anns.append(_make_rule_annotation(
                "format", "warning" if len(token) >= 6 else "info", line_no, token, match.start(), match.end(),
                f"此处可能存在{label}。", "请核对是否为 OCR 或输入重复。", "boss_repetition_scan",
            ))
    return anns


def _scan_ocr_artifact_annotations(line_no: int, line: str) -> List[dict]:
    anns: List[dict] = []
    for match in OCR_ARTIFACT_RE.finditer(line):
        token = match.group(0)
        anns.append(_make_rule_annotation(
            "format", "warning", line_no, token, match.start(), match.end(),
            "此处可能存在 OCR 伪影或扫描噪声。", "请对照原始材料核对该片段。", "boss_ocr_artifact_scan",
        ))
    for phrase in ["打印时间", "扫描时间", "本页无正文", "仅供参考"]:
        pos = line.find(phrase)
        if pos >= 0:
            anns.append(_make_rule_annotation(
                "format", "info", line_no, phrase, pos, pos + len(phrase),
                "此处可能是页眉页脚或扫描噪声。", "如非正文内容，请删除或修正。", "boss_ocr_artifact_scan",
            ))
            break
    if re.search(r"[一-龥].{0,10}\b(?:I1|l1|Tll|O0)\b", line):
        m = re.search(r"\b(?:I1|l1|Tll|O0)\b", line)
        if m:
            anns.append(_make_rule_annotation(
                "format", "warning", line_no, m.group(0), m.start(), m.end(),
                "此处可能存在 OCR 字符混淆。", "请核对是否应为数字或椎体节段。", "boss_ocr_artifact_scan",
            ))
    return anns


def _ranges_overlap(start: int, end: int, ranges: List[tuple[int, int]]) -> bool:
    return any(start < existing_end and end > existing_start for existing_start, existing_end in ranges)


def _invalid_date_reason(token: str) -> str:
    nums = re.findall(r"\d+", token)
    if len(nums) < 3:
        return ""
    try:
        month = int(nums[-2])
        day = int(nums[-1])
    except ValueError:
        return ""
    if month <= 0 or month > 12:
        return "月份数值异常"
    if day <= 0 or day > 31:
        return "日期数值异常"
    return ""


def _append_span_annotation(anns: List[dict], used_ranges: List[tuple[int, int]], category: str, severity: str, line_no: int, line: str, start: int, end: int, message: str, note: str, source: str) -> None:
    if start < 0 or end <= start or _ranges_overlap(start, end, used_ranges):
        return
    span = line[start:end]
    if not span.strip():
        return
    anns.append(_make_rule_annotation(category, severity, line_no, span, start, end, message, note, source))
    used_ranges.append((start, end))


def _scan_key_identifier_annotations(line_no: int, line: str, max_items: int = 8) -> List[dict]:
    """在最终预览阶段蓝色标出需人工核对的关键数字/号码。"""
    anns: List[dict] = []
    used_ranges: List[tuple[int, int]] = []

    for match in KEY_IDENTIFIER_LABEL_RE.finditer(line):
        label = match.group("label")
        value = match.group("value")
        if not value or value in {"不详", "未查找到", "待补充"}:
            continue
        _append_span_annotation(
            anns, used_ranges, "format", "info", line_no, line, match.start(), match.end(),
            f"{label}属于出具前需要逐字核对的关键号码。", "请对照原始材料核对该号码/编号。", "boss_key_identifier_scan",
        )
        if len(anns) >= max_items:
            return anns

    for match in CN_ID_RE.finditer(line):
        _append_span_annotation(
            anns, used_ranges, "format", "warning", line_no, line, match.start(), match.end(),
            "此处疑似公民身份号码，属于出具前必须核对的关键号码。", "请核对身份证号码是否与原始材料一致。", "boss_key_identifier_scan",
        )
        if len(anns) >= max_items:
            return anns

    for match in DATE_RE.finditer(line):
        reason = _invalid_date_reason(match.group(0))
        if reason:
            _append_span_annotation(
                anns, used_ranges, "uncertain", "critical", line_no, line, match.start(), match.end(),
                f"此处日期存在{reason}，可能是 OCR 或抽取错误。", "请对照原始材料修正日期。", "boss_key_identifier_scan",
            )
        else:
            _append_span_annotation(
                anns, used_ranges, "format", "info", line_no, line, match.start(), match.end(),
                "此处为关键日期，建议出具前核对。", "请核对日期是否与原始材料一致。", "boss_key_identifier_scan",
            )
        if len(anns) >= max_items:
            return anns

    for regex, message, note in [
        (CLAUSE_NUMBER_RE, "此处为标准条款号，建议核对条款引用。", "请核对条款号与适用标准是否一致。"),
        (PERIOD_NUMBER_RE, "此处为三期关键天数，建议核对。", "请核对误工/护理/营养天数。"),
        (GRADE_NUMBER_RE, "此处为伤残等级结论，建议核对。", "请核对第五、六部分伤残等级是否一致。"),
    ]:
        for match in regex.finditer(line):
            _append_span_annotation(anns, used_ranges, "format", "info", line_no, line, match.start(), match.end(), message, note, "boss_key_identifier_scan")
            if len(anns) >= max_items:
                return anns

    if any(k in line for k in ["椎", "骨折", "影像", "CT", "MR", "MRI", "诊断", "伤残", "三期", "误工", "护理", "营养", "瘢痕", "活动度", "ROM", "压缩"]):
        for regex, message, note in [
            (VERTEBRA_LEVEL_RE, "此处为椎体节段，建议核对。", "请核对椎体节段是否为损伤部位而非扫描范围。"),
            (MEASURE_NUMBER_RE, "此处为鉴定关键数值，建议核对。", "请核对比例、角度、长度或面积数值。"),
        ]:
            for match in regex.finditer(line):
                _append_span_annotation(anns, used_ranges, "format", "info", line_no, line, match.start(), match.end(), message, note, "boss_key_identifier_scan")
                if len(anns) >= max_items:
                    return anns
    return anns


def _is_plausible_person_name(name: str) -> bool:
    clean = re.sub(r"[^一-龥·]", "", str(name or "")).strip("·")
    if len(clean) < 2 or len(clean) > 4:
        return False
    if clean in PERSON_NAME_STOPWORDS or any(token in clean for token in ["医院", "法院", "鉴定", "中心", "材料", "伤情", "损伤"]):
        return False
    if clean in {"目前", "本次", "其余", "上述", "相关", "材料", "伤后", "外伤", "进行", "符合", "需要", "建议"}:
        return False
    return True


def _subject_name_mentions(report_md: str) -> List[dict]:
    mentions: List[dict] = []
    patterns = [
        re.compile(r"被鉴定人[:：]\s*([一-龥·]{2,4})"),
        re.compile(r"被鉴定人([一-龥·]{2,4})(?=于|在|因|伤后|经|的|，|。)"),
        re.compile(r"据([一-龥·]{2,4})在"),
        re.compile(r"(?:造成|致)([一-龥·]{2,4})(?:受伤|损伤)"),
    ]
    for line_no, line in enumerate(report_md.splitlines(), start=1):
        for pattern in patterns:
            for match in pattern.finditer(line):
                name = match.group(1)
                if _is_plausible_person_name(name):
                    mentions.append({"name": name, "line_no": line_no, "start": match.start(1), "end": match.end(1), "line": line})
    return mentions


def _scan_address_annotations(report_md: str) -> List[dict]:
    anns: List[dict] = []
    for line_no, line in enumerate(report_md.splitlines(), start=1):
        if "被鉴定人" not in line or "住址" not in line:
            continue
        match = re.search(r"住址[:：](?P<address>.*?)(?:，公民身份号码|，身份证|。|$)", line)
        if not match:
            continue
        address = match.group("address")
        for bad in ADDRESS_POLLUTION_RE.finditer(address):
            start = match.start("address") + bad.start()
            end = match.start("address") + bad.end()
            anns.append(_make_rule_annotation(
                "uncertain", "warning", line_no, line[start:end], start, end,
                "被鉴定人住址中疑似混入医疗字段、时间或编号。", "请核对住址字段，删除就诊时间、病案号、科室等非住址内容。", "boss_address_audit_scan",
            ))
            break
    return anns


def _scan_identity_consistency_annotations(report_md: str) -> List[dict]:
    anns: List[dict] = []
    lines = report_md.splitlines()
    mentions = _subject_name_mentions(report_md)
    names = {item["name"] for item in mentions}
    if len(names) > 1:
        first_by_name: Dict[str, dict] = {}
        for item in mentions:
            first_by_name.setdefault(item["name"], item)
        for item in first_by_name.values():
            anns.append(_make_rule_annotation(
                "uncertain", "critical", item["line_no"], item["name"], item["start"], item["end"],
                "报告中被鉴定人姓名前后不一致，可能存在 OCR 误识别或主体混淆。", "请以委托书/身份证明为准统一被鉴定人姓名。", "boss_identity_consistency_scan",
            ))
    id_mentions: List[dict] = []
    for line_no, line in enumerate(lines, start=1):
        if not any(k in line for k in ["身份", "身份证", "公民身份号码", "被鉴定人"]):
            continue
        for match in CN_ID_RE.finditer(line):
            raw = match.group(0)
            norm = re.sub(r"\D", "", raw[:-1]) + raw[-1].upper()
            id_mentions.append({"id": norm, "line_no": line_no, "start": match.start(), "end": match.end(), "span": raw})
    if len({item["id"] for item in id_mentions}) > 1:
        for item in id_mentions:
            anns.append(_make_rule_annotation(
                "uncertain", "critical", item["line_no"], item["span"], item["start"], item["end"],
                "报告中出现多个不同身份证号/身份号码。", "请核对被鉴定人身份信息是否前后一致。", "boss_identity_consistency_scan",
            ))
    return anns


def _record_mentions_for_line(line_no: int, line: str) -> List[dict]:
    record_labels = {"病案号", "病历号", "住院号", "门诊号", "就诊卡号", "检查号", "影像号", "报告编号", "报告号"}
    hospitals = [m.group(0) for m in HOSPITAL_RE.finditer(line)]
    date_match = DATE_RE.search(line)
    date = date_match.group(0) if date_match else ""
    mentions: List[dict] = []
    for match in KEY_IDENTIFIER_LABEL_RE.finditer(line):
        label = match.group("label")
        if label not in record_labels:
            continue
        value = str(match.group("value") or "").strip()
        if not value or value in {"不详", "未查找到", "待补充"}:
            continue
        mentions.append({
            "line_no": line_no,
            "line": line,
            "label": label,
            "value": value,
            "hospital": hospitals[0] if hospitals else "",
            "hospitals": hospitals,
            "date": date,
            "start": match.start(),
            "end": match.end(),
            "span": match.group(0),
        })
    return mentions


def _scan_record_number_consistency_annotations(report_md: str) -> List[dict]:
    anns: List[dict] = []
    for line_no, line in enumerate(report_md.splitlines(), start=1):
        mentions = _record_mentions_for_line(line_no, line)
        if not mentions:
            continue
        hospitals = {h for item in mentions for h in item.get("hospitals", [])}
        if len(hospitals) > 1:
            span = line.strip()[:80]
            anns.append(_make_rule_annotation(
                "uncertain", "warning", line_no, span, 0, min(len(line), len(span)),
                "同一资料摘要行中出现多个医院名称和病案/检查编号，可能存在来源混杂。", "请核对该行病历来源、医院和编号是否被 OCR 或抽取混合。", "boss_record_consistency_scan",
            ))
        by_label: Dict[str, List[dict]] = {}
        for item in mentions:
            by_label.setdefault(item["label"], []).append(item)
        for label, items in by_label.items():
            values = {item["value"] for item in items}
            if len(values) <= 1:
                continue
            for item in items:
                anns.append(_make_rule_annotation(
                    "uncertain", "warning", item["line_no"], item["span"], item["start"], item["end"],
                    f"同一行内出现多个不同{label}，可能是来源或编号混杂。", "请按医院、日期和病历来源分别核对编号。", "boss_record_consistency_scan",
                ))
    return anns


def _is_ignorable_similarity_line(line: str) -> bool:
    clean = line.strip()
    if not clean:
        return True
    if re.match(r"^[一二三四五六七八九十]+、", clean) or clean.startswith(("浙江大学司法鉴定中心", "司法鉴定意见书", "司法鉴定人签名", "《司法鉴定人执业证》", "七、附件")):
        return True
    return False


def _normalize_sentence_for_similarity(text: str) -> str:
    text = re.sub(r"[0-9０-９一二三四五六七八九十年月日号第条级\.．:/：\-—_\s]+", "", text)
    text = re.sub(r"[，。；：、！？,.;:!?（）()《》“”\"'【】\[\]]", "", text)
    return text.strip()


def _split_report_sentences(report_md: str) -> List[dict]:
    sentences: List[dict] = []
    for line_no, line in enumerate(report_md.splitlines(), start=1):
        if _is_ignorable_similarity_line(line):
            continue
        for match in re.finditer(r"[^。！？；;!?]{18,}[。！？；;!?]?", line):
            sentence = match.group(0).strip()
            normalized = _normalize_sentence_for_similarity(sentence)
            if len(normalized) < 18:
                continue
            sentences.append({"line_no": line_no, "start": match.start(), "end": match.end(), "sentence": sentence, "normalized": normalized})
    return sentences


def _scan_similar_sentence_annotations(report_md: str, limit: int = 8) -> List[dict]:
    anns: List[dict] = []
    previous: List[dict] = []
    for item in _split_report_sentences(report_md):
        duplicate_of = None
        for prev in previous:
            if item["normalized"] == prev["normalized"]:
                duplicate_of = prev
                break
            if SequenceMatcher(None, item["normalized"], prev["normalized"]).ratio() >= 0.90:
                duplicate_of = prev
                break
        if duplicate_of:
            anns.append(_make_rule_annotation(
                "format", "warning", item["line_no"], item["sentence"], item["start"], item["end"],
                "此处与前文表述高度相似，可能存在重复上下文、模板残留或 OCR 复制污染。", f"请核对是否与第{duplicate_of['line_no']}行重复。", "boss_similar_context_scan",
            ))
            if len(anns) >= limit:
                return anns
        previous.append(item)
    return anns


def _grade_mentions(report_md: str) -> List[tuple[int, str]]:
    out: List[tuple[int, str]] = []
    for line_no, line in enumerate(report_md.splitlines(), start=1):
        if not any(k in line for k in ["构成", "伤残", "鉴定意见", "分析说明"]):
            continue
        for match in re.finditer(r"([一二三四五六七八九十]|[1-9]|10)级伤残", line):
            out.append((line_no, match.group(0)))
    return out


def _period_mentions(report_md: str) -> Dict[str, List[tuple[int, str]]]:
    found: Dict[str, List[tuple[int, str]]] = {"误工": [], "护理": [], "营养": []}
    for line_no, line in enumerate(report_md.splitlines(), start=1):
        for key in found:
            for match in re.finditer(rf"{key}(?:期|期限)?[^0-9一二三四五六七八九十]{{0,8}}([0-9]{{1,3}})\s*(?:日|天)", line):
                found[key].append((line_no, match.group(0)))
    return found


def _scan_report_fact_consistency(report_md: str) -> List[dict]:
    anns: List[dict] = []
    lines = report_md.splitlines()
    text = "\n".join(lines)
    grades = _grade_mentions(report_md)
    grade_values = {g for _, g in grades}
    if len(grade_values) > 1:
        for line_no, span in grades:
            line = lines[line_no - 1]
            pos = line.find(span)
            anns.append(_make_rule_annotation(
                "uncertain", "warning", line_no, span, pos, pos + len(span),
                "报告中出现多个不同伤残等级表述。", "请核对分析说明与鉴定意见是否一致。", "boss_fact_consistency_scan",
            ))
    if re.search(r"不构成伤残|未达伤残等级", text) and re.search(r"构成[一二三四五六七八九十0-9]+级伤残", text):
        for line_no, line in enumerate(lines, start=1):
            if "不构成伤残" in line or "未达伤残等级" in line or re.search(r"构成[一二三四五六七八九十0-9]+级伤残", line):
                span = line.strip()[:80]
                anns.append(_make_rule_annotation(
                    "uncertain", "critical", line_no, span, 0, min(len(line), len(span)),
                    "报告同时出现伤残阴性和阳性结论。", "请核对伤残结论。", "boss_fact_consistency_scan",
                ))
    periods = _period_mentions(report_md)
    for key, values in periods.items():
        nums = {re.search(r"([0-9]{1,3})", span).group(1) for _, span in values if re.search(r"([0-9]{1,3})", span)}
        if len(nums) <= 1:
            continue
        for line_no, span in values:
            line = lines[line_no - 1]
            pos = line.find(span)
            anns.append(_make_rule_annotation(
                "uncertain", "warning", line_no, span, pos, pos + len(span),
                f"报告中{key}期限前后数值不一致。", "请核对三期结论和分析说明是否一致。", "boss_fact_consistency_scan",
            ))
    non_surgery = re.search(r"非手术治疗|未手术|没有做手术|未行手术|手术史[:：]?无", text)
    if non_surgery and "9.1.2" in text:
        for line_no, line in enumerate(lines, start=1):
            if "9.1.2" in line or re.search(r"非手术治疗|未手术|没有做手术|未行手术|手术史[:：]?无", line):
                span = "9.1.2" if "9.1.2" in line else line.strip()[:80]
                pos = line.find(span)
                anns.append(_make_rule_annotation(
                    "uncertain", "critical", line_no, span, max(pos, 0), max(pos, 0) + len(span),
                    "报告存在非手术事实与脊柱骨折手术治疗条款并存的风险。", "请核对三期条款是否应为9.1.1。", "boss_fact_consistency_scan",
                ))
    if re.search(r"手术治疗|内固定", text) and "9.1.1" in text:
        for line_no, line in enumerate(lines, start=1):
            if "9.1.1" in line:
                pos = line.find("9.1.1")
                anns.append(_make_rule_annotation(
                    "uncertain", "warning", line_no, "9.1.1", pos, pos + len("9.1.1"),
                    "报告存在手术事实与脊柱骨折非手术治疗条款并存的风险。", "请核对三期条款。", "boss_fact_consistency_scan",
                ))
    return anns


def _annotation_priority(annotation: dict) -> tuple[int, int]:
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    source = str(annotation.get("source") or "")
    category = annotation.get("category")
    if category == "missing":
        base = 0
    elif source in {"boss_fact_consistency_scan", "boss_identity_consistency_scan", "boss_address_audit_scan", "boss_record_consistency_scan"}:
        base = 1
    elif source in {"boss_ocr_artifact_scan", "boss_key_identifier_scan"}:
        base = 2
    elif category == "format":
        base = 3
    elif source == "boss_summary_default_audit":
        base = 4
    else:
        base = 5
    return base, severity_rank.get(str(annotation.get("severity") or "warning"), 1)


def _finalize_annotations(annotations: List[dict], limit: int = 60) -> List[dict]:
    seen = set()
    unique: List[dict] = []
    for ann in sorted(annotations, key=_annotation_priority):
        key = (ann.get("category"), ann.get("line_no"), ann.get("span_text"), ann.get("span_start_col"), ann.get("source"))
        if key in seen:
            continue
        seen.add(key)
        ann["id"] = f"ai_ann_{len(unique)+1:03d}"
        unique.append(ann)
        if len(unique) >= limit:
            break
    return unique


def _deterministic_preview_annotations(report_md: str) -> List[dict]:
    annotations: List[dict] = []
    summary_ann = _summary_section_annotation(report_md)
    if summary_ann:
        annotations.append(summary_ann)
    for line_no, line in enumerate(report_md.splitlines(), start=1):
        if not line.strip():
            continue
        annotations.extend(_scan_placeholder_annotations(line_no, line))
        annotations.extend(_scan_repetition_annotations(line_no, line))
        annotations.extend(_scan_ocr_artifact_annotations(line_no, line))
        annotations.extend(_scan_key_identifier_annotations(line_no, line))
    annotations.extend(_scan_address_annotations(report_md))
    annotations.extend(_scan_identity_consistency_annotations(report_md))
    annotations.extend(_scan_record_number_consistency_annotations(report_md))
    annotations.extend(_scan_similar_sentence_annotations(report_md))
    annotations.extend(_scan_report_fact_consistency(report_md))
    return _finalize_annotations(annotations, limit=60)


def _finalize_ai_preview_result(annotations: List[dict]) -> dict:
    unique = _finalize_annotations(annotations, limit=60)
    summary = {
        "total": len(unique),
        "format": sum(1 for ann in unique if ann.get("category") == "format"),
        "missing": sum(1 for ann in unique if ann.get("category") == "missing"),
        "uncertain": sum(1 for ann in unique if ann.get("category") == "uncertain"),
    }
    return {"schema_version": "forenagent_ai_preview_annotations_v1", "summary": summary, "annotations": unique}


_AI_PREVIEW_TRUE_VALUES = {"1", "true", "yes", "y", "on", "是", "启用"}
_AI_PREVIEW_FALSE_VALUES = {"0", "false", "no", "n", "off", "否", "禁用", "skip"}


def _ai_preview_llm_enabled(config: RuntimeConfig) -> Tuple[bool, str]:
    """最终预览审计默认先试 LLM；连接/调用失败时回退到规则标注。"""
    value = os.getenv("FORENAGENT_ENABLE_AI_PREVIEW_AUDIT", "").strip().lower()
    if value in _AI_PREVIEW_TRUE_VALUES:
        return True, ""
    if value in _AI_PREVIEW_FALSE_VALUES:
        return False, "环境变量 FORENAGENT_ENABLE_AI_PREVIEW_AUDIT 已关闭"
    return config.llm_provider != "off", "LLM provider 为 off" if config.llm_provider == "off" else ""


def _ai_preview_timeout(config: RuntimeConfig) -> int:
    # 本地模型只给一次短窗口，避免出具前审计长时间卡在 /chat/completions；失败后立即用规则结果继续。
    default_timeout = 30 if config.llm_provider == "local" else 120
    try:
        return max(5, int(os.getenv("FORENAGENT_AI_PREVIEW_AUDIT_TIMEOUT", str(default_timeout)) or default_timeout))
    except Exception:
        return default_timeout


def build_ai_preview_annotations(full_report: str, uncertainties: List[dict], log: Callable[[str], None], runtime_config: Optional[RuntimeConfig] = None) -> dict:
    """生成最终预览用 AI/规则辅助审计标注；不直接修改正式报告。"""
    config = runtime_config or get_runtime_config()
    audit_model = config.audit_llm_model if config.llm_provider == "local" else config.llm_model
    model_note = f"/{audit_model}" if config.llm_provider != "off" else ""
    log(f"[审查] 运行出具前 AI 辅助审计标注（LLM: {config.llm_provider}{model_note}）...")
    annotations = _deterministic_preview_annotations(full_report)
    llm_enabled, skip_reason = _ai_preview_llm_enabled(config)
    if not llm_enabled:
        log(f"[审查] AI辅助审计LLM跳过：{skip_reason}；保留规则审计标注。")
        return _finalize_ai_preview_result(annotations)

    uncertainties_text = json.dumps(uncertainties[:30], ensure_ascii=False, indent=2) if uncertainties else "无"
    deterministic_brief = json.dumps([
        {"line_no": a.get("line_no"), "span_text": a.get("span_text"), "category": a.get("category"), "source": a.get("source")}
        for a in annotations[:30]
    ], ensure_ascii=False)
    if config.llm_provider == "local":
        cap = min(7000, int(getattr(config, "local_llm_context_chars", 9000) or 9000))
        report_text = full_report if len(full_report) <= cap else full_report[:cap] + "\n...(报告过长，已按本地模型上下文截断)"
        prompt = append_local_prompt_if_needed(f"""你是一位司法鉴定意见书出具前本地质控助手。请只补充规则扫描没有标出的遗漏、格式、OCR伪影、语义重复或事实矛盾；不要修改报告，不要裁决伤残等级或三期。

规则扫描已标出的问题摘要：
{deterministic_brief}

已知存疑信息：
{uncertainties_text}

审查重点：
1. format_issues：标点重复、错别字、异常短语、上下文重复或高度相似、模板残留、OCR造成的语义不顺；span_text 必须尽量短。
2. missing_info：待补充、系统提示语、明显缺少且不能成文的关键位置；不要把每个“不详”都列为问题。
3. uncertain_points：被鉴定人姓名前后变化、身份证号冲突、住址混入就诊时间/科室/病案号/门诊号/检查号、同一来源编号矛盾、伤残阳性/阴性结论并存、三期数值冲突。
4. key_identifiers：只补充规则未标出的关键数字或号码，如日期、身份证号、病案号、住院号、门诊号、检查号、案号、报告号、委托号、车牌号、条款号、伤残等级、三期天数、椎体节段、压缩比例、ROM角度。不同医院的病案号/住院号可不同，不要简单当成冲突。
5. 对“住址”特别敏感：住址应是居住地址，若混入“预检时间、就诊时间、科室、病案号、门诊号、检查号、CT/MR”等，应标出。

输出要求：
1. 只输出 JSON object，不要 Markdown。
2. 每个问题必须给出准确 line_no 和最小 span_text；不知道行号就不要输出。
3. 最多补充 20 项，避免泛泛评论。
4. 不要重复规则扫描已经标出的同一位置。

JSON格式：
{{
  "format_issues": [{{"line_no": 1, "span_text": "最小问题片段", "message": "问题说明", "suggested_note": "处理建议"}}],
  "missing_info": [{{"line_no": 1, "span_text": "最小缺失片段", "message": "缺失说明", "suggested_note": "处理建议"}}],
  "uncertain_points": [{{"line_no": 1, "span_text": "最小存疑片段", "message": "存疑说明", "suggested_note": "处理建议"}}],
  "key_identifiers": [{{"line_no": 1, "span_text": "关键数字或号码", "message": "关键内容核对说明", "suggested_note": "请对照原始材料核对"}}]
}}

报告全文：
{report_text}""", config, "boss_preview")
    else:
        report_text = full_report if len(full_report) <= 10000 else full_report[:10000] + "\n...(报告过长，已截断)"
        prompt = f"""你是一位司法鉴定意见书出具前质控助手。请只指出需要司法人员在最终预览中查看的位置，不要修改报告，不要裁决伤残等级或三期。

审查范围：
1. format_issues：标点重复、明显错别字、语义不顺、异常用语、规范引用表述不自然；不要把不同章节编号格式不同列为问题。span_text 必须是最小问题片段：标点问题只返回问题标点（如“。。”），重复词只返回重复词/短语，不要返回整句。
2. missing_info：只标出“待补充”、系统提示语、明显缺少且无法成文的关键位置；不要事事要求完整，不要把每个“不详”都列为问题。
3. uncertain_points：明显逻辑矛盾、提取错误或需要人工复核之处。三、资料摘要默认需要司法人员核对，但不要因此把资料摘要每一句都列为缺失。

已知存疑信息：
{uncertainties_text}

请严格输出 JSON object，格式如下：
{{
  "format_issues": [{{"line_no": 1, "span_text": "原文短句", "message": "问题说明", "suggested_note": "处理建议"}}],
  "missing_info": [{{"line_no": 1, "span_text": "原文短句", "message": "缺失说明", "suggested_note": "红色括号提示文字"}}],
  "uncertain_points": [{{"line_no": 1, "span_text": "原文短句", "message": "存疑说明", "suggested_note": "处理建议"}}]
}}

报告全文：
{report_text}"""
    try:
        response = _call_llm(
            prompt,
            timeout=_ai_preview_timeout(config),
            runtime_config=config,
            model=audit_model,
            max_retries=0,
            retry_without_json_mode=False,
        )
        data = _parse_json_from_response(response) if response else {}
        if not response:
            log("[审查] AI辅助审计LLM无返回，使用规则审计标注继续。")
        elif not data:
            log(f"[审查] AI辅助审计标注解析失败，原始返回前200字：{response[:200]}；使用规则审计标注继续。")
        index = len(annotations) + 1
        key_aliases = {
            "format_issues": "格式规范性",
            "missing_info": "缺失信息",
            "uncertain_points": "存疑点",
            "key_identifiers": "关键数字和号码",
        }
        for key, category in [("format_issues", "format"), ("missing_info", "missing"), ("uncertain_points", "uncertain"), ("key_identifiers", "format")]:
            raw_items = data.get(key) or data.get(key_aliases[key]) or []
            for raw in (_as_review_items(raw_items) if not isinstance(raw_items, list) else raw_items):
                ann = _annotation_from_item(full_report, raw, category, index)
                if ann:
                    if key == "key_identifiers":
                        ann["source"] = "ai_preview_key_identifier"
                        ann["severity"] = ann.get("severity") or "info"
                    annotations.append(ann)
                    index += 1
    except Exception as e:
        log(f"[审查] AI辅助审计标注跳过：{e}；使用规则审计标注继续。")
    return _finalize_ai_preview_result(annotations)


def _format_review_report(data: dict) -> str:
    """将 LLM 审查 JSON 格式化为 Markdown 片段。"""
    if not isinstance(data, dict) or not data:
        return "## AI一致性审查\n\n（审查结果解析失败）\n"

    data = {
        "format_issues": data.get("format_issues") or data.get("格式规范性") or data.get("格式问题") or data.get("格式/文书质量问题"),
        "missing_info": data.get("missing_info") or data.get("缺失信息") or data.get("完整性"),
        "uncertain_points": data.get("uncertain_points") or data.get("存疑点") or data.get("疑点"),
        "overall_assessment": data.get("overall_assessment") or data.get("整体评估") or data.get("assessment"),
    }

    lines = ["## AI一致性审查", ""]

    for key, label in [
        ("format_issues", "格式规范性"),
        ("missing_info", "缺失信息"),
        ("uncertain_points", "存疑点"),
    ]:
        items = _as_review_items(data.get(key, []))
        lines.append(f"### {label}")
        if items:
            for item in items:
                lines.append(f"- {item}")
        else:
            lines.append("- 无问题")
        lines.append("")

    assessment_items = _as_review_items(data.get("overall_assessment", ""))
    if assessment_items:
        lines.append("### 整体评估")
        lines.append(f"- {assessment_items[0]}")
        lines.append("")

    return "\n".join(lines)


def _format_human_review_section(review: dict) -> str:
    """把 Ruler 人工补充请求格式化为 Boss 审计报告章节。"""
    lines = [
        "## 人工审查与事实补充清单",
        "",
        f"- 伤残结论状态：{review.get('conclusion_status') or '-'}｜{review.get('conclusion_label') or '-'}",
        f"- 待人工处理项：{review.get('summary', {}).get('request_count', 0)}",
        "- 原则：人工只补充检查事实和证据说明，不直接替 Ruler 填写伤残等级或三期结论；补充后应重新运行 Ruler。",
        "",
    ]
    requests = review.get("requests") or []
    if not requests:
        lines.append("当前 Ruler 未生成必须人工补充的结构化请求。")
        lines.append("")
    for req in requests:
        lines.extend([
            f"### {req.get('id')}｜{req.get('category')}",
            f"- 问题：{req.get('question')}",
            f"- 原因：{req.get('reason')}",
            f"- 来源：{req.get('source')}",
        ])
        if req.get("clause"):
            lines.append(f"- 相关条款：{req.get('clause')} {req.get('branch_id') or ''}".rstrip())
        targets = req.get("target_fields") or []
        if targets:
            lines.append("- 建议补充内容：")
            for target in targets:
                lines.append(f"  - {target.get('label') or '相关事实'}")
        else:
            lines.append("- 建议处理：补充文字说明和证据来源，必要时由专家直接审查。")
        lines.append("")
    lines.extend([
        "### 如何处理",
        "请在 GUI 的“审计前信息补充”窗口或“人工审计确认”窗口中填写事实、复核意见和证据来源；系统会回填结构化事实并重新运行 Ruler/Boss。",
        "本审计报告只保留司法人员可读的审查清单，不再展示机器可读的回填模板。",
        "",
    ])
    return "\n".join(lines)


def _format_audit_questions_section(audit_session: Optional[dict]) -> str:
    lines = ["## GUI人工审计问答记录", ""]
    if not audit_session:
        lines.append("本次未进入 GUI 问答式人工审计；如需人工补充事实，可在 GUI 中运行或使用 --human-overrides。")
        lines.append("")
        return "\n".join(lines)
    summary = audit_session.get("summary") or {}
    lines.extend([
        f"- 审计事项总数：{summary.get('total', 0)}",
        f"- 必须确认：{summary.get('required', 0)}；建议确认：{summary.get('recommended', 0)}；风险提示：{summary.get('risk', 0)}",
        f"- 已回答：{len(audit_session.get('answers') or [])}",
        "- 原则：司法人员确认/补充事实和理由；系统据结构化事实重新运行 Ruler/Boss，LLM 不直接裁决伤残等级或三期。",
        "",
    ])
    answers_by_id = {answer.get("question", {}).get("id") or answer.get("question_id"): answer for answer in audit_session.get("answers", []) or [] if isinstance(answer, dict)}
    for q in audit_session.get("questions", []) or []:
        qid = q.get("id", "")
        answer = answers_by_id.get(qid, {})
        lines.extend([
            f"### {qid}｜{q.get('group')}｜{q.get('category')}",
            f"- 优先级：{q.get('priority')}",
            f"- 问题：{q.get('question')}",
            f"- 原因：{q.get('reason')}",
        ])
        current_value = q.get("current_value")
        if isinstance(current_value, str) and current_value.strip():
            lines.append(f"- 当前识别内容：{current_value.strip()[:500]}")
        if q.get("report_context"):
            lines.append("- 报告上下文：")
            for ctx_line in str(q.get("report_context")).splitlines():
                lines.append(f"  - {ctx_line}")
        if q.get("uncertainty"):
            lines.append(f"- 不确定点/风险点：{q.get('uncertainty')}")
        if q.get("needs_confirmation"):
            lines.append(f"- 需要补充或确认：{q.get('needs_confirmation')}")
        if answer:
            lines.append(f"- 审计状态：{answer.get('status', 'answered')}")
            if answer.get("choice"):
                lines.append(f"- 选择：{answer.get('choice')}")
            field_values = answer.get("field_values") or {}
            field_value_texts = {str(value or "").strip() for value in field_values.values() if str(value or "").strip()}
            answer_text_value = str(answer.get("answer_text") or "").strip()
            if answer_text_value and answer_text_value not in field_value_texts:
                lines.append(f"- 补充说明：{answer_text_value}")
            if field_values:
                label_by_target = {
                    str(field.get("target") or ""): str(field.get("label") or field.get("target") or "补充内容")
                    for field in q.get("fields", []) or []
                    if isinstance(field, dict)
                }
                lines.append("- 补充内容：")
                for target, value in field_values.items():
                    text = str(value or "").strip()
                    if not text:
                        continue
                    label = label_by_target.get(str(target), str(target) or "补充内容")
                    lines.append(f"  - {label}：{text}")
            if answer.get("evidence_note"):
                lines.append(f"- 证据/理由：{answer.get('evidence_note')}")
        else:
            lines.append("- 审计状态：未回答/未处理")
        if q.get("risk_if_unresolved"):
            lines.append(f"- 未处理风险：{q.get('risk_if_unresolved')}")
        lines.append("")
    unresolved = audit_session.get("unresolved_risks") or []
    if unresolved:
        lines.append("### 未解决风险")
        for risk in unresolved:
            lines.append(f"- {risk}")
        lines.append("")
    return "\n".join(lines)


def _format_final_edit_section(final_edit: Optional[dict]) -> str:
    lines = ["## 最终预览编辑记录", ""]
    if not final_edit:
        lines.append("本次未记录最终预览人工编辑。")
        lines.append("")
        return "\n".join(lines)
    lines.append(f"- 是否人工编辑：{'是' if final_edit.get('edited') else '否'}")
    if final_edit.get("operator"):
        lines.append(f"- 操作人：{final_edit.get('operator')}")
    if final_edit.get("note"):
        lines.append(f"- 编辑说明：{final_edit.get('note')}")
    if final_edit.get("key_conclusion_changed"):
        lines.append("- 风险提示：用户标记最终预览涉及关键结论修改，需专家确认并留痕。")
    lines.append("")
    return "\n".join(lines)


def _format_uncertainties_section(uncertainties: List[dict]) -> str:
    lines = ["## 上游存疑点汇总", ""]
    if not uncertainties:
        lines.append("未收集到上游 agent 标记的额外存疑点。")
        lines.append("")
        return "\n".join(lines)
    for item in uncertainties:
        lines.append(f"- 【{item.get('source', '-')}】{item.get('text', '')}")
    lines.append("")
    return "\n".join(lines)


def _format_days_range(rng: Any) -> str:
    if isinstance(rng, list) and len(rng) >= 2:
        return f"{rng[0]}-{rng[1]}日"
    return "-"


def _format_period_selection_section(ruler_payload: Optional[dict], structuring_payload: Optional[dict] = None) -> str:
    if structuring_payload is not None and not _commission_requests_period(structuring_payload):
        return ""
    lines = ["## 三期评定区间与取值审计", ""]
    if not ruler_payload:
        lines.append("未读取到 Ruler 三期审计信息。")
        lines.append("")
        return "\n".join(lines)
    section5 = ruler_payload.get("section5", {}) or {}
    period = section5.get("period_result", {}) or {}
    audit = ruler_payload.get("audit", {}) or {}
    selection = audit.get("period_selection", {}) or {}
    if not period:
        lines.append("Ruler 未输出三期评定结果。")
        lines.append("")
        return "\n".join(lines)

    clause = period.get("clause") or "-"
    label = period.get("sub_label") or "-"
    ranges = period.get("ranges") or selection.get("selected_ranges") or {}
    selected_values = selection.get("selected_values") or {key: period.get(key) for key in ["误工", "护理", "营养"]}
    policy = period.get("selected_value_policy") or selection.get("selected_value_policy") or "-"
    lines.append(f"- 适用条款：{clause}｜{label}")
    lines.append("- 适用区间：" + "；".join([f"{key} {_format_days_range(ranges.get(key))}" for key in ["误工", "护理", "营养"]]))
    lines.append("- 本次取值：" + "；".join([f"{key} {selected_values.get(key, period.get(key, '-'))}日" for key in ["误工", "护理", "营养"]]))
    lines.append(f"- 取值策略：{policy}")
    reasons = period.get("selection_reasons") or selection.get("selection_reasons") or []
    if reasons:
        lines.append("- 取值理由：")
        for reason in reasons:
            lines.append(f"  - {reason}")
    else:
        lines.append("- 取值理由：未记录具体区间内取值理由，需人工结合恢复情况复核。")
    candidates = audit.get("period_interval_candidates") or []
    if candidates:
        lines.append("- 候选区间：")
        for item in candidates[:6]:
            item_ranges = item.get("ranges") or {}
            item_selected = item.get("selected") or {}
            lines.append(
                f"  - {item.get('clause', '-')}｜{item.get('label', '-')}："
                f"区间（误工{_format_days_range(item_ranges.get('误工'))}，护理{_format_days_range(item_ranges.get('护理'))}，营养{_format_days_range(item_ranges.get('营养'))}）；"
                f"取值（误工{item_selected.get('误工', '-')}日，护理{item_selected.get('护理', '-')}日，营养{item_selected.get('营养', '-')}日）。"
            )
    lines.append("- 人工复核重点：条款识别是否正确、手术/非手术事实是否正确、区间内取值是否符合实际治疗和恢复情况。")
    lines.append("")
    return "\n".join(lines)


def _format_human_change_summary_section(change_log: Optional[dict]) -> str:
    lines = ["## 人工审计修改摘要", ""]
    changes = (change_log or {}).get("changes") or []
    if not changes:
        lines.append("本次未记录到人工回填导致的结构化字段变化。")
        lines.append("")
        lines.extend([
            "```json",
            json.dumps({"schema_version": "forenagent_human_change_summary_v1", "change_count": 0, "changes": []}, ensure_ascii=False, indent=2),
            "```",
            "",
        ])
        return "\n".join(lines)

    summary = {
        "schema_version": "forenagent_human_change_summary_v1",
        "change_count": len(changes),
        "changes_by_agent": {},
        "changes": [],
    }
    lines.extend([
        f"- 修改字段数：{len(changes)}",
        "- 说明：以下只摘要人工审计/预审/补充事实导致的结构化字段变化；before/after 为写入 02/04 阶段文件前后的值，便于后续抽取分析。",
        "- “进正文”列说明该字段是否会被 06_boss_reviewed.md 采用；“仅留痕”表示只在本审计报告中记录，不改变报告正文。",
        "",
        "| 序号 | Agent/阶段 | 字段 | 修改前 | 修改后 | 进正文 | 来源 |",
        "| ---: | --- | --- | --- | --- | --- | --- |",
    ])
    for idx, change in enumerate(changes, start=1):
        agent = str(change.get("source_agent") or "未知")
        summary["changes_by_agent"][agent] = summary["changes_by_agent"].get(agent, 0) + 1
        target = str(change.get("target") or "")
        label = str(change.get("label") or target or "字段")
        source = str(change.get("source") or change.get("source_file") or "manual")
        before_text = _short_json_value(change.get("before"), 120).replace("|", "\\|")
        after_text = _short_json_value(change.get("after"), 120).replace("|", "\\|")
        reaches = change.get("reaches_report")
        reaches_text = "是" if reaches else "仅留痕"
        lines.append(f"| {idx} | {agent} | {label}<br>{target} | {before_text} | {after_text} | {reaches_text} | {source} |")
        summary["changes"].append({
            "index": idx,
            "source_agent": agent,
            "target_json": change.get("target_json") or "",
            "target": target,
            "label": label,
            "before": change.get("before"),
            "after": change.get("after"),
            "source": source,
            "evidence_note": change.get("evidence_note") or "",
            "reaches_report": bool(reaches) if reaches is not None else None,
        })
    lines.extend(["", "### 结构化修改摘要 JSON", "", "```json"])
    lines.append(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    lines.extend(["```", ""])
    return "\n".join(lines)


def _format_runtime_audit_report(runtime_metrics: Optional[dict], change_log: Optional[dict] = None) -> str:
    metrics = runtime_metrics or {}
    lines = [
        "# Boss 运行摘要",
        "",
        "本文件仅记录本案处理文件数和各阶段实际工作耗时；弹窗打开后的长时间等待不计入人工审计耗时。详细过程信息不进入公开输出。",
        "",
        f"- 处理文件数（1 鉴定材料）：{int(metrics.get('material_file_count') or 0)}",
        f"- 模型调用：{metrics.get('runtime_summary') or '不详'}",
        f"- 人工审计：{metrics.get('human_audit_status') or '未发生'}",
        f"- 工作总耗时：{_format_elapsed(metrics.get('total_elapsed_seconds'))}",
        f"- 墙钟总耗时（含等待）：{_format_elapsed(metrics.get('wall_elapsed_seconds'))}",
        "",
        "## 阶段实际工作耗时",
        "",
        "| 阶段 | 耗时 | 备注 |",
        "| --- | ---: | --- |",
    ]
    for item in metrics.get("stages") or []:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"| {item.get('name', '不详')} | {_format_elapsed(item.get('elapsed_seconds'))} | {item.get('note', '')} |"
        )
    lines.append("")
    lines.append(_format_human_change_summary_section(change_log))
    return "\n".join(lines).strip() + "\n"


def build_boss_audit_report(
    human_review: dict,
    uncertainties: List[dict],
    llm_review_md: str,
    ruler_payload: Optional[dict] = None,
    audit_session: Optional[dict] = None,
    final_edit: Optional[dict] = None,
    structuring_payload: Optional[dict] = None,
    runtime_metrics: Optional[dict] = None,
    change_log: Optional[dict] = None,
) -> str:
    """统一生成 Boss 审计报告；正式报告不混入内部审计信息。"""
    sections = [
        _format_runtime_audit_report(runtime_metrics, change_log=change_log).strip(),
        _format_human_review_section(human_review or {}).strip(),
        _format_period_selection_section(ruler_payload, structuring_payload).strip(),
        _format_audit_questions_section(audit_session).strip(),
        _format_uncertainties_section(uncertainties or []).strip(),
        _format_final_edit_section(final_edit).strip(),
    ]
    if llm_review_md and str(llm_review_md).strip():
        sections.append("## LLM审查意见\n\n" + str(llm_review_md).strip())
    return "\n\n".join(section for section in sections if section).strip() + "\n"


# ============================================================
# 模块7：Pipeline主流程
# ============================================================

EMPTY_EXTRACTION_MARKER = "未提取到任何文本内容"


def _assert_materials_readable(extracted_md: str, materials_folder: str) -> None:
    """材料没能读出来时中止本案，不要继续往下生成报告。

    若放任继续，Structuring/Judge/Ruler 会在空输入上照常推理，最终出具一份
    结构完整、措辞正常、但没有任何材料依据的鉴定意见书。这种文书在真实场景里
    与正常输出的外观几乎一致，比直接报错危险得多，因此这里选择硬停。
    """
    markdown = extracted_md or ""
    # run_extractor 返回的是 01_extractor.md 的路径；这里按需读取实际内容。
    if os.path.isfile(markdown):
        try:
            with open(markdown, "r", encoding="utf-8", errors="ignore") as handle:
                markdown = handle.read()
        except OSError:
            markdown = ""
    reason = ""
    if EMPTY_EXTRACTION_MARKER in markdown:
        reason = "OCR 未提取到任何文本"
    elif re.search(r"-\s*成功：0\s*$", markdown, flags=re.MULTILINE):
        reason = "所有送检文件均提取失败"
    else:
        body = markdown.split("## OCR结果", 1)[-1]
        if len(re.sub(r"\s+", "", body)) < 80:
            reason = "OCR 提取到的正文过短，不足以支撑鉴定分析"
    if not reason:
        return
    raise RuntimeError(
        f"未能从「{materials_folder}」读取到可用材料（{reason}），已中止本案生成。"
        "请确认材料目录是否正确、PDF/图片是否加密或损坏，处理后重新运行。"
    )


def run_pipeline(
    transfer_folder: Optional[str] = None,
    check_folder: Optional[str] = None,
    mode: str = "hybrid",
    output_dir: Optional[str] = None,
    report_date: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
    case_folder: Optional[str] = None,
    skip_visual: bool = False,
    materials_folder: Optional[str] = None,
    exam_doc_folder: Optional[str] = None,
    photo_folder: Optional[str] = None,
    reference_report_folder: Optional[str] = None,
    batch_label: Optional[str] = None,
    human_overrides_path: Optional[str] = None,
    ignore_saved_overrides: bool = False,
    visual_review_callback: Optional[Callable[[dict, str], Optional[dict]]] = None,
    pre_audit_supplement_callback: Optional[Callable[[dict, str], Optional[dict]]] = None,
    audit_callback: Optional[Callable[[dict, str], Optional[dict]]] = None,
    preview_callback: Optional[Callable[[str, str, Optional[dict]], Tuple[str, dict]]] = None,
    runtime_config: Optional[RuntimeConfig] = None,
) -> str:
    """运行完整流水线，返回最终报告路径。"""
    if log is None:
        log = print
    config = runtime_config or get_runtime_config()
    apply_runtime_env(config)
    if (config.llm_provider == "cloud" or config.ocr_provider == "cloud") and not config.allow_cloud:
        config.require_cloud_allowed("使用云端模型验证")

    log("=" * 50)
    log("ForenAgent 1.0 — 鉴定意见书生成流水线")
    log(f"[模型调用] {config.summary()}")
    log("=" * 50)

    output_base_folder = None
    if case_folder:
        log(f"案件根目录: {case_folder}")
        channels = resolve_case_channels(case_folder)
        materials_folder = materials_folder or channels.get("materials_folder")
        exam_doc_folder = exam_doc_folder or channels.get("exam_doc_folder")
        photo_folder = photo_folder or channels.get("photo_folder")
        reference_report_folder = reference_report_folder or channels.get("reference_report_folder")
        output_base_folder = channels.get("output_base_folder")
        log(f"  案件编号: {channels.get('case_number') or '未识别'}")
        log(f"  鉴定材料目录: {materials_folder}")
        log(f"  检查记录目录: {exam_doc_folder or '未发现，Judge 将使用结构化摘要/视觉结果'}")
        log(f"  检查照片目录: {photo_folder or '未发现'}")
        log(f"  专家意见书目录: {reference_report_folder or '未发现；仅评价用，不进入生成'}")
        log(f"  AI输出基目录: {output_base_folder}")
        log(f"  AI输出编号目录: {channels.get('output_folder')}")
    else:
        materials_folder = materials_folder or transfer_folder
        photo_folder = photo_folder or check_folder
        if materials_folder and not transfer_folder:
            # 直接选文件夹模式（GUI 第 1/2 步）：材料夹即案件材料，
            # 检查记录取其中按关键词命中的子目录，而非把整夹都当检查记录。
            direct = resolve_direct_channels(materials_folder, photo_folder)
            exam_doc_folder = exam_doc_folder or direct.get("exam_doc_folder")
            reference_report_folder = reference_report_folder or direct.get("reference_report_folder")
            log(f"案件材料目录: {materials_folder}")
            log(f"  检查记录子目录: {exam_doc_folder or '未发现，Judge 将使用结构化摘要/视觉结果'}")
            log(f"  检查照片目录: {photo_folder or '未选择，跳过视觉'}")
            if reference_report_folder:
                log(f"  专家意见书子目录: {reference_report_folder}（仅评价用，不进入生成）")

    if not materials_folder:
        raise ValueError("请提供 case_folder，或提供 materials_folder/transfer_folder。")

    # 确定输出目录。
    # - case_folder（旧 mode）：database 编号案件写入 test_result/<编号>/；
    # - 直接选文件夹（GUI 两步上传）：交付目录 <材料文件夹>/输出/，过程文件收进 输出/_work/；
    # - 显式 --output-dir：沿用该目录，交付物也写在那里。
    deliverable_dir: Optional[str] = None
    if case_folder:
        out = get_case_output_dir(case_folder, output_dir, batch_label=batch_label)
    elif output_dir:
        out = get_output_dir(materials_folder, output_dir, batch_label=batch_label)
        deliverable_dir = out
    else:
        deliverable_dir = resolve_deliverable_dir(materials_folder)
        # 过程文件与交付物同写在交付目录下，收尾时由 _consolidate_work_files
        # 统一归入 输出/_work/。这样各阶段照常按 <输出目录>/_work/... 写文件，
        # 不会多出一层嵌套的 _work。
        out = deliverable_dir
    log(f"输出目录: {out}")

    if not report_date:
        from datetime import date
        today = date.today()
        report_date = f"{today.year}年{today.month}月{today.day}日"

    protected_output_names = set(os.listdir(out)) if os.path.isdir(out) else set()
    pipeline_started = time.perf_counter()
    stage_metrics: List[Dict[str, Any]] = []
    human_audit_elapsed = 0.0
    human_audit_occurred = False
    material_file_count = _count_material_files(materials_folder)

    def record_stage(name: str, started: Optional[float] = None, note: str = "", elapsed_seconds: Optional[float] = None) -> None:
        if elapsed_seconds is None:
            elapsed_seconds = time.perf_counter() - started if started is not None else 0.0
        stage_metrics.append({"name": name, "elapsed_seconds": max(0.0, float(elapsed_seconds or 0.0)), "note": note})

    try:
        # Step 1: Extractor
        stage_started = time.perf_counter()
        extracted_md = run_extractor(materials_folder, out, log, runtime_config=config)
        record_stage("OCR/Extractor", stage_started, f"OCR={config.ocr_provider}")
        _assert_materials_readable(extracted_md, materials_folder)

        # Step 2: Structuring
        stage_started = time.perf_counter()
        structuring_json, structuring_md = run_structuring(extracted_md, mode, out, log, runtime_config=config)
        record_stage("Structuring/规则抽取", stage_started, f"mode={mode}, LLM={config.llm_provider}")

        # Optional Visual Step: ROM image reading
        stage_started = time.perf_counter()
        visual_json = run_visual(photo_folder, out, log, skip_visual=skip_visual)
        record_stage("Visual", stage_started, "skip_visual=true" if skip_visual else "")
        visual_pre_review_overrides: Optional[dict] = None
        if visual_review_callback and visual_json and os.path.isfile(visual_json):
            visual_payload = _read_json(visual_json)
            visual_session = build_visual_pre_review_session(
                structuring_payload=_read_json(structuring_json),
                visual_payload=visual_payload,
            )
            if visual_session.get("items"):
                log(f"[视觉预审] 检出 {len(visual_session.get('items') or [])} 张量角器/疑似量角器图片，进入生成前预审。")
                review_started = time.perf_counter()
                answered_visual_session = visual_review_callback(visual_session, out)
                review_wall = time.perf_counter() - review_started
                record_stage("视觉预审", elapsed_seconds=_callback_active_elapsed(answered_visual_session, review_wall) or 0.0, note="人工确认量角器图片，等待不计")
                if answered_visual_session:
                    visual_session = answered_visual_session
                    visual_session_path = os.path.join(out, STAGE_WORK_DIR, VISUAL_PRE_REVIEW_SESSION_JSON)
                    os.makedirs(os.path.dirname(visual_session_path), exist_ok=True)
                    _write_json(visual_session_path, visual_session)
                    updated_visual_payload = apply_visual_pre_review_to_visual_payload(visual_payload, visual_session)
                    _write_json(visual_json, updated_visual_payload)
                    visual_pre_review_overrides = visual_pre_review_to_overrides(visual_session)
                    adopted_count = len(visual_pre_review_overrides.get("fact_overrides") or []) // 4
                    log(f"[视觉预审] 已采纳 {adopted_count} 张图片的活动度事实，Judge 将读取确认后的 03_visual.json。")
            else:
                log("[视觉预审] 未检出需逐图确认的量角器候选图片，跳过预审窗口。")

        # Step 3: Judge
        stage_started = time.perf_counter()
        judge_json, judge_md = run_judge(
            structuring_md=structuring_md,
            output_dir=out,
            report_date=report_date,
            log=log,
            visual_json=visual_json,
            exam_doc_folder=exam_doc_folder,
            runtime_config=config,
        )
        record_stage("Judge", stage_started, "")

        if visual_pre_review_overrides and visual_pre_review_overrides.get("fact_overrides"):
            overrides_path = os.path.join(out, STAGE_WORK_DIR, VISUAL_PRE_REVIEW_OVERRIDES_JSON)
            os.makedirs(os.path.dirname(overrides_path), exist_ok=True)
            _write_json(overrides_path, visual_pre_review_overrides)
            log(f"[视觉预审] 已生成 {len(visual_pre_review_overrides['fact_overrides'])} 个 ROM 事实回填项，Ruler 将使用采纳图片。")
            structuring_json, judge_json, _applied_path = apply_human_overrides(
                structuring_json=structuring_json,
                judge_json=judge_json,
                overrides_path=overrides_path,
                output_dir=out,
                log=log,
            )
            persist_human_overrides(out, visual_pre_review_overrides)

        # 重跑时自动回放本案累计的人工回填（07_human_overrides.json）。
        # 没有这一步，鉴定人在 GUI 里做的审计会随着 02/04 阶段文件被重建而静默消失。
        saved_overrides_path = None if ignore_saved_overrides else load_persisted_human_overrides(out)
        if saved_overrides_path:
            log(f"[人工补充] 检出本案累计人工回填，自动重放: {saved_overrides_path}")
            structuring_json, judge_json, _applied_path = apply_human_overrides(
                structuring_json=structuring_json,
                judge_json=judge_json,
                overrides_path=saved_overrides_path,
                output_dir=out,
                log=log,
            )

        # Optional human-in-the-loop: 回填外部人工补充事实后再交给 Ruler 裁决
        if human_overrides_path:
            log(f"[人工补充] 读取人工事实补充: {human_overrides_path}")
            structuring_json, judge_json, _applied_path = apply_human_overrides(
                structuring_json=structuring_json,
                judge_json=judge_json,
                overrides_path=human_overrides_path,
                output_dir=out,
                log=log,
            )
            persist_human_overrides(out, _read_json(human_overrides_path))

        if pre_audit_supplement_callback:
            supplement_session = build_pre_audit_supplement_session(
                structuring_payload=_read_json(structuring_json),
                visual_payload=_read_json(visual_json) if visual_json and os.path.isfile(visual_json) else {},
                judge_payload=_read_json(judge_json),
            )
            supplement_started = time.perf_counter()
            answered_supplement = pre_audit_supplement_callback(supplement_session, out)
            supplement_wall = time.perf_counter() - supplement_started
            record_stage(
                "审计前补充",
                elapsed_seconds=_callback_active_elapsed(answered_supplement, supplement_wall) or 0.0,
                note="人工补充事实，等待不计" if answered_supplement else "未补充",
            )
            if answered_supplement:
                supplement_overrides = pre_audit_supplement_to_overrides(answered_supplement)
                if supplement_overrides.get("fact_overrides"):
                    overrides_path = os.path.join(out, STAGE_WORK_DIR, "pre_audit_supplement_overrides.json")
                    os.makedirs(os.path.dirname(overrides_path), exist_ok=True)
                    _write_json(overrides_path, supplement_overrides)
                    log(f"[审计前补充] 已回填 {len(supplement_overrides['fact_overrides'])} 个体格检查/辅助检查字段。")
                    structuring_json, judge_json, _applied_path = apply_human_overrides(
                        structuring_json=structuring_json,
                        judge_json=judge_json,
                        overrides_path=overrides_path,
                        output_dir=out,
                        log=log,
                    )
                    persist_human_overrides(out, supplement_overrides)

        # Step 4: Ruler（审计前）
        stage_started = time.perf_counter()
        ruler_json, ruler_md = run_ruler(structuring_json, judge_json, out, log)
        human_review, _human_review_json, _human_draft_json = write_human_review_files(ruler_json, out, log)
        record_stage("Ruler 初裁", stage_started, "")

        # Step 5: Boss 审计前草稿
        log("[组装] 渲染审计前报告草稿...")
        stage_started = time.perf_counter()
        draft_md = assemble_report(
            structuring_md,
            judge_md,
            ruler_md,
            structuring_json_path=structuring_json,
            judge_json_path=judge_json,
            ruler_json_path=ruler_json,
            report_date=report_date,
        )
        draft_md = normalize_format(draft_md)
        draft_path = os.path.join(out, FINAL_DRAFT_MD)
        with open(draft_path, "w", encoding="utf-8") as f:
            f.write(draft_md)
        record_stage("审计前草稿组装", stage_started, "")

        # Step 6: GUI 问答式人工审计；回答会回填事实并重跑 Ruler/Boss
        ruler_payload = _read_json(ruler_json)
        audit_session = build_audit_questions(
            structuring_payload=_read_json(structuring_json),
            visual_payload=_read_json(visual_json) if visual_json and os.path.isfile(visual_json) else {},
            judge_payload=_read_json(judge_json),
            ruler_payload=ruler_payload,
            draft_report=draft_md,
            human_review=human_review,
            report_date=report_date,
            include_repeated_validation=not bool(visual_review_callback),
        )
        if audit_callback:
            human_audit_occurred = True
            audit_started = time.perf_counter()
            answered_session = audit_callback(audit_session, out)
            audit_wall = time.perf_counter() - audit_started
            active_audit_elapsed = _callback_active_elapsed(answered_session, audit_wall) or 0.0
            human_audit_elapsed += active_audit_elapsed
            record_stage("人工审计", elapsed_seconds=active_audit_elapsed, note="GUI 问答，等待不计")
            if answered_session:
                audit_session = answered_session
                answers_path = os.path.join(out, STAGE_WORK_DIR, HUMAN_AUDIT_ANSWERS_JSON)
                os.makedirs(os.path.dirname(answers_path), exist_ok=True)
                _write_json(answers_path, audit_session)
                generated_overrides = audit_answers_to_overrides(audit_session)
                if generated_overrides.get("fact_overrides"):
                    overrides_path = os.path.join(out, STAGE_WORK_DIR, "gui_human_overrides.json")
                    _write_json(overrides_path, generated_overrides)
                    log(f"[人工审计] 已生成 {len(generated_overrides['fact_overrides'])} 个事实回填项，重新运行 Ruler/Boss。")
                    structuring_json, judge_json, _applied_path = apply_human_overrides(
                        structuring_json=structuring_json,
                        judge_json=judge_json,
                        overrides_path=overrides_path,
                        output_dir=out,
                        log=log,
                    )
                    persist_human_overrides(out, generated_overrides)
                    rerun_started = time.perf_counter()
                    ruler_json, ruler_md = run_ruler(structuring_json, judge_json, out, log)
                    ruler_payload = _read_json(ruler_json)
                    human_review, _human_review_json, _human_draft_json = write_human_review_files(ruler_json, out, log)
                    record_stage("Ruler 重跑", rerun_started, "人工审计后")
        else:
            log("[人工审计] 当前为无交互模式，审计问题将写入审计报告，不阻断生成 reviewed 报告。")

        # Step 7: 审计后报告
        log("[组装] 渲染审计后报告...")
        stage_started = time.perf_counter()
        reviewed_md = assemble_report(
            structuring_md,
            judge_md,
            ruler_md,
            structuring_json_path=structuring_json,
            judge_json_path=judge_json,
            ruler_json_path=ruler_json,
            report_date=report_date,
        )
        reviewed_md = normalize_format(apply_audit_answers_to_report(reviewed_md, audit_session))
        consistency_warnings = validate_report_consistency(
            structuring=_read_json(structuring_json),
            judge=_read_json(judge_json),
            ruler=_read_json(ruler_json),
            report_text=reviewed_md,
        )
        consistency_path = os.path.join(out, STAGE_WORK_DIR, "final_consistency_gate.json")
        os.makedirs(os.path.dirname(consistency_path), exist_ok=True)
        _write_json(consistency_path, {
            "schema_version": "forenagent_final_consistency_gate_v1",
            "warning_count": len(consistency_warnings),
            "warnings": consistency_warnings,
        })
        if consistency_warnings:
            log(f"[一致性检查] 检出 {len(consistency_warnings)} 个最终报告一致性/占位风险，已写入审计报告。")
        uncertainties = collect_uncertainties(out) + collect_report_placeholders(reviewed_md) + consistency_warnings
        ai_preview_annotations = build_ai_preview_annotations(reviewed_md, uncertainties, log, runtime_config=config)
        if audit_session is None:
            audit_session = {}
        audit_session["ai_preview_annotations"] = ai_preview_annotations
        annotations_path = os.path.join(out, STAGE_WORK_DIR, "ai_preview_annotations.json")
        os.makedirs(os.path.dirname(annotations_path), exist_ok=True)
        _write_json(annotations_path, ai_preview_annotations)
        record_stage("Reviewed 报告与AI预览生成", stage_started, "含最终高光审计")

        final_edit = {"edited": False}
        if preview_callback:
            preview_started = time.perf_counter()
            preview_result = preview_callback(reviewed_md, out, audit_session)
            preview_wall = time.perf_counter() - preview_started
            record_stage(
                "最终预览人工小修",
                elapsed_seconds=_callback_active_elapsed(preview_result, preview_wall) or 0.0,
                note="人工预览/小修，等待不计",
            )
            if preview_result:
                reviewed_md, final_edit = preview_result
                reviewed_md = normalize_format(reviewed_md)
        write_started = time.perf_counter()
        reviewed_path = os.path.join(out, FINAL_REVIEWED_MD)
        with open(reviewed_path, "w", encoding="utf-8") as f:
            f.write(reviewed_md)
        record_stage("Reviewed 报告写入", write_started, "")

        # Step 8: Boss 审计报告（运行摘要 + 人工修改摘要 + 最终一致性风险）
        audit_started = time.perf_counter()
        uncertainties = collect_uncertainties(out) + collect_report_placeholders(reviewed_md) + consistency_warnings
        audit_path = os.path.join(out, FINAL_AUDIT_MD)
        change_log_path = os.path.join(out, STAGE_WORK_DIR, HUMAN_CHANGE_LOG_JSON)
        change_log_payload = _read_json(change_log_path) if os.path.isfile(change_log_path) else {}
        runtime_stages = stage_metrics + [{"name": "Audit 文件生成", "elapsed_seconds": 0.0, "note": "运行摘要+人工修改摘要"}]
        runtime_metrics = {
            "material_file_count": material_file_count,
            "runtime_summary": config.summary(),
            "human_audit_status": f"已发生（实际操作 {_format_elapsed(human_audit_elapsed)}，等待不计）" if human_audit_occurred else "未发生（无交互模式）",
            "wall_elapsed_seconds": time.perf_counter() - pipeline_started,
            "total_elapsed_seconds": _sum_stage_elapsed(runtime_stages),
            "stages": runtime_stages,
        }
        audit_md = build_boss_audit_report(
            human_review,
            uncertainties,
            "",
            ruler_payload=ruler_payload,
            audit_session=audit_session,
            final_edit=final_edit,
            structuring_payload=_read_json(structuring_json),
            runtime_metrics=runtime_metrics,
            change_log=change_log_payload,
        )
        with open(audit_path, "w", encoding="utf-8") as f:
            f.write(audit_md)
        runtime_metrics["stages"][-1]["elapsed_seconds"] = time.perf_counter() - audit_started
        runtime_metrics["wall_elapsed_seconds"] = time.perf_counter() - pipeline_started
        runtime_metrics["total_elapsed_seconds"] = _sum_stage_elapsed(runtime_metrics["stages"])
        audit_md = build_boss_audit_report(
            human_review,
            uncertainties,
            "",
            ruler_payload=ruler_payload,
            audit_session=audit_session,
            final_edit=final_edit,
            structuring_payload=_read_json(structuring_json),
            runtime_metrics=runtime_metrics,
            change_log=change_log_payload,
        )
        with open(audit_path, "w", encoding="utf-8") as f:
            f.write(audit_md)

        # 交付模式：产出鉴定人实际要用的三个文件，过程文件留在 输出/_work/ 供重跑与回填重放。
        if deliverable_dir:
            deliverable_paths = write_deliverables(deliverable_dir, reviewed_md, audit_md, log=log)
            log("=" * 50)
            log(f"完成！交付目录: {deliverable_dir}")
            log(f"  鉴定意见书(Word): {deliverable_paths.get('docx') or '（生成失败，见上方告警）'}")
            log(f"  鉴定意见书(Markdown): {deliverable_paths['report_md']}")
            log(f"  AI审计报告: {deliverable_paths['audit_md']}")
            log("=" * 50)
            return deliverable_paths["report_md"]

        # 成功后只保留公开 06 文件和必要阶段 JSON；过程 work/overlay 目录不进入 demo 输出。
        _delete_files(structuring_md, judge_md, ruler_md)
        _cleanup_transient_output_artifacts(out)
        _cleanup_public_output_dir(out, protected_output_names)

        log("=" * 50)
        log(f"完成！审计前草稿: {draft_path}")
        log(f"完成！审计后报告: {reviewed_path}")
        log(f"完成！审计报告: {audit_path}")
        log("=" * 50)

        return reviewed_path

    except Exception as e:
        log(f"[ERROR] 流水线异常: {e}")
        raise


def run_case_batch(
    case_folders: List[str],
    mode: str = "hybrid",
    report_date: Optional[str] = None,
    skip_visual: bool = False,
    log: Optional[Callable[[str], None]] = None,
    batch_label: Optional[str] = None,
    human_overrides_path: Optional[str] = None,
    ignore_saved_overrides: bool = False,
    visual_review_callback: Optional[Callable[[dict, str], Optional[dict]]] = None,
    pre_audit_supplement_callback: Optional[Callable[[dict, str], Optional[dict]]] = None,
    audit_callback: Optional[Callable[[dict, str], Optional[dict]]] = None,
    preview_callback: Optional[Callable[[str, str, Optional[dict]], Tuple[str, dict]]] = None,
    runtime_config: Optional[RuntimeConfig] = None,
    materials_folder: Optional[str] = None,
    photo_folder: Optional[str] = None,
) -> List[Dict[str, str]]:
    """按顺序运行所选案件。

    两种输入形态：
    - 传入案件根目录（database/001 这类）：沿用旧的通道识别，输出到 test_result/<编号>/；
    - 传入 materials_folder（GUI 两步上传的"案件材料文件夹"）：直接以该目录为材料，
      交付物写进 <材料文件夹>/输出/。
    """
    if log is None:
        log = print
    config = runtime_config or get_runtime_config()
    apply_runtime_env(config)
    if not batch_label:
        batch_label = time.strftime("batch_%Y%m%d_%H%M%S")

    results: List[Dict[str, str]] = []
    total = len(case_folders)
    for index, case in enumerate(case_folders, start=1):
        log("\n" + "#" * 60)
        log(f"[批量 {index}/{total}] {case}")
        try:
            # materials_folder 显式给出时走"直接选文件夹"模式，不再把 case 当案件根目录解析。
            pipeline_kwargs: Dict[str, Any] = dict(
                mode=mode,
                report_date=report_date,
                skip_visual=skip_visual,
                log=log,
                batch_label=batch_label,
                human_overrides_path=human_overrides_path,
                ignore_saved_overrides=ignore_saved_overrides,
                visual_review_callback=visual_review_callback,
                pre_audit_supplement_callback=pre_audit_supplement_callback,
                audit_callback=audit_callback,
                preview_callback=preview_callback,
                runtime_config=config,
            )
            if materials_folder:
                pipeline_kwargs["materials_folder"] = materials_folder
                pipeline_kwargs["photo_folder"] = photo_folder
            else:
                pipeline_kwargs["case_folder"] = case
            final_path = run_pipeline(**pipeline_kwargs)
            results.append({"case_folder": case, "status": "success", "final_report": final_path, "error": ""})
        except Exception as exc:
            log(f"[批量 {index}/{total}] 失败: {exc}")
            results.append({"case_folder": case, "status": "failed", "final_report": "", "error": str(exc)})

    log("\n" + "=" * 50)
    log(f"批量运行完成: 成功 {sum(1 for r in results if r['status'] == 'success')}/{total}")
    log("=" * 50)
    return results


# ============================================================
# 模块8：GUI界面
# ============================================================

def _fit_window_to_screen(
    window,
    width: int,
    height: int,
    min_width: int = 0,
    min_height: int = 0,
) -> None:
    """把窗口收进屏幕可视范围并居中。

    1280×860 这类尺寸在 1366×768 的笔记本上会超出屏幕，底部按钮点不到。
    这里统一按屏幕尺寸裁剪，并把 minsize 一并收进可用范围，避免用户拖不小。
    """
    try:
        screen_w = window.winfo_screenwidth()
        screen_h = window.winfo_screenheight()
    except Exception:
        window.geometry(f"{width}x{height}")
        return
    # 留出任务栏与窗口边框余量
    max_w = max(640, screen_w - 80)
    max_h = max(480, screen_h - 140)
    actual_w = min(width, max_w)
    actual_h = min(height, max_h)
    x = max(0, (screen_w - actual_w) // 2)
    y = max(0, (screen_h - actual_h) // 3)
    window.geometry(f"{actual_w}x{actual_h}+{x}+{y}")
    if min_width or min_height:
        window.minsize(min(min_width, actual_w), min(min_height, actual_h))


def _apply_ui_style(root) -> None:
    """统一 ttk 控件字体与按钮层级，让界面清爽一些。"""
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except Exception:
        pass
    base_font = ("Microsoft YaHei UI", 10)
    root.option_add("*Font", base_font)
    for name in ("TLabel", "TButton", "TEntry", "TLabelframe.Label", "TRadiobutton", "TCheckbutton"):
        try:
            style.configure(name, font=base_font)
        except Exception:
            pass
    try:
        style.configure("TLabelframe.Label", foreground="#1a3d6d")
        # 主操作按钮加重，一眼能看到要点哪个
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(14, 6))
        style.configure("Hint.TLabel", foreground="#6b7280")
    except Exception:
        pass


class BossAgentGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ForenAgent 1.0 — 鉴定意见书生成")
        _apply_ui_style(self.root)
        _fit_window_to_screen(self.root, 900, 720, min_width=760, min_height=560)
        self.root.resizable(True, True)

        # 输入改为两步选文件夹：① 案件材料（必选）② 检查照片（可选）。
        self.materials_var = tk.StringVar(value="")
        self.photo_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="hybrid")
        self.skip_visual_var = tk.BooleanVar(value=False)
        self.human_overrides_var = tk.StringVar(value="")
        initial_config = get_runtime_config()
        self.llm_provider_var = tk.StringVar(value=initial_config.llm_provider)
        self.ocr_provider_var = tk.StringVar(value=initial_config.ocr_provider)
        self.local_llm_base_url_var = tk.StringVar(value=initial_config.local_llm_base_url)
        self.local_chat_model_var = tk.StringVar(value=initial_config.local_chat_model)
        self.local_audit_model_var = tk.StringVar(value=initial_config.local_audit_model)
        self.local_llm_device_var = tk.StringVar(value=initial_config.local_llm_device)
        self.cloud_chat_model_var = tk.StringVar(value=initial_config.cloud_chat_model)
        self.allow_cloud_var = tk.BooleanVar(value=False)
        self.ocr_engine_var = tk.StringVar(value=initial_config.local_ocr_engine)
        self.ocr_device_var = tk.StringVar(value=initial_config.local_ocr_device)
        self.ocr_render_scale_var = tk.StringVar(value=str(initial_config.local_ocr_render_scale))
        self.ocr_second_pass_var = tk.BooleanVar(value=initial_config.local_ocr_second_pass)
        self.ocr_second_pass_scale_var = tk.StringVar(value=str(initial_config.local_ocr_second_pass_scale))
        self.running = False
        self.last_output_dir: Optional[str] = None
        self.last_review_path: Optional[str] = None
        self.batch_label: Optional[str] = None

        self._build_ui()
        self.root.after(200, self._prompt_initial_materials)

    def _build_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=6, pady=6)
        tab_run = ttk.Frame(notebook)
        tab_model = ttk.Frame(notebook)
        notebook.add(tab_run, text="案件运行")
        notebook.add(tab_model, text="模型调用")

        # --- 输入区（两步）---
        frame_input = ttk.LabelFrame(tab_run, text="案件输入", padding=10)
        frame_input.pack(fill="x", padx=10, pady=(6, 4))

        row1 = ttk.Frame(frame_input)
        row1.pack(fill="x")
        ttk.Label(row1, text="第 1 步", width=7).pack(side="left")
        ttk.Button(row1, text="选择案件材料文件夹", command=self._browse_materials).pack(side="left")
        ttk.Entry(row1, textvariable=self.materials_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Label(row1, text="（必选）", style="Hint.TLabel").pack(side="left")

        row2 = ttk.Frame(frame_input)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="第 2 步", width=7).pack(side="left")
        ttk.Button(row2, text="选择检查照片文件夹", command=self._browse_photos).pack(side="left")
        ttk.Entry(row2, textvariable=self.photo_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Label(row2, text="（可选，没有可不选）", style="Hint.TLabel").pack(side="left")

        row3 = ttk.Frame(frame_input)
        row3.pack(fill="x", pady=(8, 0))
        ttk.Button(row3, text="清空", command=self._clear_inputs).pack(side="left")
        self.input_status_label = ttk.Label(row3, text="尚未选择案件材料文件夹")
        self.input_status_label.pack(side="left", padx=10)

        # --- 模式选择 ---
        frame_mode = ttk.Frame(tab_run, padding=5)
        frame_mode.pack(fill="x", padx=10)
        ttk.Label(frame_mode, text="提取模式:").pack(side="left")
        for m, label in [("hybrid", "混合(hybrid)"), ("rule", "纯规则(rule)"), ("llm", "纯LLM(llm)")]:
            ttk.Radiobutton(frame_mode, text=label, variable=self.mode_var, value=m).pack(side="left", padx=5)
        ttk.Checkbutton(frame_mode, text="跳过视觉", variable=self.skip_visual_var).pack(side="left", padx=12)

        frame_human = ttk.LabelFrame(tab_run, text="人工补充事实（可选：选择从审计报告模板另存的 JSON）", padding=8)
        frame_human.pack(fill="x", padx=10, pady=4)
        ttk.Entry(frame_human, textvariable=self.human_overrides_var).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(frame_human, text="选择", command=self._browse_human_overrides).pack(side="left", padx=3)
        ttk.Button(frame_human, text="清空", command=lambda: self.human_overrides_var.set("")).pack(side="left", padx=3)

        # --- 运行按钮 ---
        self.btn_run = ttk.Button(tab_run, text="▶  运行所选案件", command=self._on_run, style="Primary.TButton")
        self.btn_run.pack(pady=6)

        # --- 进度日志 ---
        frame_log = ttk.LabelFrame(tab_run, text="运行日志", padding=5)
        frame_log.pack(fill="both", expand=True, padx=10, pady=(4, 8))

        self.log_text = tk.Text(frame_log, wrap="word", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(frame_log, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

        # --- 输出/人工审查按钮 ---
        frame_actions = ttk.Frame(tab_run, padding=5)
        frame_actions.pack(fill="x")
        self.btn_open = ttk.Button(frame_actions, text="打开最近输出目录", command=self._open_output, state="disabled")
        self.btn_open.pack(side="left", padx=8)
        self.btn_review = ttk.Button(frame_actions, text="查看审计报告", command=self._open_human_review, state="disabled")
        self.btn_review.pack(side="left", padx=8)

        # --- 模型调用 tab ---
        frame_llm = ttk.LabelFrame(tab_model, text="LLM 调用", padding=10)
        frame_llm.pack(fill="x", padx=10, pady=8)
        ttk.Radiobutton(frame_llm, text="本地 Ollama（默认，不上传司法材料）", variable=self.llm_provider_var, value="local").grid(row=0, column=0, sticky="w", columnspan=2)
        ttk.Radiobutton(frame_llm, text="云端 API（仅明确选择时验证）", variable=self.llm_provider_var, value="cloud").grid(row=1, column=0, sticky="w", columnspan=2)
        ttk.Radiobutton(frame_llm, text="不调用 LLM，仅规则/人工审计", variable=self.llm_provider_var, value="off").grid(row=2, column=0, sticky="w", columnspan=2)
        ttk.Label(frame_llm, text="本地地址:").grid(row=3, column=0, sticky="e", pady=3)
        ttk.Entry(frame_llm, textvariable=self.local_llm_base_url_var, width=48).grid(row=3, column=1, sticky="we", pady=3)
        ttk.Label(frame_llm, text="本地普通模型:").grid(row=4, column=0, sticky="e", pady=3)
        ttk.Entry(frame_llm, textvariable=self.local_chat_model_var, width=48).grid(row=4, column=1, sticky="we", pady=3)
        ttk.Label(frame_llm, text="本地审计模型:").grid(row=5, column=0, sticky="e", pady=3)
        ttk.Entry(frame_llm, textvariable=self.local_audit_model_var, width=48).grid(row=5, column=1, sticky="we", pady=3)
        ttk.Label(frame_llm, text="本地LLM设备:").grid(row=6, column=0, sticky="e", pady=3)
        ttk.Combobox(frame_llm, textvariable=self.local_llm_device_var, values=["auto", "gpu", "cpu"], width=10, state="readonly").grid(row=6, column=1, sticky="w", pady=3)
        ttk.Label(frame_llm, text="auto/gpu 会请求本地 Ollama 尽量使用显卡；不会自动切云端。", style="Hint.TLabel").grid(row=7, column=1, sticky="w", pady=1)
        ttk.Label(frame_llm, text="云端模型:").grid(row=8, column=0, sticky="e", pady=3)
        ttk.Entry(frame_llm, textvariable=self.cloud_chat_model_var, width=48).grid(row=8, column=1, sticky="we", pady=3)
        frame_llm.columnconfigure(1, weight=1)

        frame_ocr = ttk.LabelFrame(tab_model, text="OCR 调用", padding=10)
        frame_ocr.pack(fill="x", padx=10, pady=8)
        ttk.Radiobutton(frame_ocr, text="本地 PaddleOCR（默认）", variable=self.ocr_provider_var, value="local").grid(row=0, column=0, sticky="w", columnspan=2)
        ttk.Radiobutton(frame_ocr, text="云端 LlamaParse（仅明确选择时）", variable=self.ocr_provider_var, value="cloud").grid(row=1, column=0, sticky="w", columnspan=2)
        ttk.Radiobutton(frame_ocr, text="仅文本，不做 OCR", variable=self.ocr_provider_var, value="text-only").grid(row=2, column=0, sticky="w", columnspan=2)
        ttk.Label(frame_ocr, text="OCR引擎:").grid(row=3, column=0, sticky="e", pady=3)
        ttk.Entry(frame_ocr, textvariable=self.ocr_engine_var, width=32).grid(row=3, column=1, sticky="w", pady=3)
        ttk.Label(frame_ocr, text="OCR设备:").grid(row=4, column=0, sticky="e", pady=3)
        ttk.Combobox(frame_ocr, textvariable=self.ocr_device_var, values=["auto", "gpu", "cpu"], width=10, state="readonly").grid(row=4, column=1, sticky="w", pady=3)
        ttk.Label(frame_ocr, text="默认 auto/gpu 优先；GPU 不可用或初始化失败时转本地 CPU。", style="Hint.TLabel").grid(row=5, column=1, sticky="w", pady=1)
        ttk.Label(frame_ocr, text="PDF渲染倍率:").grid(row=6, column=0, sticky="e", pady=3)
        ttk.Entry(frame_ocr, textvariable=self.ocr_render_scale_var, width=8).grid(row=6, column=1, sticky="w", pady=3)
        ttk.Checkbutton(frame_ocr, text="疑难页/图片二次OCR", variable=self.ocr_second_pass_var).grid(row=7, column=0, sticky="w", columnspan=2, pady=3)
        ttk.Label(frame_ocr, text="二次倍率:").grid(row=8, column=0, sticky="e", pady=3)
        ttk.Entry(frame_ocr, textvariable=self.ocr_second_pass_scale_var, width=8).grid(row=8, column=1, sticky="w", pady=3)
        cache_home = os.getenv("PADDLE_PDX_CACHE_HOME", DEFAULT_PADDLE_CACHE_DIR)
        ttk.Label(frame_ocr, text=f"模型/缓存目录：{cache_home}").grid(row=9, column=0, sticky="w", columnspan=2, pady=3)
        ttk.Label(frame_ocr, text="默认模型：PP-OCRv5_mobile_det + PP-OCRv5_mobile_rec").grid(row=10, column=0, sticky="w", columnspan=2, pady=3)

        frame_cloud = ttk.LabelFrame(tab_model, text="云端安全确认", padding=10)
        frame_cloud.pack(fill="x", padx=10, pady=8)
        ttk.Checkbutton(frame_cloud, text="我确认允许本次将相关文本发送至云端 API", variable=self.allow_cloud_var).pack(anchor="w")
        ttk.Label(frame_cloud, text="默认本地运行；本地失败不会自动切换云端。", foreground="#aa5500").pack(anchor="w", pady=(6, 0))

    def _prompt_initial_materials(self):
        if not self.materials_var.get().strip():
            self._browse_materials()

    def _browse_materials(self):
        """第 1 步：选择案件材料文件夹（案件证据文档所在目录）。"""
        current = self.materials_var.get().strip()
        initial = current if current and os.path.isdir(current) else (
            DEFAULT_DATABASE_DIR if os.path.isdir(DEFAULT_DATABASE_DIR) else PROJECT_ROOT
        )
        folder = filedialog.askdirectory(
            title="第 1 步：请选择案件材料文件夹（案件证据文档）",
            initialdir=initial,
            mustexist=True,
        )
        if not folder:
            return
        self.materials_var.set(os.path.abspath(folder))
        self._refresh_input_status()

    def _browse_photos(self):
        """第 2 步：选择检查照片文件夹（可跳过）。"""
        current = self.photo_var.get().strip()
        initial = current if current and os.path.isdir(current) else (
            self.materials_var.get().strip()
            or (DEFAULT_DATABASE_DIR if os.path.isdir(DEFAULT_DATABASE_DIR) else PROJECT_ROOT)
        )
        folder = filedialog.askdirectory(
            title="第 2 步：请选择检查照片文件夹（没有可直接跳过）",
            initialdir=initial,
            mustexist=True,
        )
        if not folder:
            return
        self.photo_var.set(os.path.abspath(folder))
        self._refresh_input_status()

    def _clear_inputs(self):
        self.materials_var.set("")
        self.photo_var.set("")
        self._refresh_input_status()

    def _refresh_input_status(self):
        materials = self.materials_var.get().strip()
        photos = self.photo_var.get().strip()
        if not materials:
            self.input_status_label.configure(text="尚未选择案件材料文件夹")
            return
        try:
            count = _count_material_files(materials)
        except Exception:
            count = 0
        parts = [f"材料：{count} 个文件"]
        parts.append(f"照片：{'已选择' if photos else '未选择（跳过视觉）'}")
        self.input_status_label.configure(text="；".join(parts))

    def _browse_human_overrides(self):
        path = filedialog.askopenfilename(
            title="选择人工补充事实 JSON",
            initialdir=self.last_output_dir or PROJECT_ROOT,
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.human_overrides_var.set(path)

    def _log(self, msg: str):
        """线程安全的日志输出。"""
        def append():
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
        if threading.current_thread() is threading.main_thread():
            append()
        else:
            self.root.after(0, append)

    def _call_on_main_sync(self, func: Callable[[], Any]) -> Any:
        """从后台 pipeline 线程同步调用 Tk GUI。"""
        if threading.current_thread() is threading.main_thread():
            return func()
        done = threading.Event()
        box: Dict[str, Any] = {"result": None, "error": None}

        def wrapper():
            try:
                box["result"] = func()
            except Exception as exc:  # pragma: no cover - GUI fallback
                box["error"] = exc
            finally:
                done.set()

        self.root.after(0, wrapper)
        done.wait()
        if box["error"]:
            raise box["error"]
        return box["result"]

    def _pre_audit_supplement_dialog(self, session: dict, output_dir: str) -> dict:
        """主审计前补充体格检查/辅助检查信息；不向司法人员展示 JSON。"""
        def show_dialog() -> dict:
            fields = session.get("fields") or []
            if not fields:
                return session
            dialog = tk.Toplevel(self.root)
            dialog.title(session.get("title") or "审计前信息补充")
            _fit_window_to_screen(dialog, 900, 680, min_width=0, min_height=0)
            dialog.transient(self.root)
            dialog.grab_set()
            interaction_timer = ActiveInteractionTimer()

            ttk.Label(
                dialog,
                text=session.get("intro") or "可先补充第四部分体格检查/辅助检查信息；留空也可继续。",
                wraplength=840,
                padding=8,
            ).pack(fill="x")

            body = ttk.Frame(dialog, padding=8)
            body.pack(fill="both", expand=True)
            value_widgets: Dict[str, Any] = {}

            for field in fields:
                target = field.get("target", "")
                frame = ttk.LabelFrame(body, text=field.get("label", target), padding=6)
                frame.pack(fill="both", expand=True if field.get("value_type") != "file" else False, pady=(0, 8))
                guidance = field.get("guidance") or ""
                current = field.get("current_value") or ""
                if guidance:
                    ttk.Label(frame, text=guidance, wraplength=820, foreground="#555").pack(fill="x", pady=(0, 4))
                if current:
                    ttk.Label(frame, text=f"当前系统识别/已有内容：{current}", wraplength=820, foreground="#1f2937").pack(fill="x", pady=(0, 4))
                if field.get("value_type") == "file":
                    var = tk.StringVar(value="")
                    row = ttk.Frame(frame)
                    row.pack(fill="x")
                    ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True, padx=(0, 6))
                    def browse_file(v=var):
                        path = filedialog.askopenfilename(title="选择审片/辅助检查文件", initialdir=output_dir or PROJECT_ROOT)
                        if path:
                            v.set(path)
                    ttk.Button(row, text="选择文件", command=browse_file).pack(side="left")
                    value_widgets[target] = var
                else:
                    text = tk.Text(frame, height=6, wrap="word")
                    if current:
                        text.insert("1.0", current)
                    text.pack(fill="both", expand=True)
                    value_widgets[target] = text

            result = {"session": session}

            def collect(status: str) -> dict:
                updated = copy.deepcopy(session)
                values: Dict[str, str] = {}
                for target, widget in value_widgets.items():
                    if isinstance(widget, tk.Text):
                        value = widget.get("1.0", "end").strip()
                    else:
                        value = widget.get().strip()
                    if value:
                        values[target] = value
                updated["field_values"] = values
                updated["status"] = status
                return updated

            def save_and_continue():
                result["session"] = collect("answered")
                dialog.destroy()

            def skip_and_continue():
                result["session"] = collect("skipped")
                dialog.destroy()

            bottom = ttk.Frame(dialog, padding=8)
            bottom.pack(fill="x")
            ttk.Button(bottom, text="保存并继续", command=save_and_continue).pack(side="right", padx=4)
            ttk.Button(bottom, text="本案暂无，继续", command=skip_and_continue).pack(side="right", padx=4)
            _bind_active_interaction_timer(dialog, interaction_timer)
            self.root.wait_window(dialog)
            return interaction_timer.attach(result["session"])

        return self._call_on_main_sync(show_dialog)

    def _visual_rom_review_dialog(self, session: dict, output_dir: str) -> dict:
        """Visual 后、Judge 前的逐图量角器预审窗口。"""
        def show_dialog() -> dict:
            items = session.get("items") or []
            if not items:
                return session
            updated_session = copy.deepcopy(session)
            answers: Dict[str, dict] = dict(updated_session.get("answers") or {})
            current_index = tk.IntVar(value=0)
            has_loaded = {"value": False}

            dialog = tk.Toplevel(self.root)
            dialog.title(updated_session.get("title") or "关节活动度｜量角器图片预审")
            _fit_window_to_screen(dialog, 1240, 820, min_width=1080, min_height=720)
            dialog.transient(self.root)
            dialog.grab_set()
            interaction_timer = ActiveInteractionTimer()

            ttk.Label(
                dialog,
                text=updated_session.get("intro") or "请逐张查看量角器图片；只有采纳的图片会进入后续 Agent。",
                wraplength=1200,
                padding=8,
                font=("Microsoft YaHei UI", 10, "bold"),
            ).pack(fill="x")

            body = ttk.Frame(dialog, padding=8)
            body.pack(fill="both", expand=True)
            left = ttk.Frame(body)
            left.pack(side="left", fill="y", padx=(0, 8))
            center = ttk.LabelFrame(body, text="overlay / 原图预览", padding=6)
            center.pack(side="left", fill="both", expand=True, padx=(0, 8))
            right = ttk.LabelFrame(body, text="当前图片判定", padding=8)
            right.pack(side="left", fill="y")

            listbox = tk.Listbox(left, width=36, height=34, font=("Microsoft YaHei UI", 9))
            list_scroll = ttk.Scrollbar(left, orient="vertical", command=listbox.yview)
            listbox.configure(yscrollcommand=list_scroll.set)
            list_scroll.pack(side="right", fill="y")
            listbox.pack(side="left", fill="y")

            status_var = tk.StringVar()
            image_info_var = tk.StringVar()
            ttk.Label(center, textvariable=image_info_var, wraplength=760, foreground="#374151").pack(fill="x", pady=(0, 6))
            canvas = tk.Canvas(center, background="#111827", highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            image_refs: List[Any] = []

            preview_buttons = ttk.Frame(center)
            preview_buttons.pack(fill="x", pady=(6, 0))

            adopt_var = tk.StringVar(value="pending")
            joint_var = tk.StringVar()
            side_var = tk.StringVar()
            direction_var = tk.StringVar()
            angle_var = tk.StringVar()
            angle_mode_var = tk.StringVar(value="")
            angle_hint_var = tk.StringVar()
            reason_text = tk.Text(right, height=5, width=34, wrap="word")

            ttk.Label(right, textvariable=status_var, wraplength=280, foreground="#555").pack(fill="x", pady=(0, 8))
            ttk.Label(right, text="是否采纳：").pack(anchor="w")
            adopt_frame = ttk.Frame(right)
            adopt_frame.pack(fill="x", pady=(0, 8))
            ttk.Radiobutton(adopt_frame, text="采纳", variable=adopt_var, value="adopt").pack(side="left", padx=(0, 8))
            ttk.Radiobutton(adopt_frame, text="不采纳", variable=adopt_var, value="reject").pack(side="left")

            def field_row(label: str, var: tk.StringVar, values: Optional[List[str]] = None) -> ttk.Combobox:
                row = ttk.Frame(right)
                row.pack(fill="x", pady=3)
                ttk.Label(row, text=label, width=10).pack(side="left")
                combo = ttk.Combobox(row, textvariable=var, values=values or [], state="normal", width=22)
                combo.pack(side="left", fill="x", expand=True)
                return combo

            joint_combo = field_row("关节", joint_var, [])
            field_row("侧别", side_var, ["左", "右"])
            direction_combo = field_row("活动方向", direction_var, [])
            row_angle = ttk.Frame(right)
            row_angle.pack(fill="x", pady=3)
            ttk.Label(row_angle, text="确认角度", width=10).pack(side="left")
            ttk.Entry(row_angle, textvariable=angle_var, width=24).pack(side="left", fill="x", expand=True)
            ttk.Label(right, textvariable=angle_hint_var, wraplength=280, foreground="#1f2937").pack(fill="x", pady=(4, 2))
            angle_button_row = ttk.Frame(right)
            angle_button_row.pack(fill="x", pady=(0, 6))
            ttk.Label(right, text="说明（可选）：").pack(anchor="w", pady=(8, 2))
            reason_text.pack(fill="x")

            def resolve_path(path: str) -> str:
                text = str(path or "").strip()
                if not text:
                    return ""
                if os.path.isabs(text):
                    return text
                return os.path.abspath(os.path.join(output_dir or PROJECT_ROOT, text))

            def current_item() -> dict:
                idx = max(0, min(current_index.get(), len(items) - 1))
                return items[idx]

            def answer_for(item: dict) -> dict:
                rid = str(item.get("review_id") or "")
                answer = answers.get(rid)
                return answer if isinstance(answer, dict) else {}

            def marker_for(item: dict) -> str:
                answer = answer_for(item)
                adopt = str(answer.get("adopt") or item.get("adopt") or "pending")
                if adopt == "adopt":
                    return "采纳"
                if adopt == "reject":
                    return "不采纳"
                return "未审"

            def refresh_listbox() -> None:
                listbox.delete(0, "end")
                for idx, item in enumerate(items, start=1):
                    angle = item.get("machine_angle")
                    angle_text = f"｜{float(angle):.1f}°" if angle not in (None, "") else ""
                    listbox.insert("end", f"[{marker_for(item)}] {idx}. {item.get('image_name', '图片')}{angle_text}")
                if items:
                    listbox.selection_clear(0, "end")
                    listbox.selection_set(current_index.get())
                    listbox.see(current_index.get())

            def update_direction_choices(_event=None) -> None:
                joint = _normalize_rom_joint(joint_var.get())
                choices = ROM_JOINT_DIRECTIONS.get(joint, [])
                direction_combo.configure(values=choices)
                current = _normalize_rom_direction(joint, direction_var.get())
                if choices and current not in choices:
                    direction_var.set(choices[0])
                elif current != direction_var.get():
                    direction_var.set(current)

            def set_angle_from_item(item: dict, mode: str) -> None:
                value = item.get("complement_angle") if mode == "complement" else item.get("machine_angle")
                parsed = _parse_rom_angle(value)
                if parsed is None:
                    return
                angle_var.set(f"{parsed:g}")
                angle_mode_var.set(mode)
                machine_text = _format_rom_angle(item.get("machine_angle")) or "-"
                complement_text = _format_rom_angle(item.get("complement_angle")) or "-"
                angle_hint_var.set(f"机器读数：{machine_text}°｜补角建议：{complement_text}°｜当前采用：{parsed:g}°")

            def infer_angle_mode(item: dict) -> str:
                explicit = str(angle_mode_var.get() or "").strip()
                current = _parse_rom_angle(angle_var.get())
                machine = _parse_rom_angle(item.get("machine_angle"))
                complement = _parse_rom_angle(item.get("complement_angle"))
                if current is None:
                    return explicit or str(item.get("default_angle_mode") or "")
                if machine is not None and abs(current - machine) <= 0.001:
                    return "machine"
                if complement is not None and abs(current - complement) <= 0.001:
                    return "complement"
                return "manual"

            def open_path(path: str) -> None:
                resolved = resolve_path(path)
                if not resolved or not os.path.exists(resolved):
                    messagebox.showwarning("图片不存在", f"找不到图片：{resolved or path}", parent=dialog)
                    return
                try:
                    os.startfile(resolved)  # type: ignore[attr-defined]
                except Exception as exc:
                    messagebox.showwarning("无法打开图片", f"请手动打开：{resolved}\n{exc}", parent=dialog)

            def render_image(item: dict) -> None:
                canvas.delete("all")
                image_refs.clear()
                path = resolve_path(item.get("preview_path") or item.get("overlay_path") or item.get("source_image_path") or "")
                canvas.update_idletasks()
                canvas_w = max(640, canvas.winfo_width() or 640)
                canvas_h = max(520, canvas.winfo_height() or 520)
                if not path or not os.path.exists(path):
                    canvas.create_text(canvas_w // 2, canvas_h // 2, fill="white", text="未找到预览图片，请点击打开原图。")
                    return
                try:
                    from PIL import Image, ImageTk
                    image = Image.open(path)
                    image.thumbnail((canvas_w - 20, canvas_h - 20))
                    photo = ImageTk.PhotoImage(image)
                    image_refs.append(photo)
                    canvas.create_image(canvas_w // 2, canvas_h // 2, image=photo, anchor="center")
                except Exception:
                    try:
                        photo = tk.PhotoImage(file=path)
                        factor = max(1, int(math.ceil(max(photo.width() / max(1, canvas_w - 20), photo.height() / max(1, canvas_h - 20)))))
                        if factor > 1:
                            photo = photo.subsample(factor, factor)
                        image_refs.append(photo)
                        canvas.create_image(canvas_w // 2, canvas_h // 2, image=photo, anchor="center")
                    except Exception:
                        canvas.create_text(canvas_w // 2, canvas_h // 2, fill="white", text="本窗口无法直接预览该图片，请点击下方按钮打开。")

            def save_current() -> None:
                if not has_loaded["value"] or not items:
                    return
                item = current_item()
                rid = str(item.get("review_id") or "")
                if not rid:
                    return
                answers[rid] = {
                    "review_id": rid,
                    "action_id": item.get("action_id") or "",
                    "adopt": adopt_var.get(),
                    "joint": joint_var.get().strip(),
                    "side": side_var.get().strip(),
                    "direction": direction_var.get().strip(),
                    "angle": angle_var.get().strip(),
                    "angle_mode": infer_angle_mode(item),
                    "machine_angle": item.get("machine_angle"),
                    "complement_angle": item.get("complement_angle"),
                    "reason": reason_text.get("1.0", "end").strip(),
                    "operator": "GUI用户",
                }
                refresh_listbox()

            def load_item(index: int) -> None:
                save_current()
                current_index.set(max(0, min(index, len(items) - 1)))
                item = current_item()
                answer = answer_for(item)
                joint_choices = item.get("joint_choices") or _rom_joint_options_for_case(item)
                joint_combo.configure(values=joint_choices)
                adopt_var.set(str(answer.get("adopt") or item.get("adopt") or "pending"))
                joint_var.set(str(answer.get("joint") or item.get("joint") or (joint_choices[0] if joint_choices else "")))
                side_var.set(str(answer.get("side") or item.get("side") or ""))
                direction_var.set(str(answer.get("direction") or item.get("direction") or ""))
                angle_var.set(str(answer.get("angle") or item.get("default_angle") or ""))
                angle_mode_var.set(str(answer.get("angle_mode") or item.get("default_angle_mode") or ""))
                reason_text.delete("1.0", "end")
                reason_text.insert("1.0", str(answer.get("reason") or ""))
                update_direction_choices()
                machine_text = _format_rom_angle(item.get("machine_angle")) or "-"
                complement_text = _format_rom_angle(item.get("complement_angle")) or "-"
                angle_hint_var.set(f"机器读数：{machine_text}°｜补角建议：{complement_text}°｜当前采用：{angle_var.get() or '-'}°")
                for child in angle_button_row.winfo_children():
                    child.destroy()
                ttk.Button(angle_button_row, text="采用机器读数", command=lambda it=item: set_angle_from_item(it, "machine")).pack(side="left", padx=(0, 6))
                ttk.Button(angle_button_row, text="采用补角", command=lambda it=item: set_angle_from_item(it, "complement")).pack(side="left")
                status_var.set(
                    f"机器读数：{machine_text}°｜补角建议：{complement_text}°｜状态：{item.get('machine_status', '-')}。"
                    "请核对 overlay 后决定是否采纳。"
                )
                reasons = item.get("reasons") or []
                reason_suffix = ("\n风险/提示：" + "；".join(str(x) for x in reasons[:3])) if reasons else ""
                image_info_var.set(
                    f"第 {current_index.get() + 1}/{len(items)} 张：{item.get('image_name', '')}\n"
                    f"overlay：{item.get('overlay_path') or '-'}{reason_suffix}"
                )
                for child in preview_buttons.winfo_children():
                    child.destroy()
                ttk.Button(preview_buttons, text="打开 overlay", command=lambda p=item.get("overlay_path") or item.get("preview_path") or "": open_path(str(p))).pack(side="left", padx=4)
                ttk.Button(preview_buttons, text="打开原图", command=lambda p=item.get("source_image_path") or "": open_path(str(p))).pack(side="left", padx=4)
                refresh_listbox()
                render_image(item)
                has_loaded["value"] = True

            def on_select(_event=None) -> None:
                sel = listbox.curselection()
                if sel:
                    load_item(sel[0])

            def finish() -> None:
                save_current()
                missing_adopted: List[Tuple[int, str]] = []
                unreviewed = []
                for idx, item in enumerate(items):
                    rid = str(item.get("review_id") or "")
                    answer = answers.get(rid) if isinstance(answers.get(rid), dict) else {}
                    adopt = str(answer.get("adopt") or "")
                    if adopt not in {"adopt", "reject"}:
                        unreviewed.append(idx)
                        continue
                    if adopt == "adopt":
                        joint = _normalize_rom_joint(answer.get("joint"))
                        side = _normalize_rom_side(answer.get("side"))
                        direction = _normalize_rom_direction(joint, answer.get("direction"))
                        angle = _parse_rom_angle(answer.get("angle"))
                        if not (joint and side and direction and angle is not None):
                            missing_adopted.append((idx, item.get("image_name", "图片")))
                if missing_adopted:
                    first_idx, name = missing_adopted[0]
                    messagebox.showwarning("采纳项信息不完整", f"“{name}”已选择采纳，但关节、侧别、活动方向或角度不完整。", parent=dialog)
                    load_item(first_idx)
                    return
                if unreviewed:
                    ok = messagebox.askyesno("仍有图片未审", f"还有 {len(unreviewed)} 张量角器图片未选择采纳/不采纳。\n未审图片不会进入后续判断。是否继续？", parent=dialog)
                    if not ok:
                        load_item(unreviewed[0])
                        return
                final_session = copy.deepcopy(updated_session)
                final_session["answers"] = answers
                adopted = sum(1 for ans in answers.values() if isinstance(ans, dict) and ans.get("adopt") == "adopt")
                rejected = sum(1 for ans in answers.values() if isinstance(ans, dict) and ans.get("adopt") == "reject")
                final_session["summary"] = {
                    **(final_session.get("summary") or {}),
                    "adopted_image_count": adopted,
                    "rejected_image_count": rejected,
                }
                final_session["status"] = "answered"
                result["session"] = final_session
                dialog.destroy()

            def skip() -> None:
                skipped = copy.deepcopy(updated_session)
                skipped["answers"] = answers
                skipped["status"] = "skipped"
                result["session"] = skipped
                dialog.destroy()

            joint_combo.bind("<<ComboboxSelected>>", update_direction_choices)
            joint_combo.bind("<KeyRelease>", update_direction_choices)
            listbox.bind("<<ListboxSelect>>", on_select)
            canvas.bind("<Configure>", lambda _event: render_image(current_item()) if has_loaded["value"] else None)

            bottom = ttk.Frame(dialog, padding=8)
            bottom.pack(fill="x")
            ttk.Button(bottom, text="上一张", command=lambda: load_item(current_index.get() - 1)).pack(side="left", padx=4)
            ttk.Button(bottom, text="保存本张", command=save_current).pack(side="left", padx=4)
            ttk.Button(bottom, text="下一张", command=lambda: load_item(current_index.get() + 1)).pack(side="left", padx=4)
            ttk.Button(bottom, text="完成预审并继续", command=finish).pack(side="right", padx=4)
            ttk.Button(bottom, text="跳过预审", command=skip).pack(side="right", padx=4)

            result: Dict[str, Any] = {"session": updated_session}
            refresh_listbox()
            load_item(0)
            _bind_active_interaction_timer(dialog, interaction_timer)
            self.root.wait_window(dialog)
            return interaction_timer.attach(result["session"])

        return self._call_on_main_sync(show_dialog)

    def _audit_questions_dialog(self, audit_session: dict, output_dir: str) -> dict:
        """GUI 问答式人工审计窗口；返回带 answers 的 audit_session。"""
        def show_dialog() -> dict:
            questions = audit_session.get("questions") or []
            if not questions:
                return audit_session
            summary = audit_session.get("summary") or {}
            answers: Dict[str, dict] = {}
            current_index = tk.IntVar(value=0)
            field_vars: Dict[str, tk.StringVar] = {}
            current_input_type = tk.StringVar(value="text")
            single_text_target = tk.StringVar(value="")

            dialog = tk.Toplevel(self.root)
            dialog.title("人工审计确认")
            _fit_window_to_screen(dialog, 980, 720, min_width=0, min_height=0)
            dialog.transient(self.root)
            dialog.grab_set()
            interaction_timer = ActiveInteractionTimer()

            header = ttk.Label(
                dialog,
                text=(
                    f"本案共有 {summary.get('total', len(questions))} 个待确认事项："
                    f"必须确认 {summary.get('required', 0)} 项，"
                    f"建议确认 {summary.get('recommended', 0)} 项，"
                    f"风险提示 {summary.get('risk', 0)} 项。"
                ),
                font=("Microsoft YaHei UI", 10, "bold"),
                padding=8,
            )
            header.pack(fill="x")

            body = ttk.Frame(dialog, padding=8)
            body.pack(fill="both", expand=True)
            left = ttk.Frame(body)
            left.pack(side="left", fill="y", padx=(0, 8))
            right = ttk.Frame(body)
            right.pack(side="left", fill="both", expand=True)

            listbox = tk.Listbox(left, width=38, height=28)
            list_scroll = ttk.Scrollbar(left, orient="vertical", command=listbox.yview)
            listbox.configure(yscrollcommand=list_scroll.set)
            list_scroll.pack(side="right", fill="y")
            listbox.pack(side="left", fill="y")
            for q in questions:
                marker = "必" if q.get("priority") == "required" else "建" if q.get("priority") == "recommended" else "险"
                listbox.insert("end", f"[{marker}] {q.get('group')}｜{q.get('category')}")

            question_var = tk.StringVar()
            reason_var = tk.StringVar()
            risk_var = tk.StringVar()
            current_var = tk.StringVar()
            choice_var = tk.StringVar()
            status_var = tk.StringVar(value="answered")
            answer_text = tk.Text(right, height=5, wrap="word")
            evidence_text = tk.Text(right, height=3, wrap="word")
            fields_frame = ttk.Frame(right, padding=0)
            choice_combo = ttk.Combobox(right, textvariable=choice_var, state="readonly")

            context_frame = ttk.LabelFrame(right, text="当前报告与上下文", padding=6)
            context_frame.pack(fill="x", pady=(0, 8))
            ttk.Label(context_frame, textvariable=question_var, wraplength=650, font=("Microsoft YaHei UI", 10, "bold")).pack(fill="x", pady=(0, 6))
            ttk.Label(context_frame, textvariable=current_var, wraplength=650, foreground="#1f2937").pack(fill="x", pady=(0, 4))

            reason_frame = ttk.LabelFrame(right, text="需审查原因", padding=6)
            reason_frame.pack(fill="x", pady=(0, 8))
            ttk.Label(reason_frame, textvariable=reason_var, wraplength=650, foreground="#555").pack(fill="x", pady=(0, 4))
            ttk.Label(reason_frame, textvariable=risk_var, wraplength=650, foreground="#9a3412").pack(fill="x", pady=(0, 4))

            ttk.Label(right, text="结果认定：").pack(anchor="w")
            choice_combo.pack(fill="x", pady=(0, 8))
            answer_label = ttk.Label(right, text="补充内容（如需调整，请填写最终希望进入报告的正式文字）：")
            answer_label.pack(anchor="w")
            answer_text.configure(height=9)
            answer_text.pack(fill="both", expand=True, pady=(0, 6))
            fields_frame.pack(fill="x", pady=(0, 6))
            evidence_label = ttk.Label(right, text="证据/理由（可选）：")
            evidence_label.pack(anchor="w")
            evidence_text.pack(fill="x", pady=(0, 6))

            preview_frame = ttk.LabelFrame(right, text="量角器图片 / overlay 预览", padding=6)
            preview_label = tk.Label(preview_frame, text="本项如有量角器图片，会在此显示 overlay 预览。", anchor="center", justify="center")
            preview_label.pack(fill="x", pady=(0, 4))
            preview_buttons = ttk.Frame(preview_frame)
            preview_buttons.pack(fill="x")
            preview_image_refs: List[Any] = []

            def _resolve_preview_path(path: str) -> str:
                text = str(path or "").strip()
                if not text:
                    return ""
                if os.path.isabs(text):
                    return text
                return os.path.abspath(os.path.join(output_dir or PROJECT_ROOT, text))

            def _open_preview_path(path: str) -> None:
                resolved = _resolve_preview_path(path)
                if not resolved or not os.path.exists(resolved):
                    messagebox.showwarning("图片不存在", f"找不到图片：{resolved or path}", parent=dialog)
                    return
                try:
                    os.startfile(resolved)  # type: ignore[attr-defined]
                except Exception as exc:
                    messagebox.showwarning("无法打开图片", f"请手动打开：{resolved}\n{exc}", parent=dialog)

            def _clear_preview() -> None:
                for child in preview_buttons.winfo_children():
                    child.destroy()
                preview_image_refs.clear()
                preview_label.configure(image="", text="本项如有量角器图片，会在此显示 overlay 预览。")
                preview_frame.pack_forget()

            def _load_preview(q: dict) -> None:
                _clear_preview()
                attachments = q.get("attachments") or []
                if not attachments:
                    return
                preview_frame.pack(fill="x", pady=(0, 8))
                first_image_path = ""
                for idx, item in enumerate(attachments[:8], start=1):
                    if not isinstance(item, dict):
                        continue
                    path = str(item.get("path") or item.get("overlay_path") or item.get("source_image_path") or "")
                    if not path:
                        continue
                    label = str(item.get("label") or f"图片{idx}")
                    if not first_image_path and path.lower().endswith((".png", ".gif")):
                        first_image_path = path
                    ttk.Button(preview_buttons, text=f"打开：{label}", command=lambda p=path: _open_preview_path(p)).pack(side="left", padx=3, pady=2)
                if first_image_path:
                    resolved = _resolve_preview_path(first_image_path)
                    try:
                        img = tk.PhotoImage(file=resolved)
                        factor = max(1, int(math.ceil(max(img.width() / 620, img.height() / 220))))
                        if factor > 1:
                            img = img.subsample(factor, factor)
                        preview_image_refs.append(img)
                        preview_label.configure(image=img, text="")
                    except Exception:
                        preview_label.configure(text="overlay 图片已生成；如本窗口无法预览，请点击上方按钮打开。")
                else:
                    preview_label.configure(text="本项有量角器图片；请点击上方按钮打开原图或 overlay。")

            def current_question() -> dict:
                idx = max(0, min(current_index.get(), len(questions) - 1))
                return questions[idx]

            def clear_fields() -> None:
                for child in fields_frame.winfo_children():
                    child.destroy()
                field_vars.clear()

            def collect_field_values() -> Dict[str, str]:
                values: Dict[str, str] = {}
                text_value = answer_text.get("1.0", "end").strip()
                single_target = single_text_target.get().strip()
                for target, var in field_vars.items():
                    value = text_value if single_target and target == single_target else var.get().strip()
                    if value:
                        values[target] = value
                return values

            def has_manual_adjustment(q: dict, field_values: Dict[str, str], answer_note: str) -> bool:
                if str(answer_note or "").strip():
                    return True
                defaults = {
                    str(field.get("target") or ""): str(field.get("default") or "").strip()
                    for field in q.get("fields", []) or []
                    if isinstance(field, dict) and field.get("target")
                }
                if q.get("input_type") == "fixed_fields":
                    return any(str(value or "").strip() != defaults.get(target, "") for target, value in field_values.items())
                return any(str(value or "").strip() for value in field_values.values())

            def manual_adjustment_choice(q: dict) -> str:
                options = q.get("options") or ["确认，无需调整", "需要调整", "无法确认/暂不处理"]
                for keyword in ["调整", "补充", "需要", "无法确认", "暂不"]:
                    for option in options:
                        if keyword in str(option) and not str(option).startswith("确认"):
                            return str(option)
                return "需要人工审计"

            def sync_choice_with_inputs(_event=None) -> None:
                if not questions:
                    return
                q = current_question()
                field_values = collect_field_values()
                answer_note = "" if single_text_target.get().strip() else answer_text.get("1.0", "end").strip()
                if has_manual_adjustment(q, field_values, answer_note) and str(choice_var.get()).startswith("确认"):
                    choice_var.set(manual_adjustment_choice(q))

            answer_text.bind("<KeyRelease>", sync_choice_with_inputs)

            def load_question(index: int) -> None:
                save_current()
                current_index.set(max(0, min(index, len(questions) - 1)))
                q = current_question()
                qid = q.get("id", "")
                current_input_type.set(q.get("input_type") or "text")
                listbox.selection_clear(0, "end")
                listbox.selection_set(current_index.get())
                listbox.see(current_index.get())
                question_var.set(f"{qid}｜{q.get('question', '')}")
                context_parts = []
                if q.get("report_context"):
                    context_parts.append("报告位置/预计写法：\n" + str(q.get("report_context")))
                current_var.set("\n\n".join(context_parts) or str(q.get("question", "")))
                reason_parts = [f"原因：{q.get('reason', '')}"]
                if q.get("uncertainty"):
                    reason_parts.append(f"不确定点/风险点：{q.get('uncertainty')}")
                if q.get("needs_confirmation"):
                    reason_parts.append(f"需要补充或确认：{q.get('needs_confirmation')}")
                reason_var.set("\n".join(reason_parts))
                risk_var.set(f"未处理风险：{q.get('risk_if_unresolved', '')}" if q.get("risk_if_unresolved") else "")
                options = q.get("options") or ["确认，无需调整", "需要调整", "无法确认/暂不处理"]
                choice_combo.configure(values=options)
                prior = answers.get(qid, {})
                choice_var.set(prior.get("choice") or options[0])
                status_var.set(prior.get("status") or "answered")
                evidence_text.delete("1.0", "end")
                evidence_text.insert("1.0", prior.get("evidence_note", q.get("evidence_hint", "")))
                clear_fields()
                field_values = prior.get("field_values") or {}
                fields = q.get("fields") or []
                is_fixed_fields = q.get("input_type") == "fixed_fields"
                is_single_field_text = (not is_fixed_fields) and len(fields) == 1 and str((fields[0] or {}).get("value_type") or "text") == "text"
                single_text_target.set(str((fields[0] or {}).get("target") or "") if is_single_field_text else "")
                answer_label.pack_forget()
                answer_text.pack_forget()
                fields_frame.pack_forget()
                answer_text.delete("1.0", "end")
                if is_single_field_text:
                    field = fields[0]
                    target = str(field.get("target") or "")
                    default_value = field_values.get(target, field.get("default", prior.get("answer_text", "")))
                    answer_label.configure(text=f"{field.get('label') or '请填写正式内容'}：")
                    answer_text.configure(height=9)
                    answer_text.insert("1.0", str(default_value or ""))
                    answer_label.pack(anchor="w")
                    answer_text.pack(fill="both", expand=True, pady=(0, 6))
                elif is_fixed_fields:
                    answer_label.configure(text="备注（可选，不作为主要入文字段）：")
                    answer_text.configure(height=3)
                    answer_text.insert("1.0", prior.get("answer_text", ""))
                    answer_label.pack(anchor="w")
                    answer_text.pack(fill="x", pady=(0, 6))
                    fields_frame.pack(fill="x", pady=(0, 6))
                elif fields:
                    answer_label.configure(text="备注（可选，不作为主要入文字段）：")
                    answer_text.configure(height=3)
                    answer_text.insert("1.0", prior.get("answer_text", ""))
                    answer_label.pack(anchor="w")
                    answer_text.pack(fill="x", pady=(0, 6))
                    fields_frame.pack(fill="x", pady=(0, 6))
                else:
                    answer_label.configure(text="补充内容（请填写最终希望进入报告的正式文字）：")
                    answer_text.configure(height=9)
                    answer_text.insert("1.0", prior.get("answer_text", ""))
                    answer_label.pack(anchor="w")
                    answer_text.pack(fill="both", expand=True, pady=(0, 6))
                dependent_direction_entries: List[Tuple[Any, tk.StringVar, dict]] = []

                def refresh_dependent_direction_choices(_event=None) -> None:
                    joint_var = field_vars.get("rom_review.joint")
                    selected_joint = _normalize_rom_joint(joint_var.get() if joint_var else "")
                    for combo, dep_var, choices_map in dependent_direction_entries:
                        choices_for_joint = choices_map.get(selected_joint, []) or []
                        try:
                            combo.configure(values=choices_for_joint)
                        except Exception:
                            continue
                        current_direction = _normalize_rom_direction(selected_joint, dep_var.get())
                        if choices_for_joint and current_direction not in choices_for_joint:
                            dep_var.set(choices_for_joint[0])
                        elif current_direction != dep_var.get():
                            dep_var.set(current_direction)
                    sync_choice_with_inputs()

                for field in fields:
                    target = field.get("target", "")
                    if not target:
                        continue
                    default_value = field_values.get(target, field.get("default", ""))
                    var = tk.StringVar(value=str(default_value or ""))
                    field_vars[target] = var
                    row = ttk.Frame(fields_frame)
                    row.pack(fill="x", pady=2)
                    ttk.Label(row, text=field.get("label") or target, width=26).pack(side="left", padx=(0, 6))
                    choices = field.get("choices") or []
                    choices_by_joint = field.get("choices_by_joint") or {}
                    if choices_by_joint:
                        selected_joint = _normalize_rom_joint(field_vars.get("rom_review.joint").get() if field_vars.get("rom_review.joint") else "")
                        choices = choices_by_joint.get(selected_joint, []) or []
                    if field.get("value_type") == "appraiser_select":
                        choices = choices or APPRAISER_CHOICE_LABELS
                        entry = ttk.Combobox(row, textvariable=var, values=choices, state="readonly")
                    elif choices or choices_by_joint:
                        entry = ttk.Combobox(row, textvariable=var, values=choices, state="normal")
                    else:
                        entry = ttk.Entry(row, textvariable=var)
                    entry.pack(side="left", fill="x", expand=True)
                    entry.bind("<KeyRelease>", sync_choice_with_inputs)
                    entry.bind("<<ComboboxSelected>>", sync_choice_with_inputs)
                    if field.get("value_type") == "appraiser_select":
                        def _sync_appraiser_license(_event=None, name_target=target, source_var=var):
                            license_target = name_target.replace("_name", "_license_no")
                            license_var = field_vars.get(license_target)
                            license_no = _appraiser_license_by_label(source_var.get())
                            if license_var and license_no:
                                license_var.set(license_no)
                            sync_choice_with_inputs()
                        entry.bind("<<ComboboxSelected>>", _sync_appraiser_license)
                    if choices_by_joint:
                        dependent_direction_entries.append((entry, var, choices_by_joint))
                    if target == "rom_review.joint":
                        entry.bind("<KeyRelease>", refresh_dependent_direction_choices)
                        entry.bind("<<ComboboxSelected>>", refresh_dependent_direction_choices)
                refresh_dependent_direction_choices()
                _load_preview(q)

            def save_current() -> None:
                if not questions:
                    return
                q = current_question()
                qid = q.get("id", "")
                if not qid:
                    return
                field_values = collect_field_values()
                answer_note = "" if single_text_target.get().strip() else answer_text.get("1.0", "end").strip()
                if has_manual_adjustment(q, field_values, answer_note) and str(choice_var.get()).startswith("确认"):
                    choice_var.set(manual_adjustment_choice(q))
                answers[qid] = {
                    "question_id": qid,
                    "question": q,
                    "status": status_var.get(),
                    "choice": choice_var.get(),
                    "answer_text": answer_note,
                    "evidence_note": evidence_text.get("1.0", "end").strip(),
                    "field_values": field_values,
                    "operator": "GUI用户",
                }

            def on_select(_event=None):
                sel = listbox.curselection()
                if sel:
                    load_question(sel[0])

            listbox.bind("<<ListboxSelect>>", on_select)

            bottom = ttk.Frame(dialog, padding=8)
            bottom.pack(fill="x")
            ttk.Button(bottom, text="上一项", command=lambda: load_question(current_index.get() - 1)).pack(side="left", padx=4)
            ttk.Button(bottom, text="保存本项", command=save_current).pack(side="left", padx=4)
            ttk.Button(bottom, text="下一项", command=lambda: load_question(current_index.get() + 1)).pack(side="left", padx=4)

            result: Dict[str, Any] = {"session": audit_session}

            def finish():
                save_current()
                updated = copy.deepcopy(audit_session)
                updated["answers"] = list(answers.values())
                answered_ids = {ans.get("question_id") for ans in updated["answers"] if ans.get("status") == "answered"}
                unresolved_required = [
                    q for q in updated.get("questions", [])
                    if q.get("priority") == "required" and q.get("id") not in answered_ids
                ]
                if unresolved_required:
                    ok = messagebox.askyesno(
                        "仍有必须确认事项未完成",
                        f"还有 {len(unresolved_required)} 个必须确认事项未填写。\n\n继续生成后，这些问题会保留在审计报告风险中；建议先逐项处理。\n\n是否仍要继续生成？",
                        parent=dialog,
                    )
                    if not ok:
                        return
                updated["unresolved_risks"] = [
                    f"{q.get('id')}｜{q.get('category')}：{q.get('risk_if_unresolved')}"
                    for q in updated.get("questions", [])
                    if q.get("risk_if_unresolved") and (q.get("priority") == "required" and q.get("id") not in answered_ids)
                ]
                result["session"] = updated
                dialog.destroy()

            def cancel():
                result["session"] = audit_session
                dialog.destroy()

            ttk.Button(bottom, text="完成审计并重新生成", command=finish).pack(side="right", padx=4)
            ttk.Button(bottom, text="跳过本轮审计", command=cancel).pack(side="right", padx=4)

            load_question(0)
            _bind_active_interaction_timer(dialog, interaction_timer)
            self.root.wait_window(dialog)
            return interaction_timer.attach(result["session"])

        return self._call_on_main_sync(show_dialog)

    def _preview_report_dialog(self, report_md: str, output_dir: str, audit_session: Optional[dict] = None) -> Tuple[str, dict]:
        """最终报告预览小修窗口；仅在编辑器里高亮审计/待补充位置，保存时不写入高亮标记。"""
        def show_dialog() -> Tuple[str, dict]:
            dialog = tk.Toplevel(self.root)
            dialog.title("最终报告预览与小修")
            _fit_window_to_screen(dialog, 1000, 760, min_width=0, min_height=0)
            dialog.transient(self.root)
            dialog.grab_set()
            interaction_timer = ActiveInteractionTimer()

            annotations = ((audit_session or {}).get("ai_preview_annotations") or {}).get("annotations") or []
            summary = ((audit_session or {}).get("ai_preview_annotations") or {}).get("summary") or {}
            ttk.Label(
                dialog,
                text=(
                    "可在下方对审计后报告作最终小修。绿色为本轮审计已处理位置；"
                    "红色括号/高光为缺失信息，蓝色为格式或语义提示，橙色为存疑点。"
                    "这些颜色和 AI 临时提示保存时不会写入最终文档。若修改关键结论，请勾选留痕。"
                ),
                padding=8,
            ).pack(fill="x")
            if annotations:
                ttk.Label(
                    dialog,
                    text=f"AI辅助审计提示：共 {summary.get('total', len(annotations))} 项，其中缺失 {summary.get('missing', 0)} 项、格式 {summary.get('format', 0)} 项、存疑 {summary.get('uncertain', 0)} 项。",
                    foreground="#7c2d12",
                    padding=(8, 0, 8, 4),
                ).pack(fill="x")
            text = tk.Text(dialog, wrap="word", font=("Microsoft YaHei UI", 10))
            scroll = ttk.Scrollbar(dialog, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            text.pack(fill="both", expand=True, padx=8, pady=4)
            text.insert("1.0", report_md)
            text.tag_configure("audit_pending", background="#fff3bf", font=("Microsoft YaHei UI", 10, "bold"))
            text.tag_configure("audit_answered", background="#dcfce7")
            text.tag_configure("ai_format", background="#dbeafe")
            text.tag_configure("ai_missing", background="#fecaca")
            text.tag_configure("ai_uncertain", background="#fed7aa")
            text.tag_configure("ai_inline_note", foreground="#b91c1c", background="#fee2e2", font=("Microsoft YaHei UI", 10, "bold"))

            def mark_phrase(phrase: str, tag: str) -> None:
                phrase = str(phrase or "").strip()
                if not phrase:
                    return
                start = "1.0"
                while True:
                    pos = text.search(phrase, start, stopindex="end")
                    if not pos:
                        break
                    end = f"{pos}+{len(phrase)}c"
                    text.tag_add(tag, pos, end)
                    start = end

            for phrase in _placeholder_markers():
                mark_phrase(phrase, "audit_pending")

            def mark_annotation(annotation: dict) -> None:
                category = annotation.get("category") or "uncertain"
                tag = {"format": "ai_format", "missing": "ai_missing", "uncertain": "ai_uncertain"}.get(category, "ai_uncertain")
                span = str(annotation.get("span_text") or "").strip()
                message = str(annotation.get("suggested_note") or annotation.get("message") or "").strip()
                pos = ""
                line_no = 0
                try:
                    line_no = int(annotation.get("line_no") or 0)
                except (TypeError, ValueError):
                    line_no = 0
                try:
                    start_col = int(annotation.get("span_start_col", -1))
                    end_col = int(annotation.get("span_end_col", -1))
                except (TypeError, ValueError):
                    start_col = -1
                    end_col = -1
                if line_no and start_col >= 0 and end_col >= start_col:
                    try:
                        pos = f"{line_no}.{start_col}"
                        end = f"{line_no}.{end_col}"
                        text.tag_add(tag, pos, end)
                    except tk.TclError:
                        pos = ""
                if not pos and span and line_no:
                    try:
                        line_start = f"{line_no}.0"
                        line_end = f"{line_no}.end"
                        found = text.search(span, line_start, stopindex=line_end)
                        if found:
                            pos = found
                            text.tag_add(tag, found, f"{found}+{len(span)}c")
                    except tk.TclError:
                        pos = ""
                if not pos and span:
                    pos = text.search(span, "1.0", stopindex="end")
                    if pos:
                        end = f"{pos}+{len(span)}c"
                        text.tag_add(tag, pos, end)
                if not pos and line_no and category != "format":
                    try:
                        pos = f"{line_no}.0"
                        line_end = f"{line_no}.end"
                        text.tag_add(tag, pos, line_end)
                    except (TypeError, ValueError, tk.TclError):
                        pos = ""
                if category == "missing" and pos and message:
                    note = f"【缺失：{message}】"
                    insert_at = f"{pos} lineend"
                    text.insert(insert_at, note, ("ai_inline_note",))

            for annotation in annotations:
                if isinstance(annotation, dict):
                    mark_annotation(annotation)
            answered_question_ids = {
                (answer.get("question", {}) or {}).get("id") or answer.get("question_id")
                for answer in ((audit_session or {}).get("answers") or [])
                if isinstance(answer, dict) and answer.get("status") == "answered"
            }
            for q in (audit_session or {}).get("questions", []) or []:
                if q.get("id") in answered_question_ids:
                    line = q.get("report_line") or ""
                    if line:
                        mark_phrase(line[:80], "audit_answered")

            bottom = ttk.Frame(dialog, padding=8)
            bottom.pack(fill="x")
            key_changed = tk.BooleanVar(value=False)
            note_var = tk.StringVar(value="")
            ttk.Checkbutton(bottom, text="本次编辑涉及伤残等级/三期/关键事实等核心结论", variable=key_changed).pack(anchor="w")
            note_frame = ttk.Frame(bottom)
            note_frame.pack(fill="x", pady=4)
            ttk.Label(note_frame, text="编辑说明：").pack(side="left")
            ttk.Entry(note_frame, textvariable=note_var).pack(side="left", fill="x", expand=True, padx=4)

            result = {"text": report_md, "edit": {"edited": False}}

            def remove_ai_inline_notes() -> None:
                ranges = list(text.tag_ranges("ai_inline_note"))
                for start, end in reversed(list(zip(ranges[0::2], ranges[1::2]))):
                    text.delete(start, end)

            def save():
                remove_ai_inline_notes()
                new_text = text.get("1.0", "end").rstrip() + "\n"
                edited = new_text != report_md
                result["text"] = new_text
                result["edit"] = {
                    "edited": edited,
                    "operator": "GUI用户" if edited else "",
                    "note": note_var.get().strip(),
                    "key_conclusion_changed": bool(key_changed.get()),
                }
                dialog.destroy()

            def skip():
                dialog.destroy()

            ttk.Button(bottom, text="保存为审计后报告", command=save).pack(side="right", padx=4)
            ttk.Button(bottom, text="不修改", command=skip).pack(side="right", padx=4)
            _bind_active_interaction_timer(dialog, interaction_timer)
            self.root.wait_window(dialog)
            result["edit"][HUMAN_TIMING_META_KEY] = {
                "active_elapsed_seconds": interaction_timer.elapsed(),
                "idle_cap_seconds": interaction_timer.idle_cap_seconds,
            }
            return result["text"], result["edit"]

        return self._call_on_main_sync(show_dialog)

    def _runtime_config_from_gui(self) -> RuntimeConfig:
        def parse_float(value: str, default: float) -> float:
            try:
                return float(str(value).strip())
            except (TypeError, ValueError):
                return default

        initial = get_runtime_config()
        return get_runtime_config(
            llm_provider=self.llm_provider_var.get(),
            ocr_provider=self.ocr_provider_var.get(),
            allow_cloud=bool(self.allow_cloud_var.get()),
            local_llm_base_url=self.local_llm_base_url_var.get().strip(),
            local_chat_model=self.local_chat_model_var.get().strip(),
            local_audit_model=self.local_audit_model_var.get().strip(),
            local_llm_device=self.local_llm_device_var.get(),
            cloud_chat_model=self.cloud_chat_model_var.get().strip(),
            local_ocr_engine=self.ocr_engine_var.get().strip() or "paddleocr",
            local_ocr_device=self.ocr_device_var.get(),
            local_ocr_render_scale=parse_float(self.ocr_render_scale_var.get(), initial.local_ocr_render_scale),
            local_ocr_second_pass=bool(self.ocr_second_pass_var.get()),
            local_ocr_second_pass_scale=parse_float(self.ocr_second_pass_scale_var.get(), initial.local_ocr_second_pass_scale),
        )

    def _on_run(self):
        if self.running:
            return
        materials_folder = self.materials_var.get().strip()
        if not materials_folder:
            messagebox.showwarning("提示", "请先完成第 1 步：选择案件材料文件夹")
            return
        if not os.path.isdir(materials_folder):
            messagebox.showwarning("提示", f"案件材料文件夹不存在：\n{materials_folder}")
            return
        photo_folder = self.photo_var.get().strip() or None
        if photo_folder and not os.path.isdir(photo_folder):
            messagebox.showwarning("提示", f"检查照片文件夹不存在：\n{photo_folder}")
            return
        self.running = True
        self.btn_run.configure(state="disabled")
        self.btn_open.configure(state="disabled")
        self.btn_review.configure(state="disabled")
        self.log_text.delete("1.0", "end")

        # 检查照片是第 2 步的可选项；没选就跳过 Visual，而不是去材料夹里乱猜。
        skip_visual = self.skip_visual_var.get() or not photo_folder
        human_overrides_path = self.human_overrides_var.get().strip() or None
        if human_overrides_path and not os.path.isfile(human_overrides_path):
            messagebox.showwarning("提示", "人工补充事实 JSON 不存在，请重新选择或清空。")
            self.running = False
            self.btn_run.configure(state="normal")
            return
        runtime_config = self._runtime_config_from_gui()
        if (runtime_config.llm_provider == "cloud" or runtime_config.ocr_provider == "cloud") and not runtime_config.allow_cloud:
            messagebox.showwarning("云端确认", "选择云端 LLM/OCR 前，请先在“模型调用”页勾选云端安全确认。")
            self.running = False
            self.btn_run.configure(state="normal")
            return
        batch_label = time.strftime("batch_%Y%m%d_%H%M%S")
        self.batch_label = batch_label

        # 在后台线程中运行pipeline
        def worker():
            try:
                results = run_case_batch(
                    [materials_folder],
                    mode=self.mode_var.get(),
                    skip_visual=skip_visual,
                    log=self._log,
                    batch_label=batch_label,
                    human_overrides_path=human_overrides_path,
                    visual_review_callback=self._visual_rom_review_dialog,
                    pre_audit_supplement_callback=self._pre_audit_supplement_dialog,
                    audit_callback=self._audit_questions_dialog,
                    preview_callback=self._preview_report_dialog,
                    runtime_config=runtime_config,
                    materials_folder=materials_folder,
                    photo_folder=photo_folder,
                )
                successful = [r for r in results if r.get("final_report")]
                if successful:
                    self.last_output_dir = os.path.dirname(successful[-1]["final_report"])
                    report_path = os.path.join(self.last_output_dir, DELIVERABLE_REPORT_MD)
                    self.last_review_path = report_path if os.path.isfile(report_path) else None
                    self.root.after(0, lambda: self.btn_open.configure(state="normal"))
                    if self.last_review_path:
                        self.root.after(0, lambda: self.btn_review.configure(state="normal"))
            except Exception as e:
                self._log(f"[FATAL] {e}")
            finally:
                self.running = False
                self.root.after(0, lambda: self.btn_run.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _open_path(self, path: str):
        import platform
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _open_output(self):
        if self.last_output_dir and os.path.isdir(self.last_output_dir):
            self._open_path(self.last_output_dir)

    def _open_human_review(self):
        if self.last_review_path and os.path.isfile(self.last_review_path):
            self._open_path(self.last_review_path)

    def run(self):
        self.root.mainloop()


# ============================================================
# 模块9：CLI入口
# ============================================================

def main():
    _maybe_reexec_with_preferred_python(["paddleocr", "pydantic", "langchain_openai", "langchain_core", "openai"])
    parser = argparse.ArgumentParser(description="boss_agent — 鉴定意见书总控流水线")
    parser.add_argument("--case-folder", action="append", help="案件根目录，可重复传入多个；自动读取 1鉴定材料/3检查照片，输出到 test_result/<编号>")
    parser.add_argument("--materials-folder", help="案件材料文件夹（GUI 第 1 步所选）；交付物写入 <该文件夹>/输出/")
    parser.add_argument("--case-folders", nargs="+", help="多个案件根目录（批量CLI模式）")
    parser.add_argument("--transfer-folder", help="移交/证据材料文件夹路径（兼容旧模式）")
    parser.add_argument("--check-folder", help="检查照片文件夹路径（兼容旧模式，作为 photo_folder 使用）")
    parser.add_argument("--exam-doc-folder", help="检查记录/文本材料文件夹路径（可选，不应指向专家鉴定意见书）")
    parser.add_argument("--photo-folder", help="检查照片文件夹路径（可选，优先于 --check-folder）")
    parser.add_argument("--mode", default="hybrid", choices=["hybrid", "llm", "rule"])
    parser.add_argument("--output-dir", default=None, help="单案输出目录；批量 database 模式默认写入 test_result/<编号>")
    parser.add_argument("--batch-label", default=None, help="批量输出子目录名，默认 batch_YYYYMMDD_HHMMSS")
    parser.add_argument("--skip-visual", action="store_true", help="跳过视觉读数步骤")
    parser.add_argument("--human-overrides", help="人工补充事实 JSON；Boss 会回填运行副本后重跑 Ruler")
    parser.add_argument(
        "--ignore-saved-overrides",
        action="store_true",
        help="忽略输出目录中累计的 07_human_overrides.json，从零重新生成（默认会自动重放该文件，避免人工审计成果丢失）",
    )
    parser.add_argument("--llm-provider", choices=["local", "cloud", "off"], default=None, help="LLM调用通道，默认 local")
    parser.add_argument("--ocr-provider", choices=["local", "cloud", "text-only"], default=None, help="OCR调用通道，默认 local")
    parser.add_argument("--local-chat-model", default=None, help="本地 LLM 普通抽取模型名")
    parser.add_argument("--local-audit-model", default=None, help="本地最终审计/高光专用模型名；留空则使用普通模型")
    parser.add_argument("--local-llm-base-url", default=None, help="本地 Ollama OpenAI-compatible base URL")
    parser.add_argument("--local-llm-device", choices=["auto", "gpu", "cpu"], default=None, help="本地 LLM 设备，默认 GPU；不可用则本地 CPU")
    parser.add_argument("--cloud-chat-model", default=None, help="云端 LLM 模型名")
    parser.add_argument("--local-ocr-engine", default=None, help="本地 OCR 引擎，默认 paddleocr")
    parser.add_argument("--local-ocr-device", choices=["auto", "gpu", "cpu"], default=None, help="本地 OCR 设备，默认 GPU；不可用则本地 CPU")
    parser.add_argument("--local-ocr-render-scale", type=float, default=None, help="本地 PDF OCR 首次渲染倍率，默认 1.5")
    parser.add_argument("--local-ocr-second-pass-scale", type=float, default=None, help="疑难页二次 OCR 渲染/放大倍率，默认 2.5")
    parser.add_argument("--disable-local-ocr-second-pass", action="store_true", help="关闭疑难页/图片二次 OCR")
    parser.add_argument("--allow-cloud", action="store_true", help="明确允许本次使用云端模型处理材料")
    parser.add_argument("--no-gui", action="store_true", help="无GUI模式")
    args = parser.parse_args()

    runtime_config = get_runtime_config(
        llm_provider=args.llm_provider,
        ocr_provider=args.ocr_provider,
        allow_cloud=True if args.allow_cloud else None,
        local_chat_model=args.local_chat_model,
        local_audit_model=args.local_audit_model,
        local_llm_base_url=args.local_llm_base_url,
        local_llm_device=args.local_llm_device,
        cloud_chat_model=args.cloud_chat_model,
        local_ocr_engine=args.local_ocr_engine,
        local_ocr_device=args.local_ocr_device,
        local_ocr_render_scale=args.local_ocr_render_scale,
        local_ocr_second_pass=False if args.disable_local_ocr_second_pass else None,
        local_ocr_second_pass_scale=args.local_ocr_second_pass_scale,
    )

    case_folders: List[str] = []
    if args.case_folder:
        case_folders.extend(args.case_folder)
    if args.case_folders:
        case_folders.extend(args.case_folders)

    if args.no_gui:
        if len(case_folders) > 1:
            run_case_batch(
                case_folders,
                mode=args.mode,
                skip_visual=args.skip_visual,
                batch_label=args.batch_label,
                human_overrides_path=args.human_overrides,
                ignore_saved_overrides=args.ignore_saved_overrides,
                runtime_config=runtime_config,
            )
        elif len(case_folders) == 1:
            run_pipeline(
                mode=args.mode,
                output_dir=args.output_dir,
                case_folder=case_folders[0],
                skip_visual=args.skip_visual,
                exam_doc_folder=args.exam_doc_folder,
                photo_folder=args.photo_folder,
                batch_label=args.batch_label,
                human_overrides_path=args.human_overrides,
                ignore_saved_overrides=args.ignore_saved_overrides,
                runtime_config=runtime_config,
            )
        elif args.materials_folder:
            run_pipeline(
                materials_folder=args.materials_folder,
                photo_folder=args.photo_folder,
                mode=args.mode,
                skip_visual=args.skip_visual,
                exam_doc_folder=args.exam_doc_folder,
                human_overrides_path=args.human_overrides,
                ignore_saved_overrides=args.ignore_saved_overrides,
                runtime_config=runtime_config,
            )
        elif args.transfer_folder:
            run_pipeline(
                transfer_folder=args.transfer_folder,
                check_folder=args.check_folder,
                mode=args.mode,
                output_dir=args.output_dir,
                skip_visual=args.skip_visual,
                exam_doc_folder=args.exam_doc_folder,
                photo_folder=args.photo_folder or args.check_folder,
                human_overrides_path=args.human_overrides,
                ignore_saved_overrides=args.ignore_saved_overrides,
                runtime_config=runtime_config,
            )
        else:
            parser.error("--no-gui 模式请提供 --case-folder/--case-folders，或至少提供 --transfer-folder。")
    else:
        gui = BossAgentGUI()
        # 命令行给定案件目录时预填（兼容旧习惯）；否则由 GUI 第 1 步选择。
        if case_folders:
            gui.materials_var.set(os.path.abspath(case_folders[0]))
            gui._refresh_input_status()
        gui.run()


if __name__ == "__main__":
    main()
