"""Tokens da API pública v1.

Formato: ``cai_`` + 43 caracteres aleatórios (256 bits, base64url). Na BD fica só o
SHA-256 do token (suficiente para segredos de alta entropia gerados pelo servidor) e
um prefixo curto para o admin reconhecer a chave. O token completo é mostrado uma vez.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_PREFIX = "cai_"
DISPLAY_PREFIX_LEN = 12


def generate_token() -> tuple[str, str, str]:
    """Devolve (token, prefixo para mostrar, hash)."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, token[:DISPLAY_PREFIX_LEN], hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_token(token: str) -> bool:
    return token.startswith(TOKEN_PREFIX) and 20 <= len(token) <= 128


def same_hash(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
