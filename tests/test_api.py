"""Core API tests: auth, visitors, sessions, chat flow (stub agent)."""

from __future__ import annotations

import pytest

from app.main import create_app
from tests.conftest import StubAgentService

pytestmark = pytest.mark.asyncio


async def test_health(client):
    c, h = client
    res = await c.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


async def test_visitor_login_and_wrong_token(client):
    c, h = client
    _, token = await h.make_visitor("Alice")

    res = await c.post("/api/visitor/login", json={"token": token})
    assert res.status_code == 200
    assert res.json() == {"visitor_name": "Alice"}

    res = await c.post("/api/visitor/login", json={"token": "nope-bad"})
    assert res.status_code == 401


async def test_visitor_routes_require_token(client):
    c, h = client
    res = await c.get("/api/visitor/sessions")
    assert res.status_code == 401
    res = await c.post("/api/visitor/chat", json={"session_key": "x", "message": "hi"})
    assert res.status_code == 401


async def test_create_session_and_chat_flow(client):
    c, h = client
    _, token = await h.make_visitor("Bob")
    auth = {"Authorization": f"Bearer {token}"}

    res = await c.post("/api/visitor/sessions", headers=auth)
    assert res.status_code == 200
    session_key = res.json()["session_key"]

    res = await c.post("/api/visitor/chat", headers=auth,
                       json={"session_key": session_key, "message": "Hello!"})
    assert res.status_code == 200
    assert res.json()["reply"] == "Stub reply to: Hello!"

    res = await c.get(f"/api/visitor/sessions/{session_key}/messages", headers=auth)
    assert res.status_code == 200
    msgs = res.json()["messages"]
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "Hello!"
    assert msgs[-1]["role"] == "assistant"

    res = await c.get("/api/visitor/sessions", headers=auth)
    keys = [s["session_key"] for s in res.json()["sessions"]]
    assert session_key in keys


async def test_chat_on_foreign_session_key_fails_closed(client):
    """A visitor must not chat into a session they don't own (spec §20)."""
    c, h = client
    _, t_alice = await h.make_visitor("Alice")
    _, t_bob = await h.make_visitor("Bob")
    alice_auth = {"Authorization": f"Bearer {t_alice}"}

    res = await c.post("/api/visitor/sessions", headers=alice_auth)
    alice_session = res.json()["session_key"]

    # Bob tries to use Alice's session key.
    bob_auth = {"Authorization": f"Bearer {t_bob}"}
    res = await c.post("/api/visitor/chat", headers=bob_auth,
                       json={"session_key": alice_session, "message": "intrude"})
    assert res.status_code == 404  # fail-closed: session invisible to Bob

    # Bob also can't read Alice's transcript.
    res = await c.get(f"/api/visitor/sessions/{alice_session}/messages", headers=bob_auth)
    assert res.status_code == 404


async def test_delete_own_session_only(client):
    c, h = client
    _, t_alice = await h.make_visitor("Alice")
    _, t_bob = await h.make_visitor("Bob")

    res = await c.post("/api/visitor/sessions", headers={"Authorization": f"Bearer {t_alice}"})
    alice_session = res.json()["session_key"]

    res = await c.delete(f"/api/visitor/sessions/{alice_session}",
                         headers={"Authorization": f"Bearer {t_bob}"})
    assert res.status_code == 404

    res = await c.delete(f"/api/visitor/sessions/{alice_session}",
                         headers={"Authorization": f"Bearer {t_alice}"})
    assert res.status_code == 200


async def test_owner_auth_required(client):
    c, h = client
    res = await c.get("/api/owner/visitors")
    assert res.status_code == 401
    res = await c.get("/api/owner/visitors",
                      headers={"Authorization": "Bearer wrong"})
    assert res.status_code == 401


async def test_owner_full_flow(client):
    c, h = client
    auth = h.owner_headers

    # create visitor
    res = await c.post("/api/owner/visitors", headers=auth,
                       json={"name": "Charlie", "relationship": "colleague",
                             "disclosure_boundary": "Never mention project X."})
    assert res.status_code == 200
    data = res.json()
    visitor, token = data["visitor"], data["token"]
    assert token.startswith("charlie-")

    # token works
    res = await c.post("/api/visitor/login", json={"token": token})
    assert res.status_code == 200

    # visitor list shows them
    res = await c.get("/api/owner/visitors", headers=auth)
    names = [v["name"] for v in res.json()["visitors"]]
    assert "Charlie" in names

    # revoke
    creds = (await c.get(f"/api/owner/visitors/{visitor['id']}", headers=auth)).json()["credentials"]
    cred_id = creds[0]["id"]
    res = await c.post(f"/api/owner/credentials/{cred_id}/revoke", headers=auth)
    assert res.status_code == 200

    # revoked token stops working
    res = await c.post("/api/visitor/login", json={"token": token})
    assert res.status_code == 401


async def test_workspace_file_editing_and_path_escape(client):
    c, h = client
    auth = h.owner_headers
    res = await c.post("/api/owner/visitors", headers=auth, json={"name": "Dana"})
    visitor = res.json()["visitor"]
    vid = visitor["id"]

    # write a file
    res = await c.put(f"/api/owner/visitors/{vid}/workspace/profile.md",
                      headers=auth, json={"content": "# Dana\n\nLikes tea."})
    assert res.status_code == 200

    # path escape attempts must be rejected (405 = client/httpx collapsed the
    # path to a route without PUT, which still rejects the request)
    for bad in ["../evil.md", "..%2Fevil.md", "sub/../../evil.md", "/etc/passwd", "a\\..\\..\\evil.md"]:
        res = await c.put(f"/api/owner/visitors/{vid}/workspace/{bad}",
                          headers=auth, json={"content": "x"})
        assert res.status_code in (400, 404, 405), bad

    # normal read/write works
    res = await c.get(f"/api/owner/visitors/{vid}/workspace/profile.md", headers=auth)
    assert res.status_code == 200
    assert res.json()["content"].startswith("# Dana")


