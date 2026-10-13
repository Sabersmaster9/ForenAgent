import os
import sys
import glob
import re
import time
from datetime import datetime
from dotenv import load_dotenv
from typing import Any, TypedDict, List, Dict, Tuple

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agents.common.ocr_client import build_paddle_ocr, extract_local_file_text, format_paddle_device_status, score_text_quality
from agents.common.output_paths import resolve_numbered_output_dir, sanitize_folder_name
from agents.common.runtime_config import RuntimeConfig, apply_runtime_env, get_runtime_config

try:
    from llama_parse import LlamaParse
except ImportError:  # 允许在未安装 LlamaParse 的环境中用本地文本降级模式跑通流程。
    LlamaParse = None

try:
    import nest_asyncio
except ImportError:  # nest_asyncio 只用于已有事件循环环境；缺失时不阻断 CLI/Boss 运行。
    nest_asyncio = None

try:
    from langgraph.graph import StateGraph, END
except ImportError:  # Boss 直接调用时可使用简易图执行器，避免缺 langgraph 直接中断。
    StateGraph = None
    END = "__end__"
import tkinter as tk
from tkinter import filedialog, messagebox

# 允许在事件循环中嵌套，适配某些IDE和环境；普通 CLI/Boss 运行缺少该包时无需强制依赖。
if nest_asyncio is not None:
    nest_asyncio.apply()

# 加载项目根目录下的环境变量，避免受当前工作目录影响
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(dotenv_path=ENV_PATH, override=True)

# 定义 LangGraph 的状态字典
class AgentState(TypedDict, total=False):
    input_folder: str       # 输入的文件夹路径
    extracted_texts: List[str] # 提取出的每个文件的 markdown 文本
    final_markdown: str     # 最终组合的 markdown 文本
    output_path: str        # 输出文件路径
    total_files: int        # 扫描到的文件总数
    success_files: int      # 成功提取文件数
    failed_files: int       # 失败文件数
    runtime_config: Any     # 本地/云端模型调用配置
    warnings: List[str]     # 提取警告


def _normalize_folder_for_case_scan(selected_folder: str) -> str:
    """严格边界模式：仅扫描用户选择的目录及其子目录。"""
    return selected_folder


def _sanitize_folder_name(name: str) -> str:
    """清理文件夹名中的非法路径字符。"""
    return sanitize_folder_name(name)


def _resolve_output_path(selected_folder: str) -> str:
    """输出路径规则：优先写入项目 test_result/<案件编号>/extracted_result.md。"""
    selected_norm = os.path.normpath(selected_folder)
    selected_name = os.path.basename(selected_norm)

    # 若选中的是“移交材料/鉴定材料”等材料目录，优先用路径中的三位案件编号映射 test_result。
    case_dir = resolve_numbered_output_dir(selected_norm, fallback_name=selected_name)
    return os.path.join(case_dir, "extracted_result.md")


