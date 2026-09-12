"""Agent tools — submit_card and rag_search.

Security (spec §20): tools resolve the visitor/workspace from the nanobot
RequestContext session key (`visitor:{visitor_id}:{session_key}`), which is
always constructed server-side by the adapter. Message text is never trusted.

Both tools are nanobot Tool subclasses registered after Nanobot construction
(builtin tools are registered during from_config; custom tools must register
afterwards).
"""

from __future__ import annotations

import asyncio
import logging
import re

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context

logger = logging.getLogger("silentary.agent_tools")

_SESSION_KEY_RE = re.compile(r"^visitor:([0-9a-f]{32}):(.+)$")


def resolve_visitor_binding() -> tuple[str, str] | None:
    """Return (visitor_id, session_key) from the current request context, or None."""
    ctx = current_request_context()
    if ctx is None or not ctx.session_key:
        return None
    m = _SESSION_KEY_RE.match(ctx.session_key)
    if not m:
        return None
    return m.group(1), m.group(2)


class SubmitCardTool(Tool):
    """Agent tool: submit a card for the owner's attention."""

    def __init__(self, database):
        self._database = database

    @property
    def name(self) -> str:
        return "submit_card"

    @property
    def description(self) -> str:
        return (
            "Submit an attention card to the owner (the person you work for). "
            "Use when a matter needs the owner's personal decision, when the visitor "
            "raises an important emotional/personal matter, or when you cannot confirm "
            "information and need the owner. Do not use for ordinary conversation."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Concise summary for the owner (max ~3 sentences).",
                },
                "context": {
                    "type": "string",
                    "description": "Optional longer context: relevant quotes, background, what is being asked.",
                },
            },
            "required": ["summary"],
        }

    async def execute(self, summary: str, context: str | None = None) -> Any:  # noqa: F821
        binding = resolve_visitor_binding()
        if binding is None:
            logger.warning("submit_card called without a valid visitor binding")
            return ToolResult.error("submit_card is unavailable in this context.")
        visitor_id, session_key = binding
        summary = (summary or "").strip()
        if not summary:
            return ToolResult.error("summary must not be empty.")
        summary = summary[:500]
        context = (context or "").strip() or None
        if context:
            context = context[:4000]

        def _persist() -> dict:
            return self._database.create_card(
                visitor_id=visitor_id,
                session_key=session_key,
                summary=summary,
                context=context,
            )

        card = await asyncio.to_thread(_persist)
        logger.info("card submitted: visitor=%s session=%s card=%s",
                    visitor_id, session_key, card["id"])
        return (
            "Card submitted to the agent's owner (your boss). It will be reviewed in "
            "the owner dashboard; do not promise the visitor a specific response time."
        )


class RagSearchTool(Tool):
    """Agent tool: on-demand retrieval from the current visitor's workspace."""

    def __init__(self, rag_service):
        self._rag = rag_service

    @property
    def name(self) -> str:
        return "rag_search"

    @property
    def description(self) -> str:
        return (
            "Search the private knowledge base associated with the current visitor for "
            "relevant background information (past history, preferences, facts the owner "
            "prepared). Use when the conversation touches on past events, personal "
            "details, or anything you are unsure about. On-demand: do not assume you "
            "already know; search first when unsure."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for, phrased as a natural question or keyword query.",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str) -> str:
        binding = resolve_visitor_binding()
        if binding is None:
            logger.warning("rag_search called without a valid visitor binding")
            return "Retrieval is unavailable in this context."
        visitor_id, _session_key = binding
        query = (query or "").strip()[:500]
        if not query:
            return "Query was empty; nothing to search."
        results = await asyncio.to_thread(self._rag.search, visitor_id, query)
        if not results:
            return (
                "No relevant information found in the knowledge base. Do not invent "
                "details; if the answer matters, say you are not sure."
            )
        lines = ["Knowledge base excerpts (most relevant first):"]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] (score {r['score']:.3f}) {r['content']}")
        lines.append(
            "Use only what is relevant; respect the disclosure boundary for this visitor."
        )
        return "\n".join(lines)
