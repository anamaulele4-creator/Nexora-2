"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import { AppNav, useMe } from "@/components/AppNav";
import { Alert } from "@/components/AuthShell";
import { Header } from "@/components/Header";
import { useUi } from "@/components/Providers";
import {
  ApiError,
  api,
  del,
  patch,
  post,
  put,
  type AiPreference,
  type AiProviderChoice,
  type Profile,
  type UserSession,
} from "@/lib/api";
import { describeUserAgent } from "@/lib/connectors.ts";
import { errorText, passwordProblems } from "@/lib/errors";

type Msg = { tone: "error" | "success" | "info"; text: string } | null;

export function SettingsView() {
  const { t, lang } = useUi();
  const { me, reload } = useMe("/definicoes");
  const fmtDate = useCallback(
    (iso: string) =>
      new Intl.DateTimeFormat(lang === "pt" ? "pt-PT" : "en-US", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso)),
    [lang],
  );

  return (
    <div className="min-h-screen">
      <Header>
        <AppNav me={me} />
      </Header>
      <main className="mx-auto max-w-3xl space-y-8 px-4 py-8 sm:px-6 sm:py-10">
        <h1 className="text-2xl font-semibold tracking-tight">{t("settings.title")}</h1>
        {me ? (
          <>
            <ProfileSection onSaved={() => void reload()} />
            <PasswordSection />
            <AiSection />
            <SessionsSection fmtDate={fmtDate} />
          </>
        ) : (
          <p className="text-slate-500">{t("dash.loading")}</p>
        )}
      </main>
    </div>
  );
}

function Section({ title, lead, children }: { title: string; lead?: string; children: React.ReactNode }) {
  return (
    <section className="card space-y-4 p-4 sm:p-6">
      <div>
        <h2 className="font-semibold">{title}</h2>
        {lead && <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">{lead}</p>}
      </div>
      {children}
    </section>
  );
}

function Label({ htmlFor, children }: { htmlFor: string; children: React.ReactNode }) {
  return <label htmlFor={htmlFor} className="text-sm font-medium text-slate-700 dark:text-slate-300">{children}</label>;
}

// --- perfil ----------------------------------------------------------------------------------------
function ProfileSection({ onSaved }: { onSaved: () => void }) {
  const { t } = useUi();
  const [profile, setProfile] = useState<Profile | null>(null);
  const [name, setName] = useState("");
  const [avatar, setAvatar] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  const [avatarBroken, setAvatarBroken] = useState(false);

  useEffect(() => {
    api<Profile>("/api/settings/profile")
      .then((p) => {
        setProfile(p);
        setName(p.display_name ?? "");
        setAvatar(p.avatar_url ?? "");
      })
      .catch((err) => setMsg({ tone: "error", text: errorText(err, t) }));
  }, [t]);

  async function save(e: FormEvent) {
    e.preventDefault();
    const url = avatar.trim();
    if (url && !/^https:\/\/[^\s]+$/.test(url)) {
      setMsg({ tone: "error", text: t("settings.profile.avatarInvalid") });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      const p = await patch<Profile>("/api/settings/profile", { display_name: name.trim(), avatar_url: url });
      setProfile(p);
      setAvatarBroken(false);
      setMsg({ tone: "success", text: t("settings.saved") });
      onSaved();
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(false);
    }
  }

  const initial = (profile?.display_name || profile?.email || "?").slice(0, 1).toUpperCase();

  return (
    <Section title={t("settings.profile")}>
      <form onSubmit={save} className="space-y-4">
        <div className="flex items-center gap-4">
          {profile?.avatar_url && !avatarBroken ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={profile.avatar_url} alt="" referrerPolicy="no-referrer" onError={() => setAvatarBroken(true)}
              className="h-14 w-14 shrink-0 rounded-full border border-slate-200 object-cover dark:border-slate-700" />
          ) : (
            <span className="grid h-14 w-14 shrink-0 place-items-center rounded-full bg-brand-600 text-lg font-semibold text-white">
              {initial}
            </span>
          )}
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">{profile?.email ?? "…"}</p>
            {profile && (
              <p className="text-xs text-slate-500">
                {profile.email_verified ? t("settings.profile.verified") : t("settings.profile.unverified")} ·{" "}
                {t("settings.profile.emailHint")}
              </p>
            )}
          </div>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="profile-name">{t("settings.profile.name")}</Label>
          <input id="profile-name" className="input" value={name} maxLength={120} autoComplete="name"
            onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="profile-avatar">{t("settings.profile.avatar")}</Label>
          <input id="profile-avatar" className="input" type="url" inputMode="url" value={avatar} maxLength={500}
            placeholder="https://…" onChange={(e) => setAvatar(e.target.value)} />
          <p className="text-xs text-slate-500">{t("settings.profile.avatarHint")}</p>
        </div>
        {msg && <Alert tone={msg.tone}>{msg.text}</Alert>}
        <button type="submit" className="btn-primary w-full sm:w-auto" disabled={busy || !profile}>{t("settings.save")}</button>
      </form>
    </Section>
  );
}

