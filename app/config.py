"""Application settings.

Loaded from environment variables and/or an optional JSON config file
(SILENTARY_CONFIG, default: ./data/config.json). The config file may hold the
owner token and provider API key; env vars win over file values.

Secrets never live in source code (spec §21).
"""

from __future__ import annotations

import json
import os
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
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in config file {path}: {exc}") from exc


def load_settings() -> Settings:
    """Build settings from env + optional JSON config file."""
    config_path = Path(os.environ.get("SILENTARY_CONFIG",
                                      str(PROJECT_ROOT / "data" / "config.json")))
    file_cfg = _load_json_config(config_path)

    s = Settings()
    s.data_dir = Path(os.environ.get("SILENTARY_DATA_DIR", file_cfg.get("data_dir", s.data_dir))).resolve()
    s.workspaces_dir = Path(os.environ.get("SILENTARY_WORKSPACES_DIR", file_cfg.get("workspaces_dir", s.workspaces_dir))).resolve()
    s.frontend_dir = Path(os.environ.get("SILENTARY_FRONTEND_DIR", file_cfg.get("frontend_dir", s.frontend_dir))).resolve()
    s.host = os.environ.get("SILENTARY_HOST", str(file_cfg.get("host", s.host)))
    s.port = int(os.environ.get("SILENTARY_PORT", file_cfg.get("port", s.port)))

    s.owner_token = os.environ.get("SILENTARY_OWNER_TOKEN", file_cfg.get("owner_token", ""))
    s.provider_api_key = os.environ.get("SILENTARY_PROVIDER_API_KEY",
                                        file_cfg.get("provider_api_key", ""))
    s.model = os.environ.get("SILENTARY_MODEL", file_cfg.get("model", s.model))
    s.provider_name = os.environ.get("SILENTARY_PROVIDER", file_cfg.get("provider", ""))
    s.agent_max_context_chars = int(file_cfg.get("agent_max_context_chars", s.agent_max_context_chars))
    s.rag_top_k = int(file_cfg.get("rag_top_k", s.rag_top_k))
    s.trust_proxy_headers = os.environ.get(
        "SILENTARY_TRUST_PROXY_HEADERS", str(file_cfg.get("trust_proxy_headers", s.trust_proxy_headers))
    ).lower() in ("1", "true", "yes")
    s.chat_daily_quota = int(os.environ.get(
        "SILENTARY_CHAT_DAILY_QUOTA", file_cfg.get("chat_daily_quota", s.chat_daily_quota)))

    # For local development convenience only: if no owner token is configured,
    # generate one and persist it to the data dir so the owner can retrieve it.
    # Production deployments must set SILENTARY_OWNER_TOKEN (README documents this).
    if not s.owner_token:
        s.owner_token = secrets.token_urlsafe(32)
        s.data_dir.mkdir(parents=True, exist_ok=True)
        bootinfo = s.data_dir / "owner_token.txt"
        bootinfo.write_text(s.owner_token + "\n", encoding="utf-8")

    return s
