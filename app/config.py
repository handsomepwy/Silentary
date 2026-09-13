"""Application settings.

Loaded from environment variables and/or an optional JSON config file
(SILENTARY_CONFIG, default: ./data/config.json). The config file may hold the
owner token and provider API key; env vars win over file values.

Secrets never live in source code (spec §21).
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    # --- paths ---
    data_dir: Path = PROJECT_ROOT / "data"
    workspaces_dir: Path = PROJECT_ROOT / "workspaces"
    frontend_dir: Path = PROJECT_ROOT / "frontend"

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8000

    # --- secrets ---
    owner_token: str = ""  # required; dashboard auth
    provider_api_key: str = ""  # LLM provider key, injected into nanobot config

    # --- LLM ---
    model: str = "anthropic/claude-opus-4-5"
    # Provider name is the part before "/" in `model` unless overridden.
    provider_name: str = ""
    # Optional custom endpoint for OpenAI-compatible gateways/proxies
    # (OpenRouter, AiHubMix, vLLM, Ollama, corporate proxies...). Empty =
    # provider's default endpoint.
    llm_base_url: str = ""

    # --- agent ---
    agent_max_context_chars: int = 4000  # bound for injected workspace context
    rag_top_k: int = 6

    # --- security topology ---
    # True ONLY when the app is behind a trusted reverse proxy that overwrites
    # X-Forwarded-For (the bundled nginx config does). When False (direct
    # exposure / unknown topology), client IP for rate limiting falls back to
    # the socket peer, so XFF spoofing cannot bypass per-IP buckets.
    trust_proxy_headers: bool = False

    # --- cost ceiling (M2): hard daily chat-turn quota ---
    chat_daily_quota: int = 200  # max agent turns per visitor per UTC day

    # --- derived paths ---
    @property
    def db_path(self) -> Path:
        return self.data_dir / "silentary.db"

    @property
    def nanobot_dir(self) -> Path:
        """nanobot instance dir (config.json + sessions/ live here)."""
        return self.data_dir / "nanobot"

    @property
    def nanobot_config_path(self) -> Path:
        return self.nanobot_dir / "config.json"

    @property
    def nanobot_workspace(self) -> Path:
        """Shared agent workspace (SOUL.md/AGENTS.md); NOT visitor data."""
        return self.nanobot_dir / "agent"

    @property
    def provider(self) -> str:
        return self.provider_name or self.model.split("/", 1)[0]

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.workspaces_dir, self.nanobot_dir,
                  self.nanobot_workspace):
            p.mkdir(parents=True, exist_ok=True)


def _load_json_config(path: Path) -> dict:
    try:
        # utf-8-sig: tolerate editors that save a BOM (Notepad, PowerShell).
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in config file {path}: {exc}") from exc


_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Precedence: real env > .env > config.json.

    Supported: KEY=value, optional `export ` prefix, blank/# comment lines,
    and single- or double-quoted values (quotes stripped). Multi-line values
    are not supported — keep secrets on one line.
    """
    values: dict[str, str] = {}
    try:
        # utf-8-sig: tolerate editors that save a BOM (Notepad, PowerShell).
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return values
    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(line)
        if not m:
            raise RuntimeError(f"Invalid line {lineno} in env file {path}: {line[:60]}")
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _dotenv_value(env_name: str, file_cfg: dict, json_key: str) -> str:
    """Lookup order: process env (set) > .env file > JSON config file."""
    if os.environ.get(env_name):
        return os.environ[env_name]
    return _ENV_FILE.get(env_name) or file_cfg.get(json_key, "")


# Loaded once at import time; the project-root .env is the conventional spot.
_ENV_FILE: dict[str, str] = _load_env_file(PROJECT_ROOT / ".env")


def load_settings() -> Settings:
    """Build settings from env + optional JSON config file."""
    config_path = Path(os.environ.get("SILENTARY_CONFIG",
                                      str(PROJECT_ROOT / "data" / "config.json")))
    file_cfg = _load_json_config(config_path)

    s = Settings()
    s.data_dir = Path(os.environ.get("SILENTARY_DATA_DIR", file_cfg.get("data_dir", s.data_dir))).resolve()
    s.workspaces_dir = Path(os.environ.get("SILENTARY_WORKSPACES_DIR", file_cfg.get("workspaces_dir", s.workspaces_dir))).resolve()
    s.frontend_dir = Path(os.environ.get("SILENTARY_FRONTEND_DIR", file_cfg.get("frontend_dir", s.frontend_dir))).resolve()
    s.host = _dotenv_value("SILENTARY_HOST", file_cfg, "host") or s.host
    s.port = int(_dotenv_value("SILENTARY_PORT", file_cfg, "port") or s.port)

    s.owner_token = _dotenv_value("SILENTARY_OWNER_TOKEN", file_cfg, "owner_token")
    s.provider_api_key = _dotenv_value("SILENTARY_PROVIDER_API_KEY", file_cfg, "provider_api_key")
    s.model = _dotenv_value("SILENTARY_MODEL", file_cfg, "model") or s.model
    s.provider_name = _dotenv_value("SILENTARY_PROVIDER", file_cfg, "provider")
    s.llm_base_url = _dotenv_value("SILENTARY_LLM_BASE_URL", file_cfg, "llm_base_url")
    s.agent_max_context_chars = int(file_cfg.get("agent_max_context_chars", s.agent_max_context_chars))
    s.rag_top_k = int(file_cfg.get("rag_top_k", s.rag_top_k))
    s.trust_proxy_headers = _dotenv_value(
        "SILENTARY_TRUST_PROXY_HEADERS", file_cfg, "trust_proxy_headers"
    ).lower() in ("1", "true", "yes")
    s.chat_daily_quota = int(_dotenv_value(
        "SILENTARY_CHAT_DAILY_QUOTA", file_cfg, "chat_daily_quota") or s.chat_daily_quota)

    # For local development convenience only: if no owner token is configured,
    # generate one and persist it to the data dir so the owner can retrieve it.
    # Production deployments must set SILENTARY_OWNER_TOKEN (README documents this).
    if not s.owner_token:
        s.owner_token = secrets.token_urlsafe(32)
        s.data_dir.mkdir(parents=True, exist_ok=True)
        bootinfo = s.data_dir / "owner_token.txt"
        bootinfo.write_text(s.owner_token + "\n", encoding="utf-8")

    return s
