// Ajudas puras (sem React) das páginas /conectores e /definicoes. Testadas em tests/connectors.test.ts.

import type { MessageKey } from "./i18n.ts";

/** Códigos de erro que o backend põe em /conectores?error=… depois do retorno OAuth. */
const CALLBACK_ERRORS = new Set([
  "access_denied", "oauth_state_invalid", "oauth_exchange_failed", "oauth_provider_error", "oauth_not_configured",
  "encryption_not_configured", "connector_unreachable", "token_invalid", "connector_error", "unknown_provider",
]);

export type CallbackFeedback =
  | { tone: "success"; key: MessageKey; connector: string }
  | { tone: "error"; key: MessageKey; connector: string | null }
  | null;

/** Interpreta ?connected=github ou ?error=access_denied&connector=gmail (retorno do OAuth). */
export function callbackFeedback(search: string): CallbackFeedback {
  const params = new URLSearchParams(search);
  const connected = params.get("connected");
  if (connected && /^[a-z0-9_]{2,64}$/.test(connected)) {
    return { tone: "success", key: "conn.callback.connected", connector: connected };
  }
  const error = params.get("error");
  if (!error) return null;
  const connector = params.get("connector");
  const code = CALLBACK_ERRORS.has(error) ? error : "generic";
  return {
    tone: "error",
    key: `conn.callback.${code}` as MessageKey,
    connector: connector && /^[a-z0-9_]{2,64}$/.test(connector) ? connector : null,
  };
}

/** Só aceitamos URLs de autorização dos fornecedores conhecidos (defesa contra open redirect). */
const AUTHORIZE_HOSTS = new Set(["github.com", "accounts.google.com"]);

export function isTrustedAuthorizeUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return u.protocol === "https:" && AUTHORIZE_HOSTS.has(u.hostname);
  } catch {
    return false;
  }
}

/** Mesma normalização que o backend: "loja" -> "loja.myshopify.com". Devolve null se inválido. */
export function normalizeShopDomain(value: string): string | null {
  let v = value.trim().toLowerCase().replace(/^https?:\/\//, "").split("/")[0] ?? "";
  if (!v) return null;
  if (!v.includes(".")) v = `${v}.myshopify.com`;
  return /^[a-z0-9][a-z0-9-]{0,59}\.myshopify\.com$/.test(v) ? v : null;
}

/** Descrição curta de um user agent para a lista de sessões ("Chrome · Android"). */
export function describeUserAgent(ua: string | null | undefined): string {
  if (!ua) return "—";
  const browser =
    /Edg\//.test(ua) ? "Edge"
    : /OPR\/|Opera/.test(ua) ? "Opera"
    : /Firefox\//.test(ua) ? "Firefox"
    : /Chrome\//.test(ua) ? "Chrome"
    : /Safari\//.test(ua) ? "Safari"
    : /python-httpx|curl|python-requests/i.test(ua) ? "API"
    : null;
  const os =
    /Android/.test(ua) ? "Android"
    : /iPhone|iPad|iPod/.test(ua) ? "iOS"
    : /Windows/.test(ua) ? "Windows"
    : /Mac OS X|Macintosh/.test(ua) ? "macOS"
    : /Linux/.test(ua) ? "Linux"
    : null;
  if (browser && os) return `${browser} · ${os}`;
  return browser ?? os ?? ua.slice(0, 40);
}

export type ConnectionState = "connected" | "expired" | "error" | "revoked" | "pending" | "none";

/** Estado a mostrar no cartão de um conector. */
export function connectionState(connection: { status: string } | null | undefined): ConnectionState {
  const s = connection?.status;
  if (s === "connected" || s === "expired" || s === "error" || s === "revoked" || s === "pending") return s;
  return "none";
}
