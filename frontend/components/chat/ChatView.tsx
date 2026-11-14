"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { AppNav, useMe } from "@/components/AppNav";
import { Alert } from "@/components/AuthShell";
import { Header } from "@/components/Header";
import { useUi } from "@/components/Providers";
import {
  ApiError,
  api,
  del,
  post,
  postStream,
  type Approval,
  type ApprovalDecisionResult,
  type ChatMessage,
  type ChatModels,
  type ConnectAction,
  type Conversation,
  type MessageMetadata,
  type SendMessageResult,
  type ToolActivity,
} from "@/lib/api";
import { approvalPreview, chatReturnFeedback, connectPath, mergeToolActivity, messageActions } from "@/lib/chatAgent.ts";
import { isTrustedAuthorizeUrl } from "@/lib/connectors.ts";
import { errorText } from "@/lib/errors";

const CONNECTOR_NAMES: Record<string, string> = { github: "GitHub", gmail: "Gmail", google_drive: "Google Drive", shopify: "Shopify" };

type Pending = { content: string };

// Valor do seletor: "auto" | "ollama|<modelo>" | "cloud|<fornecedor>|<modelo>"
function selectionBody(value: string): { provider?: string; model?: string } {
  if (value.startsWith("ollama|")) return { provider: "ollama", model: value.slice(7) };
  if (value.startsWith("cloud|")) {
    const [, provider, ...rest] = value.split("|");
    return { provider, model: rest.join("|") || undefined };
  }
  return {};
}

