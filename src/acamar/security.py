"""Access-token hashing — the single definition shared by scripts and the API.

Tokens are opaque bearer strings. Only their sha256 hex digest is stored
(``subscribers.token_hash``); the API hashes the presented token the same way
and looks for an active row.
"""

from __future__ import annotations

import hashlib

TOKEN_PREFIX = "acamar_"  # noqa: S105 - a public label on every token, not a secret


def hash_token(token: str) -> str:
    """Return the sha256 hex digest stored in ``subscribers.token_hash``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
