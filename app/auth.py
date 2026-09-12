"""Authentication — visitor credentials and owner token.

Security model (spec §5/§6/§7/§20):
- The visitor credential is a simple persistent token, shown once at creation.
- Only sha256(token) is stored; lookups are by hash. Plaintext never logged.
- Token → visitor → workspace resolution happens exclusively server-side.
- The owner token is separate, compared with constant-time equality.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from . import db as dbm
from .config import Settings
from .workspaces import WorkspaceManager, slugify_name


class AuthError(Exception):
    """Raised on any authentication failure (fail-closed)."""

    def __init__(self, message: str = "authentication required"):
        super().__init__(message)
        self.message = message


@dataclass
class VisitorAuth:
    visitor: dict
    credential: dict

    @property
    def visitor_id(self) -> str:
        return self.visitor["id"]


def hash_owner_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_owner_token(provided: str, settings: Settings) -> bool:
    if not settings.owner_token or not provided:
        return False
    return hmac.compare_digest(provided, settings.owner_token)


def authenticate_visitor(token: str, database: dbm.Database) -> VisitorAuth:
    """Resolve token → credential → visitor. Raises AuthError on failure."""
    if not token or not isinstance(token, str) or len(token) > 256:
        raise AuthError("invalid token")
    cred = database.get_credential_by_hash(dbm.hash_token(token))
    if cred is None:
        raise AuthError("invalid token")
    visitor = database.get_visitor(cred["visitor_id"])
    if visitor is None:
        # Credential without visitor should not exist; fail closed.
        raise AuthError("invalid token")
    return VisitorAuth(visitor=visitor, credential=cred)


def new_visitor_with_credential(
    database: dbm.Database,
    workspaces: WorkspaceManager,
    name: str,
    relationship: str = "",
    disclosure_boundary: str = "",
) -> tuple[dict, str]:
    """Create visitor + workspace dir + first credential. Returns (visitor, plaintext_token)."""
    visitor = database.create_visitor(name=name, relationship=relationship,
                                      disclosure_boundary=disclosure_boundary)
    workspaces.ensure_visitor_dir(visitor["id"])
    name_hint = slugify_name(name)
    token, token_hash, token_prefix = dbm.generate_token(name_hint)
    database.create_credential(visitor["id"], token_hash, token_prefix)
    return visitor, token
