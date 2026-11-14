// Cliente da API. Mesmo domínio (rewrites do Next) + cookies httpOnly: o JavaScript
// nunca vê os tokens.

import { parseSseChunk, type SseEvent } from "./sse.ts";

export { parseSseChunk, type SseEvent };

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

type Json = Record<string, unknown>;

async function raw(path: string, init: RequestInit = {}): Promise<Response> {
  try {
    return await fetch(path, {
      ...init,
      credentials: "include",
      headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, "network", "network error");
  }
}

let refreshing: Promise<boolean> | null = null;

async function tryRefresh(): Promise<boolean> {
  refreshing ??= raw("/api/auth/refresh", { method: "POST" })
    .then((r) => r.ok)
    .finally(() => { refreshing = null; });
  return refreshing;
}

export async function api<T = Json>(path: string, init: RequestInit = {}, allowRefresh = true): Promise<T> {
  const res = await raw(path, init);
  if (res.status === 401 && allowRefresh && !path.startsWith("/api/auth/")) {
    if (await tryRefresh()) return api<T>(path, init, false);
  }
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { error?: { code?: string; message?: string } } | null;
    throw new ApiError(res.status, body?.error?.code ?? "http_error", body?.error?.message ?? res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const post = <T = Json>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export type Me = {
  id: string;
  email: string;
  email_verified: boolean;
  is_admin: boolean;
  display_name: string | null;
  locale: string;
  timezone: string;
  theme: "dark" | "light" | "system";
  created_at: string;
  default_workspace: { id: string; name: string; slug: string; plan: string } | null;
};

export type TaskStatus =
  | "queued" | "planning" | "running" | "waiting_approval" | "completed" | "failed" | "cancelled" | "paused";

export type DashboardSummary = {
  generated_at: string;
  tasks: { total: number; in_progress: number; by_status: Record<TaskStatus, number> };
  active_connections: number;
  pending_approvals: number;
  active_browser_sessions: number;
  unread_notifications: number;
  workflows: number;
  connectors: { available: number; planned: number };
  usage_this_month: {
    period_start: string;
    tool_calls: number;
    llm_input_tokens: number;
    llm_output_tokens: number;
    browser_minutes: number;
    tasks_run: number;
  };
  recent_tasks: { id: string; title: string; status: TaskStatus; mode: string; created_at: string; updated_at: string }[];
};

export const put = <T = Json>(path: string, body?: unknown) =>
  api<T>(path, { method: "PUT", body: body === undefined ? undefined : JSON.stringify(body) });

export const del = <T = Json>(path: string) => api<T>(path, { method: "DELETE" });

// --- Chat --------------------------------------------------------------------------
export type Conversation = {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  last_message_at: string | null;
};

export type ChatMessage = {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  content: string;
  provider: string | null;
  model: string | null;
  created_at: string;
};

export type SendMessageResult = {
  conversation: Conversation;
  user_message: ChatMessage;
  assistant_message: ChatMessage;
};

// --- Admin -------------------------------------------------------------------------
export type AdminStats = {
  users: number;
  admins: number;
  users_last_7d: number;
  conversations: number;
  messages: number;
  messages_last_24h: number;
  active_providers: number;
};

export type AdminUser = {
  id: string;
  email: string;
  display_name: string | null;
  is_admin: boolean;
  status: string;
  email_verified: boolean;
  created_at: string;
  last_login_at: string | null;
  conversations: number;
  messages: number;
};

export type AdminUsersPage = { items: AdminUser[]; page: number; page_size: number; total: number; pages: number };

export type AiProvider = {
  provider: "gemini" | "groq" | "openrouter" | "openai";
  configured: boolean;
  key_last4: string | null;
  key_masked: string | null;
  model: string;
  default_model: string;
  enabled: boolean;
  priority: number;
  updated_at: string | null;
  updated_by_email: string | null;
  last_tested_at: string | null;
  last_test_ok: boolean | null;
  last_test_message: string | null;
};

export type AiProvidersResponse = { items: AiProvider[]; encryption_ready: boolean; order: string[] };

export type ProviderTestResult = {
  provider: string;
  model: string;
  ok: boolean;
  message: string;
  reply: string | null;
  latency_ms: number;
};

// --- Streaming (Server-Sent Events por POST) ----------------------------------------------
/**
 * POST que devolve SSE. Erros antes do stream (4xx/5xx em JSON) viram ApiError.
 * ``signal`` permite parar (botão Parar): o servidor deteta e grava o texto já recebido.
 */
export async function postStream(
  path: string,
  body: unknown,
  onEvent: (ev: SseEvent) => void,
  signal?: AbortSignal,
  allowRefresh = true,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if ((err as Error)?.name === "AbortError") throw err;
    throw new ApiError(0, "network", "network error");
  }
  if (res.status === 401 && allowRefresh) {
    if (await tryRefresh()) return postStream(path, body, onEvent, signal, false);
  }
  if (!res.ok || !res.body) {
    const b = (await res.json().catch(() => null)) as { error?: { code?: string; message?: string } } | null;
    throw new ApiError(res.status, b?.error?.code ?? "http_error", b?.error?.message ?? res.statusText);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseChunk(buffer);
    buffer = parsed.rest;
    parsed.events.forEach(onEvent);
  }
  buffer += decoder.decode();
  if (buffer.trim()) parseSseChunk(buffer + "\n\n").events.forEach(onEvent);
}

// --- Chat: modelos disponíveis ----------------------------------------------------------------
export type ChatModels = {
  local: {
    configured: boolean;
    enabled: boolean;
    reachable: boolean | null;
    default_model: string | null;
    models: string[];
    warning: string | null;
  };
  cloud: { provider: string; model: string }[];
};

// --- Admin: motor local (Ollama) ------------------------------------------------------------
export type LocalEngine = {
  configured: boolean;
  base_url_hint: string | null;
  token_configured: boolean;
  token_masked: string | null;
  enabled: boolean;
  api_mode: "native" | "openai";
  default_model: string | null;
  temperature: number;
  max_tokens: number;
  context_length: number;
  think: boolean;
  ram_limit_gb: number;
  allow_cloud_fallback: boolean;
  allow_paid_fallback: boolean;
  priority: number;
  warning: string | null;
  updated_at: string | null;
  updated_by_email: string | null;
  last_tested_at: string | null;
  last_test_ok: boolean | null;
  last_test_message: string | null;
  encryption_ready?: boolean;
};

export type LocalEngineStatus = {
  configured: boolean;
  reachable: boolean;
  version: string | null;
  running: { name: string; size_bytes: number | null; context_length: number | null; expires_at: string | null }[];
  warning: string | null;
};

export type LocalEngineTest = {
  ok: boolean;
  message: string;
  version: string | null;
  model: string | null;
  reply: string | null;
  installed: string[];
  latency_ms: number;
};

export type InstalledModel = {
  name: string;
  size_bytes: number | null;
  modified_at: string | null;
  family: string | null;
  parameter_size: string | null;
  quantization: string | null;
  is_default: boolean;
  ram_gb: number | null;
  capabilities: string[];
  license: string | null;
};

export type CatalogModel = {
  name: string;
  description: string;
  size_gb: number;
  ram_gb: number;
  capabilities: string[];
  license: string;
  recommended: boolean;
  installed: boolean;
  fits: boolean;
};

// --- Admin: tokens da API v1 -----------------------------------------------------------------
export type ApiKey = {
  id: string;
  name: string;
  key_prefix: string;
  scopes: string[];
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  created_by_email: string | null;
  requests_30d: number;
  tokens_30d: number;
  token?: string;
};
