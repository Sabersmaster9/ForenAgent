"""Local OCR helpers for PaddleOCR-based extraction."""

from __future__ import annotations

import contextlib
import io
import os
import re
import tempfile
import time
import warnings
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

try:
    from PIL import Image
except ImportError:  # Pillow is optional for PDF rendering fallbacks.
    Image = None

from agents.common.runtime_config import RuntimeConfig, get_runtime_config


_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

_OCR_REPEATED_PUNCT_RE = re.compile(r"[，。；：、！？,.;:!?]{2,}")
_OCR_REPEATED_WORD_RE = re.compile(r"([一-龥]{2,8})\1+")
_OCR_REPEATED_CHAR_RE = re.compile(r"([一-龥])\1{2,}")
_OCR_ARTIFACT_RE = re.compile(r"[�□■]{1,}|[=*_~·•]{4,}|\[(?:OCR不清晰|WARN)\]")
_OCR_KEY_NUMBER_RE = re.compile(
    r"(?:\d{4}\s*(?:年|[./-])\s*\d{1,2}\s*(?:月|[./-])\s*\d{1,2}|(?:\d[ -]?){17}[0-9Xx]|"
    r"(?:病案号|病历号|住院号|门诊号|检查号|报告号|案号)\s*[:：]?\s*\S{2,})"
)
_OCR_MEDICAL_KEYWORDS = (
    "医院", "门诊", "住院", "病案", "病历", "检查", "诊断", "影像", "CT", "MRI", "DR", "X线",
    "司法鉴定", "被鉴定人", "身份证", "公民身份号码", "伤残", "误工", "护理", "营养",
)


def score_text_quality(text: str) -> Tuple[int, List[str]]:
    """给本地 PDF/OCR 文本打保守质量分，用于触发疑难页二次识别。"""
    raw = text or ""
    compact = re.sub(r"\s+", "", raw)
    if not compact:
        return 0, ["空文本"]

    score = 100
    issues: List[str] = []
    compact_len = len(compact)
    chinese_count = len(re.findall(r"[一-龥]", compact))
    artifact_count = len(_OCR_ARTIFACT_RE.findall(raw))
    repeated_punct_count = sum(1 for m in _OCR_REPEATED_PUNCT_RE.finditer(raw) if m.group(0) not in {"……", "——"})
    repeated_word_count = len(_OCR_REPEATED_WORD_RE.findall(raw)) + len(_OCR_REPEATED_CHAR_RE.findall(raw))
    symbol_count = len(re.findall(r"[^\w\s一-龥，。；：、！？（）《》【】,.:%°/\\-]", raw))

    if compact_len < 10:
        score -= 65
        issues.append("文本极短")
    elif compact_len < 30:
        score -= 35
        issues.append("文本偏短")
    elif compact_len < 80:
        score -= 12
        issues.append("文本较少")

    if chinese_count == 0 and compact_len >= 20:
        score -= 25
        issues.append("缺少中文正文")
    elif chinese_count / max(compact_len, 1) < 0.25 and compact_len >= 40:
        score -= 12
        issues.append("中文占比偏低")

    if artifact_count:
        score -= min(35, artifact_count * 8)
        issues.append("OCR伪影")
    if repeated_punct_count:
        score -= min(25, repeated_punct_count * 5)
        issues.append("重复标点")
    if repeated_word_count:
        score -= min(25, repeated_word_count * 6)
        issues.append("重复字词")
    if symbol_count / max(len(raw), 1) > 0.08:
        score -= 15
        issues.append("符号噪声偏多")

    if compact_len >= 80 and not _OCR_KEY_NUMBER_RE.search(raw) and not any(k in raw for k in _OCR_MEDICAL_KEYWORDS):
        score -= 8
        issues.append("缺少常见医学/司法关键词")

    score = max(0, min(100, score))
    return score, issues[:6]


