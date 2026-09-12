"""FastAPI application factory and route registration.

Layering: routes → services (auth/workspaces/db/rag/agent). Route handlers are
`async def` but call blocking DB/RAG work via `asyncio.to_thread` (never on the
event loop). Visitor identity is ALWAYS derived from the token; visitors never
send a visitor id or workspace id (fail-closed, spec §5).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db as dbm
from .auth import (
    AuthError,
    authenticate_visitor,
    new_visitor_with_credential,
    verify_owner_token,
)
from .config import load_settings
from .nanobot_adapter import AgentError, NanobotAgentService
from .rag import RagService
from .rate_limit import TokenBucketLimiter, setup_default_rules
from .workspaces import WorkspaceError, WorkspaceManager, slugify_name

logger = logging.getLogger("silentary.app")

_SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


# ----------------------------------------------------------------------
# state container
# ----------------------------------------------------------------------

class AppState:
    """Dependency container shared across requests."""

    def __init__(self):
        self.settings = None
        self.database: dbm.Database | None = None
        self.workspaces: WorkspaceManager | None = None
        self.rag: RagService | None = None
        self.agent: NanobotAgentService | None = None
        self.limiter: TokenBucketLimiter | None = None


def _get_state(request: Request) -> AppState:
    return request.app.state.silentary


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Nginx is the trusted proxy; it appends the client address as first hop.
        return xff.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _json_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


# ----------------------------------------------------------------------
# auth helpers for routes
# ----------------------------------------------------------------------

def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


def _require_visitor(request: Request):
    state = _get_state(request)
    token = _bearer_token(request)
    if not token:
        raise AuthError("authentication required")
    return authenticate_visitor(token, state.database)


def _require_owner(request: Request) -> None:
    state = _get_state(request)
    token = _bearer_token(request)
    if not token or not verify_owner_token(token, state.settings):
        raise AuthError("owner authentication required")


# ----------------------------------------------------------------------
# exception handling
# ----------------------------------------------------------------------

async def _auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    return _json_error(401, exc.message or "authentication required")


async def _workspace_error_handler(request: Request, exc: WorkspaceError) -> JSONResponse:
    return _json_error(400, "invalid workspace request")


async def _generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak internals to visitors (spec §17/§20); log the details server-side.
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return _json_error(500, "internal server error")


# ----------------------------------------------------------------------
# agent home seeding
# ----------------------------------------------------------------------

def _seed_agent_home(agent_home: Path, workspace_dir: Path) -> None:
    """Copy packaged SOUL.md/AGENTS.md into the agent workspace if absent.

    Existing files are never overwritten (owner may have customized them).
    """
    import shutil

    workspace_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("SOUL.md", "AGENTS.md"):
        src = agent_home / fname
        dst = workspace_dir / fname
        if src.is_file() and not dst.exists():
            shutil.copyfile(src, dst)


# ----------------------------------------------------------------------
# lifespan
# ----------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    state = AppState()
    app.state.silentary = state

    state.settings = load_settings()
    state.settings.ensure_dirs()
    state.database = dbm.Database(state.settings.db_path)
    state.workspaces = WorkspaceManager(state.settings.workspaces_dir)
    state.rag = RagService(state.settings, state.workspaces)
    state.limiter = TokenBucketLimiter()
    setup_default_rules(state.limiter)

    _seed_agent_home(Path(__file__).resolve().parent / "agent_home",
                     state.settings.nanobot_workspace)

    state.agent = NanobotAgentService(state.settings, state.database,
                                      state.workspaces, state.rag)
    if state.settings.provider_api_key:
        try:
            await state.agent.start()
        except Exception:
            # The app must still boot without the agent (dashboard works,
            # visitor chat returns a clear error) — spec §17.
            logger.exception("agent failed to start; chat endpoints will return errors")
            state.agent = None
    else:
        logger.warning("no provider API key configured; agent disabled")
        state.agent = None

    logger.info("silentary started (host=%s port=%s)", state.settings.host, state.settings.port)
    try:
        yield
    finally:
        if state.agent is not None:
            await state.agent.stop()
        state.rag.close_all()


def create_app() -> FastAPI:
    app = FastAPI(title="Silentary", docs_url=None, redoc_url=None, openapi_url=None)
    app.router.lifespan_context = lifespan

    app.add_exception_handler(AuthError, _auth_error_handler)
    app.add_exception_handler(WorkspaceError, _workspace_error_handler)
    app.add_exception_handler(Exception, _generic_error_handler)

    _register_visitor_routes(app)
    _register_owner_routes(app)
    _register_frontend(app)
    return app


# ----------------------------------------------------------------------
# visitor routes
# ----------------------------------------------------------------------

def _register_visitor_routes(app: FastAPI) -> None:
    @app.post("/api/visitor/login")
    async def visitor_login(request: Request):
        state = _get_state(request)
        ip = _client_ip(request)
        body = await request.json()
        token = (body.get("token") or "").strip()
        if not state.limiter.check("login_ip", ip):
            return _json_error(429, "too many attempts, slow down")
        if token and not state.limiter.check("login_token", token[:8]):
            # rate limit per token prefix so brute force on one token stalls
            return _json_error(429, "too many attempts, slow down")
        try:
            auth = await asyncio.to_thread(authenticate_visitor, token, state.database)
        except AuthError:
            return _json_error(401, "invalid token")
        return {"visitor_name": auth.visitor["name"]}

    @app.get("/api/visitor/sessions")
    async def visitor_list_sessions(request: Request):
        state = _get_state(request)
        ip = _client_ip(request)
        if not state.limiter.check("sessions_ip", ip):
            return _json_error(429, "too many requests")
        auth = _require_visitor(request)
        sessions = await asyncio.to_thread(state.database.list_sessions, auth.visitor_id)
        return {"sessions": [
            {k: s[k] for k in ("session_key", "created_at", "updated_at", "title")}
            for s in sessions
        ]}

    @app.post("/api/visitor/sessions")
    async def visitor_create_session(request: Request):
        state = _get_state(request)
        ip = _client_ip(request)
        if not state.limiter.check("sessions_ip", ip):
            return _json_error(429, "too many requests")
        auth = _require_visitor(request)
        session_key = secrets.token_urlsafe(9)  # short, url-safe, unguessable-ish
        session = await asyncio.to_thread(state.database.create_session,
                                          auth.visitor_id, session_key)
        return {"session_key": session["session_key"],
                "created_at": session["created_at"]}

    @app.get("/api/visitor/sessions/{session_key}/messages")
    async def visitor_messages(request: Request, session_key: str):
        state = _get_state(request)
        ip = _client_ip(request)
        if not state.limiter.check("messages_ip", ip):
            return _json_error(429, "too many requests")
        auth = _require_visitor(request)
        if not _SESSION_KEY_RE.match(session_key):
            return _json_error(404, "session not found")
        session = await asyncio.to_thread(state.database.get_session,
                                          auth.visitor_id, session_key)
        if session is None:
            return _json_error(404, "session not found")
        try:
            messages = await state.agent.history(auth.visitor_id, session_key)
        except (AgentError, AttributeError):
            return _json_error(503, "agent unavailable")
        return {"session_key": session_key, "messages": messages}

    @app.post("/api/visitor/chat")
    async def visitor_chat(request: Request):
        state = _get_state(request)
        ip = _client_ip(request)
        if not state.limiter.check("chat_ip", ip):
            return _json_error(429, "too many messages, slow down")
        auth = _require_visitor(request)
        body = await request.json()
        message = (body.get("message") or "").strip()
        session_key = (body.get("session_key") or "").strip()
        if not message or len(message) > 8000:
            return _json_error(400, "message must be 1..8000 characters")
        if not _SESSION_KEY_RE.match(session_key):
            return _json_error(404, "session not found")
        if not state.limiter.check("chat_token", auth.visitor_id):
            return _json_error(429, "too many messages, slow down")
        # Session must exist and belong to THIS visitor (ownership check, spec §20).
        session = await asyncio.to_thread(state.database.get_session,
                                          auth.visitor_id, session_key)
        if session is None:
            return _json_error(404, "session not found")
        try:
            result = await state.agent.chat(auth.visitor_id, session_key, message)
        except AgentError as exc:
            return _json_error(503, str(exc))
        await asyncio.to_thread(
            state.database.touch_session, auth.visitor_id, session_key,
            message[:60],
        )
        return {"reply": result["reply"], "tools_used": result["tools_used"]}

    @app.delete("/api/visitor/sessions/{session_key}")
    async def visitor_delete_session(request: Request, session_key: str):
        state = _get_state(request)
        auth = _require_visitor(request)
        if not _SESSION_KEY_RE.match(session_key):
            return _json_error(404, "session not found")
        session = await asyncio.to_thread(state.database.get_session,
                                          auth.visitor_id, session_key)
        if session is None:
            return _json_error(404, "session not found")
        await asyncio.to_thread(state.database.delete_session,
                                auth.visitor_id, session_key)
        try:
            await state.agent.delete_session(auth.visitor_id, session_key)
        except (AgentError, AttributeError):
            pass  # DB row gone already; nanobot cleanup best-effort
        return {"ok": True}


# ----------------------------------------------------------------------
# owner routes
# ----------------------------------------------------------------------

def _register_owner_routes(app: FastAPI) -> None:
    @app.get("/api/owner/visitors")
    async def owner_list_visitors(request: Request):
        _require_owner(request)
        state = _get_state(request)
        visitors = await asyncio.to_thread(state.database.list_visitors)
        return {"visitors": visitors}

    @app.post("/api/owner/visitors")
    async def owner_create_visitor(request: Request):
        _require_owner(request)
        state = _get_state(request)
        body = await request.json()
        name = (body.get("name") or "").strip()
        relationship = (body.get("relationship") or "").strip()
        disclosure = (body.get("disclosure_boundary") or "").strip()
        if not name or len(name) > 100:
            return _json_error(400, "name must be 1..100 characters")
        visitor, token = await asyncio.to_thread(
            new_visitor_with_credential, state.database, state.workspaces,
            name, relationship, disclosure,
        )
        # Plaintext token returned exactly once, here.
        return {"visitor": visitor, "token": token}

    @app.get("/api/owner/visitors/{visitor_id}")
    async def owner_get_visitor(request: Request, visitor_id: str):
        _require_owner(request)
        state = _get_state(request)
        visitor = await asyncio.to_thread(state.database.get_visitor, visitor_id)
        if visitor is None:
            return _json_error(404, "visitor not found")
        creds = await asyncio.to_thread(state.database.list_credentials, visitor_id)
        sessions = await asyncio.to_thread(state.database.list_sessions, visitor_id)
        cards = await asyncio.to_thread(state.database.list_cards, visitor_id=visitor_id)
        return {
            "visitor": visitor,
            "credentials": creds,
            "sessions": sessions,
            "cards": cards,
        }

    @app.put("/api/owner/visitors/{visitor_id}")
    async def owner_update_visitor(request: Request, visitor_id: str):
        _require_owner(request)
        state = _get_state(request)
        body = await request.json()
        visitor = await asyncio.to_thread(
            state.database.update_visitor, visitor_id,
            name=body.get("name"),
            relationship=body.get("relationship"),
            disclosure_boundary=body.get("disclosure_boundary"),
        )
        if visitor is None:
            return _json_error(404, "visitor not found")
        return {"visitor": visitor}

    @app.delete("/api/owner/visitors/{visitor_id}")
    async def owner_delete_visitor(request: Request, visitor_id: str):
        _require_owner(request)
        state = _get_state(request)
        ok = await asyncio.to_thread(state.database.delete_visitor, visitor_id)
        if not ok:
            return _json_error(404, "visitor not found")
        # Best-effort cleanup of workspace dir + nanobot sessions.
        try:
            await asyncio.to_thread(state.workspaces.delete_visitor_dir, visitor_id)
        except WorkspaceError:
            pass
        return {"ok": True}

    @app.post("/api/owner/visitors/{visitor_id}/credentials")
    async def owner_new_credential(request: Request, visitor_id: str):
        _require_owner(request)
        state = _get_state(request)
        visitor = await asyncio.to_thread(state.database.get_visitor, visitor_id)
        if visitor is None:
            return _json_error(404, "visitor not found")
        token, token_hash, token_prefix = dbm.generate_token(slugify_name(visitor["name"]))
        cred = await asyncio.to_thread(state.database.create_credential,
                                       visitor_id, token_hash, token_prefix)
        return {"credential": {k: cred[k] for k in ("id", "token_prefix", "created_at")},
                "token": token}

    @app.post("/api/owner/credentials/{cred_id}/revoke")
    async def owner_revoke_credential(request: Request, cred_id: str):
        _require_owner(request)
        state = _get_state(request)
        ok = await asyncio.to_thread(state.database.revoke_credential, cred_id)
        if not ok:
            return _json_error(404, "credential not found or already revoked")
        return {"ok": True}

    @app.get("/api/owner/visitors/{visitor_id}/sessions/{session_key}/messages")
    async def owner_read_messages(request: Request, visitor_id: str, session_key: str):
        _require_owner(request)
        state = _get_state(request)
        if not _SESSION_KEY_RE.match(session_key):
            return _json_error(404, "session not found")
        session = await asyncio.to_thread(state.database.get_session,
                                          visitor_id, session_key)
        if session is None:
            return _json_error(404, "session not found")
        if state.agent is None:
            return _json_error(503, "agent unavailable")
        try:
            messages = await state.agent.history(visitor_id, session_key)
        except AgentError:
            return _json_error(503, "agent unavailable")
        return {"session_key": session_key, "messages": messages}

    @app.get("/api/owner/cards")
    async def owner_list_cards(request: Request):
        _require_owner(request)
        state = _get_state(request)
        unread_only = request.query_params.get("unread") in ("1", "true")
        visitor_id = request.query_params.get("visitor_id")
        cards = await asyncio.to_thread(state.database.list_cards,
                                        unread_only=unread_only, visitor_id=visitor_id)
        return {"cards": cards}

    @app.post("/api/owner/cards/{card_id}/status")
    async def owner_card_status(request: Request, card_id: str):
        _require_owner(request)
        state = _get_state(request)
        body = await request.json()
        status = (body.get("status") or "").strip()
        try:
            card = await asyncio.to_thread(state.database.update_card_status,
                                           card_id, status)
        except ValueError:
            return _json_error(400, "status must be unread|read|resolved")
        if card is None:
            return _json_error(404, "card not found")
        return {"card": card}

    @app.get("/api/owner/visitors/{visitor_id}/workspace")
    async def owner_list_workspace(request: Request, visitor_id: str):
        _require_owner(request)
        state = _get_state(request)
        visitor = await asyncio.to_thread(state.database.get_visitor, visitor_id)
        if visitor is None:
            return _json_error(404, "visitor not found")
        files = await asyncio.to_thread(state.workspaces.list_files, visitor_id)
        return {"files": files}

    @app.get("/api/owner/visitors/{visitor_id}/workspace/{filename:path}")
    async def owner_read_workspace_file(request: Request, visitor_id: str, filename: str):
        _require_owner(request)
        state = _get_state(request)
        try:
            content = await asyncio.to_thread(state.workspaces.read_file,
                                              visitor_id, filename)
        except FileNotFoundError:
            return _json_error(404, "file not found")
        except WorkspaceError:
            return _json_error(400, "invalid path")
        return {"path": filename, "content": content}

    @app.put("/api/owner/visitors/{visitor_id}/workspace/{filename:path}")
    async def owner_write_workspace_file(request: Request, visitor_id: str, filename: str):
        _require_owner(request)
        state = _get_state(request)
        visitor = await asyncio.to_thread(state.database.get_visitor, visitor_id)
        if visitor is None:
            return _json_error(404, "visitor not found")
        body = await request.json()
        content = body.get("content")
        if not isinstance(content, str) or len(content) > 500_000:
            return _json_error(400, "content must be a string up to 500k chars")
        try:
            await asyncio.to_thread(state.workspaces.write_file, visitor_id,
                                    filename, content)
        except WorkspaceError:
            return _json_error(400, "invalid path or file name")
        # The RAG index rebuilds lazily on next search (mtime fingerprint).
        state.rag.invalidate(visitor_id)
        return {"ok": True}


# ----------------------------------------------------------------------
# frontend + health
# ----------------------------------------------------------------------

def _register_frontend(app: FastAPI) -> None:
    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/{full_path:path}")
    async def spa(request: Request, full_path: str):
        state = _get_state(request)
        frontend = state.settings.frontend_dir
        if full_path:
            candidate = (frontend / full_path).resolve()
            try:
                candidate.relative_to(frontend.resolve())
            except ValueError:
                return _json_error(404, "not found")
            if candidate.is_file():
                return FileResponse(candidate)
        index = frontend / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(status_code=404, content={"error": "frontend not built"})