// --- palavra-passe ---------------------------------------------------------------------------------------
function PasswordSection() {
  const { t } = useUi();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  const problems = next ? passwordProblems(next) : [];

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (next !== confirm) {
      setMsg({ tone: "error", text: t("settings.password.mismatch") });
      return;
    }
    if (problems.length) {
      setMsg({ tone: "error", text: t("error.weak_password") });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      const r = await post<{ revoked_sessions: number }>("/api/settings/password", {
        current_password: current, new_password: next,
      });
      setCurrent("");
      setNext("");
      setConfirm("");
      setMsg({ tone: "success", text: t("settings.password.done", { n: r.revoked_sessions }) });
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title={t("settings.password")} lead={t("auth.password.rules")}>
      <form onSubmit={submit} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="pw-current">{t("settings.password.current")}</Label>
          <input id="pw-current" className="input" type="password" autoComplete="current-password" required
            value={current} onChange={(e) => setCurrent(e.target.value)} />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="pw-new">{t("settings.password.new")}</Label>
            <input id="pw-new" className="input" type="password" autoComplete="new-password" required
              value={next} onChange={(e) => setNext(e.target.value)} aria-invalid={problems.length > 0} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="pw-confirm">{t("settings.password.confirm")}</Label>
            <input id="pw-confirm" className="input" type="password" autoComplete="new-password" required
              value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          </div>
        </div>
        {msg && <Alert tone={msg.tone}>{msg.text}</Alert>}
        <button type="submit" className="btn-primary w-full sm:w-auto" disabled={busy || !current || !next}>
          {t("settings.password.submit")}
        </button>
      </form>
    </Section>
  );
}