def _is_low_quality_text(text: str, min_score: int) -> bool:
    score, issues = score_text_quality(text)
    compact_len = len(re.sub(r"\s+", "", text or ""))
    if compact_len < 20:
        return True
    severe = any(issue in {"OCR伪影", "重复标点", "重复字词", "符号噪声偏多"} for issue in issues)
    return score < min_score and (compact_len < 100 or severe)


def _better_text_candidate(candidates: List[Tuple[str, str]]) -> Tuple[str, str, int, List[str]]:
    best_label = ""
    best_text = ""
    best_score = -1
    best_issues: List[str] = []
    for label, text in candidates:
        if not str(text or "").strip():
            continue
        score, issues = score_text_quality(text)
        compact_len = len(re.sub(r"\s+", "", text or ""))
        best_len = len(re.sub(r"\s+", "", best_text or ""))
        if score > best_score or (score == best_score and compact_len > best_len):
            best_label = label
            best_text = text
            best_score = score
            best_issues = issues
    return best_label, best_text, best_score, best_issues


def configure_paddle_env(config: Optional[RuntimeConfig] = None) -> RuntimeConfig:
    cfg = config or get_runtime_config()
    for key, value in {
        "PADDLE_PDX_CACHE_HOME": cfg.paddle_pdx_cache_home,
        "PADDLEOCR_HOME": cfg.paddleocr_home,
        "PADDLE_HOME": cfg.paddle_home,
        "XDG_CACHE_HOME": cfg.xdg_cache_home,
    }.items():
        if value:
            os.environ[key] = value
    return cfg


def _preload_torch_before_paddle() -> None:
    """On Windows, importing Paddle GPU before torch can break torch DLL loading via PaddleOCR/modelscope."""
    try:
        import torch  # noqa: F401
    except Exception:
        # PaddleOCR can still run without using torch directly; keep OCR startup resilient.
        pass


def get_paddle_device_status(config: Optional[RuntimeConfig] = None) -> dict:
    """Return conservative Paddle/CUDA status for logging and OCR device selection."""
    cfg = configure_paddle_env(config)
    _preload_torch_before_paddle()
    requested = getattr(cfg, "local_ocr_device", "auto") or "auto"
    status = {
        "requested": requested,
        "resolved": "cpu",
        "paddle_device": "cpu",
        "cuda_available": False,
        "gpu_count": 0,
        "gpu_name": "",
        "reason": "未检测到 GPU 版 Paddle 或 CUDA。",
    }
    if requested == "cpu":
        status["reason"] = "已按配置使用 CPU。"
        return status

    try:
        import paddle
        compiled = bool(getattr(paddle.device, "is_compiled_with_cuda", lambda: False)())
        count = 0
        if compiled:
            try:
                count = int(paddle.device.cuda.device_count())
            except Exception:
                count = 1
        status["cuda_available"] = bool(compiled and count > 0)
        status["gpu_count"] = max(0, count)
        if status["cuda_available"]:
            try:
                status["gpu_name"] = str(paddle.device.cuda.get_device_name(0))
            except Exception:
                status["gpu_name"] = "CUDA GPU"
            status["resolved"] = "gpu"
            status["paddle_device"] = "gpu:0"
            status["reason"] = "已检测到 GPU 版 Paddle/CUDA。"
        elif requested == "gpu":
            status["reason"] = "配置为 GPU，但当前 Paddle 未检测到可用 CUDA，回退本地 CPU。"
        else:
            status["reason"] = "auto 模式未检测到可用 CUDA，使用本地 CPU。"
    except Exception as exc:
        status["reason"] = f"Paddle/CUDA 状态检测失败，使用本地 CPU：{exc}"
    return status


def format_paddle_device_status(config: Optional[RuntimeConfig] = None) -> str:
    status = get_paddle_device_status(config)
    gpu = f"，GPU={status.get('gpu_name')}" if status.get("gpu_name") else ""
    return (
        f"requested={status.get('requested')}，resolved={status.get('resolved')}，"
        f"cuda_available={str(status.get('cuda_available')).lower()}，"
        f"gpu_count={status.get('gpu_count')}{gpu}；{status.get('reason')}"
    )


