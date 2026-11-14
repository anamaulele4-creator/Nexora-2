"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Alert } from "@/components/AuthShell";
import { Header } from "@/components/Header";
import { useUi } from "@/components/Providers";
import { ApiError, api, post, type DashboardSummary, type Me, type TaskStatus } from "@/lib/api";
import { errorKey } from "@/lib/errors";
import type { MessageKey } from "@/lib/i18n";

import { StatCard } from "./StatCard";

const STATUSES: TaskStatus[] = ["queued", "planning", "running", "waiting_approval", "completed", "failed", "cancelled", "paused"];

export function DashboardView() {
  const { t, lang } = useUi();
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [data, setData] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [meRes, summary] = await Promise.all([
        api<Me>("/api/auth/me"),
        api<DashboardSummary>("/api/dashboard/summary"),
      ]);
      setMe(meRes);
      setData(summary);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login?next=/dashboard");
        return;
      }
      setError(t(errorKey(err instanceof ApiError ? err.code : undefined)));
    } finally {
      setLoading(false);
    }
  }, [router, t]);

  useEffect(() => {
    void load();
  }, [load]);

  async function logout() {
    try {
      await post("/api/auth/logout");
    } finally {
      router.replace("/login");
    }
  }

  const fmt = new Intl.NumberFormat(lang === "pt" ? "pt-PT" : "en-US");
  const fmtDate = (iso: string) =>
    new Intl.DateTimeFormat(lang === "pt" ? "pt-PT" : "en-US", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));

  return (
    <div className="min-h-screen">
      <Header>
        <button type="button" className="btn-ghost" onClick={logout}>{t("nav.logout")}</button>
      </Header>
      <main className="mx-auto max-w-6xl px-6 py-10">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">{t("dash.title")}</h1>
            {me && (
              <p className="mt-1 text-slate-600 dark:text-slate-400">
                {t("dash.welcome", { name: me.display_name || me.email })}
                {me.default_workspace ? ` · ${me.default_workspace.name}` : ""}
              </p>
            )}
          </div>
          <div className="flex items-center gap-3">
            {data && <span className="text-xs text-slate-500">{t("dash.updated", { time: fmtDate(data.generated_at) })}</span>}
            <button type="button" className="btn-ghost" onClick={() => void load()} disabled={loading}>{t("dash.reload")}</button>
          </div>
        </div>

        {me && !me.email_verified && <div className="mt-6"><Alert tone="info">{t("dash.unverified")}</Alert></div>}
        {error && <div className="mt-6"><Alert tone="error">{error}</Alert></div>}
        {loading && !data && <p className="mt-10 text-slate-500">{t("dash.loading")}</p>}

        {data && (
          <>
            <section className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <StatCard label={t("dash.card.tasks")} value={fmt.format(data.tasks.total)} />
              <StatCard label={t("dash.card.inProgress")} value={fmt.format(data.tasks.in_progress)} />
              <StatCard label={t("dash.card.approvals")} value={fmt.format(data.pending_approvals)} />
              <StatCard
                label={t("dash.card.connections")}
                value={fmt.format(data.active_connections)}
                hint={t("dash.connectors", { available: data.connectors.available, planned: data.connectors.planned })}
              />
              <StatCard label={t("dash.card.browser")} value={fmt.format(data.active_browser_sessions)} />
              <StatCard label={t("dash.card.notifications")} value={fmt.format(data.unread_notifications)} />
            </section>

            <section className="mt-8 grid gap-4 lg:grid-cols-2">
              <div className="card">
                <h2 className="font-semibold">{t("dash.byStatus")}</h2>
                <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
                  {STATUSES.map((s) => (
                    <div key={s} className="flex justify-between border-b border-slate-100 py-1 dark:border-slate-800">
                      <dt className="text-slate-600 dark:text-slate-400">{t(`status.${s}` as MessageKey)}</dt>
                      <dd className="tabular-nums">{fmt.format(data.tasks.by_status[s] ?? 0)}</dd>
                    </div>
                  ))}
                </dl>
              </div>
              <div className="card">
                <h2 className="font-semibold">{t("dash.usage")}</h2>
                <dl className="mt-4 space-y-2 text-sm">
                  <div className="flex justify-between"><dt className="text-slate-600 dark:text-slate-400">{t("dash.usage.toolCalls")}</dt><dd className="tabular-nums">{fmt.format(data.usage_this_month.tool_calls)}</dd></div>
                  <div className="flex justify-between"><dt className="text-slate-600 dark:text-slate-400">{t("dash.usage.tokens")}</dt><dd className="tabular-nums">{fmt.format(data.usage_this_month.llm_input_tokens)} / {fmt.format(data.usage_this_month.llm_output_tokens)}</dd></div>
                  <div className="flex justify-between"><dt className="text-slate-600 dark:text-slate-400">{t("dash.usage.browser")}</dt><dd className="tabular-nums">{fmt.format(data.usage_this_month.browser_minutes)}</dd></div>
                </dl>
              </div>
            </section>

            <section className="card mt-8">
              <h2 className="font-semibold">{t("dash.recent")}</h2>
              {data.recent_tasks.length === 0 ? (
                <p className="mt-4 text-sm text-slate-500">{t("dash.recent.empty")}</p>
              ) : (
                <ul className="mt-4 divide-y divide-slate-100 text-sm dark:divide-slate-800">
                  {data.recent_tasks.map((task) => (
                    <li key={task.id} className="flex items-center justify-between gap-4 py-2">
                      <span className="truncate">{task.title}</span>
                      <span className="shrink-0 text-slate-500">{t(`status.${task.status}` as MessageKey)} · {fmtDate(task.created_at)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
      </main>
    </div>
  );
}
