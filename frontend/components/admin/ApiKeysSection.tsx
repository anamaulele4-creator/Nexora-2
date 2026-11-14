"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Alert } from "@/components/AuthShell";
import { useUi } from "@/components/Providers";
import { api, del, post, type ApiKey } from "@/lib/api";
import { errorText } from "@/lib/errors";

export function ApiKeysSection({ fmtDate }: { fmtDate: (iso: string) => string }) {
  const { t, lang } = useUi();
  const [items, setItems] = useState<ApiKey[] | null>(null);
  const [name, setName] = useState("");
  const [days, setDays] = useState("");
  const [created, setCreated] = useState<ApiKey | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fmt = new Intl.NumberFormat(lang === "pt" ? "pt-PT" : "en-US");

  const load = useCallback(async () => {
    try {
      setItems((await api<{ items: ApiKey[] }>("/api/admin/api-keys")).items);
    } catch (err) {
      setError(errorText(err, t));
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  async function create(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    setCopied(false);
    try {
      const body: Record<string, unknown> = { name: name.trim() };
      if (days.trim()) body.expires_in_days = Number(days);
      const key = await post<ApiKey>("/api/admin/api-keys", body);
      setCreated(key);
      setName("");
      setDays("");
      await load();
    } catch (err) {
      setError(errorText(err, t));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    if (!window.confirm(t("admin.apikeys.revokeConfirm"))) return;
    try {
      await del(`/api/admin/api-keys/${id}`);
      if (created?.id === id) setCreated(null);
      await load();
    } catch (err) {
      setError(errorText(err, t));
    }
  }

  async function copy() {
    if (!created?.token) return;
    await navigator.clipboard.writeText(created.token).then(() => setCopied(true)).catch(() => undefined);
  }

  const origin = typeof window === "undefined" ? "" : window.location.origin;

  return (
    <section>
      <h2 className="font-semibold">{t("admin.apikeys.title")}</h2>
      <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">{t("admin.apikeys.lead")}</p>
      {error && <div className="mt-4"><Alert tone="error">{error}</Alert></div>}

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <form onSubmit={create} className="card space-y-3">
          <div className="space-y-1.5">
            <label htmlFor="key-name" className="text-sm font-medium text-slate-700 dark:text-slate-300">{t("admin.apikeys.name")}</label>
            <input id="key-name" className="input" maxLength={100} placeholder={t("admin.apikeys.namePlaceholder")}
              value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <label htmlFor="key-days" className="text-sm font-medium text-slate-700 dark:text-slate-300">{t("admin.apikeys.expires")}</label>
            <input id="key-days" className="input" type="number" min={1} max={3650} value={days} onChange={(e) => setDays(e.target.value)} />
          </div>
          <button type="submit" className="btn-primary" disabled={busy || !name.trim()}>{t("admin.apikeys.create")}</button>
          {created?.token && (
            <div className="space-y-2">
              <Alert tone="success">{t("admin.apikeys.created")}</Alert>
              <div className="flex gap-2">
                <code className="input block flex-1 overflow-x-auto whitespace-nowrap font-mono text-xs">{created.token}</code>
                <button type="button" className="btn-ghost shrink-0" onClick={() => void copy()}>
                  {copied ? t("admin.apikeys.copied") : t("admin.apikeys.copy")}
                </button>
              </div>
            </div>
          )}
          <details className="text-xs text-slate-500">
            <summary className="cursor-pointer">{t("admin.apikeys.example")}</summary>
            <pre className="mt-2 overflow-x-auto rounded-lg bg-slate-100 p-3 dark:bg-slate-900">{`curl ${origin}/api/v1/chat/completions \\
  -H "Authorization: Bearer cai_…" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"auto","stream":true,
       "messages":[{"role":"user","content":"Olá"}]}'`}</pre>
          </details>
        </form>

        <div className="card p-0">
          {items && items.length === 0 && <p className="px-4 py-6 text-sm text-slate-500">{t("admin.apikeys.empty")}</p>}
          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {items?.map((k) => (
              <li key={k.id} className={`flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-sm ${k.revoked_at ? "opacity-60" : ""}`}>
                <div className="min-w-0">
                  <span className="font-medium">{k.name}</span>
                  <span className="ml-2 font-mono text-xs text-slate-500">{k.key_prefix}…</span>
                  <span className="block text-xs text-slate-500">
                    {t("admin.apikeys.usage", { requests: fmt.format(k.requests_30d), tokens: fmt.format(k.tokens_30d) })}
                    {" · "}
                    {t("admin.apikeys.lastUsed", { time: k.last_used_at ? fmtDate(k.last_used_at) : t("admin.apikeys.never") })}
                    {k.expires_at && <>{" · "}{t("admin.apikeys.expiresAt", { time: fmtDate(k.expires_at) })}</>}
                  </span>
                </div>
                {k.revoked_at ? (
                  <span className="text-xs text-slate-500">{t("admin.apikeys.revoked")}</span>
                ) : (
                  <button type="button" className="btn-ghost px-2 py-1 text-xs text-red-600 dark:text-red-400" onClick={() => void revoke(k.id)}>
                    {t("admin.apikeys.revoke")}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