@contextlib.contextmanager
def _quiet_where_ccache_message() -> Iterator[None]:
    """Hide Windows `where ccache` noise while keeping real OCR errors visible."""
    old_path = os.environ.get("PATH", "")
    with tempfile.TemporaryDirectory() as tmp_dir:
        shim = os.path.join(tmp_dir, "where.bat")
        with open(shim, "w", encoding="utf-8") as f:
            f.write("@echo off\r\nexit /b 1\r\n")
        os.environ["PATH"] = tmp_dir + os.pathsep + old_path
        try:
            yield
        finally:
            os.environ["PATH"] = old_path


@contextlib.contextmanager
def _quiet_native_output() -> Iterator[None]:
    """Suppress Paddle's native/subprocess startup noise during local OCR calls."""
    with open(os.devnull, "w") as devnull:
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        try:
            os.dup2(devnull.fileno(), 1)
            os.dup2(devnull.fileno(), 2)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                yield
        finally:
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)


def _build_paddle_ocr_for_device(cfg: RuntimeConfig, device: str):
    _preload_torch_before_paddle()
    from paddleocr import PaddleOCR

    kwargs = {
        "text_detection_model_name": cfg.local_ocr_det_model,
        "text_recognition_model_name": cfg.local_ocr_rec_model,
        "use_doc_orientation_classify": cfg.local_ocr_use_doc_orientation,
        "use_doc_unwarping": cfg.local_ocr_use_doc_unwarping,
        "use_textline_orientation": cfg.local_ocr_use_textline_orientation,
        "device": device,
    }
    if device == "cpu":
        kwargs.update({
            "enable_mkldnn": cfg.local_ocr_enable_mkldnn,
            "cpu_threads": cfg.local_ocr_cpu_threads,
        })
    return PaddleOCR(**kwargs)


def build_paddle_ocr(config: Optional[RuntimeConfig] = None):
    cfg = configure_paddle_env(config)
    warnings.filterwarnings(
        "ignore",
        message="No ccache found.*",
        category=UserWarning,
        module="paddle.utils.cpp_extension.extension_utils",
    )
    status = get_paddle_device_status(cfg)
    requested_device = str(status.get("paddle_device") or "cpu")
    started = time.perf_counter()
    with _quiet_where_ccache_message(), _quiet_native_output():
        try:
            engine = _build_paddle_ocr_for_device(cfg, requested_device)
            setattr(engine, "_forenagent_device_status", status)
            setattr(engine, "_forenagent_init_seconds", time.perf_counter() - started)
            return engine
        except Exception as exc:
            if requested_device == "cpu":
                raise
            fallback_status = dict(status)
            fallback_status.update({
                "resolved": "cpu",
                "paddle_device": "cpu",
                "reason": f"GPU OCR 初始化失败，已回退本地 CPU：{exc}",
            })
            engine = _build_paddle_ocr_for_device(cfg, "cpu")
            setattr(engine, "_forenagent_device_status", fallback_status)
            setattr(engine, "_forenagent_init_seconds", time.perf_counter() - started)
            return engine


def extract_docx_text(file_path: str) -> str:
    parts: List[str] = []
    namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    try:
        with zipfile.ZipFile(file_path) as zf:
            xml_names = ["word/document.xml"]
            xml_names += sorted(n for n in zf.namelist() if n.startswith("word/header") and n.endswith(".xml"))
            xml_names += sorted(n for n in zf.namelist() if n.startswith("word/footer") and n.endswith(".xml"))
            for name in xml_names:
                if name not in zf.namelist():
                    continue
                root = ET.fromstring(zf.read(name))
                texts = [node.text for node in root.findall(".//w:t", namespaces) if node.text]
                if texts:
                    parts.append("".join(texts))
    except Exception:
        return ""
    return "\n".join(parts).strip()


