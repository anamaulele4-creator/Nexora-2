from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

ALGORITHM = "HS256"
AUDIENCE = "authenticated"


class InvalidToken(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    email: str
    session_id: uuid.UUID | None


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def issue_access_token(
    *, user_id: uuid.UUID, email: str, session_id: uuid.UUID, secret: str, issuer: str, ttl_seconds: int
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "email": email,
        "sid": str(session_id),
        "role": "authenticated",
        "aud": AUDIENCE,
        "iss": issuer,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, *, secret: str, issuer: str | None) -> dict:
    try:
        options = {"require": ["exp", "sub", "aud"]}
        kwargs: dict = {"algorithms": [ALGORITHM], "audience": AUDIENCE, "options": options, "leeway": 5}
        if issuer:
            kwargs["issuer"] = issuer
        return jwt.decode(token, secret, **kwargs)
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc
