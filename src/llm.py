"""
LLM Factory - provider-aware model instantiation (OpenAI-compatible API).

Supported providers: openai | deepseek | zhipu | custom.
All providers speak the OpenAI-compatible chat completions protocol,
so we keep using `langchain_openai.ChatOpenAI` and its
`with_structured_output()` for the single LLM call in SummaryMetaNode.
This means switching to a domestic model (DeepSeek / Zhipu) requires
ZERO new dependencies — just change config.

Usage:
    from src.llm import get_summary_model
    model = get_summary_model()
"""

from __future__ import annotations

from typing import Optional

from langchain_openai import ChatOpenAI

from src.config_loader import get_config

# 各服务商对应的 OpenAI 兼容 base_url（config.yaml 中 base_url 留空时生效）
_PROVIDER_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
}


def _resolve_base_url(provider: str, configured_url: str) -> str:
    """Return the effective base_url.

    Priority: explicitly configured base_url > provider default > openai default.
    """
    if configured_url:
        return configured_url
    return _PROVIDER_BASE_URLS.get(provider, _PROVIDER_BASE_URLS["openai"])


def get_summary_model(
    model_name: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> ChatOpenAI:
    """Create and return a ChatOpenAI(-compatible) instance for summary generation.

    This remains the single LLM entry point for the entire application.
    All LLM calls (currently only in SummaryMetaNode) must go through
    this factory to ensure consistent provider / url / key configuration.

    Switching to a domestic model (DeepSeek / Zhipu) requires no code
    change — simply set `provider` / `summary_model` / `api_key` in
    config.yaml (or the corresponding environment variable).

    Args:
        model_name: Model identifier. Defaults to config value or 'gpt-4o-mini'.
        temperature: Sampling temperature. Lower = more deterministic.
            Defaults to config value or 0.3.
        max_tokens: Maximum tokens in response.
            Defaults to config value or 1024.

    Returns:
        Configured ChatOpenAI-compatible instance ready for structured output.

    Raises:
        ValueError: If no API key is configured.
    """
    config = get_config()

    provider = (config.model.get("provider", "openai") or "openai").lower()
    resolved_model = model_name or config.model.get("summary_model", "gpt-4o-mini")
    resolved_temp = (
        temperature if temperature is not None
        else config.model.get("temperature", 0.3)
    )
    resolved_max_tokens = (
        max_tokens if max_tokens is not None
        else config.model.get("max_tokens", 1024)
    )
    base_url = _resolve_base_url(provider, config.model.get("base_url", "") or "")
    api_key = config.model.get("api_key", "") or ""

    if not api_key:
        raise ValueError(
            "未配置 LLM API Key。请在 config.yaml 的 model.api_key 中填写 "
            "对应环境变量（例如 ${DEEPSEEK_API_KEY}），并在 .env 中设置该变量。"
            f"当前 provider={provider}。"
        )

    model = ChatOpenAI(
        model=resolved_model,
        temperature=resolved_temp,
        max_tokens=resolved_max_tokens,
        base_url=base_url,
        api_key=api_key,
    )

    return model