def read_text_file(file_path: str) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            with open(file_path, "r", encoding=encoding, errors="strict") as f:
                return f.read()
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _ocr_result_to_text(result) -> str:
    texts: List[str] = []
    pages = result if isinstance(result, list) else [result]
    for page in pages:
        if isinstance(page, dict):
            data = page
        else:
            data = None
            if hasattr(page, "json"):
                try:
                    data = page.json
                except Exception:
                    data = None
            if not isinstance(data, dict) and hasattr(page, "get"):
                data = page

        if isinstance(data, dict):
            res = data.get("res") if isinstance(data.get("res"), dict) else data
            rec_texts = res.get("rec_texts") or res.get("texts") or []
            texts.extend(str(t) for t in rec_texts if str(t).strip())
            continue

        if isinstance(page, (list, tuple)):
            for item in page:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    value = item[1]
                    if isinstance(value, (list, tuple)) and value:
                        texts.append(str(value[0]))
                    elif isinstance(value, str):
                        texts.append(value)
    return "\n".join(t for t in texts if t.strip()).strip()


def ocr_image_file(file_path: str, ocr=None, config: Optional[RuntimeConfig] = None, allow_second_pass: bool = True) -> str:
    cfg = config or get_runtime_config()
    engine = ocr or build_paddle_ocr(cfg)
    with _quiet_where_ccache_message(), _quiet_native_output():
        result = engine.predict(file_path)
    text = _ocr_result_to_text(result)
    min_score = int(getattr(cfg, "local_ocr_second_pass_min_score", 70) or 70)
    if not allow_second_pass or not getattr(cfg, "local_ocr_second_pass", True) or Image is None or not _is_low_quality_text(text, min_score):
        return text

    first_scale = float(getattr(cfg, "local_ocr_render_scale", 1.5) or 1.5)
    second_scale = float(getattr(cfg, "local_ocr_second_pass_scale", 2.5) or 2.5)
    resize_factor = max(1.2, min(2.5, second_scale / max(first_scale, 0.1)))
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        with Image.open(file_path) as image:
            width, height = image.size
            enlarged = image.resize((max(1, int(width * resize_factor)), max(1, int(height * resize_factor))))
            enlarged.save(tmp_path)
        with _quiet_where_ccache_message(), _quiet_native_output():
            second_result = engine.predict(tmp_path)
        second_text = _ocr_result_to_text(second_result)
        _label, best_text, _score, _issues = _better_text_candidate([("原图OCR", text), ("图像放大OCR", second_text)])
        return best_text or text
    except Exception:
        return text
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _render_pymupdf_page_to_ocr(page, scale: float, engine, cfg: RuntimeConfig) -> str:
    import fitz

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        pix.save(tmp_path)
        return ocr_image_file(tmp_path, ocr=engine, config=cfg, allow_second_pass=False)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _render_pdfium_page_to_ocr(page, scale: float, engine, cfg: RuntimeConfig) -> str:
    if Image is None:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        image.save(tmp_path)
        return ocr_image_file(tmp_path, ocr=engine, config=cfg, allow_second_pass=False)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _select_pdf_page_text(page_label: str, direct_text: str, ocr_text: str, second_text: str = "") -> Tuple[str, str, int, List[str]]:
    candidates: List[Tuple[str, str]] = []
    if direct_text.strip():
        candidates.append((page_label, direct_text.strip()))
    if ocr_text.strip():
        candidates.append((f"{page_label} OCR", ocr_text.strip()))
    if second_text.strip():
        candidates.append((f"{page_label} OCR二次", second_text.strip()))
    return _better_text_candidate(candidates)


