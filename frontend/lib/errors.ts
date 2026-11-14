import type { MessageKey } from "./i18n.ts";

const KNOWN = new Set([
  "invalid_credentials", "email_not_verified", "email_taken", "weak_password", "account_locked",
  "rate_limited", "invalid_or_expired_token", "validation_error",
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
