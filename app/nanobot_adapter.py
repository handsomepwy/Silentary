"""nanobot adapter — the thin isolation layer between Silentary and nanobot.

All nanobot-specific APIs are confined to this module (spec §2). The rest of the
application talks to `AgentService` only.

Integration facts (verified against installed nanobot 0.3.0):
- `Nanobot.from_config(config_path, workspace=...)` builds the bot; builtin tools
  are registered inside from_config, so custom tools register after construction.
- `await bot.run(message, session_key=..., channel=..., chat_id=...)` performs a
  full agent turn and returns `RunResult(content=..., tools_used=...)`.
- Session keys are arbitrary strings; we use `visitor:{visitor_id}:{session_key}`
  so a session can never escape its visitor's namespace.
- `bot._loop.register_runtime_context_provider(fn)` injects per-turn context
  blocks (profile + disclosure boundary) into the model prompt; the blocks are
  stored with a metadata marker that is stripped on history replay, so context is
  re-resolved fresh every turn (no stale per-visitor text persists in history).
- Built-in tools (exec/web/file/...) are disabled via config for security.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from .agent_tools import RagSearchTool, SubmitCardTool
from .config import Settings
from .db import Database
from .rag import RagService
from .workspaces import WorkspaceManager

logger = logging.getLogger("silentary.nanobot_adapter")

_SESSION_KEY_RE = re.compile(r"^visitor:([0-9a-f]{32}):(.+)$")


class AgentError(Exception):
    """Raised when the agent turn fails (LLM errors, malformed runs)."""


def nanobot_session_key(visitor_id: str, session_key: str) -> str:
    """Compose the namespaced nanobot session key (server-side only)."""
    if not re.fullmatch(r"[0-9a-f]{32}", visitor_id):
        raise AgentError("invalid visitor id")
    if not session_key or len(session_key) > 128:
        raise AgentError("invalid session key")
    return f"visitor:{visitor_id}:{session_key}"


class NanobotAgentService:
    """Owns the single shared Nanobot instance."""

    def __init__(self, settings: Settings, database: Database,
                 workspaces: WorkspaceManager, rag: RagService):
        self.settings = settings
        self.database = database
        self.workspaces = workspaces
        self.rag = rag
        self._bot = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build config and construct the Nanobot instance."""
        async with self._lock:
            if self._bot is not None:
                return
            self._write_nanobot_config()
            # from_config is blocking (file IO, provider resolution) — offload.
            from nanobot import Nanobot

            bot = await asyncio.to_thread(
                Nanobot.from_config,
                str(self.settings.nanobot_config_path),
                workspace=str(self.settings.nanobot_workspace),
            )
            # Security: remove every built-in tool (exec/web/file/etc. must
            # never be reachable by visitors), then register exactly our two.
            for name in list(bot._loop.tools.tool_names):
                bot._loop.tools.unregister(name)
            # Custom tools (after construction — builtins already registered).
            bot._loop.tools.register(SubmitCardTool(self.database))
            bot._loop.tools.register(RagSearchTool(self.rag))
            bot._loop.register_runtime_context_provider(self._context_provider)
            self._bot = bot
            logger.info("nanobot agent started (model=%s) tools=%s",
                        self.settings.model, bot._loop.tools.tool_names)

    async def stop(self) -> None:
        async with self._lock:
            if self._bot is not None:
                try:
                    await self._bot.aclose()
                finally:
                    self._bot = None

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    def _write_nanobot_config(self) -> None:
        """Generate nanobot config.json from Silentary settings."""
        self.settings.ensure_dirs()
        disabled_defaults = {
            "web": {"enable": False},
            "exec": {"enable": False},
            "file": {"enable": False},
            "cli_apps": {"enable": False},
            "my": {"enable": False},
            "image_generation": {"enabled": False},
        }
        provider_block: dict = {"apiKey": self.settings.provider_api_key}
        if self.settings.llm_base_url:
            # nanobot accepts both api_base/apiBase (camelCase alias generator);
            # apiBase matches the casing used in nanobot docs/examples.
            provider_block["apiBase"] = self.settings.llm_base_url
        config = {
            "providers": {
                self.settings.provider: provider_block,
            },
            "agents": {
                "defaults": {
                    "model": self.settings.model,
                    "workspace": str(self.settings.nanobot_workspace),
                },
            },
            "tools": disabled_defaults,
        }
        self.settings.nanobot_dir.mkdir(parents=True, exist_ok=True)
        self.settings.nanobot_config_path.write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # per-turn context (profile + disclosure boundary)
    # ------------------------------------------------------------------

    def _workspace_context_text(self, visitor_id: str) -> str | None:
        """Profile markdown + disclosure boundary, bounded. Returns None if empty."""
        parts: list[str] = []
        try:
            profile = self.workspaces.read_file(visitor_id, "profile.md")
        except Exception:
            profile = None
        if profile and profile.strip():
            parts.append("### Visitor profile (prepared by the owner)\n" + profile.strip())

        visitor = self.database.get_visitor(visitor_id)
        if visitor:
            boundary = (visitor.get("disclosure_boundary") or "").strip()
            if boundary:
                parts.append(
                    "### Disclosure boundary for this visitor (must be respected)\n"
                    + boundary
                )
            rel = (visitor.get("relationship") or "").strip()
            if rel:
                parts.append(f"### Visitor relationship to the owner\n{rel}")
        if not parts:
            return None
        text = "\n\n".join(parts)
        limit = self.settings.agent_max_context_chars
        if len(text) > limit:
            text = text[:limit] + "\n[…truncated]"
        return text

    async def _context_provider(self, request_context):
        """nanobot RuntimeContextProvider: inject per-visitor workspace context."""
        from nanobot.runtime_context import RuntimeContextBlock

        session_key = getattr(request_context, "session_key", None) or ""
        m = _SESSION_KEY_RE.match(session_key)
        if not m:
            return None
        visitor_id = m.group(1)
        text = await asyncio.to_thread(self._workspace_context_text, visitor_id)
        if not text:
            return None
        return RuntimeContextBlock(source="silentary_visitor_context", content=text)

    # ------------------------------------------------------------------
    # agent turns
    # ------------------------------------------------------------------

    async def chat(self, visitor_id: str, session_key: str, message: str) -> dict:
        """Run one agent turn. Returns {reply, tools_used}."""
        if self._bot is None:
            raise AgentError("agent service not started")
        ns_key = nanobot_session_key(visitor_id, session_key)
        message = (message or "").strip()
        if not message:
            raise AgentError("empty message")
        try:
            result = await asyncio.wait_for(
                self._bot.run(
                    message,
                    session_key=ns_key,
                    channel="silentary",
                    chat_id=visitor_id,
                    sender_id="visitor",
                ),
                timeout=300,
            )
        except asyncio.TimeoutError:
            raise AgentError("the agent took too long to respond") from None
        except Exception as exc:
            logger.exception("agent turn failed")
            raise AgentError("the agent could not process this message") from exc
        if result.error:
            raise AgentError("the agent could not process this message")
        reply = (result.content or "").strip()
        if not reply:
            # A run may legitimately end with tool-only output; make it visible.
            reply = "(The agent returned an empty response.)"
        # Keep session metadata fresh for the dashboard.
        self.database.touch_session(visitor_id, session_key)
        return {"reply": reply, "tools_used": list(result.tools_used or [])}

    async def history(self, visitor_id: str, session_key: str) -> list[dict]:
        """Conversation history for display (user/assistant messages only)."""
        if self._bot is None:
            raise AgentError("agent service not started")
        ns_key = nanobot_session_key(visitor_id, session_key)
        snapshot = self._bot.sessions.get(ns_key)
        if snapshot is None:
            return []
        messages = []
        for msg in getattr(snapshot, "messages", []) or []:
            role = msg.get("role")
            content = msg.get("content")
            if role not in ("user", "assistant") or not content:
                continue
            if not isinstance(content, str):
                continue
            # Strip the runtime-context marker metadata if present (display-safe).
            messages.append({
                "role": role,
                "content": content,
                "timestamp": msg.get("timestamp"),
            })
        return messages

    async def delete_session(self, visitor_id: str, session_key: str) -> bool:
        if self._bot is None:
            raise AgentError("agent service not started")
        ns_key = nanobot_session_key(visitor_id, session_key)
        return bool(self._bot.sessions.delete(ns_key))

    async def delete_all_sessions(self, visitor_id: str) -> int:
        """Delete every nanobot session for a visitor (privacy on deletion)."""
        if self._bot is None:
            raise AgentError("agent service not started")
        prefix = f"visitor:{visitor_id}:"
        deleted = 0
        for info in self._bot.sessions.list():
            if info.key.startswith(prefix):
                if self._bot.sessions.delete(info.key):
                    deleted += 1
        return deleted
