"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import { AppNav, useMe } from "@/components/AppNav";
import { Alert } from "@/components/AuthShell";
import { Header } from "@/components/Header";
import { useUi } from "@/components/Providers";
import { api, del, post, type ConnectorItem, type ConnectorsResponse } from "@/lib/api";
import {
  callbackFeedback,
  connectionState,
  isTrustedAuthorizeUrl,
  normalizeShopDomain,
  type ConnectionState,
} from "@/lib/connectors.ts";
import { errorText } from "@/lib/errors";
import type { MessageKey } from "@/lib/i18n.ts";

type Msg = { tone: "error" | "success" | "info"; text: string } | null;

const ICONS: Record<string, string> = { github: "GH", gmail: "M", google_drive: "D", shopify: "S" };
const PROVIDER_NAMES: Record<string, string> = { github: "GitHub", google: "Google" };

const STATE_STYLE: Record<ConnectionState, string> = {
  connected: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  expired: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  error: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  revoked: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400",
  pending: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400",
  none: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400",
};

export function ConnectorsView() {
  const { t, lang } = useUi();
  const { me } = useMe("/conectores");
  const [data, setData] = useState<ConnectorsResponse | null>(null);
  const [banner, setBanner] = useState<Msg>(null);
  const fmtDate = useCallback(
    (iso: string) =>
      new Intl.DateTimeFormat(lang === "pt" ? "pt-PT" : "en-US", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso)),
    [lang],
  );

  const load = useCallback(async () => {
    try {
      setData(await api<ConnectorsResponse>("/api/connectors"));
    } catch (err) {
      setBanner({ tone: "error", text: errorText(err, t) });
    }
  }, [t]);

  useEffect(() => {
    if (me) void load();
  }, [me, load]);

  // resultado do retorno OAuth (?connected=… / ?error=…): mostra e limpa o URL
  useEffect(() => {
    const fb = callbackFeedback(window.location.search);
    if (!fb) return;
    const name = fb.connector ? (data?.items.find((i) => i.key === fb.connector)?.name ?? fb.connector) : "";
    setBanner({ tone: fb.tone, text: t(fb.key, { name }) });
    if (data) window.history.replaceState(null, "", window.location.pathname);
  }, [data, t]);

  function onChange(updated: ConnectorItem) {
    setData((prev) => prev ? { ...prev, items: prev.items.map((i) => (i.key === updated.key ? { ...i, ...updated } : i)) } : prev);
  }

  const connectable = data?.items.filter((i) => i.connectable) ?? [];
  const later = data?.items.filter((i) => !i.connectable) ?? [];

  return (
    <div className="min-h-screen">
      <Header>
        <AppNav me={me} />
      </Header>
      <main className="mx-auto max-w-5xl space-y-8 px-4 py-8 sm:px-6 sm:py-10">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t("conn.title")}</h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">{t("conn.lead")}</p>
          <p className="mt-1 text-xs text-slate-500">{t("conn.agentNote")}</p>
        </div>
        {banner && <Alert tone={banner.tone}>{banner.text}</Alert>}
        {data && !data.encryption_ready && <Alert tone="error">{t("conn.noEncryption")}</Alert>}

        {!data ? (
          <p className="text-slate-500">{t("dash.loading")}</p>
        ) : (
          <div className="grid gap-4 md:grid-cols-2">
            {connectable.map((item) => (
              <ConnectorCard key={item.key} item={item} fmtDate={fmtDate} onChange={onChange} reload={load}
                disabled={!data.encryption_ready} />
            ))}
          </div>
        )}

        {later.length > 0 && (
          <section>
            <h2 className="mb-3 font-semibold">{t("conn.otherTitle")}</h2>
            <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {later.map((i) => (
                <li key={i.key} className="card flex min-w-0 items-center justify-between gap-3 p-4">
                  <div className="min-w-0">
                    <p className="font-medium">{i.name}</p>
                    <p className="truncate text-xs text-slate-500">{i.description}</p>
                  </div>
                  <span className="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                    {t("conn.planned")}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </main>
    </div>
  );
}

function ConnectorCard({
  item, fmtDate, onChange, reload, disabled,
}: {
  item: ConnectorItem;
  fmtDate: (iso: string) => string;
  onChange: (i: ConnectorItem) => void;
  reload: () => Promise<void>;
  disabled: boolean;
}) {
  const { t } = useUi();
  const [busy, setBusy] = useState<"connect" | "check" | "disconnect" | null>(null);
  const [msg, setMsg] = useState<Msg>(null);
  const [shop, setShop] = useState("");
  const [token, setToken] = useState("");
  const state = connectionState(item.connection);
  const isConnected = state === "connected" || state === "expired" || state === "error";
  const oauthMissing = item.method === "oauth" && item.oauth_configured === false;

  async function connectOAuth() {
    setBusy("connect");
    setMsg(null);
    try {
      const r = await post<{ authorize_url: string }>(`/api/connectors/${item.key}/connect`);
      if (!isTrustedAuthorizeUrl(r.authorize_url)) throw new Error("untrusted");
      window.location.assign(r.authorize_url);
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
      setBusy(null);
    }
  }

  async function connectShopify(e: FormEvent) {
    e.preventDefault();
    const domain = normalizeShopDomain(shop);
    if (!domain) {
      setMsg({ tone: "error", text: t("conn.shop.domainInvalid") });
      return;
    }
    setBusy("connect");
    setMsg(null);
    try {
      const updated = await post<ConnectorItem>(`/api/connectors/${item.key}/connect`, {
        shop_domain: domain, access_token: token.trim(),
      });
      setToken("");
      onChange(updated);
      setMsg({ tone: "success", text: t("conn.callback.connected", { name: item.name }) });
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(null);
    }
  }

  async function check() {
    setBusy("check");
    setMsg(null);
    try {
      const updated = await api<ConnectorItem>(`/api/connectors/${item.key}/status?check=true`);
      onChange(updated);
      if (updated.check?.ok) setMsg({ tone: "success", text: t("conn.checkOk") });
      else setMsg({ tone: "error", text: updated.check?.message ?? t("conn.callback.generic") });
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(null);
    }
  }

  async function disconnect() {
    if (!window.confirm(t("conn.disconnectConfirm", { name: item.name }))) return;
    setBusy("disconnect");
    setMsg(null);
    try {
      await del(`/api/connectors/${item.key}`);
      await reload();
      setMsg({ tone: "info", text: t("conn.disconnected") });
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err, t) });
    } finally {
      setBusy(null);
    }
  }

  const shopId = `shop-${item.key}`;
  const tokenId = `token-${item.key}`;

  return (
    <article className="card flex flex-col gap-4 p-4 sm:p-6">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-slate-900 text-sm font-bold text-white dark:bg-slate-700">
            {ICONS[item.key] ?? item.name.slice(0, 1)}
          </span>
          <div className="min-w-0">
            <h2 className="font-semibold">{item.name}</h2>
            <p className="text-xs text-slate-500">{item.description}</p>
          </div>
        </div>
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${STATE_STYLE[state]}`}>
          {t(`conn.state.${state}` as MessageKey)}
        </span>
      </div>

      {item.connection?.account_label && isConnected && (
        <div className="space-y-0.5 text-sm">
          <p className="break-all">{t("conn.account", { label: item.connection.account_label })}</p>
          {item.connection.last_health_check_at && (
            <p className="text-xs text-slate-500">{t("conn.lastCheck", { time: fmtDate(item.connection.last_health_check_at) })}</p>
          )}
        </div>
      )}

      {oauthMissing && (
        <p className="text-xs text-amber-700 dark:text-amber-400">
          {t("conn.oauthMissing", { provider: PROVIDER_NAMES[item.oauth_provider ?? ""] ?? "" })}
        </p>
      )}

      {item.method === "api_key" && !isConnected && (
        <form onSubmit={connectShopify} className="space-y-3">
          <div className="space-y-1.5">
            <label htmlFor={shopId} className="text-sm font-medium text-slate-700 dark:text-slate-300">{t("conn.shop.domain")}</label>
            <input id={shopId} className="input" value={shop} onChange={(e) => setShop(e.target.value)} required
              autoCapitalize="none" spellCheck={false} placeholder="minha-loja.myshopify.com" disabled={disabled} />
            <p className="text-xs text-slate-500">{t("conn.shop.domainHint")}</p>
          </div>
          <div className="space-y-1.5">
            <label htmlFor={tokenId} className="text-sm font-medium text-slate-700 dark:text-slate-300">{t("conn.shop.token")}</label>
            <input id={tokenId} className="input font-mono" type="password" autoComplete="off" spellCheck={false}
              value={token} onChange={(e) => setToken(e.target.value)} required placeholder="shpat_…" disabled={disabled} />
            <p className="text-xs text-slate-500">{t("conn.shop.tokenHint")}</p>
          </div>
          <button type="submit" className="btn-primary w-full sm:w-auto" disabled={disabled || busy !== null || !shop || !token}>
            {busy === "connect" ? t("conn.connecting") : t("conn.connect")}
          </button>
        </form>
      )}

      {msg && <Alert tone={msg.tone}>{msg.text}</Alert>}

      <div className="mt-auto flex flex-col gap-2 sm:flex-row sm:flex-wrap">
        {item.method === "oauth" && (
          <button type="button" className={isConnected ? "btn-ghost" : "btn-primary"}
            disabled={disabled || oauthMissing || busy !== null} onClick={() => void connectOAuth()}>
            {busy === "connect" ? t("conn.connecting") : isConnected ? t("conn.reconnect") : t("conn.connect")}
          </button>
        )}
        {isConnected && (
          <>
            <button type="button" className="btn-ghost" disabled={busy !== null} onClick={() => void check()}>
              {busy === "check" ? t("conn.checking") : t("conn.check")}
            </button>
            <button type="button" className="btn-ghost text-red-600 dark:text-red-400" disabled={busy !== null}
              onClick={() => void disconnect()}>
              {t("conn.disconnect")}
            </button>
          </>
        )}
      </div>
    </article>
  );
}
