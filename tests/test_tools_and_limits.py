"""Rate limiting + card + tool isolation tests."""

from __future__ import annotations

import asyncio

import pytest

from app.agent_tools import RagSearchTool, SubmitCardTool
from app.rate_limit import TokenBucketLimiter

pytestmark = pytest.mark.asyncio


async def test_rate_limit_login_per_ip(client):
    c, h = client
    # login_ip capacity is 10 — exhaust it
    statuses = []
    for _ in range(12):
        res = await c.post("/api/visitor/login", json={"token": "bad-token"})
        statuses.append(res.status_code)
    assert 429 in statuses  # eventually limited
    assert statuses[-1] == 429


async def test_rate_limiter_unit():
    limiter = TokenBucketLimiter()
    limiter.add_rule("r", capacity=2, refill_per_second=1000)
    assert limiter.check("r", "a") is True
    assert limiter.check("r", "a") is True
    assert limiter.check("r", "a") is False  # exhausted
    assert limiter.check("r", "b") is True   # different key independent
    limiter.reset()
    assert limiter.check("r", "a") is True


async def test_rate_limiter_fail_closed_on_unknown_rule():
    limiter = TokenBucketLimiter()
    assert limiter.check("never_registered", "x") is False


async def test_rate_limiter_eviction():
    limiter = TokenBucketLimiter(max_buckets=10)
    limiter.add_rule("r", capacity=1000, refill_per_second=1000)
    for i in range(50):
        limiter.check("r", f"key-{i}")
    # internal state stays bounded
    assert len(limiter._buckets) <= 10


async def test_daily_chat_quota(client):
    c, h = client
    visitor, token = await h.make_visitor("QuotaUser")
    st = h.app.state.silentary
    st.settings.chat_daily_quota = 2  # tighten for the test
    auth = {"Authorization": f"Bearer {token}"}
    res = await c.post("/api/visitor/sessions", headers=auth)
    sk = res.json()["session_key"]
    for i in range(2):
        res = await c.post("/api/visitor/chat", headers=auth,
                           json={"session_key": sk, "message": f"m{i}"})
        assert res.status_code == 200
    res = await c.post("/api/visitor/chat", headers=auth,
                       json={"session_key": sk, "message": "m3"})
    assert res.status_code == 429
    assert "quota" in res.json()["error"]


async def test_card_created_via_tool_direct(client):
    """submit_card writes a card row associated with the right visitor."""
    c, h = client
    visitor, _ = await h.make_visitor("Eve")
    st = h.app.state.silentary

    from unittest.mock import patch

    from app import agent_tools as at

    ctx = type("Ctx", (), {"session_key": f"visitor:{visitor['id']}:sess1"})()
    with patch.object(at, "current_request_context", return_value=ctx):
        tool = SubmitCardTool(st.database)
        result = await tool.execute(summary="Visitor needs owner decision",
                                    context="Asked about X on Tuesday")
        assert "Card submitted" in result

    cards = st.database.list_cards(visitor_id=visitor["id"])
    assert len(cards) == 1
    assert cards[0]["summary"] == "Visitor needs owner decision"
    assert cards[0]["session_key"] == "sess1"
    assert cards[0]["status"] == "unread"


async def test_card_tool_refuses_without_binding():
    """No request context -> tool must refuse (fail-closed)."""
    from unittest.mock import patch

    from app import agent_tools as at
    from app.db import Database
    from nanobot.agent.tools.base import ToolResult

    tool = SubmitCardTool(Database(":memory:"))
    with patch.object(at, "current_request_context", return_value=None):
        result = await tool.execute(summary="sneaky")
    assert isinstance(result, ToolResult)


async def test_card_tool_refuses_foreign_session_format():
    from unittest.mock import patch

    from app import agent_tools as at
    from app.db import Database
    from nanobot.agent.tools.base import ToolResult

    tool = SubmitCardTool(Database(":memory:"))
    ctx = type("Ctx", (), {"session_key": "cli:direct"})()  # not visitor:*:*
    with patch.object(at, "current_request_context", return_value=ctx):
        result = await tool.execute(summary="sneaky")
    assert isinstance(result, ToolResult)


async def test_owner_card_endpoints(client):
    c, h = client
    visitor, _ = await h.make_visitor("Frank")
    st = h.app.state.silentary
    st.database.create_card(visitor["id"], "sess9", "Need decision", None)

    res = await c.get("/api/owner/cards?unread=1", headers=h.owner_headers)
    assert res.status_code == 200
    cards = res.json()["cards"]
    assert any(c["summary"] == "Need decision" for c in cards)
    card_id = cards[0]["id"]

    res = await c.post(f"/api/owner/cards/{card_id}/status",
                       headers=h.owner_headers, json={"status": "read"})
    assert res.status_code == 200

    res = await c.post(f"/api/owner/cards/{card_id}/status",
                       headers=h.owner_headers, json={"status": "bogus"})
    assert res.status_code == 400


async def test_rag_search_tool_scoped_and_graceful(client):
    c, h = client
    visitor, _ = await h.make_visitor("Grace")
    st = h.app.state.silentary
    st.workspaces.write_file(visitor["id"], "facts.md",
                             "Grace adopted a cat named Whiskers in 2023.")

    tool = RagSearchTool(st.rag)

    from unittest.mock import patch

    from app import agent_tools as at
    ctx = type("Ctx", (), {"session_key": f"visitor:{visitor['id']}:s1"})()

    with patch.object(at, "current_request_context", return_value=ctx):
        out = await tool.execute(query="What pet does Grace have?")
        assert "Whiskers" in out

    # no binding -> graceful refusal
    with patch.object(at, "current_request_context", return_value=None):
        out = await tool.execute(query="anything")
        assert "unavailable" in out


async def test_rag_isolation_between_visitors(client):
    """RAG for visitor A must not surface visitor B's documents."""
    c, h = client
    va, _ = await h.make_visitor("AliceR")
    vb, _ = await h.make_visitor("BobR")
    st = h.app.state.silentary
    st.workspaces.write_file(va["id"], "secret.md", "The vault code is 4471.")
    st.workspaces.write_file(vb["id"], "other.md", "BobR enjoys gardening on weekends.")

    from unittest.mock import patch

    from app import agent_tools as at
    from app.agent_tools import RagSearchTool

    tool = RagSearchTool(st.rag)
    ctx_b = type("Ctx", (), {"session_key": f"visitor:{vb['id']}:s"})()
    with patch.object(at, "current_request_context", return_value=ctx_b):
        out = await tool.execute(query="vault code 4471")
        assert "4471" not in out

    ctx_a = type("Ctx", (), {"session_key": f"visitor:{va['id']}:s"})()
    with patch.object(at, "current_request_context", return_value=ctx_a):
        out = await tool.execute(query="vault code")
        assert "4471" in out
