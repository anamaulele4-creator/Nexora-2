"""Conectores: catálogo, ligação de contas (GitHub, Gmail, Google Drive, Shopify) e estado.

* GitHub, Gmail e Google Drive ligam-se por OAuth 2.0 (``/oauth/{github|google}/start`` e
  ``/callback``); o Shopify liga-se com o domínio da loja e um token de Admin API.
* Os tokens ficam só cifrados (``connector_hub.vault``) e nunca saem do servidor: a API
  devolve apenas o estado e a conta ligada (login, email ou nome da loja).
* O catálogo continua 'planned' até as ferramentas do agente existirem; ``connectable``
  diz se a conta já pode ser ligada.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import Field

from api.deps import client_info, current_principal, enforce_limit, get_db, optional_principal, safe_ip
from api.schemas import _In
from authentication.tokens import Principal
from connector_hub import oauth
from connector_hub.registry import OAUTH_PROVIDERS, connection_spec, connectors_for_provider
from connector_hub.vault import (
    VaultError,
    delete_connection,
    epoch_to_dt,
    load_connection,
    record_health,
    save_connection,
    update_payload,
)
from connectors.base import ConnectorError
from core.config import Settings
from core.db import Database
from core.errors import ApiError
from security.audit import record_audit

log = logging.getLogger("connector_ai.connectors")

router = APIRouter(prefix="/api/connectors", tags=["connectors"])

CONNECT_LIMIT_PER_15MIN = 20


class ConnectIn(_In):
    # Só para o Shopify (os outros usam OAuth e ignoram o corpo)
    shop_domain: str | None = Field(default=None, max_length=255)
    access_token: str | None = Field(default=None, max_length=512)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _cipher(request: Request):
    cipher = request.app.state.cipher
    if cipher is None:
        raise ApiError(503, "encryption_not_configured",
                       "CREDENTIALS_ENCRYPTION_KEY não está configurada no servidor; não é possível guardar tokens.")
    return cipher


def _connector_error(exc: ConnectorError) -> ApiError:
    status_code = {"oauth_not_configured": 503, "invalid_shop_domain": 422, "invalid_shopify_token": 422,
                   "connector_unreachable": 502, "oauth_exchange_failed": 502}.get(exc.code, 400)
    return ApiError(status_code, exc.code, str(exc))


async def _definition(db: Database, principal: Principal, key: str) -> dict:
    async with db.as_user(principal.user_id) as conn:
        row = await conn.fetchrow(
            """select id, key, name, auth_type, scopes from public.connector_definitions where key = $1""", key)
    spec = connection_spec(key)
    if row is None or spec is None:
        raise ApiError(404, "connector_not_found", "Conector desconhecido ou ainda sem ligação disponível")
    return dict(row)


def _connection_out(r) -> dict | None:
    if r is None or r["app_status"] is None:
        return None
    return {
        "status": r["app_status"],
        "account_label": r["external_account_label"],
        "connected_at": r["app_updated_at"].isoformat() if r["app_updated_at"] else None,
        "last_health_check_at": r["last_health_check_at"].isoformat() if r["last_health_check_at"] else None,
        "last_health_status": r["last_health_status"],
        "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
    }


def _item(r, settings: Settings) -> dict:
    spec = connection_spec(r["key"])
    return {
        "key": r["key"], "name": r["name"], "description": r["description"], "category": r["category"],
        "auth_type": r["auth_type"], "status": r["status"], "planned_stage": r["planned_stage"],
        "version": r["version"],
        "connectable": spec is not None,
        "method": spec.method if spec else None,
        "oauth_provider": spec.oauth_provider if spec else None,
        "oauth_configured": oauth.oauth_configured(settings, spec.oauth_provider) if spec and spec.oauth_provider else None,
        "connection": _connection_out(r),
    }


_LIST_SQL = """select d.key, d.name, d.description, d.category, d.auth_type, d.status, d.planned_stage, d.version,
                      a.status as app_status, a.external_account_label, a.updated_at as app_updated_at,
                      a.last_health_check_at, a.last_health_status, a.revoked_at
               from public.connector_definitions d
               left join public.connected_apps a on a.connector_definition_id = d.id and a.user_id = $1"""


@router.get("")
async def list_connectors(request: Request, principal: Principal = Depends(current_principal),
                          db: Database = Depends(get_db)) -> dict:
    """Catálogo visível ao utilizador, com o estado da ligação de cada conector (RLS)."""
    async with db.as_user(principal.user_id) as conn:
        rows = await conn.fetch(_LIST_SQL + " order by d.planned_stage nulls last, d.name", principal.user_id)
    settings = _settings(request)
    return {"items": [_item(r, settings) for r in rows], "encryption_ready": request.app.state.cipher is not None}


async def _status(db: Database, principal: Principal, key: str, settings: Settings) -> dict:
    async with db.as_user(principal.user_id) as conn:
        row = await conn.fetchrow(_LIST_SQL + " where d.key = $2", principal.user_id, key)
    return _item(row, settings)


# --- ligar ------------------------------------------------------------------------------------------
def _return_extra(return_to: str | None, conversation: uuid.UUID | None) -> dict:
    """Para onde voltar depois do OAuth. Só destinos internos conhecidos (nada de URLs livres)."""
    if return_to != "chat":
        return {}
    extra = {"return_to": "chat"}
    if conversation is not None:
        extra["conversation"] = str(conversation)
    return extra


async def _start_oauth(request: Request, principal: Principal, db: Database, key: str,
                       extra: dict | None = None) -> dict:
    definition = await _definition(db, principal, key)
    spec = connection_spec(key)
    assert spec is not None
    if spec.method != "oauth" or spec.oauth_provider is None:
        raise ApiError(400, "wrong_connect_method", "Este conector não usa OAuth")
    _cipher(request)
    await enforce_limit(request, "connector_connect", str(principal.user_id), CONNECT_LIMIT_PER_15MIN, 15 * 60)
    scopes = list(dict.fromkeys([*definition["scopes"], *spec.identity_scopes]))
    try:
        url = await oauth.start_authorization(_settings(request), request.app.state.redis, provider=spec.oauth_provider,
                                              connector=key, user_id=principal.user_id, scopes=scopes,
                                              extra=extra)
    except ConnectorError as exc:
        raise _connector_error(exc) from exc
    return {"connector": key, "method": "oauth", "authorize_url": url}


@router.get("/oauth/{provider}/start")
async def oauth_start(provider: str, request: Request, connector: str | None = Query(None, max_length=64),
                      principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> dict:
    """URL de autorização do fornecedor. ``connector`` escolhe gmail ou google_drive (Google)."""
    if provider not in OAUTH_PROVIDERS:
        raise ApiError(404, "unknown_provider", "Fornecedor OAuth desconhecido (usa github ou google)")
    options = connectors_for_provider(provider)
    key = connector or (options[0] if len(options) == 1 else None)
    if key not in options:
        raise ApiError(422, "validation_error", f"Indica o conector: {', '.join(options)}")
    return await _start_oauth(request, principal, db, key)


@router.post("/{key}/connect")
async def connect(key: str, request: Request, body: ConnectIn | None = None,
                  return_to: str | None = Query(None, pattern="^chat$"),
                  conversation: uuid.UUID | None = Query(None),
                  principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> dict:
    """``return_to=chat`` (opcional, com ``conversation``): depois do OAuth volta ao chat em vez de /conectores."""
    spec = connection_spec(key)
    if spec is None:
        raise ApiError(404, "connector_not_found", "Conector desconhecido ou ainda sem ligação disponível")
    if spec.method == "oauth":
        return await _start_oauth(request, principal, db, key, _return_extra(return_to, conversation))

    # Shopify: domínio + token de Admin API, verificados na loja antes de gravar
    definition = await _definition(db, principal, key)
    cipher = _cipher(request)
    if body is None or not body.shop_domain or not body.access_token:
        raise ApiError(422, "validation_error", "Indica o domínio da loja e o token de Admin API")
    token = body.access_token.strip()
    if len(token) < 20 or any(c.isspace() for c in token):
        raise ApiError(422, "invalid_shopify_token", "Token de Admin API inválido")
    await enforce_limit(request, "connector_connect", str(principal.user_id), CONNECT_LIMIT_PER_15MIN, 15 * 60)
    settings = _settings(request)
    try:
        shop = oauth.normalize_shop_domain(body.shop_domain)
        identity = await oauth.verify_shopify(settings, shop, token)
    except ConnectorError as exc:
        raise _connector_error(exc) from exc
    client = client_info(request)
    async with db.service() as conn:
        await save_connection(
            conn, cipher, user_id=principal.user_id, connector_key=key, display_name=definition["name"],
            account_id=identity.account_id, account_label=f"{identity.label} ({shop})",
            credential_type="api_key", payload={"access_token": token, "shop_domain": shop}, expires_at=None,
            metadata={"shop_domain": shop, "api_version": settings.shopify_api_version})
        await record_audit(conn, "connector.connected", user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent, details={"connector": key, "shop_domain": shop})
    return await _status(db, principal, key, settings)


def _frontend_redirect(settings: Settings, *, _extra: dict | None = None, **params: str) -> RedirectResponse:
    page = "/conectores"
    if _extra and _extra.get("return_to") == "chat":
        page = "/chat"
        conv = _extra.get("conversation")
        try:
            if conv:
                params["conversation"] = str(uuid.UUID(str(conv)))
        except ValueError:
            pass
    return RedirectResponse(f"{settings.frontend_url}{page}?{urlencode(params)}", status_code=302)


@router.get("/oauth/{provider}/callback", include_in_schema=False)
async def oauth_callback(provider: str, request: Request, code: str | None = Query(None, max_length=2048),
                         state: str | None = Query(None, max_length=200),
                         error: str | None = Query(None, max_length=200),
                         principal: Principal | None = Depends(optional_principal),
                         db: Database = Depends(get_db)) -> Response:
    """Retorno do fornecedor. Termina sempre com um redirecionamento para /conectores (ou /chat, se o pedido veio do chat)."""
    settings = _settings(request)
    if provider not in OAUTH_PROVIDERS:
        return _frontend_redirect(settings, error="unknown_provider")
    saved = await oauth.consume_state(request.app.state.redis, state or "")
    if saved is None or saved.provider != provider:
        return _frontend_redirect(settings, error="oauth_state_invalid")
    key = saved.connector
    user_id = uuid.UUID(saved.user_id)
    client = client_info(request)
    back = saved.extra or {}
    if principal is not None and principal.user_id != user_id:
        return _frontend_redirect(settings, error="oauth_state_invalid", connector=key)

    async def fail(code_: str) -> Response:
        async with db.service() as conn:
            await record_audit(conn, "connector.oauth_failed", user_id=user_id, ip=safe_ip(client.ip),
                               user_agent=client.user_agent, details={"connector": key, "reason": code_})
        return _frontend_redirect(settings, _extra=back, error=code_, connector=key)

    if error:
        return await fail("access_denied" if error == "access_denied" else "oauth_provider_error")
    if not code:
        return await fail("oauth_provider_error")
    cipher = request.app.state.cipher
    if cipher is None:
        return await fail("encryption_not_configured")
    try:
        tokens = await oauth.exchange_code(settings, provider, code, saved.code_verifier)
        identity = await oauth.fetch_identity(settings, provider, tokens.access_token)
    except ConnectorError as exc:
        log.warning("OAuth falhou", extra={"fields": {"connector": key, "code": exc.code}})
        return await fail(exc.code)

    async with db.service() as conn:
        name = await conn.fetchval("select name from public.connector_definitions where key = $1", key)
        try:
            await save_connection(
                conn, cipher, user_id=user_id, connector_key=key, display_name=name or key,
                account_id=identity.account_id, account_label=identity.label, credential_type="oauth_token",
                payload=tokens.to_payload(), expires_at=epoch_to_dt(tokens.expires_at),
                metadata={"provider": provider, "scopes": tokens.scope.split(), "has_refresh_token":
                          bool(tokens.refresh_token)})
        except VaultError:
            return _frontend_redirect(settings, _extra=back, error="connector_error", connector=key)
        await record_audit(conn, "connector.connected", user_id=user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent, details={"connector": key, "account": identity.label})
    return _frontend_redirect(settings, _extra=back, connected=key)


# --- estado e desligar ------------------------------------------------------------------------------
async def _live_check(request: Request, principal: Principal, db: Database, key: str) -> dict:
    settings = _settings(request)
    spec = connection_spec(key)
    assert spec is not None
    cipher = _cipher(request)
    async with db.service() as conn:
        stored = await load_connection(conn, cipher, user_id=principal.user_id, connector_key=key)
    if stored is None or stored.status == "revoked":
        return {"ok": False, "code": "not_connected", "message": "Conta não ligada"}
    if stored.payload is None:
        async with db.service() as conn:
            await record_health(conn, user_id=principal.user_id, app_id=stored.id, ok=False, status="error")
        return {"ok": False, "code": "credential_unreadable",
                "message": "Não foi possível decifrar o token (a chave de cifra mudou?). Volta a ligar a conta."}
    try:
        if spec.method == "api_key":
            await oauth.verify_shopify(settings, stored.payload["shop_domain"], stored.payload["access_token"])
        else:
            tokens = oauth.TokenSet.from_payload(stored.payload)
            if spec.oauth_provider == "google" and tokens.expired():
                tokens = await oauth.refresh_google(settings, tokens)
                async with db.service() as conn:
                    await update_payload(conn, cipher, user_id=principal.user_id, credential_id=stored.credential_id,
                                         payload=tokens.to_payload(), expires_at=epoch_to_dt(tokens.expires_at))
            await oauth.fetch_identity(settings, spec.oauth_provider or "", tokens.access_token)
    except ConnectorError as exc:
        expired = exc.code in ("token_invalid", "token_expired", "invalid_shopify_token")
        async with db.service() as conn:
            await record_health(conn, user_id=principal.user_id, app_id=stored.id, ok=False,
                                status="expired" if expired else None)
        return {"ok": False, "code": exc.code, "message": str(exc)}
    async with db.service() as conn:
        await record_health(conn, user_id=principal.user_id, app_id=stored.id, ok=True, status="connected")
    return {"ok": True, "code": "ok", "message": "Ligação a funcionar"}


@router.get("/{key}/status")
async def connector_status(key: str, request: Request, check: bool = Query(False),
                           principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> dict:
    """Estado guardado; com ``check=true`` testa o token no fornecedor (e renova o da Google)."""
    await _definition(db, principal, key)
    result = None
    if check:
        await enforce_limit(request, "connector_check", str(principal.user_id), 60, 15 * 60)
        result = await _live_check(request, principal, db, key)
    out = await _status(db, principal, key, _settings(request))
    out["check"] = result
    return out


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(key: str, request: Request, principal: Principal = Depends(current_principal),
                     db: Database = Depends(get_db)) -> Response:
    """Revoga no fornecedor (melhor esforço), apaga o token cifrado e marca a ligação como revogada."""
    await _definition(db, principal, key)
    spec = connection_spec(key)
    assert spec is not None
    cipher = request.app.state.cipher
    async with db.service() as conn:
        stored = await load_connection(conn, cipher, user_id=principal.user_id, connector_key=key)
    if stored is None or stored.status == "revoked":
        raise ApiError(404, "not_connected", "Este conector não está ligado")
    remote = False
    if stored.payload and spec.oauth_provider:
        remote = await oauth.revoke_remote(_settings(request), spec.oauth_provider,
                                           oauth.TokenSet.from_payload(stored.payload))
    client = client_info(request)
    async with db.service() as conn:
        await delete_connection(conn, user_id=principal.user_id, app_id=stored.id)
        await record_audit(conn, "connector.disconnected", user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent, details={"connector": key, "remote_revoked": remote})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
