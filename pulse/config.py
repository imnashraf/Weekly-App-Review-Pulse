"""Loads `.env` + `config.yaml` into a validated settings model."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


class AppConfig(BaseModel):
    google_play_id: str
    country: str = "in"
    language: str = "en"


class WindowConfig(BaseModel):
    weeks: int = Field(default=12, ge=8, le=12)


class LimitsConfig(BaseModel):
    max_reviews_per_source: int = Field(default=20000, gt=0)
    max_themes: int = Field(default=5, ge=1, le=5)
    pulse_max_words: int = Field(default=250, gt=0)
    # Keep reviews with at least this many words (9 == "more than 8 words").
    min_review_words: int = Field(default=9, ge=1)


class LLMConfig(BaseModel):
    provider: str = "gemini"
    model: str
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_output_tokens: int = Field(default=8192, gt=0)
    # Optional USD prices per million tokens, used only for cost logging.
    price_per_mtok_in: float | None = Field(default=None, ge=0)
    price_per_mtok_out: float | None = Field(default=None, ge=0)


class AnalysisConfig(BaseModel):
    sample_size: int = Field(default=300, gt=0)          # Pass A discovery sample
    batch_size: int = Field(default=50, gt=0)            # Pass B reviews per call
    max_other_share: float = Field(default=0.40, gt=0, le=1)


class McpDocsConfig(BaseModel):
    document: str | None = None     # target Google Doc: docs.google.com URL or raw id


class McpConfig(BaseModel):
    """Our Streamable HTTP MCP server (Docs append + Gmail send). Checked when publishing."""
    url: str | None = None
    tools: dict[str, str] = Field(default_factory=lambda: {
        "status": "google_workspace_status",
        "docs_append": "google_docs_append_text",
        "gmail_send": "gmail_send_email",
    })
    docs: McpDocsConfig = McpDocsConfig()


class DeliveryConfig(BaseModel):
    recipient: str | None = None


class Settings(BaseModel):
    app: AppConfig
    window: WindowConfig = WindowConfig()
    limits: LimitsConfig = LimitsConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    llm: LLMConfig
    mcp: McpConfig = McpConfig()
    delivery: DeliveryConfig = DeliveryConfig()
    llm_api_key: str | None = None
    mcp_access_token: str | None = None
    runs_dir: Path = PROJECT_ROOT / "data" / "runs"


def _expand_env(value: Any) -> Any:
    """Replace `${VAR}` with the env value; unset vars become None for a whole-string ref."""
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, str):
        whole = _ENV_REF.fullmatch(value)
        if whole:
            return os.environ.get(whole.group(1)) or None
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    return value


def load_config(
    path: Path | str | None = None,
    require_env: tuple[str, ...] = (),
) -> Settings:
    """Load settings. `require_env` names env vars that must be set for the caller's command."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    load_dotenv(config_path.parent / ".env")

    missing = [name for name in require_env if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Set them in .env (see .env.example) or the shell environment."
        )

    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    data = _expand_env(raw)
    data["llm_api_key"] = os.environ.get("LLM_API_KEY") or None
    data["mcp_access_token"] = os.environ.get("MCP_ACCESS_TOKEN") or None
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"Invalid config in {config_path}: {problems}") from exc
