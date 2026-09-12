"""Workspace management — per-visitor Markdown directories on disk.

Layout: {workspaces_dir}/{visitor_id}/*.md

visitor_id is always a DB-issued UUID hex string, so the directory namespace is
not client-influenceable. Every path returned by this module is validated with
resolve() + relative_to() against the workspace root (fail-closed, spec §20).
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,119}\.md$")


class WorkspaceError(Exception):
    pass


class WorkspaceManager:
    def __init__(self, workspaces_root: Path):
        self.root = Path(workspaces_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------

    def visitor_dir(self, visitor_id: str) -> Path:
        """Directory for one visitor. Fails closed on any malformed id."""
        if not visitor_id or not re.fullmatch(r"[0-9a-f]{32}", visitor_id):
            raise WorkspaceError("invalid visitor id")
        path = (self.root / visitor_id).resolve()
        if path.parent != self.root:
            raise WorkspaceError("workspace path escape blocked")
        return path

    def ensure_visitor_dir(self, visitor_id: str) -> Path:
        path = self.visitor_dir(visitor_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def delete_visitor_dir(self, visitor_id: str) -> None:
        import shutil
        path = self.visitor_dir(visitor_id)
        if path.exists():
            shutil.rmtree(path)

    # ------------------------------------------------------------------

    def list_files(self, visitor_id: str) -> list[dict]:
        """List markdown files in a workspace."""
        base = self.visitor_dir(visitor_id)
        if not base.exists():
            return []
        files = []
        for p in sorted(base.rglob("*.md")):
            resolved = p.resolve()
            try:
                rel = resolved.relative_to(base)
            except ValueError:
                continue  # symlink escape — skip
            if resolved.is_file():
                files.append({
                    "path": str(PurePosixPath(rel.as_posix())),
                    "size": resolved.stat().st_size,
                })
        return files

    def read_file(self, visitor_id: str, rel_path: str) -> str:
        base = self.visitor_dir(visitor_id)
        path = self._resolve_within(base, rel_path)
        if not path.is_file():
            raise FileNotFoundError(rel_path)
        return path.read_text(encoding="utf-8")

    def write_file(self, visitor_id: str, rel_path: str, content: str) -> None:
        base = self.visitor_dir(visitor_id)
        path = self._resolve_within(base, rel_path)
        if not _FILENAME_RE.match(rel_path.replace("\\", "/").split("/")[-1]):
            raise WorkspaceError("invalid file name")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def read_all_markdown(self, visitor_id: str) -> list[tuple[str, str]]:
        """All (rel_path, content) pairs — used by RAG indexing and context."""
        docs = []
        base = self.visitor_dir(visitor_id)
        if not base.exists():
            return docs
        for p in sorted(base.rglob("*.md")):
            resolved = p.resolve()
            try:
                rel = resolved.relative_to(base)
            except ValueError:
                continue
            if resolved.is_file():
                try:
                    docs.append((rel.as_posix(), resolved.read_text(encoding="utf-8")))
                except (OSError, UnicodeDecodeError):
                    continue
        return docs

    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_within(base: Path, rel_path: str) -> Path:
        """Resolve rel_path under base; raise WorkspaceError on any escape.

        base is resolved first so comparisons are against its canonical form
        (Windows 8.3 short names like WENYUA~1 would otherwise mismatch the
        expanded path of the candidate and cause false rejections).
        """
        if not rel_path or "\x00" in rel_path:
            raise WorkspaceError("invalid path")
        base_resolved = base.resolve()
        candidate = (base_resolved / rel_path).resolve()
        try:
            candidate.relative_to(base_resolved)
        except ValueError:
            raise WorkspaceError("path escape blocked") from None
        return candidate


def slugify_name(name: str) -> str:
    """Turn a visitor display name into a token prefix hint (a-z0-9-)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug or "visitor")[:16]