def _extract_pdf_text_with_pymupdf(file_path: str, ocr=None, config: Optional[RuntimeConfig] = None) -> str:
    import fitz  # PyMuPDF

    cfg = config or get_runtime_config()
    engine = ocr
    parts: List[str] = []
    doc = fitz.open(file_path)
    try:
        max_pages = int(os.getenv("FORENAGENT_LOCAL_OCR_MAX_PAGES", "0") or 0)
        page_count = len(doc) if max_pages <= 0 else min(len(doc), max_pages)
        for page_index in range(page_count):
            page = doc[page_index]
            text = (page.get_text("text") or "").strip()
            page_label = f"第{page_index + 1}页"
            min_score = int(getattr(cfg, "local_ocr_second_pass_min_score", 70) or 70)
            direct_is_good = len(re.sub(r"\s+", "", text)) >= 80 and not _is_low_quality_text(text, min_score)
            if direct_is_good:
                parts.append(f"[{page_label}]\n{text}")
                continue
            if engine is None:
                engine = build_paddle_ocr(cfg)
            ocr_text = _render_pymupdf_page_to_ocr(page, float(getattr(cfg, "local_ocr_render_scale", 1.5) or 1.5), engine, cfg)
            second_text = ""
            if getattr(cfg, "local_ocr_second_pass", True) and _is_low_quality_text(ocr_text or text, min_score):
                second_scale = float(getattr(cfg, "local_ocr_second_pass_scale", 2.5) or 2.5)
                first_scale = float(getattr(cfg, "local_ocr_render_scale", 1.5) or 1.5)
                if second_scale > first_scale:
                    second_text = _render_pymupdf_page_to_ocr(page, second_scale, engine, cfg)
            label, best_text, score, issues = _select_pdf_page_text(page_label, text, ocr_text, second_text)
            if best_text:
                parts.append(f"[{label}]\n{best_text}")
    finally:
        doc.close()
    return "\n\n".join(parts).strip()


def _extract_pdf_text_with_pdfium(file_path: str, ocr=None, config: Optional[RuntimeConfig] = None) -> str:
    import pypdfium2 as pdfium

    cfg = config or get_runtime_config()
    engine = ocr
    parts: List[str] = []
    doc = pdfium.PdfDocument(file_path)
    max_pages = int(os.getenv("FORENAGENT_LOCAL_OCR_MAX_PAGES", "0") or 0)
    page_count = len(doc) if max_pages <= 0 else min(len(doc), max_pages)
    for page_index in range(page_count):
        page = doc[page_index]
        text = ""
        page_label = f"第{page_index + 1}页"
        min_score = int(getattr(cfg, "local_ocr_second_pass_min_score", 70) or 70)
        try:
            textpage = page.get_textpage()
            text = (textpage.get_text_range() or "").strip()
            textpage.close()
        except Exception:
            text = ""
        direct_is_good = len(re.sub(r"\s+", "", text)) >= 80 and not _is_low_quality_text(text, min_score)
        if direct_is_good:
            parts.append(f"[{page_label}]\n{text}")
            page.close()
            continue
        if Image is None:
            if text.strip():
                parts.append(f"[{page_label}]\n{text}")
            page.close()
            continue
        if engine is None:
            engine = build_paddle_ocr(cfg)
        try:
            ocr_text = _render_pdfium_page_to_ocr(page, float(getattr(cfg, "local_ocr_render_scale", 1.5) or 1.5), engine, cfg)
            second_text = ""
            if getattr(cfg, "local_ocr_second_pass", True) and _is_low_quality_text(ocr_text or text, min_score):
                second_scale = float(getattr(cfg, "local_ocr_second_pass_scale", 2.5) or 2.5)
                first_scale = float(getattr(cfg, "local_ocr_render_scale", 1.5) or 1.5)
                if second_scale > first_scale:
                    second_text = _render_pdfium_page_to_ocr(page, second_scale, engine, cfg)
            label, best_text, _score, _issues = _select_pdf_page_text(page_label, text, ocr_text, second_text)
            if best_text:
                parts.append(f"[{label}]\n{best_text}")
        finally:
            page.close()
    doc.close()
    return "\n\n".join(parts).strip()


