from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _parse_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _parse_csv(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@dataclass(slots=True)
class Settings:
    vllm_url: str = "http://localhost:8000"
    model_name: str = "mitko"
    allowed_models: frozenset[str] = frozenset({"mitko"})
    port: int = 9000

    # Backward-compatible fields from phase 1.
    max_records: int = 10_000
    max_record_age_seconds: Optional[int] = None
    state_path: Optional[str] = None

    # Phase 2 state and loop controls.
    state_db_path: str = "./sonic_state.db"
    max_steps: int = 25
    max_tool_calls: int = 50
    tool_wait_timeout_seconds: int = 120

    require_api_key: bool = False
    api_key: Optional[str] = None

    backend_connect_timeout: float = 5.0
    backend_read_timeout: float = 300.0

    max_input_bytes: int = 256 * 1024
    rate_limit_per_minute: int = 120

    default_temperature: float = 0.2
    default_top_p: float = 0.9

    tool_allowlist: frozenset[str] = frozenset()
    enable_shell_exec: bool = False
    enable_http_get: bool = False
    filesystem_root: str = "."

    @classmethod
    def from_env(cls) -> "Settings":
        model_name = os.getenv("MODEL_NAME", "mitko")
        allowed_models = _parse_csv(os.getenv("ALLOWED_MODELS")) or frozenset({model_name})

        max_age_raw = os.getenv("MAX_RECORD_AGE_SECONDS")
        max_age = int(max_age_raw) if max_age_raw and max_age_raw.strip() else None

        state_db_path = os.getenv("STATE_DB_PATH")
        fallback_state_path = os.getenv("STATE_PATH")

        return cls(
            vllm_url=os.getenv("VLLM_URL", "http://localhost:8000").rstrip("/"),
            model_name=model_name,
            allowed_models=allowed_models,
            port=_parse_int(os.getenv("PORT"), 9000),
            max_records=_parse_int(os.getenv("MAX_RECORDS"), 10_000),
            max_record_age_seconds=max_age,
            state_path=fallback_state_path,
            state_db_path=state_db_path or fallback_state_path or "./sonic_state.db",
            max_steps=_parse_int(os.getenv("MAX_STEPS"), 25),
            max_tool_calls=_parse_int(os.getenv("MAX_TOOL_CALLS"), 50),
            tool_wait_timeout_seconds=_parse_int(
                os.getenv("TOOL_WAIT_TIMEOUT_SECONDS"),
                120,
            ),
            require_api_key=_parse_bool(os.getenv("REQUIRE_API_KEY"), False),
            api_key=os.getenv("API_KEY"),
            backend_connect_timeout=_parse_float(
                os.getenv("BACKEND_CONNECT_TIMEOUT"),
                5.0,
            ),
            backend_read_timeout=_parse_float(
                os.getenv("BACKEND_READ_TIMEOUT"),
                300.0,
            ),
            max_input_bytes=_parse_int(os.getenv("MAX_INPUT_BYTES"), 256 * 1024),
            rate_limit_per_minute=_parse_int(os.getenv("RATE_LIMIT_PER_MINUTE"), 120),
            default_temperature=_parse_float(os.getenv("DEFAULT_TEMPERATURE"), 0.2),
            default_top_p=_parse_float(os.getenv("DEFAULT_TOP_P"), 0.9),
            tool_allowlist=_parse_csv(os.getenv("TOOL_ALLOWLIST")),
            enable_shell_exec=_parse_bool(os.getenv("ENABLE_SHELL_EXEC"), False),
            enable_http_get=_parse_bool(os.getenv("ENABLE_HTTP_GET"), False),
            filesystem_root=os.getenv("FILESYSTEM_ROOT", "."),
        )
