"""RAG adapter — per-workspace MicroRAG instances.

Isolation (spec §10): one MicroRAG index per visitor workspace. The index is
built ONLY from that workspace's Markdown files. A query can never reach another
workspace because the instance itself is workspace-scoped and the workspace is
resolved server-side from the authenticated visitor.

Indexing strategy: lazily built on first use per workspace, invalidated when the
workspace's file set/mtimes change (cheap stat-based check). RAG failures degrade
gracefully — the tool returns "retrieval unavailable" text to the agent instead of
raising into the conversation (spec §17).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

from .config import Settings
from .workspaces import WorkspaceManager

logger = logging.getLogger("silentary.rag")

_SILENTARY_MICRORAG_NOTE = (
    "Search results are excerpts from the visitor workspace knowledge base."
)


class RagIndex:
    """One MicroRAG index over one visitor workspace."""

    def __init__(self, workspace_dir: Path, settings: Settings):
        self.workspace_dir = workspace_dir
        self.settings = settings
        self._rag = None
        self._fingerprint: tuple | None = None
        self._lock = threading.Lock()

    def _fingerprint_of(self) -> tuple:
        """Cheap stat fingerprint of workspace markdown files."""
        entries = []
        try:
            for p in sorted(self.workspace_dir.rglob("*.md")):
                if p.is_file():
                    st = p.stat()
                    entries.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            return ("<error>",)
        return tuple(entries)

    def _build_locked(self) -> None:
        from microrag import MicroRAG, RAGConfig

        docs = self._workspace_docs()
        cfg = RAGConfig(
            db_path=":memory:",
            similarity_threshold=0.0,  # rely on top-k; hybrid RRF scores are small
            remove_stopwords=False,
            hybrid_enabled=True,
        )
        rag = MicroRAG(cfg)
        if docs:
            rag.add_documents([content for _, content in docs])
            rag.build_index()
        self._rag = rag
        self._fingerprint = self._fingerprint_of()

    def _workspace_docs(self) -> list[tuple[str, str]]:
        from .workspaces import WorkspaceManager  # local import to avoid cycle

        # WorkspaceManager is not needed here; read directly under the dir.
        docs = []
        for p in sorted(self.workspace_dir.rglob("*.md")):
            resolved = p.resolve()
            try:
                rel = resolved.relative_to(self.workspace_dir)
            except ValueError:
                continue
            if resolved.is_file():
                try:
                    docs.append((rel.as_posix(), resolved.read_text(encoding="utf-8")))
                except (OSError, UnicodeDecodeError):
                    continue
        return docs

    def search(self, query: str, top_k: int | None = None) -> list[dict]:
        """Search this workspace's index. Returns list of {source, content, score}."""
        with self._lock:
            fp = self._fingerprint_of()
            if self._rag is None or fp != self._fingerprint:
                self._build_locked()
            if self._rag is None:
                return []
            # Import locally so unit tests can stub microrag if needed.
            from microrag import MicroRAG  # noqa: F401

            k = top_k or self.settings.rag_top_k
            try:
                results = self._rag.search(query, top_k=k)
            except Exception:
                logger.exception("rag search failed")
                return []
            return [
                {"source": "workspace", "score": float(r.score),
                 "content": r.content[:1200]}
                for r in results
            ]

    def close(self) -> None:
        with self._lock:
            if self._rag is not None:
                try:
                    self._rag.close()
                except Exception:
                    pass
                self._rag = None


class RagService:
    """Owns one RagIndex per visitor workspace (built lazily)."""

    def __init__(self, settings: Settings, workspaces: WorkspaceManager,
                 max_instances: int = 16):
        self.settings = settings
        self.workspaces = workspaces
        self._indices: dict[str, RagIndex] = {}
        self._order: list[str] = []
        self._max = max_instances
        self._lock = threading.Lock()

    def search(self, visitor_id: str, query: str, top_k: int | None = None) -> list[dict]:
        """Workspace-scoped search. Never raises; returns [] on failure."""
        try:
            ws_dir = self.workspaces.visitor_dir(visitor_id)
        except Exception:
            logger.warning("rag: refusing unknown workspace")
            return []
        try:
            index = self._get_index(visitor_id, ws_dir)
        except Exception:
            logger.exception("rag: index build failed")
            return []
        try:
            return index.search(query, top_k=top_k)
        except Exception:
            logger.exception("rag: search failed")
            return []

    def _get_index(self, visitor_id: str, ws_dir: Path) -> RagIndex:
        with self._lock:
            index = self._indices.get(visitor_id)
            if index is None:
                index = RagIndex(ws_dir, self.settings)
                self._indices[visitor_id] = index
                self._order.append(visitor_id)
                # naive LRU cap
                while len(self._order) > self._max:
                    oldest = self._order.pop(0)
                    old = self._indices.pop(oldest, None)
                    if old:
                        old.close()
            return index

    def invalidate(self, visitor_id: str) -> None:
        with self._lock:
            idx = self._indices.pop(visitor_id, None)
        if idx:
            idx.close()

    def close_all(self) -> None:
        with self._lock:
            indices = list(self._indices.values())
            self._indices.clear()
            self._order.clear()
        for idx in indices:
            idx.close()
