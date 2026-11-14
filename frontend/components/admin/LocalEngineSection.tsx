"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Alert } from "@/components/AuthShell";
import { useUi } from "@/components/Providers";
import {
  ApiError,
  api,
  del,
  post,
  postStream,
  put,
  type CatalogModel,
  type InstalledModel,
  type LocalEngine,
  type LocalEngineStatus,
  type LocalEngineTest,
} from "@/lib/api";
import { errorText } from "@/lib/errors";

type Msg = { tone: "error" | "success" | "info"; text: string } | null;

const gb = (bytes: number | null) => (bytes ? (bytes / 1e9).toFixed(2) : "?");

export function LocalEngineSection({ fmtDate }: { fmtDate: (iso: string) => string }) {
  const { t } = useUi();
  const [engine, setEngine] = useState<LocalEngine | null>(null);
  const [status, setStatus] = useState<LocalEngineStatus | null>(null);
  const [installed, setInstalled] = useState<InstalledModel[] | null>(null);
  const [catalog, setCatalog] = useState<CatalogModel[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refreshModels = useCallback(async (eng: LocalEngine | null) => {
    const cat = await api<{ items: CatalogModel[] }>("/api/admin/local-engine/catalog").catch(() => null);
    setCatalog(cat?.items ?? null);
    if (!eng?.configured) {
      setInstalled(null);
      setStatus(null);
      return;
    }
    const [st, inst] = await Promise.all([
      api<LocalEngineStatus>("/api/admin/local-engine/status").catch(() => null),
      api<{ items: InstalledModel[] }>("/api/admin/local-engine/models").catch(() => null),
    ]);
    setStatus(st);
    setInstalled(inst?.items ?? null);
  }, []);

  const load = useCallback(async () => {
    try {
      const eng = await api<LocalEngine>("/api/admin/local-engine");
      setEngine(eng);
      setError(null);
      await refreshModels(eng);
    } catch (err) {
      setError(errorText(err, t));
    }
  }, [refreshModels, t]);

  useEffect(() => {
    void load();
  }, [load]);

  function onEngine(updated: LocalEngine) {
    setEngine((prev) => ({ ...updated, encryption_ready: updated.encryption_ready ?? prev?.encryption_ready }));
    void refreshModels(updated);
  }

  return (
    <section>
      <h2 className="font-semibold">{t("admin.local.title")}</h2>
      <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">{t("admin.local.lead")}</p>
      {error && <div className="mt-4"><Alert tone="error">{error}</Alert></div>}
      {engine && engine.encryption_ready === false && (
        <div className="mt-4"><Alert tone="error">{t("admin.providers.noEncryption")}</Alert></div>
      )}
      {engine && engine.warning && (
        <div className="mt-4"><Alert tone="info">{engine.warning}</Alert></div>
      )}
      {engine && (
        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <EngineForm engine={engine} installed={installed} status={status} onChange={onEngine} fmtDate={fmtDate} />
          <ModelsPanel engine={engine} installed={installed} catalog={catalog} onChanged={() => void load()} />
        </div>
      )}
    </section>
  );
}

function Toggle({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <label className={`flex items-center gap-3 text-sm ${disabled ? "opacity-50" : "cursor-pointer"}`}>
      <input type="checkbox" className="peer sr-only" checked={checked} disabled={disabled}
        onChange={(e) => onChange(e.target.checked)} />
      <span className="relative h-6 w-11 shrink-0 rounded-full bg-slate-300 transition after:absolute after:left-0.5 after:top-0.5 after:h-5 after:w-5 after:rounded-full after:bg-white after:transition peer-checked:bg-brand-600 peer-checked:after:translate-x-5 peer-focus-visible:ring-2 peer-focus-visible:ring-brand-500/50 dark:bg-slate-700" aria-hidden="true" />
      {label}
    </label>
  );
}

function EngineForm({
  engine, installed, status, onChange, fmtDate,
}: {
  engine: LocalEngine;
  installed: InstalledModel[] | null;
  status: LocalEngineStatus | null;
  onChange: (e: LocalEngine) => void;
  fmtDate: (iso: string) => string;
}) {
  const { t } = useUi();
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [form, setForm] = useState(engine);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [msg, setMsg] = useState<Msg>(null);

  useEffect(() => setForm(engine), [engine]);
  const set = <K extends keyof LocalEngine>(k: K, v: LocalEngine[K]) => setForm((f) => ({ ...f, [k]: v }));

  async function save(e?: FormEvent, extra: Record<string, unknown> = {}) {
    e?.preventDefault();
    setBusy("save");
    setMsg(null);
    try {
      const body: Record<string, unknown> = {
        enabled: form.enabled,
        api_mode: form.api_mode,
        temperature: Number(form.temperature),
        max_tokens: Number(form.max_tokens),
        context_length: Number(form.context_length),
        think: form.think,
        ram_limit_gb: Number(form.ram_limit_gb),
        allow_cloud_fallback: form.allow_cloud_fallback,
        allow_paid_fallback: form.allow_cloud_fallback && form.allow_paid_fallback,
        ...extra,
      };
      if (form.default_model) body.default_model = form.default_model;
      if (url.trim()) body.base_url = url.trim();
      if (token.trim()) body.token = token.trim();
      const updated = await put<LocalEngine>("/api/admin/local-engine", body);
      setUrl("");
      setToken("");
      onChange(updated);
      setMsg({ tone: "success", text: t("admin.local.saved") });
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(null);
    }
  }

  async function test() {
    setBusy("test");
    setMsg(null);
    try {
      const res = await post<LocalEngineTest>("/api/admin/local-engine/test");
      const detail = [res.version && `Ollama ${res.version}`, res.model, `${res.latency_ms} ms`, res.reply && `“${res.reply}”`]
        .filter(Boolean).join(" · ");
      setMsg({ tone: res.ok ? "success" : "error", text: `${res.message}${detail ? ` · ${detail}` : ""}` });
      onChange(await api<LocalEngine>("/api/admin/local-engine"));
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(null);
    }
  }

  const models = installed?.map((m) => m.name) ?? [];
  if (form.default_model && !models.includes(form.default_model)) models.unshift(form.default_model);

  return (
    <form onSubmit={save} className="card space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="font-semibold">Ollama</h3>
          <p className="text-xs text-slate-500">{t("admin.provider.priority", { n: 1 })}</p>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          {status && (
            <span className={`rounded-full px-2 py-0.5 ${status.reachable ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300" : "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"}`}>
              {status.reachable ? t("admin.local.status.reachable", { version: status.version ?? "?" }) : t("admin.local.status.unreachable")}
            </span>
          )}
          <span className={`rounded-full px-2 py-0.5 ${engine.enabled ? "bg-brand-50 text-brand-700 dark:bg-brand-600/20 dark:text-brand-400" : "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400"}`}>
            {engine.enabled ? t("admin.provider.active") : t("admin.provider.inactive")}
          </span>
        </div>
      </div>
      {status && status.running.length > 0 && (
        <p className="text-xs text-slate-500">{t("admin.local.status.running", { models: status.running.map((r) => r.name).join(", ") })}</p>
      )}

      <div className="space-y-1.5">
        <label htmlFor="ollama-url" className="text-sm font-medium text-slate-700 dark:text-slate-300">{t("admin.local.url")}</label>
        <input id="ollama-url" className="input font-mono" type="url" autoComplete="off" spellCheck={false}
          placeholder={t("admin.local.urlPlaceholder")} value={url} onChange={(e) => setUrl(e.target.value)} />
        {engine.base_url_hint && <p className="text-xs text-slate-500">{t("admin.local.urlCurrent", { hint: engine.base_url_hint })}</p>}
      </div>

      <div className="space-y-1.5">
        <label htmlFor="ollama-token" className="text-sm font-medium text-slate-700 dark:text-slate-300">{t("admin.local.token")}</label>
        <input id="ollama-token" className="input font-mono" type="password" autoComplete="off" spellCheck={false}
          placeholder={t("admin.local.tokenPlaceholder")} value={token} onChange={(e) => setToken(e.target.value)} />
        {engine.token_masked && (
          <div className="flex items-center justify-between gap-2 text-xs text-slate-500">
            <span>{t("admin.local.tokenCurrent", { mask: engine.token_masked })}</span>
            <button type="button" className="link" onClick={() => void save(undefined, { clear_token: true })}>{t("admin.local.removeToken")}</button>
          </div>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <label htmlFor="ollama-default" className="text-sm font-medium text-slate-700 dark:text-slate-300">{t("admin.local.defaultModel")}</label>
          <select id="ollama-default" className="input" value={form.default_model ?? ""}
            onChange={(e) => set("default_model", e.target.value || null)}>
            <option value="">{models.length ? "—" : t("admin.local.noModels")}</option>
            {models.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
        <div className="space-y-1.5">
          <label htmlFor="ollama-mode" className="text-sm font-medium text-slate-700 dark:text-slate-300">{t("admin.local.apiMode")}</label>
          <select id="ollama-mode" className="input" value={form.api_mode}
            onChange={(e) => set("api_mode", e.target.value as LocalEngine["api_mode"])}>
            <option value="native">{t("admin.local.apiMode.native")}</option>
            <option value="openai">{t("admin.local.apiMode.openai")}</option>
          </select>
        </div>
        <NumberField id="ollama-temp" label={t("admin.local.temperature")} value={form.temperature} step={0.1} min={0} max={2}
          onChange={(v) => set("temperature", v)} />
        <NumberField id="ollama-max" label={t("admin.local.maxTokens")} value={form.max_tokens} step={16} min={16} max={32768}
          onChange={(v) => set("max_tokens", v)} />
        <NumberField id="ollama-ctx" label={t("admin.local.context")} value={form.context_length} step={512} min={512} max={131072}
          onChange={(v) => set("context_length", v)} />
        <NumberField id="ollama-ram" label={t("admin.local.ramLimit")} value={form.ram_limit_gb} step={1} min={1} max={1024}
          onChange={(v) => set("ram_limit_gb", v)} />
      </div>

      <div className="space-y-2">
        <Toggle checked={form.think} onChange={(v) => set("think", v)} label={t("admin.local.think")} />
        <Toggle checked={form.allow_cloud_fallback} onChange={(v) => set("allow_cloud_fallback", v)} label={t("admin.local.fallbackCloud")} />
        <Toggle checked={form.allow_cloud_fallback && form.allow_paid_fallback} disabled={!form.allow_cloud_fallback}
          onChange={(v) => set("allow_paid_fallback", v)} label={t("admin.local.fallbackPaid")} />
        <Toggle checked={form.enabled} onChange={(v) => set("enabled", v)} label={t("admin.local.enabled")} />
      </div>

      {engine.last_tested_at && (
        <p className="text-xs text-slate-500">
          {t("admin.provider.lastTest", {
            result: engine.last_test_ok ? t("admin.provider.ok") : `${t("admin.provider.failed")}${engine.last_test_message ? ` (${engine.last_test_message})` : ""}`,
            time: fmtDate(engine.last_tested_at),
          })}
        </p>
      )}
      {msg && <Alert tone={msg.tone}>{msg.text}</Alert>}

      <div className="flex flex-wrap gap-2">
        <button type="submit" className="btn-primary" disabled={busy !== null}>{t("admin.local.save")}</button>
        <button type="button" className="btn-ghost" onClick={() => void test()} disabled={busy !== null || !engine.configured}>
          {busy === "test" ? t("admin.local.testing") : t("admin.local.test")}
        </button>
      </div>
    </form>
  );
}

function NumberField({ id, label, value, onChange, step, min, max }: {
  id: string; label: string; value: number; onChange: (v: number) => void; step: number; min: number; max: number;
}) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="text-sm font-medium text-slate-700 dark:text-slate-300">{label}</label>
      <input id={id} className="input" type="number" value={value} step={step} min={min} max={max}
        onChange={(e) => onChange(Number(e.target.value))} />
    </div>
  );
}

function ModelsPanel({ engine, installed, catalog, onChanged }: {
  engine: LocalEngine; installed: InstalledModel[] | null; catalog: CatalogModel[] | null; onChanged: () => void;
}) {
  const { t } = useUi();
  const [busy, setBusy] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ name: string; status: string; pct: number | null } | null>(null);
  const [custom, setCustom] = useState("");
  const [msg, setMsg] = useState<Msg>(null);

  async function setDefault(name: string) {
    setBusy(name);
    setMsg(null);
    try {
      await post(`/api/admin/local-engine/models/${encodeURIComponent(name)}/default`);
      onChanged();
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(null);
    }
  }

  async function remove(name: string) {
    if (!window.confirm(t("admin.local.deleteConfirm", { name }))) return;
    setBusy(name);
    setMsg(null);
    try {
      await del(`/api/admin/local-engine/models/${encodeURIComponent(name)}`);
      onChanged();
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(null);
    }
  }

  async function pull(name: string, confirmed = false) {
    const info = catalog?.find((c) => c.name === name);
    if (!confirmed && info && !info.fits) {
      if (!window.confirm(t("admin.local.pullOverLimit", { name, ram: info.ram_gb, limit: engine.ram_limit_gb }))) return;
      confirmed = true;
    }
    setBusy(name);
    setMsg(null);
    setProgress({ name, status: "", pct: null });
    let failed: string | null = null;
    try {
      await postStream("/api/admin/local-engine/models/pull", { name, confirm_over_limit: confirmed }, (ev) => {
        const data = JSON.parse(ev.data) as { status?: string; total?: number; completed?: number; message?: string };
        if (ev.event === "progress") {
          const pct = data.total && data.completed ? Math.round((data.completed / data.total) * 100) : null;
          setProgress({ name, status: data.status ?? "", pct });
        } else if (ev.event === "error") {
          failed = data.message ?? "erro";
        }
      });
      setMsg(failed ? { tone: "error", text: failed } : { tone: "success", text: t("admin.local.pullDone", { name }) });
      setCustom("");
      onChanged();
    } catch (err) {
      if (err instanceof ApiError && err.code === "ram_unknown" && !confirmed) {
        setBusy(null);
        setProgress(null);
        if (window.confirm(t("admin.local.pullUnknown", { name }))) return pull(name, true);
        return;
      }
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(null);
      setProgress(null);
    }
  }

  if (!engine.configured) {
    return <div className="card text-sm text-slate-500">{t("admin.local.configureFirst")}</div>;
  }

  return (
    <div className="card space-y-5">
      <div>
        <h3 className="font-semibold">{t("admin.local.installed")}</h3>
        {installed === null ? (
          <p className="mt-2 text-sm text-slate-500">{t("admin.local.status.unreachable")}</p>
        ) : installed.length === 0 ? (
          <p className="mt-2 text-sm text-slate-500">{t("admin.local.noModels")}</p>
        ) : (
          <ul className="mt-2 divide-y divide-slate-100 dark:divide-slate-800">
            {installed.map((m) => (
              <li key={m.name} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                <div className="min-w-0">
                  <span className="font-mono">{m.name}</span>
                  {m.is_default && <span className="ml-2 rounded-full bg-brand-50 px-2 py-0.5 text-xs text-brand-700 dark:bg-brand-600/20 dark:text-brand-400">{t("admin.local.isDefault")}</span>}
                  <span className="block text-xs text-slate-500">
                    {[t("admin.local.size", { size: gb(m.size_bytes) }), m.parameter_size, m.quantization, m.ram_gb ? t("admin.local.ram", { ram: m.ram_gb }) : null]
                      .filter(Boolean).join(" · ")}
                  </span>
                </div>
                <div className="flex gap-2">
                  {!m.is_default && (
                    <button type="button" className="btn-ghost px-2 py-1 text-xs" disabled={busy !== null} onClick={() => void setDefault(m.name)}>
                      {t("admin.local.setDefault")}
                    </button>
                  )}
                  <button type="button" className="btn-ghost px-2 py-1 text-xs text-red-600 dark:text-red-400" disabled={busy !== null}
                    onClick={() => void remove(m.name)}>{t("admin.local.delete")}</button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {progress && (
        <div className="space-y-1">
          <p className="text-sm">{t("admin.local.pulling", { name: progress.name })} {progress.status}</p>
          <div className="h-2 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
            <div className="h-full bg-brand-600 transition-all" style={{ width: `${progress.pct ?? 5}%` }} />
          </div>
        </div>
      )}
      {msg && <Alert tone={msg.tone}>{msg.text}</Alert>}

      <div>
        <h3 className="font-semibold">{t("admin.local.catalog")}</h3>
        <ul className="mt-2 max-h-96 divide-y divide-slate-100 overflow-y-auto dark:divide-slate-800">
          {catalog?.map((c) => (
            <li key={c.name} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
              <div className="min-w-0 flex-1">
                <span className="font-mono">{c.name}</span>
                {c.recommended && <span className="ml-2 rounded-full bg-emerald-100 px-2 py-0.5 text-xs text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300">{t("admin.local.recommended")}</span>}
                {!c.fits && <span className="ml-2 rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-300">{t("admin.local.tooBig")}</span>}
                <span className="block text-xs text-slate-500">{c.description}</span>
                <span className="block text-xs text-slate-500">
                  {[t("admin.local.size", { size: c.size_gb }), t("admin.local.ram", { ram: c.ram_gb }), c.capabilities.join(", "), t("admin.local.license", { license: c.license })].join(" · ")}
                </span>
              </div>
              {c.installed ? (
                <span className="text-xs text-emerald-700 dark:text-emerald-400">{t("admin.local.installedBadge")}</span>
              ) : (
                <button type="button" className="btn-ghost px-2 py-1 text-xs" disabled={busy !== null} onClick={() => void pull(c.name)}>
                  {t("admin.local.pull")}
                </button>
              )}
            </li>
          ))}
        </ul>
        <form className="mt-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); if (custom.trim()) void pull(custom.trim()); }}>
          <label htmlFor="custom-model" className="sr-only">{t("admin.local.pullCustom")}</label>
          <input id="custom-model" className="input font-mono" placeholder={t("admin.local.pullCustom")} value={custom}
            onChange={(e) => setCustom(e.target.value)} maxLength={200} />
          <button type="submit" className="btn-ghost shrink-0" disabled={busy !== null || !custom.trim()}>{t("admin.local.pull")}</button>
        </form>
      </div>
    </div>
  );
}
