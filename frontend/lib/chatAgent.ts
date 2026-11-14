// Ajudas puras (sem React) para as ações do agente no chat. Testadas em tests/chatAgent.test.ts.

export type ConnectActionLike = { type: "connect"; connector: string; name: string; method: string; tools_available: boolean; reason?: string };
export type ApprovalActionLike = { type: "approval"; id: string };
type MetadataLike = { actions?: (ConnectActionLike | ApprovalActionLike)[] } | undefined | null;

const KEY_RE = /^[a-z0-9_]{2,64}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Pedido que inicia a ligação OAuth a partir do chat (o retorno volta a /chat). */
export function connectPath(connector: string, conversationId: string | null): string {
  if (!KEY_RE.test(connector)) throw new Error("invalid connector");
  const params = new URLSearchParams({ return_to: "chat" });
  if (conversationId && UUID_RE.test(conversationId)) params.set("conversation", conversationId);
  return `/api/connectors/${connector}/connect?${params.toString()}`;
}

/** /chat?connected=github&conversation=… depois do OAuth (ou ?error=…). */
export function chatReturnFeedback(search: string):
  | { connected: string; conversation: string | null }
  | { error: string; connector: string | null; conversation: string | null }
  | null {
  const p = new URLSearchParams(search);
  const conv = p.get("conversation");
  const conversation = conv && UUID_RE.test(conv) ? conv : null;
  const connected = p.get("connected");
  if (connected && KEY_RE.test(connected)) return { connected, conversation };
  const error = p.get("error");
  if (error && /^[a-z_]{2,64}$/.test(error)) {
    const c = p.get("connector");
    return { error, connector: c && KEY_RE.test(c) ? c : null, conversation };
  }
  return null;
}

/** Botões "Ligar …" e ids de aprovações guardados numa mensagem. */
export function messageActions(metadata: MetadataLike): { connects: ConnectActionLike[]; approvalIds: string[] } {
  const actions = metadata?.actions ?? [];
  const connects = actions.filter((a): a is ConnectActionLike => a.type === "connect" && KEY_RE.test(a.connector));
  const approvalIds = actions.filter((a): a is ApprovalActionLike => a.type === "approval").map((a) => a.id);
  return { connects, approvalIds };
}

const PREVIEW_FIELDS: [string, string][] = [
  ["owner", "owner"], ["repo", "repo"], ["number", "#"], ["title", "title"], ["body", "body"], ["labels", "labels"],
];

/** Campos a mostrar no cartão de confirmação (texto cortado). */
export function approvalPreview(args: Record<string, unknown>, max = 600): [string, string][] {
  const out: [string, string][] = [];
  for (const [key, label] of PREVIEW_FIELDS) {
    const v = args[key];
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0)) continue;
    let text = Array.isArray(v) ? v.join(", ") : String(v);
    if (text.length > max) text = text.slice(0, max) + "…";
    out.push([label, text]);
  }
  return out;
}

/** Junta a atividade de ferramentas (o mesmo nome seguido conta uma vez, com o último estado). */
export function mergeToolActivity<T extends { name: string; status: string }>(list: T[], ev: T): T[] {
  const last = list[list.length - 1];
  if (last && last.name === ev.name) return [...list.slice(0, -1), ev];
  return [...list, ev];
}
