"""nanobot adapter config generation (provider/apiBase wiring)."""

from __future__ import annotations

import json

import pytest

from app.config import load_settings
from app.nanobot_adapter import NanobotAgentService


def _make_service(tmp_path, monkeypatch, **overrides) -> tuple[NanobotAgentService, object]:
    """Build a service with a Settings instance, without starting the bot."""
    monkeypatch.setenv("SILENTARY_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("SILENTARY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SILENTARY_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SILENTARY_OWNER_TOKEN", "test-owner-token")
    monkeypatch.setenv("SILENTARY_PROVIDER_API_KEY", "test-key-123")
    for k, v in overrides.items():
        monkeypatch.setenv(k, v)

    settings = load_settings()
    settings.ensure_dirs()

    from app.db import Database
    from app.rag import RagService
    from app.workspaces import WorkspaceManager

    db = Database(":memory:")
    ws = WorkspaceManager(settings.workspaces_dir)
    rag = RagService(settings, ws)
    return NanobotAgentService(settings, db, ws, rag), settings


def test_generated_config_has_api_base(tmp_path, monkeypatch):
    """SILENTARY_LLM_BASE_URL must land in the generated nanobot config."""
    svc, settings = _make_service(
        tmp_path, monkeypatch,
        SILENTARY_LLM_BASE_URL="https://my-gateway.example.com/v1",
    )
    svc._write_nanobot_config()
    cfg = json.loads(settings.nanobot_config_path.read_text(encoding="utf-8"))
    block = cfg["providers"][settings.provider]
    assert block["apiBase"] == "https://my-gateway.example.com/v1"
    assert block["apiKey"] == "test-key-123"


def test_generated_config_omits_api_base_when_unset(tmp_path, monkeypatch):
    """No base URL configured -> no apiBase key (provider default endpoint)."""
    svc, settings = _make_service(tmp_path, monkeypatch)
    svc._write_nanobot_config()
    cfg = json.loads(settings.nanobot_config_path.read_text(encoding="utf-8"))
    block = cfg["providers"][settings.provider]
    assert "apiBase" not in block


def test_generated_config_custom_provider(tmp_path, monkeypatch):
    """SILENTARY_PROVIDER=custom routes through nanobot's custom slot."""
    svc, settings = _make_service(
        tmp_path, monkeypatch,
        SILENTARY_PROVIDER="custom",
        SILENTARY_MODEL="custom/my-model",
        SILENTARY_LLM_BASE_URL="http://localhost:11434/v1",
    )
    svc._write_nanobot_config()
    cfg = json.loads(settings.nanobot_config_path.read_text(encoding="utf-8"))
    assert "custom" in cfg["providers"]
    assert cfg["providers"]["custom"]["apiBase"] == "http://localhost:11434/v1"

    # the generated config must validate against nanobot's own schema
    from nanobot.config.schema import Config as NanobotConfig
    parsed = NanobotConfig.model_validate(cfg)
    matched = parsed.get_provider("custom/my-model")
    assert matched is not None
    assert matched.api_base == "http://localhost:11434/v1"
    assert matched.api_key == "test-key-123"


def test_generated_config_validates_against_nanobot_schema(tmp_path, monkeypatch):
    """Even the default-shaped config must pass nanobot's pydantic validation."""
    svc, settings = _make_service(tmp_path, monkeypatch)
    svc._write_nanobot_config()
    cfg = json.loads(settings.nanobot_config_path.read_text(encoding="utf-8"))
    from nanobot.config.schema import Config as NanobotConfig
    parsed = NanobotConfig.model_validate(cfg)
    matched = parsed.get_provider(settings.model)
    assert matched is not None
    assert matched.api_key == "test-key-123"
