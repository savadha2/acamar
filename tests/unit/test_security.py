"""Token hashing — the contract between scripts.token and the API."""

from __future__ import annotations

from acamar.security import TOKEN_PREFIX, hash_token


def test_hash_is_deterministic_sha256_hex() -> None:
    digest = hash_token("acamar_example")
    assert digest == hash_token("acamar_example")
    assert len(digest) == 64
    assert int(digest, 16) >= 0  # valid hex


def test_distinct_tokens_hash_differently() -> None:
    assert hash_token(TOKEN_PREFIX + "a") != hash_token(TOKEN_PREFIX + "b")
