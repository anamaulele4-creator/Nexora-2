from __future__ import annotations

from fastapi import Response

ACCESS_COOKIE = "ca_access"
REFRESH_COOKIE = "ca_refresh"
REFRESH_PATH = "/api/auth"
# Marcador sem valor secreto, visível em todas as rotas: deixa o middleware do Next saber
# que existe sessão (o refresh cookie só circula em /api/auth).
SESSION_MARKER_COOKIE = "ca_session"


def set_auth_cookies(
    response: Response, *, access_token: str, refresh_token: str, access_ttl: int, refresh_ttl: int,
    secure: bool, domain: str | None,
) -> None:
    response.set_cookie(
        ACCESS_COOKIE, access_token, max_age=access_ttl, httponly=True, secure=secure,
        samesite="lax", path="/", domain=domain,
    )
    response.set_cookie(
        REFRESH_COOKIE, refresh_token, max_age=refresh_ttl, httponly=True, secure=secure,
        samesite="lax", path=REFRESH_PATH, domain=domain,
    )
    response.set_cookie(
        SESSION_MARKER_COOKIE, "1", max_age=refresh_ttl, httponly=True, secure=secure,
        samesite="lax", path="/", domain=domain,
    )


def clear_auth_cookies(response: Response, *, secure: bool, domain: str | None) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/", domain=domain, secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH, domain=domain, secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(SESSION_MARKER_COOKIE, path="/", domain=domain, secure=secure, httponly=True, samesite="lax")