async def test_owner_dashboard_served(client):
    c, h = client
    res = await c.get("/admin")
    assert res.status_code == 200
    assert "Owner dashboard" in res.text


async def test_owner_token_high_bytes_gets_401_not_500(client):
    """Bearer tokens with high bytes must be rejected cleanly (L2).

    httpx refuses non-ASCII headers client-side, so this unit-tests the
    comparison function directly (the actual transport path uses latin-1).
    """
    c, h = client
    from app.auth import verify_owner_token
    st = h.app.state.silentary
    assert verify_owner_token("café".encode("utf-8").decode("latin-1"), st.settings) is False
    assert verify_owner_token("café", st.settings) is False  # unicode str: no TypeError
    assert verify_owner_token("test-owner-token", st.settings) is True


async def test_delete_visitor_removes_transcripts(client):
    """Owner delete must also delete nanobot transcripts (privacy, M3)."""
    c, h = client
    res = await c.post("/api/owner/visitors", headers=h.owner_headers,
                       json={"name": "Vanish"})
    data = res.json()
    vid, token = data["visitor"]["id"], data["token"]

    # have a conversation (stub agent records history)
    auth = {"Authorization": f"Bearer {token}"}
    sk = (await c.post("/api/visitor/sessions", headers=auth)).json()["session_key"]
    await c.post("/api/visitor/chat", headers=auth,
                 json={"session_key": sk, "message": "remember me"})

    # delete the visitor
    res = await c.delete(f"/api/owner/visitors/{vid}", headers=h.owner_headers)
    assert res.status_code == 200

    # the adapter's delete_all_sessions must have been invoked for this visitor
    assert vid in h.agent.deleted_all
    assert h.agent.history_store.get((vid, sk)) is None


# ----------------------------------------------------------------------
# restart persistence (spec §23: conversations survive restart)
# ----------------------------------------------------------------------

def _bootstrap_state(app):
    """Sync AppState bootstrap mirroring conftest.app_env (no lifespan)."""
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
    state.agent = None


async def test_state_survives_restart(tmp_path, monkeypatch):
    """Boot the app twice over the same data dirs (simulated process restart).

    Everything the spec marks as persistent must carry over: credentials,
    visitor record, session list, workspace files, and cards. Conversation
    content itself is owned by nanobot (JSONL on disk); the stub-agent
    history store carried across boots stands in for those transcripts.
    """
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setenv("SILENTARY_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("SILENTARY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SILENTARY_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SILENTARY_OWNER_TOKEN", "test-owner-token")
    monkeypatch.setenv("SILENTARY_PROVIDER_API_KEY", "")

    # --- first boot: create data ---
    app1 = create_app()
    _bootstrap_state(app1)
    agent1 = StubAgentService()
    app1.state.silentary.agent = agent1

    async with AsyncClient(transport=ASGITransport(app=app1),
                           base_url="http://test") as c1:
        oah = {"Authorization": "Bearer test-owner-token"}
        res = await c1.post("/api/owner/visitors", headers=oah,
                            json={"name": "Phoenix", "disclosure_boundary": "No flight talk."})
        data = res.json()
        vid, token = data["visitor"]["id"], data["token"]

        auth = {"Authorization": f"Bearer {token}"}
        sk = (await c1.post("/api/visitor/sessions", headers=auth)).json()["session_key"]
        await c1.post("/api/visitor/chat", headers=auth,
                      json={"session_key": sk, "message": "hello before restart"})
        await c1.put(f"/api/owner/visitors/{vid}/workspace/notes.md", headers=oah,
                     json={"content": "# Notes\n\nSurvives restarts."})
        app1.state.silentary.database.create_card(vid, sk, "Card before restart", None)

    # --- "shutdown": DB commits per statement; nothing else to flush ---

    # --- second boot over the SAME dirs ---
    app2 = create_app()
    _bootstrap_state(app2)
    agent2 = StubAgentService()
    # carry over the transcript store — stands in for nanobot-owned JSONL
    agent2.history_store = agent1.history_store
    app2.state.silentary.agent = agent2

    async with AsyncClient(transport=ASGITransport(app=app2),
                           base_url="http://test") as c2:
        oah = {"Authorization": "Bearer test-owner-token"}

        # credential still authenticates
        res = await c2.post("/api/visitor/login", json={"token": token})
        assert res.status_code == 200

        # session list survives
        res = await c2.get("/api/visitor/sessions", headers=auth)
        assert res.status_code == 200
        assert sk in [s["session_key"] for s in res.json()["sessions"]]

        # transcript survives
        res = await c2.get(f"/api/visitor/sessions/{sk}/messages", headers=auth)
        assert res.status_code == 200
        msgs = res.json()["messages"]
        assert msgs[0]["content"] == "hello before restart"
        assert msgs[-1]["role"] == "assistant"

        # workspace file survives
        res = await c2.get(f"/api/owner/visitors/{vid}/workspace/notes.md", headers=oah)
        assert res.status_code == 200
        assert "Survives restarts." in res.json()["content"]

        # card survives
        res = await c2.get("/api/owner/cards", headers=oah)
        assert any(c["summary"] == "Card before restart" for c in res.json()["cards"])
