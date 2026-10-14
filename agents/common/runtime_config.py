"""Runtime provider configuration for local-first ForenAgent runs."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Dict, Optional


LOCAL_LLM_BASE_URL = "http://127.0.0.1:11434/v1"
LOCAL_CHAT_MODEL = "qwen3.5:4b"
LOCAL_AUDIT_MODEL = ""
CLOUD_LLM_BASE_URL = "https://api.siliconflow.cn/v1"
CLOUD_CHAT_MODEL = "deepseek-ai/DeepSeek-V3.2"
LOCAL_OCR_ENGINE = "paddleocr"
LOCAL_OCR_DET_MODEL = "PP-OCRv5_mobile_det"
LOCAL_OCR_REC_MODEL = "PP-OCRv5_mobile_rec"
LOCAL_OCR_RENDER_SCALE = 1.5
LOCAL_OCR_SECOND_PASS_SCALE = 2.5

# 模型/缓存根目录。默认放在程序目录下的 models/cache/，这样整个项目拷到
# 另一台机器（离线部署）时缓存跟着走，不必在新机器上重新下载。
# 仍可用 FORENAGENT_MODEL_CACHE_DIR 或各自的 PADDLE*_HOME 环境变量覆盖。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODEL_CACHE_ROOT = os.getenv("FORENAGENT_MODEL_CACHE_DIR") or os.path.join(_PROJECT_ROOT, "models", "cache")
DEFAULT_PADDLE_PDX_CACHE_HOME = os.path.join(MODEL_CACHE_ROOT, "paddlex")
DEFAULT_PADDLEOCR_HOME = os.path.join(MODEL_CACHE_ROOT, "paddleocr")
DEFAULT_PADDLE_HOME = os.path.join(MODEL_CACHE_ROOT, "paddle")
DEFAULT_XDG_CACHE_HOME = os.path.join(MODEL_CACHE_ROOT, "xdg")


_TRUE_VALUES = {"1", "true", "yes", "y", "on", "是", "允许"}


def _norm_provider(value: Optional[str], allowed: set[str], default: str) -> str:
    text = (value or default).strip().lower().replace("_", "-")
    aliases = {
        "rule": "off",
        "rules": "off",
        "none": "off",
        "text": "text-only",
        "text_only": "text-only",
        "textonly": "text-only",
    }
    text = aliases.get(text, text)
    return text if text in allowed else default


def _norm_device(value: Optional[str], default: str = "auto") -> str:
    text = (value or default).strip().lower().replace("_", "-")
    aliases = {
        "cuda": "gpu",
        "gpu0": "gpu",
        "gpu:0": "gpu",
        "nvidia": "gpu",
        "default": "auto",
    }
    text = aliases.get(text, text)
    return text if text in {"auto", "gpu", "cpu"} else default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in _TRUE_VALUES


@dataclass(frozen=True)
class RuntimeConfig:
    llm_provider: str = "local"
    ocr_provider: str = "local"
    allow_cloud: bool = False
    local_llm_base_url: str = LOCAL_LLM_BASE_URL
    local_llm_api_key: str = "ollama"
    local_chat_model: str = LOCAL_CHAT_MODEL
    local_audit_model: str = LOCAL_AUDIT_MODEL
    cloud_llm_base_url: str = CLOUD_LLM_BASE_URL
    cloud_chat_model: str = CLOUD_CHAT_MODEL
    local_llm_timeout: int = 240
    local_llm_max_tokens: int = 1800
    local_llm_context_chars: int = 9000
    local_llm_device: str = "gpu"
    local_ocr_engine: str = LOCAL_OCR_ENGINE
    local_ocr_device: str = "gpu"
    local_ocr_det_model: str = LOCAL_OCR_DET_MODEL
    local_ocr_rec_model: str = LOCAL_OCR_REC_MODEL
    local_ocr_cpu_threads: int = 2
    local_ocr_enable_mkldnn: bool = False
    local_ocr_render_scale: float = LOCAL_OCR_RENDER_SCALE
    local_ocr_second_pass: bool = True
    local_ocr_second_pass_scale: float = LOCAL_OCR_SECOND_PASS_SCALE
    local_ocr_second_pass_min_score: int = 70
    local_ocr_use_doc_orientation: bool = False
    local_ocr_use_doc_unwarping: bool = False
    local_ocr_use_textline_orientation: bool = False
    paddle_pdx_cache_home: str = DEFAULT_PADDLE_PDX_CACHE_HOME
    paddleocr_home: str = DEFAULT_PADDLEOCR_HOME
    paddle_home: str = DEFAULT_PADDLE_HOME
    xdg_cache_home: str = DEFAULT_XDG_CACHE_HOME

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        return cls(
            llm_provider=_norm_provider(os.getenv("FORENAGENT_LLM_PROVIDER"), {"local", "cloud", "off"}, "local"),
            ocr_provider=_norm_provider(os.getenv("FORENAGENT_OCR_PROVIDER"), {"local", "cloud", "text-only"}, "local"),
            allow_cloud=env_bool("FORENAGENT_ALLOW_CLOUD", False),
            local_llm_base_url=os.getenv("FORENAGENT_LOCAL_LLM_BASE_URL", LOCAL_LLM_BASE_URL),
            local_llm_api_key=os.getenv("FORENAGENT_LOCAL_LLM_API_KEY", "ollama"),
            local_chat_model=os.getenv("FORENAGENT_LOCAL_CHAT_MODEL", LOCAL_CHAT_MODEL),
            local_audit_model=os.getenv("FORENAGENT_LOCAL_AUDIT_MODEL", LOCAL_AUDIT_MODEL),
            cloud_llm_base_url=os.getenv("SILICONFLOW_BASE_URL", CLOUD_LLM_BASE_URL),
            cloud_chat_model=os.getenv("SILICONFLOW_CHAT_MODEL", CLOUD_CHAT_MODEL),
            local_llm_timeout=int(os.getenv("FORENAGENT_LOCAL_LLM_TIMEOUT", "240") or 240),
            local_llm_max_tokens=int(os.getenv("FORENAGENT_LOCAL_LLM_MAX_TOKENS", "1800") or 1800),
            local_llm_context_chars=int(os.getenv("FORENAGENT_LOCAL_LLM_CONTEXT_CHARS", "9000") or 9000),
            local_llm_device=_norm_device(os.getenv("FORENAGENT_LOCAL_LLM_DEVICE"), "gpu"),
            local_ocr_engine=os.getenv("FORENAGENT_LOCAL_OCR_ENGINE", LOCAL_OCR_ENGINE),
            local_ocr_device=_norm_device(os.getenv("FORENAGENT_LOCAL_OCR_DEVICE"), "gpu"),
            local_ocr_det_model=os.getenv("FORENAGENT_LOCAL_OCR_DET_MODEL", LOCAL_OCR_DET_MODEL),
            local_ocr_rec_model=os.getenv("FORENAGENT_LOCAL_OCR_REC_MODEL", LOCAL_OCR_REC_MODEL),
            local_ocr_cpu_threads=int(os.getenv("FORENAGENT_LOCAL_OCR_CPU_THREADS", "2") or 2),
            local_ocr_enable_mkldnn=env_bool("FORENAGENT_LOCAL_OCR_ENABLE_MKLDNN", False),
            local_ocr_render_scale=float(os.getenv("FORENAGENT_LOCAL_OCR_RENDER_SCALE", str(LOCAL_OCR_RENDER_SCALE)) or LOCAL_OCR_RENDER_SCALE),
            local_ocr_second_pass=env_bool("FORENAGENT_LOCAL_OCR_SECOND_PASS", True),
            local_ocr_second_pass_scale=float(os.getenv("FORENAGENT_LOCAL_OCR_SECOND_PASS_SCALE", str(LOCAL_OCR_SECOND_PASS_SCALE)) or LOCAL_OCR_SECOND_PASS_SCALE),
            local_ocr_second_pass_min_score=int(os.getenv("FORENAGENT_LOCAL_OCR_SECOND_PASS_MIN_SCORE", "70") or 70),
            local_ocr_use_doc_orientation=env_bool("FORENAGENT_LOCAL_OCR_USE_DOC_ORIENTATION", False),
            local_ocr_use_doc_unwarping=env_bool("FORENAGENT_LOCAL_OCR_USE_DOC_UNWARPING", False),
            local_ocr_use_textline_orientation=env_bool("FORENAGENT_LOCAL_OCR_USE_TEXTLINE_ORIENTATION", False),
            paddle_pdx_cache_home=os.getenv("PADDLE_PDX_CACHE_HOME", DEFAULT_PADDLE_PDX_CACHE_HOME),
            paddleocr_home=os.getenv("PADDLEOCR_HOME", DEFAULT_PADDLEOCR_HOME),
            paddle_home=os.getenv("PADDLE_HOME", DEFAULT_PADDLE_HOME),
            xdg_cache_home=os.getenv("XDG_CACHE_HOME", DEFAULT_XDG_CACHE_HOME),
        )

    def with_overrides(self, **kwargs) -> "RuntimeConfig":
        clean = {k: v for k, v in kwargs.items() if v is not None}
        if "llm_provider" in clean:
            clean["llm_provider"] = _norm_provider(clean["llm_provider"], {"local", "cloud", "off"}, self.llm_provider)
        if "ocr_provider" in clean:
            clean["ocr_provider"] = _norm_provider(clean["ocr_provider"], {"local", "cloud", "text-only"}, self.ocr_provider)
        if "local_llm_device" in clean:
            clean["local_llm_device"] = _norm_device(clean["local_llm_device"], self.local_llm_device)
        if "local_ocr_device" in clean:
            clean["local_ocr_device"] = _norm_device(clean["local_ocr_device"], self.local_ocr_device)
        return replace(self, **clean)

    @property
    def llm_model(self) -> str:
        return self.local_chat_model if self.llm_provider == "local" else self.cloud_chat_model

    @property
    def audit_llm_model(self) -> str:
        if self.llm_provider == "local" and self.local_audit_model.strip():
            return self.local_audit_model.strip()
        return self.llm_model

    @property
    def llm_base_url(self) -> str:
        return self.local_llm_base_url if self.llm_provider == "local" else self.cloud_llm_base_url

    @property
    def llm_api_key(self) -> str:
        if self.llm_provider == "local":
            return self.local_llm_api_key or "ollama"
        return os.getenv("SILICONFLOW_API_KEY", "")

    def require_cloud_allowed(self, purpose: str) -> None:
        if not self.allow_cloud:
            raise RuntimeError(
                f"当前默认禁止将司法材料发送云端；如需{purpose}，请在 GUI 模型调用页选择云端并确认，"
                "或 CLI 添加 --allow-cloud。"
            )

    def to_env(self) -> Dict[str, str]:
        return {
            "FORENAGENT_LLM_PROVIDER": self.llm_provider,
            "FORENAGENT_OCR_PROVIDER": self.ocr_provider,
            "FORENAGENT_ALLOW_CLOUD": "1" if self.allow_cloud else "0",
            "FORENAGENT_LOCAL_LLM_BASE_URL": self.local_llm_base_url,
            "FORENAGENT_LOCAL_LLM_API_KEY": self.local_llm_api_key,
            "FORENAGENT_LOCAL_CHAT_MODEL": self.local_chat_model,
            "FORENAGENT_LOCAL_AUDIT_MODEL": self.local_audit_model,
            "SILICONFLOW_BASE_URL": self.cloud_llm_base_url,
            "SILICONFLOW_CHAT_MODEL": self.cloud_chat_model,
            "FORENAGENT_LOCAL_LLM_TIMEOUT": str(self.local_llm_timeout),
            "FORENAGENT_LOCAL_LLM_MAX_TOKENS": str(self.local_llm_max_tokens),
            "FORENAGENT_LOCAL_LLM_CONTEXT_CHARS": str(self.local_llm_context_chars),
            "FORENAGENT_LOCAL_LLM_DEVICE": self.local_llm_device,
            "FORENAGENT_LOCAL_OCR_ENGINE": self.local_ocr_engine,
            "FORENAGENT_LOCAL_OCR_DEVICE": self.local_ocr_device,
            "FORENAGENT_LOCAL_OCR_DET_MODEL": self.local_ocr_det_model,
            "FORENAGENT_LOCAL_OCR_REC_MODEL": self.local_ocr_rec_model,
            "FORENAGENT_LOCAL_OCR_CPU_THREADS": str(self.local_ocr_cpu_threads),
            "FORENAGENT_LOCAL_OCR_ENABLE_MKLDNN": "1" if self.local_ocr_enable_mkldnn else "0",
            "FORENAGENT_LOCAL_OCR_RENDER_SCALE": str(self.local_ocr_render_scale),
            "FORENAGENT_LOCAL_OCR_SECOND_PASS": "1" if self.local_ocr_second_pass else "0",
            "FORENAGENT_LOCAL_OCR_SECOND_PASS_SCALE": str(self.local_ocr_second_pass_scale),
            "FORENAGENT_LOCAL_OCR_SECOND_PASS_MIN_SCORE": str(self.local_ocr_second_pass_min_score),
            "FORENAGENT_LOCAL_OCR_USE_DOC_ORIENTATION": "1" if self.local_ocr_use_doc_orientation else "0",
            "FORENAGENT_LOCAL_OCR_USE_DOC_UNWARPING": "1" if self.local_ocr_use_doc_unwarping else "0",
            "FORENAGENT_LOCAL_OCR_USE_TEXTLINE_ORIENTATION": "1" if self.local_ocr_use_textline_orientation else "0",
            "PADDLE_PDX_CACHE_HOME": self.paddle_pdx_cache_home,
            "PADDLEOCR_HOME": self.paddleocr_home,
            "PADDLE_HOME": self.paddle_home,
            "XDG_CACHE_HOME": self.xdg_cache_home,
        }

    def summary(self) -> str:
        llm = "off" if self.llm_provider == "off" else f"{self.llm_provider}/{self.llm_model}"
        if self.llm_provider == "local" and self.audit_llm_model != self.llm_model:
            llm = f"{llm}, audit/{self.audit_llm_model}"
        if self.llm_provider == "local":
            llm = f"{llm}, device={self.local_llm_device}"
        ocr = f"{self.ocr_provider}/{self.local_ocr_engine}" if self.ocr_provider == "local" else self.ocr_provider
        if self.ocr_provider == "local":
            ocr = f"{ocr}, device={self.local_ocr_device}"
        return f"OCR={ocr}, LLM={llm}, cloud_allowed={str(self.allow_cloud).lower()}"

    def metadata(self) -> Dict[str, object]:
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model if self.llm_provider != "off" else "off",
            "audit_llm_model": self.audit_llm_model if self.llm_provider != "off" else "off",
            "llm_device": self.local_llm_device if self.llm_provider == "local" else "remote",
            "ocr_provider": self.ocr_provider,
            "ocr_engine": self.local_ocr_engine if self.ocr_provider == "local" else self.ocr_provider,
            "ocr_device": self.local_ocr_device if self.ocr_provider == "local" else "remote",
            "cloud_allowed": self.allow_cloud,
            "cloud_used": self.llm_provider == "cloud" or self.ocr_provider == "cloud",
        }


def get_runtime_config(**overrides) -> RuntimeConfig:
    return RuntimeConfig.from_env().with_overrides(**overrides)


def apply_runtime_env(config: RuntimeConfig, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    target = os.environ if env is None else env
    target.update(config.to_env())
    return target
