"""
配置入口
优先级：环境变量 > config.yaml > 内置默认值

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


# --- 内置默认 ---
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
        "server_url": "http://localhost:3000",
        "api_key": "",
        "theme_id": "default",
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
    """解析config配置文件中的${VAR}占位符
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


# 全局配置实例 模块加载时是None
_config_instance: Optional[ConfigData] = None


def _find_config_dir() -> Path:
    """
    定位config目录
    先找src/../config/config.yaml
    再找当前工作目录/config/config.yaml
    """
    src_dir = Path(__file__).resolve().parent
    candidate = src_dir.parent / "config"
    if (candidate / "config.yaml").exists():
        return candidate

    cwd_candidate = Path.cwd() / "config"
    if (cwd_candidate / "config.yaml").exists():
        return cwd_candidate
    
    return candidate


def _load_yaml_file(config_dir: Path, filename: str) -> Dict[str, Any]:
    """
    """
    filepath = config_dir / filename
    if not filepath.exists():
        return {}

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            # 把yaml转成嵌套dict
            content = yaml.safe_load(f)
            return content if isinstance(content, dict) else {}
    except (yaml.YAMLError, OSError) as e:
        # Log warning but don't crash - we have defaults
        import logging
        logging.warning(f"Failed to load {filepath}: {e}")
        return {}


def get_config(force_reload: bool = False) -> ConfigData:
    global _config_instance

    # 命中缓存，直接返回，不读文件
    if _config_instance is not None and not force_reload:
        return _config_instance

    # Step 1: 加载env文件
    env_locations = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    for env_path in env_locations:
        if env_path.exists():
            # 将.env里的key = value写进os.environ
            load_dotenv(env_path)
            break

    # Step 2: 找config配置文件
    config_dir = _find_config_dir()

    # Step 3: 加载config配置文件
    main_config = _load_yaml_file(config_dir, "config.yaml")

    # Step 4: 默认值 → yaml覆盖
    merged = _deep_merge(_DEFAULTS, main_config)

    # Step 5: 配置文件中的占位符使用os.eviron中的值进行替换
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
    )

    return _config_instance