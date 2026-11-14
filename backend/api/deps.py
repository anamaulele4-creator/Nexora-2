from __future__ import annotations

from fastapi import Depends, Request

from authentication.cookies import ACCESS_COOKIE
from authentication.providers.base import AuthProviderBase, ClientInfo, unauthorized
from authentication.tokens import Principal
from core.config import Settings
from core.db import Database
from core.errors import ApiError
from security.rate_limit import RateLimiter


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_auth(request: Request) -> AuthProviderBase:
    return request.app.state.auth


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def client_info(request: Request) -> ClientInfo:
    ip = request.client.host if request.client else None
    if request.app.state.settings.trust_forwarded_for:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            ip = fwd.split(",")[0].strip()
    return ClientInfo(ip=ip, user_agent=request.headers.get("user-agent"))


def extract_access_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(ACCESS_COOKIE)


async def optional_principal(request: Request, auth: AuthProviderBase = Depends(get_auth)) -> Principal | None:
    token = extract_access_token(request)
    if not token:
        return None
    try:
        return await auth.authenticate(token)
    except ApiError:
        return None


async def current_principal(request: Request, auth: AuthProviderBase = Depends(get_auth)) -> Principal:
    token = extract_access_token(request)
    if not token:
        raise unauthorized("Autenticação necessária")
    return await auth.authenticate(token)


async def enforce_rate_limit(request: Request, bucket: str, identity: str) -> None:
    limiter: RateLimiter = request.app.state.rate_limiter
    limit = request.app.state.settings.auth_rate_limit_per_15min
    allowed, retry_after = await limiter.hit(bucket, identity, limit, 15 * 60)
    if not allowed:
        raise ApiError(429, "rate_limited", "Demasiados pedidos. Tenta mais tarde.",
                       headers={"Retry-After": str(retry_after or 60)})
