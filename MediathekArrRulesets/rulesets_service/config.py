"""Settings, read from environment variables (see README.md)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_UPSTREAMS = [
    "https://mediathekarr.pcjones.de/metadata/api/rulesets.php",
    "https://raw.githubusercontent.com/rundfunkarr/rundfunkarr/main/data/rulesets.json",
]


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


DEFAULT_DISCOVER_CHANNELS = ("ARD,ZDF,3Sat,ARTE.DE,BR,HR,MDR,NDR,rbb,Radio Bremen TV,SR,SWR,WDR,PHOENIX,"
                             "KiKA,ZDF-tivi,ARD-alpha,ONE,tagesschau24,Funk.net,DW,ORF,SRF")


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    db_path: str = ""
    api_key: str = ""
    upstream_urls: list[str] = field(default_factory=list)
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
    discover_interval_hours: float = 24
    discover_items: int = 0
    discover_min_items: int = 3
    discover_min_minutes: int = 10
    discover_max_topics: int = 200
    discover_min_name_score: float = 0.85
    discover_retry_no_match_days: float = 30
    discover_channels: tuple[str, ...] = tuple(DEFAULT_DISCOVER_CHANNELS.split(","))

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=_str("RULESETS_DB_PATH", "/data/rulesets.sqlite"),
            api_key=_str("RULESETS_API_KEY"),
            # comma separated; UPSTREAM_RULESETS_URL (single URL) is still honoured
            upstream_urls=[u.strip() for u in _str("UPSTREAM_RULESETS_URLS", _str("UPSTREAM_RULESETS_URL", ",".join(DEFAULT_UPSTREAMS))).split(",") if u.strip()],
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
            discover_interval_hours=_float("DISCOVER_INTERVAL_HOURS", 24),
            discover_items=int(_float("DISCOVER_ITEMS", 0)),
            discover_min_items=int(_float("DISCOVER_MIN_ITEMS", 3)),
            discover_min_minutes=int(_float("DISCOVER_MIN_MINUTES", 10)),
            discover_max_topics=int(_float("DISCOVER_MAX_TOPICS", 200)),
            discover_min_name_score=_float("DISCOVER_MIN_NAME_SCORE", 0.85),
            discover_retry_no_match_days=_float("DISCOVER_RETRY_NO_MATCH_DAYS", 30),
            discover_channels=tuple(c.strip() for c in _str("DISCOVER_CHANNELS", DEFAULT_DISCOVER_CHANNELS).split(",") if c.strip()),
        )
