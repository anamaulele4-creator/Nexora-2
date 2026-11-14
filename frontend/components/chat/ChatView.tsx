"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { AppNav, useMe } from "@/components/AppNav";
import { Alert } from "@/components/AuthShell";
import { Header } from "@/components/Header";
import { useUi } from "@/components/Providers";
import { api, del, post, type ChatMessage, type Conversation, type SendMessageResult } from "@/lib/api";
import { errorText } from "@/lib/errors";

type Pending = { content: string };

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
      const res = await api<{ items: ChatMessage[] }>(`/api/chat/conversations/${id}/messages`);
      setMessages(res.items);
    } catch (err) {
      setError(errorText(err, t));
      setMessages([]);
    } finally {
      setLoadingThread(false);
    }
  }, [t]);

  useEffect(() => {
    if (!me) return;
    void loadConversations().then((items) => {
      if (items.length > 0) void openConversation(items[0].id);
    });
  }, [me, loadConversations, openConversation]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pending]);

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
    setPending({ content });
    setDraft("");
    let convId = activeId;
    try {
      if (!convId) {
        const conv = await post<Conversation>("/api/chat/conversations", {});
        convId = conv.id;
        setActiveId(conv.id);
        setConversations((prev) => [conv, ...(prev ?? [])]);
      }
      const res = await post<SendMessageResult>(`/api/chat/conversations/${convId}/messages`, { content });
      setMessages((prev) => [...prev, res.user_message, res.assistant_message]);
      setConversations((prev) => {
        const rest = (prev ?? []).filter((c) => c.id !== res.conversation.id);
        return [res.conversation, ...rest];
      });
    } catch (err) {
      setError(errorText(err, t));
      setDraft(content); // devolve o texto para reenviar
    } finally {
      setPending(null);
      inputRef.current?.focus();
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
                  <MessageBubble key={m.id} role={m.role} content={m.content} label={m.role === "user" ? t("chat.you") : t("chat.ai")} />
                ))}
                {pending && (
                  <>
                    <MessageBubble role="user" content={pending.content} label={t("chat.you")} />
                    <li className="flex justify-start">
                      <div className="rounded-2xl bg-slate-100 px-4 py-2 text-sm text-slate-500 dark:bg-slate-800">
                        <span className="inline-flex items-center gap-2">
                          <span className="h-2 w-2 animate-pulse rounded-full bg-brand-500" />
                          {t("chat.thinking")}
                        </span>
                      </div>
                    </li>
                  </>
                )}
              </ol>
            )}
            <div ref={bottomRef} />
          </div>

          <form onSubmit={send} className="border-t border-slate-200 p-3 dark:border-slate-800 sm:px-6">
            <div className="mx-auto max-w-3xl">
              {error && <div className="mb-2"><Alert tone="error">{error}</Alert></div>}
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
                <button type="submit" className="btn-primary h-11 shrink-0" disabled={!!pending || !draft.trim()}>
                  {pending ? "…" : t("chat.send")}
                </button>
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
