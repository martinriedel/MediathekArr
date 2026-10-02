"""Settings, read from environment variables (see README.md)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    db_path: str = ""
    api_key: str = ""
    upstream_url: str = ""
    import_upstream_on_start: bool = True
    mediathekarr_api_base_url: str = ""
    tvdb_api_key: str = ""
    tvdb_pin: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_max_attempts: int = 3
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    target_url: str = ""
    target_api_key: str = ""
    min_match_rate: float = 0.8
    generate_interval_hours: float = 0
    retry_failed_after_hours: float = 72

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=_str("RULESETS_DB_PATH", "/data/rulesets.sqlite"),
            api_key=_str("RULESETS_API_KEY"),
            upstream_url=_str("UPSTREAM_RULESETS_URL", "https://mediathekarr.pcjones.de/metadata/api/rulesets.php"),
            import_upstream_on_start=_bool("IMPORT_UPSTREAM_ON_START", True),
            mediathekarr_api_base_url=_str("MEDIATHEKARR_API_BASE_URL", "https://mediathekarr.pcjones.de/api/v1"),
            tvdb_api_key=_str("TVDB_API_KEY"),
            tvdb_pin=_str("TVDB_PIN"),
            llm_base_url=_str("LLM_BASE_URL"),
            llm_model=_str("LLM_MODEL"),
            llm_api_key=_str("LLM_API_KEY"),
            llm_max_attempts=int(_float("LLM_MAX_ATTEMPTS", 3)),
            sonarr_url=_str("SONARR_URL").rstrip("/"),
            sonarr_api_key=_str("SONARR_API_KEY"),
            target_url=_str("GENERATOR_TARGET_URL").rstrip("/"),
            target_api_key=_str("GENERATOR_TARGET_API_KEY"),
            min_match_rate=_float("MIN_MATCH_RATE", 0.8),
            generate_interval_hours=_float("GENERATE_INTERVAL_HOURS", 0),
            retry_failed_after_hours=_float("RETRY_FAILED_AFTER_HOURS", 72),
        )