def _collect_case_files(scan_root: str) -> List[str]:
    """递归收集案件内可解析文件，过滤临时文件。"""
    supported_extensions = ('.pdf', '.docx', '.doc', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.txt', '.md', '.csv', '.json')
    all_files: List[str] = []

    for ext in supported_extensions:
        all_files.extend(glob.glob(os.path.join(scan_root, "**", f"*{ext}"), recursive=True))
        all_files.extend(glob.glob(os.path.join(scan_root, "**", f"*{ext.upper()}"), recursive=True))

    # 过滤 Office 临时文件，比如 ~$xxx.docx
    cleaned_files = [
        f for f in all_files
        if not os.path.basename(f).startswith("~$") and os.path.isfile(f)
    ]

    # 排序后去重，保证输出顺序稳定，便于跨案例复核
    return sorted(set(cleaned_files), key=lambda p: p.lower())


def _norm_key(path_text: str) -> str:
    """路径键标准化，便于匹配批量OCR返回的 metadata。"""
    return os.path.normcase(os.path.normpath(str(path_text)))


def _build_batch_doc_map(documents: List) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """把批量解析结果按文件路径和文件名建立索引。"""
    by_path: Dict[str, List[str]] = {}
    by_name: Dict[str, List[str]] = {}

    for doc in documents:
        text = getattr(doc, "text", "")
        if not text:
            continue

        metadata = getattr(doc, "metadata", {}) or {}
        meta_path = metadata.get("file_path") or metadata.get("source") or metadata.get("file_name")
        meta_name = metadata.get("file_name")

        if meta_path:
            path_key = _norm_key(meta_path)
            by_path.setdefault(path_key, []).append(text)

        if meta_name:
            name_key = os.path.basename(str(meta_name))
            by_name.setdefault(name_key, []).append(text)

    return by_path, by_name


def _get_batch_page_texts(
    file_path: str,
    fname: str,
    by_path: Dict[str, List[str]],
    by_name: Dict[str, List[str]],
    name_counts: Dict[str, int],
) -> List[str]:
    """优先按完整路径匹配，必要时按唯一文件名匹配。"""
    path_key = _norm_key(file_path)
    if path_key in by_path:
        return by_path[path_key]

    # 当 metadata 使用不同前缀时，尝试后缀匹配
    suffix = os.sep + _norm_key(fname)
    matched = [texts for key, texts in by_path.items() if key.endswith(suffix)]
    if len(matched) == 1:
        return matched[0]

    # 仅在文件名全局唯一时使用文件名匹配，避免同名文件串扰
    if name_counts.get(fname, 0) == 1 and fname in by_name:
        return by_name[fname]

    return []


def _is_noise_line(line: str) -> bool:
    """判断是否为常见页眉页脚/噪声行（保持保守，尽量不删关键内容）。"""
    s = line.strip()
    if not s:
        return False

    # 常见页码与打印信息
    noise_patterns = [
        r"^第\s*\d+\s*页\s*(共\s*\d+\s*页)?$",
        r"^Page\s*\d+(\s*/\s*\d+)?$",
        r"^\d+\s*/\s*\d+$",
        r"^打印时间[:：]?.*$",
        r"^打印日期[:：]?.*$",
        r"^扫描时间[:：]?.*$",
        r"^仅供.*参考$",
        r"^本页无正文$",
    ]
    if any(re.match(p, s, flags=re.IGNORECASE) for p in noise_patterns):
        return True

    # 纯符号线 / 仅数字编号噪声
    if re.match(r"^[\-_=*~.·•\s]{3,}$", s):
        return True

    return False


def _looks_like_table_separator(line: str) -> bool:
    """判断是否是 Markdown 表格分隔行。"""
    s = line.strip()
    return bool(re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", s))


def _remove_table_code_fences(text: str) -> str:
    """移除仅包裹 Markdown 表格的代码围栏，改善预览显示。"""
    lines = text.splitlines()
    result: List[str] = []
    i = 0

    while i < len(lines):
        start = lines[i].strip().lower()
        if start in ("```markdown", "```md", "```"):
            j = i + 1
            while j < len(lines) and lines[j].strip() != "```":
                j += 1

            # 没有闭合围栏，原样保留
            if j >= len(lines):
                result.append(lines[i])
                i += 1
                continue

            block = lines[i + 1:j]
            has_table_row = any("|" in ln for ln in block)
            has_table_sep = any(_looks_like_table_separator(ln) for ln in block)

            if has_table_row and has_table_sep:
                # 仅去掉围栏，保留表格内容
                result.extend(block)
            else:
                # 非表格代码块不动
                result.append(lines[i])
                result.extend(block)
                result.append(lines[j])

            i = j + 1
            continue

        result.append(lines[i])
        i += 1

    return "\n".join(result)


def _read_local_text_fallback(file_path: str) -> str:
    """本地文本读取；PDF/图片 OCR 由 ocr_client 的 local provider 处理。"""
    return extract_local_file_text(file_path, text_only=True)


OCR_REPEATED_PUNCT_RE = re.compile(r"[，。；：、！？,.;:!?]{2,}")
OCR_REPEATED_WORD_RE = re.compile(r"([一-龥]{2,8})\1+")
OCR_REPEATED_CHAR_RE = re.compile(r"([一-龥])\1{2,}")
OCR_ARTIFACT_RE = re.compile(r"[�□■]{1,}|[=*_~·•]{4,}|\[(?:OCR不清晰|WARN)\]")


def _scan_ocr_quality_warnings(text: str, filename: str, max_items: int = 8) -> List[str]:
    """仅报告本地 OCR 可疑片段，不自动删除或改写证据文本。"""
    warnings: List[str] = []
    seen = set()

    def add(kind: str, line_no: int, span: str) -> None:
        if not span or len(warnings) >= max_items:
            return
        key = (kind, line_no, span)
        if key in seen:
            return
        seen.add(key)
        warnings.append(f"{filename}: OCR可疑-{kind}（第{line_no}行：{span[:60]}）")

    for line_no, line in enumerate((text or "").splitlines(), start=1):
        if not line.strip():
            continue
        for match in OCR_ARTIFACT_RE.finditer(line):
            add("伪影/符号噪声", line_no, match.group(0))
        for match in OCR_REPEATED_PUNCT_RE.finditer(line):
            token = match.group(0)
            if token not in {"……", "——"}:
                add("重复标点", line_no, token)
        for regex, kind in [(OCR_REPEATED_WORD_RE, "重复词语"), (OCR_REPEATED_CHAR_RE, "单字异常重复")]:
            for match in regex.finditer(line):
                add(kind, line_no, match.group(0))
        if any(phrase in line for phrase in ["打印时间", "扫描时间", "本页无正文", "仅供参考"]):
            add("页眉页脚残留", line_no, line.strip())
        if len(warnings) >= max_items:
            break
    return warnings


def _clean_extracted_markdown(text: str) -> str:
    """清洗 OCR 文本中的明显无效格式，并保留医学/法律关键信息。"""
    lines = text.splitlines()
    if not lines:
        return ""

    # 统计短行出现频率，用于识别重复页眉页脚
    short_line_count = {}
    for raw in lines:
        s = re.sub(r"[ \t]+", " ", raw).strip()
        if 0 < len(s) <= 30:
            short_line_count[s] = short_line_count.get(s, 0) + 1

    cleaned: List[str] = []
    seen_count = {}
    prev = None
    blank_streak = 0

    for raw in lines:
        # 统一空白
        line = re.sub(r"[ \t]+", " ", raw).rstrip()
        s = line.strip()

        # 空行最多保留1行
        if not s:
            blank_streak += 1
            if blank_streak <= 1:
                cleaned.append("")
            continue
        blank_streak = 0

        # 删除常见噪声行
        if _is_noise_line(s):
            continue

        seen_count[s] = seen_count.get(s, 0) + 1

        # 删除高频短页眉页脚（保守规则：短行且高频，且从第2次出现开始剔除）
        if len(s) <= 30 and short_line_count.get(s, 0) >= 4 and seen_count[s] > 1:
            if re.search(r"(司法鉴定|鉴定中心|病案号|医院|报告单|检验科|影像|浙江大学|医学附属)", s):
                continue

        # 合并连续重复行
        if s == prev:
            continue

        cleaned.append(line)
        prev = s

    cleaned_text = "\n".join(cleaned).strip()
    return _remove_table_code_fences(cleaned_text)

def _runtime_config_from_state(state: AgentState) -> RuntimeConfig:
    raw = state.get("runtime_config")
    if isinstance(raw, RuntimeConfig):
        return raw
    if isinstance(raw, dict):
        return get_runtime_config(**raw)
    return get_runtime_config()


def _run_local_extraction(all_files: List[str], config: RuntimeConfig, text_only: bool = False) -> Tuple[List[str], List[str], List[str]]:
    extracted_texts: List[str] = []
    failed_files: List[str] = []
    warnings: List[str] = []
    apply_runtime_env(config)
    ocr = None

    if text_only:
        print(f"[*] 本地 text-only 模式：找到 {len(all_files)} 个文件，仅读取可直接提取文本的文件。")
    else:
        print(f"[*] 本地 OCR 模式：找到 {len(all_files)} 个文件，使用 {config.local_ocr_engine}/{config.local_ocr_det_model}+{config.local_ocr_rec_model}。")
        print(f"[*] OCR设备状态：{format_paddle_device_status(config)}")

    for idx, file_path in enumerate(all_files, start=1):
        fname = os.path.basename(file_path)
        print(f"[*] ({idx}/{len(all_files)}) 本地解析: {fname}")
        try:
            ext = os.path.splitext(file_path)[1].lower()
            # PDF 优先直接提取文本，只有扫描页才在 ocr_client 内部懒加载 PaddleOCR；
            # 图片文件才需要在这里初始化 OCR，避免可复制 PDF 也触发模型加载和 Paddle 日志。
            if not text_only and ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
                if ocr is None:
                    ocr = build_paddle_ocr(config)
                    status = getattr(ocr, "_forenagent_device_status", {}) or {}
                    init_seconds = float(getattr(ocr, "_forenagent_init_seconds", 0.0) or 0.0)
                    print(f"[*] PaddleOCR 已初始化：device={status.get('resolved', 'cpu')}，耗时 {init_seconds:.2f}s；{status.get('reason', '')}")
            file_started = time.perf_counter()
            local_text = extract_local_file_text(file_path, ocr=ocr, config=config, text_only=text_only)
            print(f"[*] ({idx}/{len(all_files)}) 解析耗时: {time.perf_counter() - file_started:.2f}s")
            if local_text.startswith("[WARN]"):
                warnings.append(f"{fname}: {local_text}")
            if not text_only and local_text and not local_text.startswith("[WARN]"):
                quality_score, quality_issues = score_text_quality(local_text)
                if quality_score < int(getattr(config, "local_ocr_second_pass_min_score", 70) or 70):
                    issue_text = "、".join(quality_issues) if quality_issues else "文本质量偏低"
                    warnings.append(f"{fname}: OCR质量评分 {quality_score}/100（{issue_text}），已启用可疑页/图片局部增强策略。")
            warnings.extend(_scan_ocr_quality_warnings(local_text, fname))
            cleaned_text = _clean_extracted_markdown(local_text)
            if cleaned_text and not cleaned_text.startswith("[WARN]"):
                extracted_texts.append(f"### 文件：{fname}\n\n{cleaned_text}\n\n---\n")
            else:
                failed_files.append(fname)
        except Exception as e:
            print(f"[X] 本地解析失败 {fname}: {e}")
            failed_files.append(fname)
            warnings.append(f"{fname}: {e}")

    return extracted_texts, failed_files, warnings


def _run_cloud_extraction(all_files: List[str], config: RuntimeConfig) -> Tuple[List[str], List[str], List[str]]:
    config.require_cloud_allowed("使用云端 OCR（LlamaParse）")
    if LlamaParse is None:
        raise RuntimeError("云端 OCR 需要安装 llama-parse。")
    if not os.getenv("LLAMA_CLOUD_API_KEY"):
        raise RuntimeError("云端 OCR（LlamaParse）未找到 LLAMA_CLOUD_API_KEY，请检查项目 .env。")

    print(f"[*] 找到 {len(all_files)} 个文件。正在调用 LlamaParse 进行识别（可能需要一些时间）...")
    parser = LlamaParse(
        result_type="markdown",
        verbose=False,
        language="ch_sim",
        num_workers=4,
        parsing_instruction=(
            "你是法医临床案件材料的OCR结构化提取器。"
            "任务是忠实提取原文，不推断、不总结、不补全、不改写。"
            "请严格保留时间、数值、姓名、诊断、手术、鉴定意见、误工期/护理期/营养期等关键信息。"
            "若文字不清晰，用[OCR不清晰]标记，不得臆测。"
            "请尽量忽略页眉、页脚、页码、打印时间、扫描底纹、纯装饰符号。"
            "每个文件独立提取，不要把不同文件的信息混写。"
            "若存在表格，请保留为Markdown表格。不要使用```markdown或```围栏包裹表格。"
        )
    )

    extracted_texts: List[str] = []
    failed_files: List[str] = []
    warnings: List[str] = []
    name_counts: Dict[str, int] = {}
    for fp in all_files:
        name = os.path.basename(fp)
        name_counts[name] = name_counts.get(name, 0) + 1

    batch_doc_map_by_path: Dict[str, List[str]] = {}
    batch_doc_map_by_name: Dict[str, List[str]] = {}
    batch_failed = False
    try:
        print("[*] 正在执行批量解析以提升速度...")
        batch_docs = parser.load_data(all_files)
        batch_doc_map_by_path, batch_doc_map_by_name = _build_batch_doc_map(batch_docs)
    except Exception as e:
        batch_failed = True
        print(f"[!] 批量解析失败，将回退为逐文件解析: {e}")
        warnings.append(f"批量解析失败: {e}")

    for idx, file_path in enumerate(all_files, start=1):
        fname = os.path.basename(file_path)
        print(f"[*] ({idx}/{len(all_files)}) 正在解析: {fname}")
        try:
            page_texts = []
            if not batch_failed:
                page_texts = _get_batch_page_texts(
                    file_path=file_path,
                    fname=fname,
                    by_path=batch_doc_map_by_path,
                    by_name=batch_doc_map_by_name,
                    name_counts=name_counts,
                )
            if not page_texts:
                fallback_docs = parser.load_data([file_path])
                page_texts = [doc.text for doc in fallback_docs if getattr(doc, "text", "")]
            cleaned_text = _clean_extracted_markdown("\n".join(page_texts))
            if cleaned_text:
                extracted_texts.append(f"### 文件：{fname}\n\n{cleaned_text}\n\n---\n")
            else:
                print(f"[!] 文件解析结果为空或仅含噪声: {fname}")
                failed_files.append(fname)
        except Exception as e:
            print(f"[X] 解析失败 {fname}: {e}")
            failed_files.append(fname)
            warnings.append(f"{fname}: {e}")

    return extracted_texts, failed_files, warnings


def extract_documents_node(state: AgentState) -> dict:
    """提取节点：按本地/云端 OCR provider 读取案件文件并转为 Markdown。"""
    input_folder = state["input_folder"]
    config = _runtime_config_from_state(state)
    scan_root = _normalize_folder_for_case_scan(input_folder)
    print(f"\n[*] 开始扫描案件目录: {scan_root}")
    print(f"[*] OCR provider: {config.ocr_provider}")

    all_files = _collect_case_files(scan_root)
    if not all_files:
        print("[!] 未找到支持的文件！")
        return {"extracted_texts": [], "final_markdown": "未找到支持的文件。", "warnings": ["未找到支持的文件。"]}

    if config.ocr_provider == "cloud":
        extracted_texts, failed_files, warnings = _run_cloud_extraction(all_files, config)
    elif config.ocr_provider == "text-only":
        extracted_texts, failed_files, warnings = _run_local_extraction(all_files, config, text_only=True)
    else:
        extracted_texts, failed_files, warnings = _run_local_extraction(all_files, config, text_only=False)

    if failed_files:
        print(f"[!] 共有 {len(failed_files)} 个文件解析失败或无有效文本。")
        for f in failed_files:
            print(f"    - {f}")

    return {
        "extracted_texts": extracted_texts,
        "total_files": len(all_files),
        "success_files": len(extracted_texts),
        "failed_files": len(failed_files),
        "warnings": warnings,
        "runtime_config": config,
    }

def format_output_node(state: AgentState) -> dict:
    """排版与保存节点：将所有片段组合后保存到本地"""
    extracted_texts = state.get("extracted_texts", [])
    output_path = state["output_path"]
    total_files = state.get("total_files", len(extracted_texts))
    success_files = state.get("success_files", len(extracted_texts))
    failed_files = state.get("failed_files", max(0, total_files - success_files))
    warnings = state.get("warnings", []) or []
    config = _runtime_config_from_state(state)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not extracted_texts:
        final_md = "未提取到任何文本内容。"
    else:
        final_md = "# 案件材料提取报告\n\n"
        final_md += "## 提取统计\n"
        final_md += f"- 提取文件总数：{total_files}\n"
        final_md += f"- 成功：{success_files}\n"
        final_md += f"- 失败：{failed_files}\n"
        final_md += f"- 提取时间：{now_str}\n"
        final_md += f"- OCR模式：{config.ocr_provider}\n"
        final_md += f"- OCR引擎：{config.local_ocr_engine if config.ocr_provider == 'local' else config.ocr_provider}\n"
        if config.ocr_provider == "local":
            final_md += f"- OCR设备：{format_paddle_device_status(config)}\n"
            final_md += f"- OCR局部增强：{'启用' if config.local_ocr_second_pass else '关闭'}；首次倍率 {config.local_ocr_render_scale}，二次倍率 {config.local_ocr_second_pass_scale}\n"
        final_md += f"- 云端调用：{'是' if config.ocr_provider == 'cloud' else '否'}\n"
        if warnings:
            final_md += "- 警告：\n"
            for item in warnings[:20]:
                final_md += f"  - {item}\n"
        final_md += "\n"
        final_md += "## OCR结果\n\n"
        final_md += "\n".join(extracted_texts).strip() + "\n"
    
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_md)
        print(f"[*] 提取完成！已聚合保存至: {output_path}")
    except Exception as e:
        print(f"[X] 保存文件时出现错误: {e}")
        
    return {"final_markdown": final_md}

class _SimpleExtractorGraph:
    """langgraph 不可用时的最小同步执行器。"""

    def invoke(self, initial_state: dict) -> dict:
        state = dict(initial_state)
        state.update(extract_documents_node(state))
        state.update(format_output_node(state))
        return state


def build_extractor_graph():
    if StateGraph is None:
        return _SimpleExtractorGraph()

    workflow = StateGraph(AgentState)
    workflow.add_node("extract", extract_documents_node)
    workflow.add_node("format_and_save", format_output_node)

    workflow.set_entry_point("extract")
    workflow.add_edge("extract", "format_and_save")
    workflow.add_edge("format_and_save", END)
    return workflow.compile()

if __name__ == "__main__":
    print("========== Extractor Agent 启动 ==========")
    
    # 使用 tkinter 创建一个不可见的主窗口
    root = tk.Tk()
    root.withdraw()
    
    # 强制让窗口置顶显示，防止被 VS Code 遮挡
    root.attributes('-topmost', True)
    
    # 弹出文件夹选择对话框（严格边界：仅扫描所选目录及其子目录）
    try:
        test_folder = filedialog.askdirectory(title="请选择一个证据材料文件夹（仅扫描该目录及其子目录）")
    except KeyboardInterrupt:
        print("\n[!] 已取消选择（检测到中断）。程序退出。")
        root.destroy()
        raise SystemExit(0)
    
    if not test_folder:
        print("您已取消选择。程序退出。")
    else:
        test_folder = os.path.normpath(test_folder)
        
        if not os.path.exists(test_folder):
            print(f"[X] 文件夹不存在: {test_folder}")
            messagebox.showerror("错误", "所选文件夹不存在！")
        else:
            print(f"[*] 已选择文件夹: {test_folder}")
            output_file = _resolve_output_path(test_folder)
            
            # 告诉用户正在处理 (非阻塞提示)
            print("[*] 开始解析，正在调用云端接口，请稍候...")
            
            app = build_extractor_graph()
            initial_state = {
                "input_folder": test_folder,
                "output_path": output_file
            }
            app.invoke(initial_state)
            
            # 解析完成后弹窗提示成功
            messagebox.showinfo(
                "提取完成", 
                f"文件已提取并转换为 Markdown 格式！\n\n保存路径：\n{output_file}"
            )

    root.destroy()
