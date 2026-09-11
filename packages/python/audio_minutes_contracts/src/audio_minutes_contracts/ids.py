"""artifact ID などの生成。推測しにくいランダム ID を使い、クライアント由来のパスを使わない。"""

from __future__ import annotations

import secrets
import uuid

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_artifact_id() -> str:
    """`art_` + 26 文字 (Crockford base32) のランダム ID。"""
    return "art_" + "".join(secrets.choice(_ALPHABET) for _ in range(26))


def is_artifact_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("art_")
        and len(value) == 30
        and all(character in _ALPHABET for character in value[4:])
    )


def new_job_id() -> uuid.UUID:
    return uuid.uuid4()


def new_request_id() -> str:
    return "req_" + secrets.token_hex(12)