export function ChatView() {
  const { t, lang } = useUi();
  const { me } = useMe("/chat");
  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loadingThread, setLoadingThread] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [models, setModels] = useState<ChatModels | null>(null);
  const [selected, setSelected] = useState("auto");
  const [streamText, setStreamText] = useState("");
  const [info, setInfo] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<Record<string, Approval>>({});
  const [liveTools, setLiveTools] = useState<ToolActivity[]>([]);
  const [liveMeta, setLiveMeta] = useState<MessageMetadata>({});
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const fmtTime = (iso: string) =>
    new Intl.DateTimeFormat(lang === "pt" ? "pt-PT" : "en-US", { dateStyle: "short", timeStyle: "short" }).format(new Date(iso));

  const loadConversations = useCallback(async () => {
    try {
      const res = await api<{ items: Conversation[] }>("/api/chat/conversations");
      setConversations(res.items);
      return res.items;
    } catch (err) {
      setError(errorText(err, t));
      setConversations([]);
      return [];
    }
  }, [t]);

  const openConversation = useCallback(async (id: string) => {
    setActiveId(id);
    setSidebarOpen(false);
    setError(null);
    setLoadingThread(true);
    try {
      const res = await api<{ items: ChatMessage[]; approvals?: Record<string, Approval> }>(
        `/api/chat/conversations/${id}/messages`);
      setMessages(res.items);
      setApprovals((prev) => ({ ...prev, ...(res.approvals ?? {}) }));
    } catch (err) {
      setError(errorText(err, t));
      setMessages([]);
    } finally {
      setLoadingThread(false);
    }
  }, [t]);

  useEffect(() => {
    if (!me) return;
    // regresso do OAuth iniciado no chat (/chat?connected=github&conversation=…)
    const back = chatReturnFeedback(window.location.search);
    if (back) {
      if ("connected" in back) {
        const name = CONNECTOR_NAMES[back.connected] ?? back.connected;
        setInfo(t("chat.connect.done", { name }));
        setDraft(t("chat.connect.continue", { name }));
      } else {
        setError(t(`conn.callback.${back.error}` as Parameters<typeof t>[0]) || back.error);
      }
      window.history.replaceState(null, "", window.location.pathname);
    }
    void loadConversations().then((items) => {
      const target = back?.conversation && items.some((c) => c.id === back.conversation) ? back.conversation : items[0]?.id;
      if (target) void openConversation(target);
    });
  }, [me, loadConversations, openConversation, t]);

  useEffect(() => {
    if (!me) return;
    api<ChatModels>("/api/chat/models").then(setModels).catch(() => setModels(null));
  }, [me]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pending, streamText]);

  function stop() {
    abortRef.current?.abort();
  }

  function newConversation() {
    // A conversa só é criada no servidor quando a primeira mensagem é enviada.
    setActiveId(null);
    setMessages([]);
    setError(null);
    setSidebarOpen(false);
    inputRef.current?.focus();
  }

  async function removeConversation(id: string) {
    if (!window.confirm(t("chat.deleteConfirm"))) return;
    try {
      await del(`/api/chat/conversations/${id}`);
      setConversations((prev) => (prev ?? []).filter((c) => c.id !== id));
      if (activeId === id) {
        setActiveId(null);
        setMessages([]);
      }
    } catch (err) {
      setError(errorText(err, t));
    }
  }

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const content = draft.trim();
    if (!content || pending) return;
    setError(null);
    setInfo(null);
    setPending({ content });
    setStreamText("");
    setLiveTools([]);
    setLiveMeta({});
    setDraft("");
    let convId = activeId;
    const controller = new AbortController();
    abortRef.current = controller;
    let finished = false;
    try {
      if (!convId) {
        const conv = await post<Conversation>("/api/chat/conversations", {});
        convId = conv.id;
        setActiveId(conv.id);
        setConversations((prev) => [conv, ...(prev ?? [])]);
      }
      await postStream(`/api/chat/conversations/${convId}/messages/stream`, { content, ...selectionBody(selected) }, (ev) => {
        const data = JSON.parse(ev.data);
        if (ev.event === "delta") {
          setStreamText((prev) => prev + (data.text as string));
        } else if (ev.event === "tool") {
          setLiveTools((prev) => mergeToolActivity(prev, data as ToolActivity));
        } else if (ev.event === "connect") {
          const action = data as ConnectAction;
          setLiveMeta((prev) => ({ ...prev, actions: [...(prev.actions ?? []), action] }));
        } else if (ev.event === "approval") {
          const approval = data as Approval;
          setApprovals((prev) => ({ ...prev, [approval.id]: approval }));
          setLiveMeta((prev) => ({ ...prev, actions: [...(prev.actions ?? []), { type: "approval", id: approval.id }] }));
        } else if (ev.event === "done") {
          finished = true;
          const res = data as SendMessageResult;
          setApprovals((prev) => ({ ...prev, ...(res.approvals ?? {}) }));
          setMessages((prev) => [...prev, res.user_message, res.assistant_message]);
          setConversations((prev) => {
            const rest = (prev ?? []).filter((c) => c.id !== res.conversation.id);
            return [res.conversation, ...rest];
          });
        } else if (ev.event === "error") {
          finished = true;
          throw new ApiError(503, data.code ?? "ai_provider_error", data.message ?? "");
        }
      }, controller.signal);
      if (!finished) throw new ApiError(0, "network", "stream ended");
    } catch (err) {
      if ((err as Error)?.name === "AbortError") {
        // o servidor grava o texto já recebido; recarrega a conversa para o mostrar
        setInfo(t("chat.stopped"));
        const id = convId;
        if (id) window.setTimeout(() => void openConversation(id).then(() => loadConversations()), 800);
      } else {
        setError(errorText(err, t));
        setDraft(content); // devolve o texto para reenviar
      }
    } finally {
      abortRef.current = null;
      setPending(null);
      setStreamText("");
      setLiveTools([]);
      setLiveMeta({});
      inputRef.current?.focus();
    }
  }

  async function decide(id: string, decision: "approve" | "reject") {
    setError(null);
    try {
      const res = await post<ApprovalDecisionResult>(`/api/chat/approvals/${id}`, { decision });
      if (res.approval) setApprovals((prev) => ({ ...prev, [id]: res.approval as Approval }));
      if (res.message) setMessages((prev) => [...prev, res.message as ChatMessage]);
    } catch (err) {
      setError(errorText(err, t));
      // estado mudou (expirou/decidida noutro separador): recarrega
      if (activeId) void openConversation(activeId);
    }
  }

  async function connectAccount(action: ConnectAction) {
    setError(null);
    if (action.method !== "oauth") {
      window.location.assign(`/conectores#${action.connector}`);
      return;
    }
    try {
      const r = await post<{ authorize_url: string }>(connectPath(action.connector, activeId));
      if (!isTrustedAuthorizeUrl(r.authorize_url)) throw new Error("untrusted");
      window.location.assign(r.authorize_url);
    } catch (err) {
      setError(errorText(err, t));
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  }

  const sidebar = (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between gap-2 p-3">
        <h2 className="text-sm font-semibold text-slate-600 dark:text-slate-400">{t("chat.conversations")}</h2>
        <button type="button" className="btn-primary px-3 py-1.5" onClick={newConversation}>+ {t("chat.new")}</button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {conversations === null ? (
          <p className="px-2 text-sm text-slate-500">{t("chat.loading")}</p>
        ) : conversations.length === 0 ? (
          <p className="px-2 text-sm text-slate-500">{t("chat.empty.list")}</p>
        ) : (
          <ul className="space-y-1">
            {conversations.map((c) => (
              <li key={c.id} className="group flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => void openConversation(c.id)}
                  className={`min-w-0 flex-1 rounded-lg px-3 py-2 text-left text-sm transition hover:bg-slate-100 dark:hover:bg-slate-800 ${
                    c.id === activeId ? "bg-slate-100 font-medium dark:bg-slate-800" : ""
                  }`}
                >
                  <span className="block truncate">{c.title || t("chat.untitled")}</span>
                  <span className="block text-xs text-slate-500">{fmtTime(c.last_message_at ?? c.created_at)}</span>
                </button>
                <button
                  type="button"
                  onClick={() => void removeConversation(c.id)}
                  className="shrink-0 rounded-md px-2 py-1 text-xs text-slate-500 hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-950/50"
                  aria-label={`${t("chat.delete")}: ${c.title || t("chat.untitled")}`}
                  title={t("chat.delete")}
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );

  const activeTitle = conversations?.find((c) => c.id === activeId)?.title;
  // aviso "Motor local não configurado": ao escolher o motor local, ou quando não há nenhuma alternativa
  const localUnusable = !!models && (!!models.local.warning || models.local.models.length === 0);
  const localWarning = localUnusable && (selected.startsWith("ollama|") || models.cloud.length === 0);

  return (
    <div className="flex h-[100dvh] flex-col">
      <Header>
        <AppNav me={me} />
      </Header>
      <div className="relative mx-auto flex min-h-0 w-full max-w-6xl flex-1">
        {/* barra lateral: fixa em ecrãs largos, painel sobreposto no telemóvel */}
        <aside className="hidden w-72 shrink-0 border-r border-slate-200 dark:border-slate-800 md:block">{sidebar}</aside>
        {sidebarOpen && (
          <div className="absolute inset-0 z-20 flex md:hidden">
            <aside className="w-[85%] max-w-xs border-r border-slate-200 bg-white shadow-xl dark:border-slate-800 dark:bg-[#0b0f17]">
              {sidebar}
            </aside>
            <button type="button" aria-label="✕" className="flex-1 bg-black/40" onClick={() => setSidebarOpen(false)} />
          </div>
        )}

        <section className="flex min-w-0 flex-1 flex-col">
          <div className="flex items-center gap-2 border-b border-slate-200 px-3 py-2 dark:border-slate-800 md:hidden">
            <button type="button" className="btn-ghost" onClick={() => setSidebarOpen(true)}>☰ {t("chat.conversations")}</button>
            <span className="min-w-0 flex-1 truncate text-sm text-slate-500">{activeTitle || t("chat.untitled")}</span>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-3 py-4 sm:px-6" aria-live="polite">
            {loadingThread ? (
              <p className="text-sm text-slate-500">{t("chat.loading")}</p>
            ) : messages.length === 0 && !pending ? (
              <div className="grid h-full place-items-center text-center">
                <div>
                  <h1 className="text-xl font-semibold tracking-tight">{t("chat.title")}</h1>
                  <p className="mt-2 text-sm text-slate-500">{t("chat.empty.thread")}</p>
                </div>
              </div>
            ) : (
              <ol className="mx-auto max-w-3xl space-y-4">
                {messages.map((m) => (
                  <li key={m.id} className="list-none space-y-2">
                    <ol>
                      <MessageBubble role={m.role} content={m.content} label={m.role === "user" ? t("chat.you") : t("chat.ai")} />
                    </ol>
                    {m.role === "assistant" && m.metadata && (
                      <AgentExtras metadata={m.metadata} approvals={approvals} onDecide={decide} onConnect={connectAccount} />
                    )}
                  </li>
                ))}
                {pending && (
                  <>
                    <MessageBubble role="user" content={pending.content} label={t("chat.you")} />
                    {liveTools.length > 0 && (
                      <li className="list-none"><ToolChips tools={liveTools} running /></li>
                    )}
                    {streamText ? (
                      <MessageBubble role="assistant" content={streamText + " ▍"} label={t("chat.ai")} />
                    ) : (
                    <li className="flex justify-start">
                      <div className="rounded-2xl bg-slate-100 px-4 py-2 text-sm text-slate-500 dark:bg-slate-800">
                        <span className="inline-flex items-center gap-2">
                          <span className="h-2 w-2 animate-pulse rounded-full bg-brand-500" />
                          {t("chat.thinking")}
                        </span>
                      </div>
                    </li>
                    )}
                    {(liveMeta.actions?.length ?? 0) > 0 && (
                      <li className="list-none">
                        <AgentExtras metadata={liveMeta} approvals={approvals} onDecide={decide} onConnect={connectAccount} />
                      </li>
                    )}
                  </>
                )}
              </ol>
            )}
            <div ref={bottomRef} />
          </div>

          <form onSubmit={send} className="border-t border-slate-200 p-3 dark:border-slate-800 sm:px-6">
            <div className="mx-auto max-w-3xl">
              {error && <div className="mb-2"><Alert tone="error">{error}</Alert></div>}
              {info && !error && <div className="mb-2"><Alert tone="info">{info}</Alert></div>}
              {localWarning && (
                <div className="mb-2">
                  <Alert tone="info">
                    <strong>{models?.local.warning ?? t("chat.local.notConfigured")}</strong>
                    {!models?.local.configured && <> · {t("chat.local.notConfiguredHint")}</>}
                  </Alert>
                </div>
              )}
              <div className="mb-2 flex items-center gap-2">
                <label htmlFor="chat-model" className="text-xs text-slate-500">{t("chat.model")}</label>
                <select id="chat-model" className="input h-8 max-w-xs py-0 text-xs" value={selected}
                  onChange={(e) => setSelected(e.target.value)} disabled={!!pending}>
                  <option value="auto">{t("chat.model.auto")}</option>
                  <optgroup label={t("chat.model.local")}>
                    {models && models.local.models.length > 0 ? (
                      models.local.models.map((m) => (
                        <option key={m} value={`ollama|${m}`}>
                          {m}{m === models.local.default_model ? ` (${t("chat.model.default")})` : ""}
                        </option>
                      ))
                    ) : (
                      <option value="ollama|" disabled>{models?.local.warning ?? t("chat.local.notConfigured")}</option>
                    )}
                  </optgroup>
                  {models && models.cloud.length > 0 && (
                    <optgroup label={t("chat.model.cloud")}>
                      {models.cloud.map((c) => (
                        <option key={c.provider} value={`cloud|${c.provider}|${c.model}`}>{c.provider} · {c.model}</option>
                      ))}
                    </optgroup>
                  )}
                </select>
              </div>
              <div className="flex items-end gap-2">
                <label className="sr-only" htmlFor="chat-input">{t("chat.placeholder")}</label>
                <textarea
                  id="chat-input"
                  ref={inputRef}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={onKeyDown}
                  placeholder={t("chat.placeholder")}
                  rows={2}
                  maxLength={8000}
                  className="input max-h-48 min-h-[2.75rem] flex-1 resize-y"
                  disabled={!!pending}
                />
                {pending ? (
                  <button type="button" className="btn-ghost h-11 shrink-0" onClick={stop}>
                    ■ {t("chat.stop")}
                  </button>
                ) : (
                  <button type="submit" className="btn-primary h-11 shrink-0" disabled={!draft.trim()}>
                    {t("chat.send")}
                  </button>
                )}
              </div>
              <p className="mt-1 hidden text-xs text-slate-500 sm:block">{t("chat.hint")}</p>
            </div>
          </form>
        </section>
      </div>
    </div>
  );
}

function MessageBubble({ role, content, label }: { role: "user" | "assistant"; content: string; label: string }) {
  const mine = role === "user";
  return (
    <li className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] whitespace-pre-wrap break-words rounded-2xl px-4 py-2 text-sm leading-relaxed ${
          mine ? "bg-brand-600 text-white" : "bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-slate-100"
        }`}
      >
        <span className="sr-only">{label}: </span>
        {content}
      </div>
    </li>
  );
}

function ToolChips({ tools, running = false }: { tools: ToolActivity[]; running?: boolean }) {
  const { t } = useUi();
  return (
    <div className="flex flex-wrap gap-1.5">
      {tools.map((tool, i) => {
        const name = tool.label || tool.name;
        const key = tool.status === "error" ? "chat.tool.error" : running && i === tools.length - 1 ? "chat.tool.running" : "chat.tool.ok";
        return (
          <span key={`${tool.name}-${i}`}
            className={`rounded-full px-2 py-0.5 text-xs ${tool.status === "error"
              ? "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300"
              : "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300"}`}>
            {t(key, { name })}
          </span>
        );
      })}
    </div>
  );
}

function AgentExtras({
  metadata, approvals, onDecide, onConnect,
}: {
  metadata: MessageMetadata;
  approvals: Record<string, Approval>;
  onDecide: (id: string, decision: "approve" | "reject") => Promise<void>;
  onConnect: (action: ConnectAction) => Promise<void>;
}) {
  const { t } = useUi();
  const { connects, approvalIds } = messageActions(metadata);
  const tools = metadata.tools ?? [];
  if (!tools.length && !connects.length && !approvalIds.length) return null;
  return (
    <div className="ml-1 max-w-[85%] space-y-2">
      {tools.length > 0 && <ToolChips tools={tools} />}
      {connects.map((c) => (
        <div key={c.connector} className="card flex flex-wrap items-center justify-between gap-3 p-3">
          <div className="min-w-0 text-sm">
            <p>{t("chat.connect.needs", { name: c.name })}</p>
            {c.reason && <p className="text-xs text-slate-500">{c.reason}</p>}
            {!c.tools_available && <p className="text-xs text-amber-700 dark:text-amber-400">{t("chat.connect.noTools", { name: c.name })}</p>}
          </div>
          <button type="button" className="btn-primary shrink-0 px-3 py-1.5" onClick={() => void onConnect(c as ConnectAction)}>
            {t("chat.connect.button", { name: c.name })}
          </button>
        </div>
      ))}
      {approvalIds.map((id) => approvals[id] && <ApprovalCard key={id} approval={approvals[id]} onDecide={onDecide} />)}
    </div>
  );
}

function ApprovalCard({ approval, onDecide }: { approval: Approval; onDecide: (id: string, d: "approve" | "reject") => Promise<void> }) {
  const { t, lang } = useUi();
  const [busy, setBusy] = useState(false);
  const fields = approvalPreview(approval.arguments ?? {});
  const failed = approval.status === "approved" && approval.task_status === "failed";
  const when = approval.expires_at
    ? new Intl.DateTimeFormat(lang === "pt" ? "pt-PT" : "en-US", { timeStyle: "short" }).format(new Date(approval.expires_at))
    : "—";
  async function click(d: "approve" | "reject") {
    setBusy(true);
    try {
      await onDecide(approval.id, d);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="card space-y-2 border-amber-300 p-3 text-sm dark:border-amber-700">
      <p className="font-medium">{t("chat.approval.title")}: {approval.summary}</p>
      {fields.length > 0 && (
        <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 text-xs">
          {fields.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-slate-500">{k}</dt>
              <dd className="whitespace-pre-wrap break-words">{v}</dd>
            </div>
          ))}
        </dl>
      )}
      {approval.status === "pending" ? (
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn-primary px-3 py-1.5" disabled={busy} onClick={() => void click("approve")}>
            {t("chat.approval.approve")}
          </button>
          <button type="button" className="btn-ghost px-3 py-1.5" disabled={busy} onClick={() => void click("reject")}>
            {t("chat.approval.reject")}
          </button>
          <span className="text-xs text-slate-500">{t("chat.approval.pending", { when })}</span>
        </div>
      ) : (
        <p className={`text-xs ${failed ? "text-red-700 dark:text-red-400" : "text-slate-500"}`}>
          {failed ? t("chat.approval.failed") : t(`chat.approval.${approval.status}` as "chat.approval.approved")}
        </p>
      )}
    </div>
  );
}
