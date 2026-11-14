"""Proteção CSRF para autenticação por cookie.

Pedidos que alteram estado e que chegam com cookie de sessão (sem Authorization)
têm de vir de uma origem permitida.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from authentication.cookies import ACCESS_COOKIE, REFRESH_COOKIE

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class OriginCheckMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowed_origins: list[str]) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._allowed = {o.rstrip("/") for o in allowed_origins}

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        if request.method in UNSAFE and "authorization" not in request.headers:
            uses_cookie = ACCESS_COOKIE in request.cookies or REFRESH_COOKIE in request.cookies
            if uses_cookie:
                origin = (request.headers.get("origin") or "").rstrip("/")
                if not origin:
                    referer = request.headers.get("referer", "")
                    origin = "/".join(referer.split("/")[:3]) if referer else ""
                if origin not in self._allowed:
                    return JSONResponse(
                        {"error": {"code": "origin_not_allowed", "message": "Origem do pedido não permitida"}},
                        status_code=403,
                    )
        return await call_next(request)
