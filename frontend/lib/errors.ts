import type { MessageKey } from "./i18n.ts";

const KNOWN = new Set([
  "invalid_credentials", "email_not_verified", "email_taken", "weak_password", "account_locked",
  "rate_limited", "invalid_or_expired_token", "validation_error",
  "forbidden", "ai_unavailable", "ai_provider_error", "message_too_long", "key_required",
  "encryption_not_configured", "not_found", "local_engine_not_configured",
  "invalid_current_password", "same_password", "paid_provider_disabled", "not_supported", "cannot_revoke_current",
  "oauth_not_configured", "invalid_shop_domain", "invalid_shopify_token", "connector_not_found", "not_connected",
]);

// Erros do motor local: a mensagem do servidor é específica e útil (ex.: modelo em falta, RAM).
const SERVER_MESSAGE = new Set([
  "ai_provider_error", "local_engine_unreachable", "local_engine_auth", "local_model_missing", "ram_limit_exceeded",
  "ram_unknown", "default_model_required", "base_url_required", "model_is_default", "model_not_found",
  "invalid_fallback", "local_engine_error", "pull_failed", "unknown_provider",
]);

/** Converte o código de erro da API numa chave de tradução. */
export function errorKey(code: string | undefined): MessageKey {
  if (code === "network") return "error.network";
  return (code && KNOWN.has(code) ? `error.${code}` : "error.generic") as MessageKey;
}

/** Mesmas regras de palavra-passe que o backend (authentication/passwords.py). */
export function passwordProblems(pw: string): string[] {
  const problems: string[] = [];
  if (pw.length < 10) problems.push("min_length");
  if (pw.length > 128) problems.push("max_length");
  if (pw.toLowerCase() === pw || pw.toUpperCase() === pw) problems.push("mixed_case");
  if (!/[0-9]/.test(pw)) problems.push("digit");
  return problems;
}

/** Texto de erro para mostrar: traduzido, exceto falhas do fornecedor de IA (a mensagem do servidor é útil). */
export function errorText(
  err: unknown,
  t: (key: MessageKey) => string,
): string {
  const e = err as { code?: string; message?: string } | null;
  if (e?.code && SERVER_MESSAGE.has(e.code) && e.message) return e.message;
  return t(errorKey(e?.code));
}
