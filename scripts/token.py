"""Create, list, and revoke ACAMAR access tokens (the ``subscribers`` table).

Only the sha256 of a token is stored; the token itself is shown once at
creation and cannot be recovered.

    uv run python -m scripts.token create --label me
    uv run python -m scripts.token list
    uv run python -m scripts.token revoke <subscriber-id>
"""

from __future__ import annotations

import argparse
import secrets

from acamar.security import TOKEN_PREFIX, hash_token
from scripts._db import connect, scalar


def create(label: str) -> None:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with connect() as conn:
        subscriber_id = scalar(
            conn.execute(
                "INSERT INTO subscribers (token_hash, label) VALUES (%s, %s) RETURNING id",
                (hash_token(token), label),
            )
        )
    print(f"created subscriber {subscriber_id}  ({label})\n")
    print(f"    {token}\n")
    print("Store it now — it is not recoverable.")


def revoke(subscriber_id: str) -> None:
    with connect() as conn:
        revoked = conn.execute(
            "UPDATE subscribers SET revoked_at = now() "
            "WHERE id = %s AND revoked_at IS NULL",
            (subscriber_id,),
        ).rowcount
    print("revoked" if revoked else "no active subscriber with that id")


def list_tokens() -> None:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, label, created_at, revoked_at "
            "FROM subscribers ORDER BY created_at"
        ).fetchall()
    if not rows:
        print("no subscribers — create one with:  "
              "uv run python -m scripts.token create --label me")
        return
    for subscriber_id, label, created_at, revoked_at in rows:
        state = "revoked" if revoked_at is not None else "active"
        print(f"{subscriber_id}  {state:7}  {label:12}  created {created_at:%Y-%m-%d}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage ACAMAR access tokens.")
    commands = parser.add_subparsers(dest="command", required=True)

    create_cmd = commands.add_parser("create", help="mint a new token")
    create_cmd.add_argument("--label", required=True, help="who this token is for")

    revoke_cmd = commands.add_parser("revoke", help="disable a token")
    revoke_cmd.add_argument("subscriber_id")

    commands.add_parser("list", help="list all tokens")

    args = parser.parse_args(argv)
    if args.command == "create":
        create(args.label)
    elif args.command == "revoke":
        revoke(args.subscriber_id)
    else:
        list_tokens()


if __name__ == "__main__":
    main()
