from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status

from api.deps import (
    client_info,
    current_principal,
    enforce_rate_limit,
    get_auth,
    get_db,
    get_settings_dep,
    optional_principal,
)
from api.schemas import (
    EmailIn,
    LoginIn,
    MeOut,
    RefreshIn,
    RegisterIn,
    RegisterOut,
    ResetPasswordIn,
    SessionOut,
    TokenIn,
    WorkspaceOut,
)
from authentication.cookies import REFRESH_COOKIE, clear_auth_cookies, set_auth_cookies
from authentication.providers.base import AuthProviderBase, TokenPair, unauthorized
from authentication.tokens import Principal
from core.config import Settings
from core.db import Database
from core.errors import ApiError

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _wants_tokens(request: Request) -> bool:
    # Clientes de API (CLI, mobile) pedem os tokens no corpo; o browser usa só cookies httpOnly.
    return request.headers.get("x-client-type", "").lower() == "api"


async def load_me(db: Database, user_id: uuid.UUID) -> MeOut:
    async with db.as_user(user_id) as conn:
        row = await conn.fetchrow(
            """select u.id, u.email, u.email_verified_at, u.is_admin, u.created_at,
                      p.display_name, p.locale, p.timezone, p.theme,
                      w.id as ws_id, w.name as ws_name, w.slug as ws_slug, w.plan as ws_plan
               from public.users u
               left join public.user_profiles p on p.user_id = u.id
               left join public.workspaces w on w.id = p.default_workspace_id
               where u.id = $1""",
            user_id,
        )
    if row is None:
        raise unauthorized()
    ws = (WorkspaceOut(id=row["ws_id"], name=row["ws_name"], slug=row["ws_slug"], plan=row["ws_plan"])
          if row["ws_id"] else None)
    return MeOut(
        id=row["id"], email=row["email"], email_verified=row["email_verified_at"] is not None,
        is_admin=row["is_admin"], display_name=row["display_name"], locale=row["locale"] or "en",
        timezone=row["timezone"] or "UTC", theme=row["theme"] or "dark", created_at=row["created_at"],
        default_workspace=ws,
    )


async def _session_response(request: Request, response: Response, pair: TokenPair, settings: Settings,
                            db: Database) -> SessionOut:
    set_auth_cookies(
        response, access_token=pair.access_token, refresh_token=pair.refresh_token,
        access_ttl=pair.expires_in, refresh_ttl=settings.refresh_token_ttl_seconds,
        secure=settings.cookie_secure, domain=settings.cookie_domain,
    )
    out = SessionOut(expires_in=pair.expires_in, user=await load_me(db, pair.user_id))
    if _wants_tokens(request):
        out.access_token, out.refresh_token = pair.access_token, pair.refresh_token
    return out


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=RegisterOut)
async def register(body: RegisterIn, request: Request, auth: AuthProviderBase = Depends(get_auth)) -> RegisterOut:
    client = client_info(request)
    await enforce_rate_limit(request, "register_ip", client.ip or "unknown")
    result = await auth.register(body.email, body.password, body.display_name, client)
    return RegisterOut(user_id=result.user_id, verification_required=result.verification_required)


@router.post("/login", response_model=SessionOut, response_model_exclude_none=True)
async def login(body: LoginIn, request: Request, response: Response,
                auth: AuthProviderBase = Depends(get_auth), settings: Settings = Depends(get_settings_dep),
                db: Database = Depends(get_db)) -> SessionOut:
    client = client_info(request)
    await enforce_rate_limit(request, "login_ip", client.ip or "unknown")
    await enforce_rate_limit(request, "login_email", body.email)
    pair = await auth.login(body.email, body.password, client)
    return await _session_response(request, response, pair, settings, db)


@router.post("/refresh", response_model=SessionOut, response_model_exclude_none=True)
async def refresh(request: Request, response: Response, body: RefreshIn | None = None,
                  auth: AuthProviderBase = Depends(get_auth), settings: Settings = Depends(get_settings_dep),
                  db: Database = Depends(get_db)) -> SessionOut:
    token = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise unauthorized("Sem refresh token")
    try:
        pair = await auth.refresh(token, client_info(request))
    except ApiError:
        clear_auth_cookies(response, secure=settings.cookie_secure, domain=settings.cookie_domain)
        raise
    return await _session_response(request, response, pair, settings, db)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, body: RefreshIn | None = None,
                 principal: Principal | None = Depends(optional_principal),
                 auth: AuthProviderBase = Depends(get_auth), settings: Settings = Depends(get_settings_dep)) -> Response:
    token = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE)
    await auth.logout(principal, token, client_info(request))
    response.status_code = status.HTTP_204_NO_CONTENT
    clear_auth_cookies(response, secure=settings.cookie_secure, domain=settings.cookie_domain)
    return response


@router.get("/me", response_model=MeOut)
async def me(principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> MeOut:
    return await load_me(db, principal.user_id)


@router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT)
async def verify_email(body: TokenIn, request: Request, auth: AuthProviderBase = Depends(get_auth)) -> Response:
    await enforce_rate_limit(request, "verify_ip", client_info(request).ip or "unknown")
    await auth.verify_email(body.token, client_info(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/resend-verification", status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(body: EmailIn, request: Request, auth: AuthProviderBase = Depends(get_auth)) -> dict:
    await enforce_rate_limit(request, "resend_email", body.email)
    await auth.resend_verification(body.email, client_info(request))
    return {"status": "accepted"}


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(body: EmailIn, request: Request, auth: AuthProviderBase = Depends(get_auth)) -> dict:
    client = client_info(request)
    await enforce_rate_limit(request, "forgot_ip", client.ip or "unknown")
    await enforce_rate_limit(request, "forgot_email", body.email)
    await auth.request_password_reset(body.email, client)
    # Resposta igual exista ou não a conta.
    return {"status": "accepted"}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(body: ResetPasswordIn, request: Request, auth: AuthProviderBase = Depends(get_auth)) -> Response:
    await enforce_rate_limit(request, "reset_ip", client_info(request).ip or "unknown")
    await auth.reset_password(body.token, body.new_password, client_info(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
