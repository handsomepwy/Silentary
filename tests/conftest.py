"""Shared test fixtures.

A stub agent service replaces the real nanobot integration so tests are fast
and hermetic. The real nanobot service is exercised separately (see
test_nanobot_integration.py, requires provider API key + network).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("SILENTARY_OWNER_TOKEN", "test-owner-token")

from app.config import load_settings  # noqa: E402,F401  (import check)
from app.main import create_app  # noqa: E402


class StubAgentService:
    """Stub matching NanobotAgentService surface used by routes."""

    def __init__(self):
        self.history_store: dict[tuple[str, str], list[dict]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.chat_calls: list[tuple[str, str, str]] = []
        self.fail_chat = False

    async def start(self):
        pass

    async def stop(self):
        pass

    async def chat(self, visitor_id, session_key, message):
        self.chat_calls.append((visitor_id, session_key, message))
        if self.fail_chat:
            from app.nanobot_adapter import AgentError
            raise AgentError("stub failure")
        reply = f"Stub reply to: {message}"
        hist = self.history_store.setdefault((visitor_id, session_key), [])
        if not hist:
            hist.append({"role": "user", "content": message, "timestamp": None})
        hist.append({"role": "assistant", "content": reply, "timestamp": None})
        return {"reply": reply, "tools_used": []}

    async def history(self, visitor_id, conftest_marker=None, session_key=None):
        # routes call history(visitor_id, session_key)
        if session_key is None and not isinstance(conftest_marker, str):
            raise TypeError("history() requires session_key")
        key = session_key or conftest_marker
        return self.history_store.get((visitor_id, key), [])

    async def delete_session(self, visitor_id, session_key):
        self.deleted.append((visitor_id, session_key))
        self.history_store.pop((visitor_id, session_key), None)
        return True


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Fresh isolated app + stub agent."""
    monkeypatch.setenv("SILENTARY_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("SILENTARY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SILENTARY_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SILENTARY_OWNER_TOKEN", "test-owner-token")
    monkeypatch.setenv("SILENTARY_PROVIDER_API_KEY", "")

    app = create_app()

    # Bootstrap state without running the real lifespan (no agent startup).
    # Mirrors app.main.lifespan but synchronous and hermetic.
    from app import db as dbm
    from app.config import load_settings as ls
    from app.main import AppState
    from app.rag import RagService
    from app.rate_limit import TokenBucketLimiter, setup_default_rules
    from app.workspaces import WorkspaceManager

    settings = ls()
    settings.ensure_dirs()

    state = AppState()
    app.state.silentary = state

    state.settings = settings
    state.database = dbm.Database(settings.db_path)
    state.workspaces = WorkspaceManager(settings.workspaces_dir)
    state.rag = RagService(settings, state.workspaces)
    state.limiter = TokenBucketLimiter()
    setup_default_rules(state.limiter)
    state.agent = None  # replaced by stub below

    class Helpers:
        def __init__(self):
            self.app = app
            self.agent = StubAgentService()
            app.state.silentary.agent = self.agent
            self.owner_headers = {"Authorization": "Bearer test-owner-token"}

        async def make_visitor(self, name="Alice", disclosure=""):
            from app.auth import new_visitor_with_credential
            st = app.state.silentary
            visitor, token = new_visitor_with_credential(
                st.database, st.workspaces, name, "", disclosure)
            return visitor, token

    return Helpers()


@pytest.fixture()
async def client(app_env):
    h = app_env
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=h.app),
                           base_url="http://test") as c:
        yield c, h
