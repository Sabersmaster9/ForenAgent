"""Shared output path helpers for ForenAgent case-numbered runs."""

from __future__ import annotations

import os
import re
from typing import Optional


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_TEST_RESULT_DIR = os.path.join(PROJECT_ROOT, "test_result")


def sanitize_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", str(name or "")).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "unnamed_case"


def extract_case_number(path_text: str) -> Optional[str]:
    """Extract a zero-padded case number from a database case path, e.g. `001 张三`."""
    if not path_text:
        return None
    parts = os.path.normpath(str(path_text)).split(os.sep)
    for part in reversed(parts):
        # 案件目录使用 001-181 三位编号；不要把 `1 鉴定材料` / `3 检查照片` 等通道目录误判为案件 001/003。
        match = re.match(r"^\s*(\d{3})(?:\D|$)", part)
        if match:
            return match.group(1)
    return None


def resolve_numbered_output_dir(
    input_path: str,
    output_dir: Optional[str] = None,
    fallback_name: Optional[str] = None,
) -> str:
    """Resolve output dir to `test_result/<case_number>` when a case number is present."""
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    case_number = extract_case_number(input_path)
    if case_number:
        target = os.path.join(DEFAULT_TEST_RESULT_DIR, case_number)
    else:
        name = fallback_name or os.path.basename(os.path.normpath(str(input_path))) or "unnamed_case"
        target = os.path.join(DEFAULT_TEST_RESULT_DIR, sanitize_folder_name(name))
    os.makedirs(target, exist_ok=True)
    return target
