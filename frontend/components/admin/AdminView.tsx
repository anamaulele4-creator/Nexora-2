"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { AppNav, useMe } from "@/components/AppNav";
import { Alert } from "@/components/AuthShell";
import { Header } from "@/components/Header";
import { useUi } from "@/components/Providers";
import { ApiKeysSection } from "@/components/admin/ApiKeysSection";
import { AiPolicySection, ConnectorsSection } from "@/components/admin/ConnectorsSection";
import { LocalEngineSection } from "@/components/admin/LocalEngineSection";
import { StatCard } from "@/dashboard/StatCard";
import {
  api,
  post,
  put,
  type AdminStats,
  type AdminUsersPage,
  type AiProvider,
  type AiProvidersResponse,
  type ProviderTestResult,
} from "@/lib/api";
import { errorText } from "@/lib/errors";

const PROVIDER_INFO: Record<AiProvider["provider"], { name: string; keyUrl: string }> = {
  gemini: { name: "Google Gemini", keyUrl: "https://aistudio.google.com/app/apikey" },
  groq: { name: "Groq", keyUrl: "https://console.groq.com/keys" },
  openrouter: { name: "OpenRouter", keyUrl: "https://openrouter.ai/settings/keys" },
  openai: { name: "OpenAI", keyUrl: "https://platform.openai.com/api-keys" },
};

export function AdminView() {
  const { t, lang } = useUi();
  const router = useRouter();
  const { me, loading } = useMe("/admin");
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [providers, setProviders] = useState<AiProvidersResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fmt = new Intl.NumberFormat(lang === "pt" ? "pt-PT" : "en-US");
  const fmtDate = useCallback(
    (iso: string) =>
      new Intl.DateTimeFormat(lang === "pt" ? "pt-PT" : "en-US", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso)),
    [lang],
  );

  // só administradores: os outros voltam para o Dashboard
  useEffect(() => {
    if (!loading && me && !me.is_admin) router.replace("/dashboard");
  }, [loading, me, router]);

  const loadAll = useCallback(async () => {
    setError(null);
    try {
      const [s, p] = await Promise.all([
        api<AdminStats>("/api/admin/stats"),
        api<AiProvidersResponse>("/api/admin/providers"),
      ]);
      setStats(s);
      setProviders(p);
    } catch (err) {
      const e = err as { status?: number };
      if (e?.status === 403) {
        router.replace("/dashboard");
        return;
      }
      setError(errorText(err, t));
    }
  }, [router, t]);

  useEffect(() => {
    if (me?.is_admin) void loadAll();
  }, [me, loadAll]);

  function onProviderChange(updated: AiProvider) {
    setProviders((prev) =>
      prev ? { ...prev, items: prev.items.map((p) => (p.provider === updated.provider ? updated : p)) } : prev,
    );
    void api<AdminStats>("/api/admin/stats").then(setStats).catch(() => undefined);
  }

  if (!me?.is_admin) {
    return (
      <div className="min-h-screen">
        <Header><AppNav me={me} /></Header>
        <main className="mx-auto max-w-6xl px-4 py-10 sm:px-6"><p className="text-slate-500">{t("dash.loading")}</p></main>
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <Header>
        <AppNav me={me} />
      </Header>
      <main className="mx-auto max-w-6xl space-y-10 px-4 py-8 sm:px-6 sm:py-10">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">{t("admin.title")}</h1>
          <button type="button" className="btn-ghost" onClick={() => void loadAll()}>{t("dash.reload")}</button>
        </div>
        {error && <Alert tone="error">{error}</Alert>}

        <section>
          <h2 className="mb-4 font-semibold">{t("admin.stats")}</h2>
          {stats ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <StatCard label={t("admin.stats.users")} value={fmt.format(stats.users)}
                hint={`${t("admin.stats.newUsers")}: ${fmt.format(stats.users_last_7d)}`} />
              <StatCard label={t("admin.stats.conversations")} value={fmt.format(stats.conversations)} />
              <StatCard label={t("admin.stats.messages")} value={fmt.format(stats.messages)}
                hint={`${t("admin.stats.messages24h")}: ${fmt.format(stats.messages_last_24h)}`} />
              <StatCard label={t("admin.stats.providers")} value={fmt.format(stats.active_providers)}
                hint={`${t("admin.stats.admins")}: ${fmt.format(stats.admins)}`} />
            </div>
          ) : (
            <p className="text-sm text-slate-500">{t("dash.loading")}</p>
          )}
        </section>

        <LocalEngineSection fmtDate={fmtDate} />

        <section>
          <h2 className="font-semibold">{t("admin.providers")}</h2>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">{t("admin.providers.lead")}</p>
          {providers && !providers.encryption_ready && (
            <div className="mt-4"><Alert tone="error">{t("admin.providers.noEncryption")}</Alert></div>
          )}
          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            {providers?.items.map((p) => (
              <ProviderCard key={p.provider} provider={p} onChange={onProviderChange} fmtDate={fmtDate}
                disabled={!providers.encryption_ready} />
            ))}
          </div>
        </section>

        <AiPolicySection />

        <ConnectorsSection />

        <ApiKeysSection fmtDate={fmtDate} />

        <UsersSection fmtDate={fmtDate} />
      </main>
    </div>
  );
}