// --- preferência de IA ---------------------------------------------------------------------------------------
function AiSection() {
  const { t } = useUi();
  const [pref, setPref] = useState<AiPreference | null>(null);
  const [provider, setProvider] = useState<AiProviderChoice>("ollama");
  const [model, setModel] = useState("");
  const [fallback, setFallback] = useState<string>("groq");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);

  const apply = useCallback((p: AiPreference) => {
    setPref(p);
    setProvider(p.provider);
    setModel(p.model ?? "");
    setFallback(p.fallback_provider ?? "none");
  }, []);

  useEffect(() => {
    api<AiPreference>("/api/settings/ai").then(apply).catch((err) => setMsg({ tone: "error", text: errorText(err, t) }));
  }, [apply, t]);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      apply(await put<AiPreference>("/api/settings/ai", {
        provider, model: provider === "auto" ? null : model.trim() || null,
        fallback_provider: fallback === "none" ? null : fallback,
      }));
      setMsg({ tone: "success", text: t("settings.saved") });
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    setMsg(null);
    try {
      await del("/api/settings/ai");
      apply(await api<AiPreference>("/api/settings/ai"));
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(false);
    }
  }

  const label = (o: AiPreference["options"][number]) =>
    `${o.label}${o.paid ? ` (${t("settings.ai.paid")})` : ""}${o.provider !== "auto" && !o.available ? ` · ${t("settings.ai.unavailable")}` : ""}`;

  return (
    <Section title={t("settings.ai")} lead={t("settings.ai.lead")}>
      <form onSubmit={save} className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="ai-provider">{t("settings.ai.provider")}</Label>
            <select id="ai-provider" className="input" value={provider}
              onChange={(e) => setProvider(e.target.value as AiProviderChoice)} disabled={!pref}>
              {pref?.options.map((o) => (
                <option key={o.provider} value={o.provider} disabled={!o.allowed}>{label(o)}</option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ai-fallback">{t("settings.ai.fallback")}</Label>
            <select id="ai-fallback" className="input" value={fallback} onChange={(e) => setFallback(e.target.value)}
              disabled={!pref}>
              <option value="none">{t("settings.ai.noFallback")}</option>
              {pref?.options.filter((o) => o.provider !== "auto" && o.provider !== provider).map((o) => (
                <option key={o.provider} value={o.provider} disabled={!o.allowed}>{label(o)}</option>
              ))}
            </select>
          </div>
        </div>
        {provider !== "auto" && (
          <div className="space-y-1.5">
            <Label htmlFor="ai-model">{t("settings.ai.model")}</Label>
            <input id="ai-model" className="input font-mono" value={model} maxLength={200} spellCheck={false}
              placeholder={provider === "ollama" ? "qwen3:1.7b" : ""} onChange={(e) => setModel(e.target.value)} />
            <p className="text-xs text-slate-500">{t("settings.ai.modelHint")}</p>
          </div>
        )}
        {pref && !pref.allow_paid_providers && <p className="text-xs text-slate-500">{t("settings.ai.paidLocked")}</p>}
        {pref && !pref.saved && <Alert tone="info">{t("settings.ai.notSaved")}</Alert>}
        {msg && <Alert tone={msg.tone}>{msg.text}</Alert>}
        <div className="flex flex-col gap-2 sm:flex-row">
          <button type="submit" className="btn-primary" disabled={busy || !pref}>{t("settings.save")}</button>
          {pref?.saved && (
            <button type="button" className="btn-ghost" disabled={busy} onClick={() => void reset()}>
              {t("settings.ai.reset")}
            </button>
          )}
        </div>
      </form>
    </Section>
  );
}

// --- sessões -----------------------------------------------------------------------------------------------
function SessionsSection({ fmtDate }: { fmtDate: (iso: string) => string }) {
  const { t } = useUi();
  const [items, setItems] = useState<UserSession[] | null>(null);
  const [msg, setMsg] = useState<Msg>(null);
  const [busy, setBusy] = useState(false);
  const [unsupported, setUnsupported] = useState(false);

  const load = useCallback(async () => {
    try {
      setItems((await api<{ items: UserSession[] }>("/api/settings/sessions")).items);
    } catch (err) {
      if (err instanceof ApiError && err.code === "not_supported") setUnsupported(true);
      else setMsg({ tone: "error", text: errorText(err, t) });
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  async function revoke(id: string) {
    if (!window.confirm(t("settings.sessions.revokeConfirm"))) return;
    setBusy(true);
    setMsg(null);
    try {
      await del(`/api/settings/sessions/${id}`);
      await load();
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(false);
    }
  }

  async function revokeOthers() {
    if (!window.confirm(t("settings.sessions.revokeOthersConfirm"))) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await post<{ revoked: number }>("/api/settings/sessions/revoke-others");
      setMsg({ tone: "success", text: t("settings.sessions.revokedOthers", { n: r.revoked }) });
      await load();
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(false);
    }
  }

  const others = items?.filter((s) => !s.current) ?? [];

  return (
    <Section title={t("settings.sessions")} lead={t("settings.sessions.lead")}>
      {unsupported ? (
        <p className="text-sm text-slate-500">{t("settings.notSupported")}</p>
      ) : (
        <>
          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {items?.map((s) => (
              <li key={s.id} className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                    <span className="truncate">{describeUserAgent(s.user_agent)}</span>
                    {s.current && (
                      <span className="rounded-full bg-brand-50 px-2 py-0.5 text-xs text-brand-700 dark:bg-brand-600/20 dark:text-brand-400">
                        {t("settings.sessions.current")}
                      </span>
                    )}
                  </p>
                  <p className="text-xs text-slate-500">
                    {s.ip ?? "—"} · {t("settings.sessions.lastUsed", { time: fmtDate(s.last_used_at) })} ·{" "}
                    {t("settings.sessions.created", { time: fmtDate(s.created_at) })}
                  </p>
                </div>
                {!s.current && (
                  <button type="button" className="btn-ghost shrink-0 text-red-600 dark:text-red-400" disabled={busy}
                    onClick={() => void revoke(s.id)}>
                    {t("settings.sessions.revoke")}
                  </button>
                )}
              </li>
            ))}
          </ul>
          {items && others.length === 0 && <p className="text-sm text-slate-500">{t("settings.sessions.empty")}</p>}
          {msg && <Alert tone={msg.tone}>{msg.text}</Alert>}
          {others.length > 0 && (
            <button type="button" className="btn-ghost w-full text-red-600 dark:text-red-400 sm:w-auto" disabled={busy}
              onClick={() => void revokeOthers()}>
              {t("settings.sessions.revokeOthers")}
            </button>
          )}
        </>
      )}
    </Section>
  );
}
