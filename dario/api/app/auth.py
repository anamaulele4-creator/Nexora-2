"""Autenticação por token Bearer. Sem token configurado, a API recusa tudo (falha fechada)."""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token em falta",
                            headers={"WWW-Authenticate": "Bearer"})
    return token.strip()


def _matches(token: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(token.encode(), expected.encode())


def require_user(request: Request, settings: Settings = Depends(get_settings)) -> str:
    token = _bearer(request)
    if _matches(token, settings.admin_token):
        return "admin"
    if _matches(token, settings.api_token):
        return "user"
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido", headers={"WWW-Authenticate": "Bearer"})


def require_admin(role: str = Depends(require_user)) -> str:
    if role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Precisa do token de administração")
    return role
