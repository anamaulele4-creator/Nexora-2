// Cliente da API. Mesmo domínio (rewrites do Next) + cookies httpOnly: o JavaScript
// nunca vê os tokens.

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
