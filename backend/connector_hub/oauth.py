"""OAuth 2.0 dos conectores (GitHub e Google) e verificação do Shopify, só com httpx.

* O ``state`` é aleatório, de uso único, guardado no Redis só como hash SHA-256 e ligado ao
  utilizador e ao conector que iniciou o pedido (expira em 10 minutos).
* Google: PKCE (S256), ``access_type=offline`` e ``prompt=consent`` para receber refresh token.
* Os tokens nunca vão para logs nem para o browser: só são gravados cifrados (ver ``vault``).
* Erros dos fornecedores viram ``ConnectorError`` com mensagens curtas e seguras.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from connectors.base import ConnectorError
from core.config import Settings

STATE_TTL_SECONDS = 600
_STATE_PREFIX = "oauth_state:"

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

ENV_VARS = {
    "github": ("GITHUB_OAUTH_CLIENT_ID", "GITHUB_OAUTH_CLIENT_SECRET"),
    "google": ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
}


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_at: int | None = None  # epoch (s)
    scope: str = ""
    token_type: str = "bearer"

    def to_payload(self) -> dict:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "expires_at": self.expires_at, "scope": self.scope, "token_type": self.token_type}

    @classmethod
    def from_payload(cls, data: dict) -> "TokenSet":
        return cls(access_token=data["access_token"], refresh_token=data.get("refresh_token"),
                   expires_at=data.get("expires_at"), scope=data.get("scope") or "",
                   token_type=data.get("token_type") or "bearer")

    def expired(self, skew: int = 60) -> bool:
        return self.expires_at is not None and self.expires_at <= int(time.time()) + skew


@dataclass(frozen=True)
class Identity:
    account_id: str
    label: str


@dataclass(frozen=True)
class OAuthState:
    user_id: str
    connector: str
    provider: str
    code_verifier: str | None = None
    extra: dict = field(default_factory=dict)


# --- configuração -----------------------------------------------------------------------------------
def client_credentials(settings: Settings, provider: str) -> tuple[str, str] | None:
    if provider == "github":
        cid, secret = settings.github_oauth_client_id, settings.github_oauth_client_secret
    elif provider == "google":
        cid, secret = settings.google_oauth_client_id, settings.google_oauth_client_secret
    else:
        return None
    if not cid or secret is None:
        return None
    return cid, secret.get_secret_value()


def oauth_configured(settings: Settings, provider: str) -> bool:
    return client_credentials(settings, provider) is not None


def callback_path(provider: str) -> str:
    return f"/api/connectors/oauth/{provider}/callback"


def redirect_uri(settings: Settings, provider: str) -> str:
    return settings.oauth_redirect_base + callback_path(provider)


def _require_credentials(settings: Settings, provider: str) -> tuple[str, str]:
    creds = client_credentials(settings, provider)
    if creds is None:
        cid_var, secret_var = ENV_VARS.get(provider, ("?", "?"))
        raise ConnectorError("oauth_not_configured",
                             f"OAuth do {provider} não está configurado no servidor ({cid_var} e {secret_var})")
    return creds


# --- state (Redis) ----------------------------------------------------------------------------------
def _state_key(state: str) -> str:
    return _STATE_PREFIX + hashlib.sha256(state.encode()).hexdigest()


async def save_state(redis, data: OAuthState) -> str:
    state = secrets.token_urlsafe(32)
    await redis.set(_state_key(state), json.dumps(data.__dict__), ex=STATE_TTL_SECONDS)
    return state


async def consume_state(redis, state: str) -> OAuthState | None:
    """Lê e apaga numa só operação (uso único)."""
    if not state or len(state) > 200:
        return None
    key = _state_key(state)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.get(key)
        pipe.delete(key)
        raw, _ = await pipe.execute()
    if not raw:
        return None
    data = json.loads(raw)
    return OAuthState(**data)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


# --- início do fluxo --------------------------------------------------------------------------------
async def start_authorization(settings: Settings, redis, *, provider: str, connector: str, user_id: uuid.UUID,
                              scopes: list[str], extra: dict | None = None) -> str:
    """Devolve o URL de autorização do fornecedor (o browser é redirecionado para lá)."""
    client_id, _ = _require_credentials(settings, provider)
    verifier = None
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(settings, provider),
        "scope": " ".join(scopes),
    }
    if provider == "google":
        verifier, challenge = _pkce_pair()
        params.update({"response_type": "code", "access_type": "offline", "prompt": "consent",
                       "code_challenge": challenge, "code_challenge_method": "S256"})
        base = GOOGLE_AUTHORIZE_URL
    else:
        params["allow_signup"] = "false"
        base = GITHUB_AUTHORIZE_URL
    state = await save_state(redis, OAuthState(user_id=str(user_id), connector=connector, provider=provider,
                                               code_verifier=verifier, extra=extra or {}))
    params["state"] = state
    return f"{base}?{urlencode(params)}"


# --- chamadas HTTP ----------------------------------------------------------------------------------
def _http(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "CONNECTOR-AI"})


def _short(text: object) -> str:
    return re.sub(r"\s+", " ", str(text))[:200]


async def exchange_code(settings: Settings, provider: str, code: str, code_verifier: str | None) -> TokenSet:
    client_id, client_secret = _require_credentials(settings, provider)
    data = {"client_id": client_id, "client_secret": client_secret, "code": code,
            "redirect_uri": redirect_uri(settings, provider)}
    url = GITHUB_TOKEN_URL
    if provider == "google":
        url = GOOGLE_TOKEN_URL
        data["grant_type"] = "authorization_code"
        if code_verifier:
            data["code_verifier"] = code_verifier
    try:
        async with _http(settings.connector_http_timeout_seconds) as http:
            resp = await http.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ConnectorError("oauth_exchange_failed", f"Erro de rede ao falar com {provider}", retryable=True) from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    # o GitHub responde 200 com {"error": ...} quando o código é inválido
    if resp.status_code >= 400 or not isinstance(body, dict) or body.get("error") or not body.get("access_token"):
        detail = (body.get("error_description") or body.get("error")) if isinstance(body, dict) else None
        raise ConnectorError("oauth_exchange_failed",
                             f"{provider} recusou o código de autorização ({_short(detail or resp.status_code)})")
    expires_in = body.get("expires_in")
    return TokenSet(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=int(time.time()) + int(expires_in) if expires_in else None,
        scope=str(body.get("scope") or "").replace(",", " "),
        token_type=str(body.get("token_type") or "bearer"),
    )


async def refresh_google(settings: Settings, tokens: TokenSet) -> TokenSet:
    if not tokens.refresh_token:
        raise ConnectorError("token_expired", "O acesso expirou e não há refresh token: volta a ligar a conta")
    client_id, client_secret = _require_credentials(settings, "google")
    data = {"client_id": client_id, "client_secret": client_secret, "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token}
    try:
        async with _http(settings.connector_http_timeout_seconds) as http:
            resp = await http.post(GOOGLE_TOKEN_URL, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ConnectorError("connector_unreachable", "Erro de rede ao renovar o acesso Google", retryable=True) from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or not isinstance(body, dict) or not body.get("access_token"):
        raise ConnectorError("token_expired", "A Google recusou renovar o acesso: volta a ligar a conta")
    return TokenSet(access_token=body["access_token"], refresh_token=body.get("refresh_token") or tokens.refresh_token,
                    expires_at=int(time.time()) + int(body.get("expires_in") or 3600),
                    scope=str(body.get("scope") or tokens.scope), token_type=str(body.get("token_type") or "Bearer"))


async def fetch_identity(settings: Settings, provider: str, access_token: str) -> Identity:
    """Quem é a conta ligada. Também serve de verificação de saúde (o token funciona?)."""
    if provider == "github":
        url = f"{GITHUB_API}/user"
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
    else:
        url = GOOGLE_USERINFO_URL
        headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with _http(settings.connector_http_timeout_seconds) as http:
            resp = await http.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ConnectorError("connector_unreachable", f"Erro de rede ao contactar {provider}", retryable=True) from exc
    if resp.status_code in (401, 403):
        raise ConnectorError("token_invalid", f"{provider} recusou o token (revogado ou expirado)")
    if resp.status_code >= 400:
        raise ConnectorError("connector_unreachable", f"{provider} respondeu HTTP {resp.status_code}", retryable=True)
    data = resp.json()
    if provider == "github":
        return Identity(account_id=str(data["id"]), label=str(data.get("login") or data["id"]))
    return Identity(account_id=str(data["sub"]), label=str(data.get("email") or data["sub"]))


async def revoke_remote(settings: Settings, provider: str, tokens: TokenSet) -> bool:
    """Revoga o acesso no fornecedor (melhor esforço: a ligação local é apagada de qualquer forma)."""
    try:
        async with _http(settings.connector_http_timeout_seconds) as http:
            if provider == "github":
                creds = client_credentials(settings, "github")
                if creds is None:
                    return False
                resp = await http.request("DELETE", f"{GITHUB_API}/applications/{creds[0]}/grant",
                                          auth=(creds[0], creds[1]), json={"access_token": tokens.access_token},
                                          headers={"Accept": "application/vnd.github+json"})
                return resp.status_code in (204, 404)
            if provider == "google":
                token = tokens.refresh_token or tokens.access_token
                resp = await http.post(GOOGLE_REVOKE_URL, data={"token": token})
                return resp.status_code in (200, 400)  # 400 = já não era válido
    except httpx.HTTPError:
        return False
    return False


# --- Shopify (domínio da loja + token de Admin API) -----------------------------------------------------
_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}\.myshopify\.com$")


def normalize_shop_domain(value: str) -> str:
    """Aceita 'loja', 'loja.myshopify.com' ou 'https://loja.myshopify.com/admin'."""
    v = value.strip().lower()
    v = re.sub(r"^https?://", "", v).split("/")[0]
    if "." not in v:
        v = f"{v}.myshopify.com"
    if not _SHOP_RE.match(v):
        raise ConnectorError("invalid_shop_domain", "Domínio inválido: usa o domínio *.myshopify.com da loja")
    return v


async def verify_shopify(settings: Settings, shop: str, token: str) -> Identity:
    url = f"https://{shop}/admin/api/{settings.shopify_api_version}/shop.json"
    try:
        async with _http(settings.connector_http_timeout_seconds) as http:
            resp = await http.get(url, headers={"X-Shopify-Access-Token": token, "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ConnectorError("connector_unreachable", "Não foi possível contactar a loja Shopify", retryable=True) from exc
    if resp.status_code in (401, 403):
        raise ConnectorError("invalid_shopify_token", "O Shopify recusou o token de Admin API")
    if resp.status_code == 404:
        raise ConnectorError("invalid_shop_domain", "Loja Shopify não encontrada")
    if resp.status_code >= 400:
        raise ConnectorError("connector_unreachable", f"Shopify respondeu HTTP {resp.status_code}", retryable=True)
    shop_data = (resp.json() or {}).get("shop") or {}
    return Identity(account_id=str(shop_data.get("id") or shop), label=str(shop_data.get("name") or shop))