def _extract_pdf_direct_text_with_pymupdf(file_path: str, config: Optional[RuntimeConfig] = None) -> str:
    import fitz

    cfg = config or get_runtime_config()
    parts: List[str] = []
    doc = fitz.open(file_path)
    try:
        max_pages = int(os.getenv("FORENAGENT_LOCAL_OCR_MAX_PAGES", "0") or 0)
        page_count = len(doc) if max_pages <= 0 else min(len(doc), max_pages)
        min_score = int(getattr(cfg, "local_ocr_second_pass_min_score", 70) or 70)
        for page_index in range(page_count):
            text = (doc[page_index].get_text("text") or "").strip()
            compact_len = len(re.sub(r"\s+", "", text))
            if compact_len >= 30 and not _is_low_quality_text(text, min_score):
                parts.append(f"[第{page_index + 1}页]\n{text}")
    finally:
        doc.close()
    return "\n\n".join(parts).strip()


def _extract_pdf_direct_text_with_pdfium(file_path: str, config: Optional[RuntimeConfig] = None) -> str:
    import pypdfium2 as pdfium

    cfg = config or get_runtime_config()
    parts: List[str] = []
    doc = pdfium.PdfDocument(file_path)
    try:
        max_pages = int(os.getenv("FORENAGENT_LOCAL_OCR_MAX_PAGES", "0") or 0)
        page_count = len(doc) if max_pages <= 0 else min(len(doc), max_pages)
        min_score = int(getattr(cfg, "local_ocr_second_pass_min_score", 70) or 70)
        for page_index in range(page_count):
            page = doc[page_index]
            try:
                textpage = page.get_textpage()
                text = (textpage.get_text_range() or "").strip()
                textpage.close()
                compact_len = len(re.sub(r"\s+", "", text))
                if compact_len >= 30 and not _is_low_quality_text(text, min_score):
                    parts.append(f"[第{page_index + 1}页]\n{text}")
            finally:
                page.close()
    finally:
        doc.close()
    return "\n\n".join(parts).strip()


def extract_pdf_direct_text(file_path: str, config: Optional[RuntimeConfig] = None) -> str:
    errors: List[str] = []
    try:
        text = _extract_pdf_direct_text_with_pymupdf(file_path, config=config)
        if text.strip():
            return text
    except ImportError as exc:
        errors.append(f"PyMuPDF不可用: {exc}")
    except Exception as exc:
        errors.append(f"PyMuPDF直抽失败: {exc}")

    try:
        text = _extract_pdf_direct_text_with_pdfium(file_path, config=config)
        if text.strip():
            return text
    except ImportError as exc:
        errors.append(f"pypdfium2不可用: {exc}")
    except Exception as exc:
        errors.append(f"pypdfium2直抽失败: {exc}")
    return ""


def extract_pdf_text(file_path: str, ocr=None, config: Optional[RuntimeConfig] = None) -> str:
    errors: List[str] = []
    try:
        return _extract_pdf_text_with_pymupdf(file_path, ocr=ocr, config=config)
    except ImportError as exc:
        errors.append(f"PyMuPDF不可用: {exc}")
    except Exception as exc:
        errors.append(f"PyMuPDF解析失败: {exc}")

    try:
        return _extract_pdf_text_with_pdfium(file_path, ocr=ocr, config=config)
    except ImportError as exc:
        errors.append(f"pypdfium2不可用: {exc}")
    except Exception as exc:
        errors.append(f"pypdfium2解析失败: {exc}")

    return "[WARN] 本地 PDF 解析失败；" + "；".join(errors)


def extract_local_file_text(file_path: str, ocr=None, config: Optional[RuntimeConfig] = None, text_only: bool = False) -> str:
    ext = Path(file_path).suffix.lower()
    if ext in _TEXT_EXTENSIONS:
        return read_text_file(file_path)
    if ext == ".docx":
        return extract_docx_text(file_path)
    if ext == ".doc":
        return "[WARN] 本地模式暂不直接解析旧版 .doc，请先转为 .docx 或 PDF。"
    if text_only:
        if ext == ".pdf":
            return extract_pdf_direct_text(file_path, config=config)
        return ""
    if ext == ".pdf":
        return extract_pdf_text(file_path, ocr=ocr, config=config)
    if ext in _IMAGE_EXTENSIONS:
        return ocr_image_file(file_path, ocr=ocr, config=config)
    return ""
