"""
配置加载器 - 带优先级链的YAML + .env加载。

优先级：环境变量 > config.yaml > user_prefs.yaml > 内置默认值

用法：
    from src.config_loader import get_config
    config = get_config()
    brand = config.get_brand_config()
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv


# --- Built-in defaults (lowest priority) ---
_DEFAULTS: Dict[str, Any] = {
    "oss": {
        "endpoint": "",
        "bucket_name": "",
        "access_key_id": "",
        "access_key_secret": "",
        "image_path_prefix": "images",
        "allowed_extensions": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"],
        "max_images_per_article": 10,
    },
    "github": {
        "token": "",
        "username": "",
        "hexo_repo": "",
        "local_repo_path": "temp/hexo_repo",
        "posts_dir": "source/_posts",
        "commit_prefix": "publish:",
    },
    "wechat": {
        "wenyan_path": "",
        "tunnel_enabled": False,
        "timeout_seconds": 60,
        "max_retries": 2,
    },
    "brand": {
        "name": "TechInsight",
        "audience": "tech_enthusiasts_and_practitioners",
        "tone": "professional_yet_approachable",
        "default_categories": ["Technology", "Knowledge"],
        "default_tags_prefix": ["tech", "learning"],
        "language": "zh-CN",
        "author": "TechInsight",
        "source_url": "",
    },
    "model": {
        "provider": "openai",
        "summary_model": "gpt-4o-mini",
        "base_url": "",
        "api_key": "",
        "temperature": 0.3,
        "max_tokens": 1024,
    },
    "default_options": {
        "platforms": ["blog", "wechat"],
        "auto_publish": True,
        "process_images": True,
        "format_optimize": True,
    },
    "format_optimize": {
        "mode": "rule",
        "llm_max_tokens": 4096,
        "safety_check": True,
    },
    "cover": {
        "enabled": True,
        "always": True,
        "width": 1200,
        "height": 630,
        "unsplash_access_key": "",
    },
}


def _resolve_env_vars(value: Any) -> Any:
    """Recursively resolve ${VAR_NAME} placeholders in configuration values.

    Supports string values like "${ALIYUN_OSS_ENDPOINT}" and falls back to
    empty string if the environment variable is not set.
    """
    if isinstance(value, str):
        import re
        pattern = re.compile(r"\$\{(\w+)\}")

        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, "")

        return pattern.sub(_replace, value)

    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge two dictionaries. Override values take precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class ConfigData:
    """Holds all configuration data after loading and merging."""

    oss: Dict[str, Any] = field(default_factory=dict)
    github: Dict[str, Any] = field(default_factory=dict)
    wechat: Dict[str, Any] = field(default_factory=dict)
    brand: Dict[str, Any] = field(default_factory=dict)
    model: Dict[str, Any] = field(default_factory=dict)
    default_options: Dict[str, Any] = field(default_factory=dict)
    format_optimize: Dict[str, Any] = field(default_factory=dict)
    cover: Dict[str, Any] = field(default_factory=dict)
    user_prefs: Dict[str, Any] = field(default_factory=dict)

    def get_brand_config(self) -> Dict[str, Any]:
        """返回品牌配置字典。

        返回：
            包含以下键的字典：name、audience、tone、default_categories、
            default_tags_prefix、author、source_url。
        """
        return {
            "name": self.brand.get("name", ""),
            "audience": self.brand.get("audience", ""),
            "tone": self.brand.get("tone", ""),
            "default_categories": list(
                self.brand.get("default_categories", [])
            ),
            "default_tags_prefix": list(
                self.brand.get("default_tags_prefix", [])
            ),
            "author": self.brand.get("author", ""),
            "source_url": self.brand.get("source_url", ""),
        }

    def get_platforms(self) -> List[str]:
        """Return configured default platform list."""
        return list(self.default_options.get("platforms", ["blog", "wechat"]))

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """Get a specific config value by section and key.

        Args:
            section: Top-level config section (e.g., 'oss', 'github').
            key: Key within that section.
            default: Fallback value if key not found.

        Returns:
            The resolved configuration value.
        """
        section_dict = getattr(self, section, {})
        return section_dict.get(key, default)


# Module-level cache for loaded config
_config_instance: Optional[ConfigData] = None


def _find_config_dir() -> Path:
    """Locate the project config directory.

    Searches relative to this file's location first, then current working directory.
    Returns:
        Path to the directory containing config.yaml.
    """
    # First try relative to this source file (src/ -> project root)
    src_dir = Path(__file__).resolve().parent
    candidate = src_dir.parent / "config"
    if (candidate / "config.yaml").exists():
        return candidate

    # Fallback to CWD / config
    cwd_candidate = Path.cwd() / "config"
    if (cwd_candidate / "config.yaml").exists():
        return cwd_candidate

    # Last resort: return expected location even if file doesn't exist yet
    return candidate


def _load_yaml_file(config_dir: Path, filename: str) -> Dict[str, Any]:
    """Load a YAML file from the config directory.

    Args:
        config_dir: Directory containing YAML files.
        filename: Name of the YAML file to load.

        Returns:
            Parsed dictionary, or empty dict if file not found/invalid.
        """
    filepath = config_dir / filename
    if not filepath.exists():
        return {}

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = yaml.safe_load(f)
            return content if isinstance(content, dict) else {}
    except (yaml.YAMLError, OSError) as e:
        # Log warning but don't crash - we have defaults
        import logging
        logging.warning(f"Failed to load {filepath}: {e}")
        return {}


def get_config(force_reload: bool = False) -> ConfigData:
    """
    加载并返回完整配置。

    这是整个应用程序中访问配置的主要入口。
    结果会被缓存；设置 force_reload=True 可强制重新加载。

    优先级顺序：
        1. 环境变量（.env 文件 + 系统环境变量）
        2. config.yaml 中的值
        3. user_prefs.yaml 中的值
        4. 内置默认值

    参数：
        force_reload: 若为 True，则绕过缓存并从磁盘重新加载。

    返回：
        完全解析后的 ConfigData 实例。
    """
    global _config_instance

    if _config_instance is not None and not force_reload:
        return _config_instance

    # Step 1: Load .env files (check multiple locations)
    env_locations = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    for env_path in env_locations:
        if env_path.exists():
            load_dotenv(env_path)
            break

    # Step 2: Find config directory
    config_dir = _find_config_dir()

    # Step 3: Load YAML files
    main_config = _load_yaml_file(config_dir, "config.yaml")
    user_prefs = _load_yaml_file(config_dir, "user_prefs.yaml")

    # Step 4: Merge with priority chain
    merged = _deep_merge(_DEFAULTS, user_prefs)
    merged = _deep_merge(merged, main_config)

    # Step 5: Resolve environment variable placeholders
    merged = _resolve_env_vars(merged)

    # Step 6: Build and cache ConfigData
    _config_instance = ConfigData(
        oss=merged.get("oss", {}),
        github=merged.get("github", {}),
        wechat=merged.get("wechat", {}),
        brand=merged.get("brand", {}),
        model=merged.get("model", {}),
        default_options=merged.get("default_options", {}),
        format_optimize=merged.get("format_optimize", {}),
        cover=merged.get("cover", {}),
        user_prefs=user_prefs,
    )

    return _config_instance


def update_user_prefs(updates: Dict[str, Any]) -> None:
    """Update and persist user preferences to user_prefs.yaml.

    Args:
        updates: Dictionary of preference updates to merge.
    """
    config_dir = _find_config_dir()
    prefs_path = config_dir / "user_prefs.yaml"

    # Load existing prefs
    existing = _load_yaml_file(config_dir, "user_prefs.yaml")

    # Deep merge updates
    updated = _deep_merge(existing, updates)

    # Write back
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(prefs_path, "w", encoding="utf-8") as f:
        yaml.dump(updated, f, allow_unicode=True, default_flow_style=False)

    # Invalidate cache so next get_config() picks up changes
    global _config_instance
    _config_instance = None
