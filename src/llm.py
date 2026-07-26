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
