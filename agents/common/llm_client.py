"""OpenAI-compatible LLM helpers for local-first ForenAgent runs."""

from __future__ import annotations

from typing import Iterable, Optional

from agents.common.runtime_config import RuntimeConfig, get_runtime_config


_OLLAMA_LOCAL_HINTS = ("127.0.0.1:11434", "localhost:11434", "ollama")


def _is_ollama_endpoint(base_url: str) -> bool:
    text = str(base_url or "").lower()
    return any(hint in text for hint in _OLLAMA_LOCAL_HINTS)


def _ollama_device_extra_body(cfg: RuntimeConfig) -> Optional[dict]:
    """Request local Ollama GPU/CPU placement without affecting cloud or other local endpoints."""
    if cfg.llm_provider != "local" or not _is_ollama_endpoint(cfg.local_llm_base_url):
        return None
    if cfg.local_llm_device == "cpu":
        return {"options": {"num_gpu": 0}}
    # auto/gpu: ask Ollama to offload as much as available; if no CUDA is available,
    # Ollama remains local and falls back according to its own runtime.
    return {"options": {"num_gpu": -1}}


LOCAL_JSON_PROMPT_BASE = """【本地小模型输出约束】
- 只输出一个 JSON 对象，不要 Markdown，不要解释，不要使用代码围栏。
- 所有 schema 字段必须存在；未知写“不详”，列表未知写 []。
- 只能使用输入中明确出现的事实，不要根据常识、模板或经验补全。
- 不要合并不同日期、不同医院、不同检查项目或不同当事人的材料。
- 数值、日期、左右侧、椎体节段、误工/护理/营养天数必须原样保留。
- 遇到 OCR 断行、错字、矛盾或不能稳定判断，把问题写入 uncertain_points。
- 如果材料中出现“未见/无/没有/未行/未予/未发现”，不得把后面的疾病、手术或损伤当成阳性事实。"""


LOCAL_SECTION_PROMPTS = {
    "basic_situation": """【基本情况本地抽取重点】
- 每个字段只能来自对应上下文；不要跨字段上下文补全。
- 被鉴定人住址优先从“原告/当事人/被鉴定人”的身份证、身份证明、身份信息文件提取，通常在身份证号附近或身份证反面信息中。
- 不得使用被告、公司、法定代表人、员工、代理人的身份证作为被鉴定人信息来源。
- 被鉴定人住址不得混入预检时间、就诊时间、就诊科室、病案号、门诊号、住院号、检查号、CT/MR/DR 等医疗字段；污染时写“不详”。
- 在场人员无法从材料确认时写“不详”，不要编造“代理人/工作人员”。""",
    "basic_case": """【基本案情本地抽取重点】
- 只输出两段基本案情正文；不要列清单。
- 区分诉辩观点、情况说明、监控视频内容和法院认定事实；未明确认定责任时写“不详/需核对”。
- OCR 断句或错字不能扩写成确定事实。""",
    "summary": """【资料摘要本地抽取重点】
- 一条摘要只能对应一个来源、日期、医院和检查/病历类型。
- 资料摘要不是越短越好；优先覆盖首诊急诊/门诊、主要住院链、手术或治疗经过、出院诊断/医嘱、关键复诊或影像节点。
- 每个关键病历节点尽量保留日期、医院、材料名、病案/门诊/住院/检查号和主要医学事实。
- “检查部位”不是损伤诊断；不要把扫描范围当成骨折节段。
- “未见明显骨折和撕裂”等阴性描述不得抽成韧带/骨折阳性损伤；不得编造未出现的诊疗事实。""",
    "full": """【全量结构化本地抽取重点】
- 手术/非手术、椎体节段、压缩程度、影像日期必须保守抽取。
- 出现“手术史：无”“没有做手术”“未行手术”时，手术事实必须为阴性。
- 规则不确定的事实宁可写“不详”并进入 uncertain_points，不要给 Ruler 制造阳性误判。""",
    "judge_physical": """【体格检查本地抽取重点】
- 只能摘录材料中出现的主诉、查体和 ROM；不得编造“神志清、对答切题、肌力5级”等模板句。
- 无法稳定识别时保留“不详”并写入 uncertain_points。""",
    "judge_auxiliary": """【辅助检查本地抽取重点】
- 按“日期 + 医院 + 检查方式 + 检查号 + 所见/诊断”逐项抽取。
- OCR 断行时保留原句并标 uncertain，不要静默丢弃关键检查。
- “未见/无明显/未发现”后面的异常不得作为阳性损伤。""",
    "boss_preview": """【最终预览本地审计重点】
- 只补充规则未标出的遗漏、格式、OCR伪影、语义重复或事实矛盾。
- 特别核对被鉴定人姓名、身份证号、住址、病案号/住院号/门诊号/检查号、日期和关键数值是否前后一致。
- 住址不得混入预检时间、就诊时间、科室、病案号、门诊号、检查号、CT/MR 等医疗字段。
- 每项必须给出准确 line_no 和最小 span_text；不知道行号就不要输出。
- 最多输出 20 项；不要泛泛评论，不要改写报告。""",
}


