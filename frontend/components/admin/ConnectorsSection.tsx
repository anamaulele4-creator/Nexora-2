"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert } from "@/components/AuthShell";
import { useUi } from "@/components/Providers";
import { api, put, type AdminConnectorsResponse, type AiPolicy } from "@/lib/api";
import { errorText } from "@/lib/errors";

/** Admin › Conectores: OAuth configurado por fornecedor (sem segredos) e contas ligadas. */
export function ConnectorsSection() {
  const { t } = useUi();
  const [data, setData] = useState<AdminConnectorsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api<AdminConnectorsResponse>("/api/admin/connectors"));
      setError(null);
    } catch (err) {
      setError(errorText(err, t));
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(text);
    } catch {
      setCopied(null);
    }
  }

  return (
    <section>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h2 className="font-semibold">{t("admin.connectors")}</h2>
        <button type="button" className="btn-ghost" onClick={() => void load()}>{t("dash.reload")}</button>
      </div>
      <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">{t("admin.connectors.lead")}</p>
      {error && <div className="mt-4"><Alert tone="error">{error}</Alert></div>}
      {data && !data.encryption_ready && <div className="mt-4"><Alert tone="error">{t("conn.noEncryption")}</Alert></div>}
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        {data?.items.map((c) => {
          const ok = c.method === "api_key" || c.oauth_configured;
          return (
            <div key={c.key} className="card space-y-3 p-4 sm:p-6">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <h3 className="font-semibold">{c.name}</h3>
                <span className={`rounded-full px-2 py-0.5 text-xs ${ok ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300" : "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300"}`}>
                  {c.method === "api_key" ? "API key" : c.oauth_configured ? t("admin.connectors.oauthOk") : t("admin.connectors.oauthMissing")}
                </span>
              </div>
              {c.method === "api_key" ? (
                <p className="text-sm text-slate-600 dark:text-slate-400">{t("admin.connectors.apiKey")}</p>
              ) : (
                <>
                  <ul className="space-y-1 text-xs">
                    {c.env_vars.map((v) => (
                      <li key={v.name} className="flex flex-wrap items-center gap-2">
                        <code className="break-all rounded bg-slate-100 px-1.5 py-0.5 dark:bg-slate-800">{v.name}</code>
                        <span className={v.set ? "text-emerald-700 dark:text-emerald-400" : "text-amber-700 dark:text-amber-400"}>
                          {v.set ? `✓ ${t("admin.connectors.envSet")}` : `✗ ${t("admin.connectors.envMissing")}`}
                        </span>
                      </li>
                    ))}
                  </ul>
                  {c.redirect_uri && (
                    <div className="space-y-1">
                      <p className="text-xs font-medium text-slate-600 dark:text-slate-400">{t("admin.connectors.redirect")}</p>
                      <div className="flex items-center gap-2">
                        <code className="min-w-0 flex-1 break-all rounded bg-slate-100 px-2 py-1 text-xs dark:bg-slate-800">{c.redirect_uri}</code>
                        <button type="button" className="btn-ghost shrink-0 text-xs" onClick={() => void copy(c.redirect_uri ?? "")}>
                          {copied === c.redirect_uri ? t("admin.apikeys.copied") : t("admin.apikeys.copy")}
                        </button>
                      </div>
                    </div>
                  )}
                </>
              )}
              <p className="text-xs text-slate-500">
                {t("admin.connectors.accounts", { n: c.connected_accounts })}
                {c.failing_accounts > 0 && ` · ${t("admin.connectors.failing", { n: c.failing_accounts })}`}
              </p>
            </div>
          );
        })}
      </div>
    </section>
  );
}

/** Admin › IA paga para utilizadores: autoriza ou não fornecedores pagos na preferência de IA. */
export function AiPolicySection() {
  const { t } = useUi();
  const [policy, setPolicy] = useState<AiPolicy | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<AiPolicy>("/api/admin/ai-policy").then(setPolicy).catch((err) => setError(errorText(err, t)));
  }, [t]);

  async function toggle(value: boolean) {
    setBusy(true);
    setError(null);
    try {
      setPolicy(await put<AiPolicy>("/api/admin/ai-policy", { allow_paid_providers: value }));
    } catch (err) {
      setError(errorText(err, t));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card space-y-3 p-4 sm:p-6">
      <h2 className="font-semibold">{t("admin.policy")}</h2>
      <p className="text-sm text-slate-600 dark:text-slate-400">
        {t("admin.policy.lead", { providers: policy?.paid_providers.join(", ") || "OpenAI" })}
      </p>
      <label className="flex cursor-pointer items-center gap-3 text-sm">
        <input type="checkbox" className="peer sr-only" checked={policy?.allow_paid_providers ?? false}
          disabled={!policy || busy} onChange={(e) => void toggle(e.target.checked)} />
        <span className="relative h-6 w-11 shrink-0 rounded-full bg-slate-300 transition after:absolute after:left-0.5 after:top-0.5 after:h-5 after:w-5 after:rounded-full after:bg-white after:transition peer-checked:bg-brand-600 peer-checked:after:translate-x-5 peer-focus-visible:ring-2 peer-focus-visible:ring-brand-500/50 dark:bg-slate-700" aria-hidden="true" />
        {t("admin.policy.toggle")}
      </label>
      {error && <Alert tone="error">{error}</Alert>}
    </section>
  );
}