function ProviderCard({
  provider, onChange, fmtDate, disabled,
}: { provider: AiProvider; onChange: (p: AiProvider) => void; fmtDate: (iso: string) => string; disabled: boolean }) {
  const { t } = useUi();
  const info = PROVIDER_INFO[provider.provider];
  const [key, setKey] = useState("");
  const [model, setModel] = useState(provider.model);
  const [enabled, setEnabled] = useState(provider.enabled);
  const [busy, setBusy] = useState<"save" | "test" | "remove" | null>(null);
  const [message, setMessage] = useState<{ tone: "error" | "success" | "info"; text: string } | null>(null);

  useEffect(() => {
    setModel(provider.model);
    setEnabled(provider.enabled);
  }, [provider.model, provider.enabled]);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy("save");
    setMessage(null);
    try {
      const body: Record<string, unknown> = { model: model.trim() || provider.default_model, enabled };
      if (key.trim()) body.api_key = key.trim();
      const updated = await put<AiProvider>(`/api/admin/providers/${provider.provider}`, body);
      setKey("");
      onChange(updated);
      setMessage({ tone: "success", text: t("admin.provider.saved") });
    } catch (err) {
      setMessage({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(null);
    }
  }

  async function test() {
    setBusy("test");
    setMessage(null);
    try {
      const res = await post<ProviderTestResult>(`/api/admin/providers/${provider.provider}/test`);
      setMessage({ tone: res.ok ? "success" : "error", text: `${res.message} · ${res.model} · ${res.latency_ms} ms` });
      const list = await api<AiProvidersResponse>("/api/admin/providers");
      const fresh = list.items.find((i) => i.provider === provider.provider);
      if (fresh) onChange(fresh);
    } catch (err) {
      setMessage({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(null);
    }
  }

  async function removeKey() {
    if (!window.confirm(t("admin.provider.removeConfirm"))) return;
    setBusy("remove");
    setMessage(null);
    try {
      const updated = await put<AiProvider>(`/api/admin/providers/${provider.provider}`, { clear_key: true, enabled: false });
      onChange(updated);
    } catch (err) {
      setMessage({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(null);
    }
  }

  const keyId = `key-${provider.provider}`;
  const modelId = `model-${provider.provider}`;

  return (
    <form onSubmit={save} className="card space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="font-semibold">{info.name}</h3>
          <p className="text-xs text-slate-500">{t("admin.provider.priority", { n: provider.priority })}</p>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          <span className={`rounded-full px-2 py-0.5 ${provider.configured ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300" : "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400"}`}>
            {provider.configured ? t("admin.provider.configured") : t("admin.provider.notConfigured")}
          </span>
          <span className={`rounded-full px-2 py-0.5 ${provider.enabled ? "bg-brand-50 text-brand-700 dark:bg-brand-600/20 dark:text-brand-400" : "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400"}`}>
            {provider.enabled ? t("admin.provider.active") : t("admin.provider.inactive")}
          </span>
        </div>
      </div>

      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-2">
          <label htmlFor={keyId} className="text-sm font-medium text-slate-700 dark:text-slate-300">{t("admin.provider.key")}</label>
          <a href={info.keyUrl} target="_blank" rel="noopener noreferrer" className="link text-xs">{t("admin.provider.getKey")} ↗</a>
        </div>
        <input id={keyId} className="input font-mono" type="password" autoComplete="off" spellCheck={false}
          placeholder={t("admin.provider.keyPlaceholder")} value={key} onChange={(e) => setKey(e.target.value)}
          disabled={disabled} />
        {provider.key_masked && (
          <p className="text-xs text-slate-500">{t("admin.provider.keyCurrent", { mask: provider.key_masked })}</p>
        )}
      </div>

      <div className="space-y-1.5">
        <label htmlFor={modelId} className="text-sm font-medium text-slate-700 dark:text-slate-300">{t("admin.provider.model")}</label>
        <input id={modelId} className="input font-mono" value={model} placeholder={provider.default_model}
          onChange={(e) => setModel(e.target.value)} maxLength={100} />
      </div>

      <label className="flex cursor-pointer items-center gap-3 text-sm">
        <input type="checkbox" className="peer sr-only" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        <span className="relative h-6 w-11 rounded-full bg-slate-300 transition after:absolute after:left-0.5 after:top-0.5 after:h-5 after:w-5 after:rounded-full after:bg-white after:transition peer-checked:bg-brand-600 peer-checked:after:translate-x-5 peer-focus-visible:ring-2 peer-focus-visible:ring-brand-500/50 dark:bg-slate-700" aria-hidden="true" />
        {t("admin.provider.enabled")}
      </label>

      {provider.last_tested_at && (
        <p className="text-xs text-slate-500">
          {t("admin.provider.lastTest", {
            result: provider.last_test_ok ? t("admin.provider.ok") : `${t("admin.provider.failed")}${provider.last_test_message ? ` (${provider.last_test_message})` : ""}`,
            time: fmtDate(provider.last_tested_at),
          })}
        </p>
      )}
      {message && <Alert tone={message.tone}>{message.text}</Alert>}

      <div className="flex flex-wrap gap-2">
        <button type="submit" className="btn-primary" disabled={busy !== null}>{t("admin.provider.save")}</button>
        <button type="button" className="btn-ghost" onClick={() => void test()} disabled={busy !== null || !provider.configured}>
          {busy === "test" ? t("admin.provider.testing") : t("admin.provider.test")}
        </button>
        {provider.configured && (
          <button type="button" className="btn-ghost text-red-600 dark:text-red-400" onClick={() => void removeKey()} disabled={busy !== null}>
            {t("admin.provider.removeKey")}
          </button>
        )}
      </div>
    </form>
  );
}

function UsersSection({ fmtDate }: { fmtDate: (iso: string) => string }) {
  const { t } = useUi();
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [data, setData] = useState<AdminUsersPage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const params = new URLSearchParams({ page: String(page), page_size: "20" });
    if (search) params.set("q", search);
    api<AdminUsersPage>(`/api/admin/users?${params}`)
      .then((d) => { setData(d); setError(null); })
      .catch((err) => setError(errorText(err, t)));
  }, [page, search, t]);

  return (
    <section>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h2 className="font-semibold">{t("admin.users")}</h2>
        <form className="flex w-full gap-2 sm:w-auto" onSubmit={(e) => { e.preventDefault(); setPage(1); setSearch(query.trim()); }}>
          <label htmlFor="user-search" className="sr-only">{t("admin.users.search")}</label>
          <input id="user-search" className="input sm:w-64" type="search" placeholder={t("admin.users.search")}
            value={query} onChange={(e) => setQuery(e.target.value)} />
          <button className="btn-ghost shrink-0" type="submit">⌕</button>
        </form>
      </div>
      {error && <div className="mt-4"><Alert tone="error">{error}</Alert></div>}
      <div className="card mt-4 overflow-x-auto p-0">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead className="border-b border-slate-200 text-xs uppercase text-slate-500 dark:border-slate-800">
            <tr>
              <th className="px-4 py-3 font-medium">{t("admin.users.email")}</th>
              <th className="px-4 py-3 font-medium">{t("admin.users.role")}</th>
              <th className="px-4 py-3 font-medium">{t("admin.users.verified")}</th>
              <th className="px-4 py-3 font-medium">{t("admin.users.messages")}</th>
              <th className="px-4 py-3 font-medium">{t("admin.users.created")}</th>
              <th className="px-4 py-3 font-medium">{t("admin.users.lastLogin")}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {data?.items.map((u) => (
              <tr key={u.id}>
                <td className="px-4 py-2">
                  <span className="block max-w-[16rem] truncate">{u.email}</span>
                  {u.display_name && <span className="block text-xs text-slate-500">{u.display_name}</span>}
                </td>
                <td className="px-4 py-2">{u.is_admin ? t("admin.users.admin") : t("admin.users.user")}</td>
                <td className="px-4 py-2">{u.email_verified ? t("admin.users.yes") : t("admin.users.no")}</td>
                <td className="px-4 py-2 tabular-nums">{u.messages}</td>
                <td className="px-4 py-2 whitespace-nowrap">{fmtDate(u.created_at)}</td>
                <td className="px-4 py-2 whitespace-nowrap">{u.last_login_at ? fmtDate(u.last_login_at) : t("admin.users.never")}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.items.length === 0 && <p className="px-4 py-6 text-sm text-slate-500">{t("admin.users.empty")}</p>}
      </div>
      {data && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-sm text-slate-500">
          <span>{t("admin.users.page", { page: data.page, pages: data.pages, total: data.total })}</span>
          <div className="flex gap-2">
            <button type="button" className="btn-ghost" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>{t("admin.users.prev")}</button>
            <button type="button" className="btn-ghost" disabled={page >= data.pages} onClick={() => setPage((p) => p + 1)}>{t("admin.users.next")}</button>
          </div>
        </div>
      )}
    </section>
  );
}