def is_local_llm(config: Optional[RuntimeConfig] = None) -> bool:
    cfg = config or get_runtime_config()
    return cfg.llm_provider == "local"


def local_json_prompt_addendum(section_name: str = "") -> str:
    section = LOCAL_SECTION_PROMPTS.get(section_name, "")
    return f"{LOCAL_JSON_PROMPT_BASE}\n{section}".strip()


def append_local_prompt_if_needed(base_prompt: str, config: Optional[RuntimeConfig] = None, section_name: str = "") -> str:
    cfg = config or get_runtime_config()
    if cfg.llm_provider != "local":
        return base_prompt
    return f"{base_prompt.rstrip()}\n\n{local_json_prompt_addendum(section_name)}"


def build_chat_llm(
    config: Optional[RuntimeConfig] = None,
    *,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
    max_tokens: Optional[int] = None,
    temperature: float = 0.0,
    max_retries: int = 0,
):
    """Build a LangChain ChatOpenAI client for the selected provider."""
    cfg = config or get_runtime_config()
    if cfg.llm_provider == "off":
        raise RuntimeError("当前模型调用模式为 off，不调用 LLM。")
    if cfg.llm_provider == "cloud":
        cfg.require_cloud_allowed("使用云端 LLM 验证")
        if not cfg.llm_api_key:
            raise RuntimeError("云端 LLM 未找到 SILICONFLOW_API_KEY，请检查项目 .env。")

    from langchain_openai import ChatOpenAI

    effective_timeout = timeout or (cfg.local_llm_timeout if cfg.llm_provider == "local" else 120)
    effective_max_tokens = max_tokens or (cfg.local_llm_max_tokens if cfg.llm_provider == "local" else 2000)
    extra_body = _ollama_device_extra_body(cfg)
    kwargs = {
        "api_key": cfg.llm_api_key,
        "base_url": cfg.llm_base_url,
        "model": model or cfg.llm_model,
        "temperature": temperature,
        "timeout": effective_timeout,
        "max_retries": max_retries,
        "max_tokens": effective_max_tokens,
    }
    if extra_body:
        kwargs["extra_body"] = extra_body
    return ChatOpenAI(**kwargs)


def call_chat_text(
    messages: Iterable[dict],
    config: Optional[RuntimeConfig] = None,
    *,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
    max_tokens: Optional[int] = None,
    temperature: float = 0.0,
    json_mode: bool = False,
    max_retries: Optional[int] = None,
) -> str:
    """Call an OpenAI-compatible chat endpoint and return final content text."""
    cfg = config or get_runtime_config()
    if cfg.llm_provider == "off":
        return ""
    if cfg.llm_provider == "cloud":
        cfg.require_cloud_allowed("使用云端 LLM 验证")
        if not cfg.llm_api_key:
            return ""

    from openai import OpenAI

    effective_timeout = timeout or (cfg.local_llm_timeout if cfg.llm_provider == "local" else 120)
    client_kwargs = {"api_key": cfg.llm_api_key, "base_url": cfg.llm_base_url, "timeout": effective_timeout}
    if max_retries is not None:
        client_kwargs["max_retries"] = max_retries
    client = OpenAI(**client_kwargs)
    kwargs = {
        "model": model or cfg.llm_model,
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": max_tokens or (cfg.local_llm_max_tokens if cfg.llm_provider == "local" else 2000),
    }
    extra_body = _ollama_device_extra_body(cfg)
    if extra_body:
        kwargs["extra_body"] = extra_body
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or ""
    return content.strip()


def call_chat_json(
    prompt: str,
    config: Optional[RuntimeConfig] = None,
    *,
    model: Optional[str] = None,
    timeout: int = 120,
    max_retries: Optional[int] = None,
    retry_without_json_mode: bool = True,
) -> str:
    """Call chat with JSON mode first, optionally retrying without JSON mode for compatible endpoints."""
    messages = [{"role": "user", "content": prompt}]
    try:
        return call_chat_text(
            messages,
            config=config,
            model=model,
            timeout=timeout,
            temperature=0.1,
            json_mode=True,
            max_retries=max_retries,
        )
    except Exception:
        if not retry_without_json_mode:
            raise
        return call_chat_text(
            messages,
            config=config,
            model=model,
            timeout=timeout,
            temperature=0.1,
            json_mode=False,
            max_retries=max_retries,
        )
