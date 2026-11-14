import assert from "node:assert/strict";
import { test } from "node:test";

import { approvalPreview, chatReturnFeedback, connectPath, mergeToolActivity, messageActions } from "../lib/chatAgent.ts";
import { translate } from "../lib/i18n.ts";

const CONV = "0f8fad5b-d9cb-469f-a165-70867728950e";

test("connectPath returns to the chat and validates input", () => {
  assert.equal(connectPath("github", CONV), `/api/connectors/github/connect?return_to=chat&conversation=${CONV}`);
  assert.equal(connectPath("google_drive", null), "/api/connectors/google_drive/connect?return_to=chat");
  assert.equal(connectPath("github", "not-a-uuid"), "/api/connectors/github/connect?return_to=chat");
  assert.throws(() => connectPath("../x", null));
});

test("chatReturnFeedback reads the OAuth return", () => {
  assert.deepEqual(chatReturnFeedback(`?connected=github&conversation=${CONV}`), { connected: "github", conversation: CONV });
  assert.deepEqual(chatReturnFeedback("?error=access_denied&connector=github"),
    { error: "access_denied", connector: "github", conversation: null });
  assert.equal(chatReturnFeedback("?connected=<script>"), null);
  assert.equal(chatReturnFeedback(""), null);
});

test("messageActions splits connect buttons and approvals", () => {
  const r = messageActions({ actions: [
    { type: "connect", connector: "github", name: "GitHub", method: "oauth", tools_available: true },
    { type: "approval", id: "a1" },
    { type: "connect", connector: "bad key", name: "x", method: "oauth", tools_available: false },
  ] });
  assert.deepEqual(r.connects.map((c) => c.connector), ["github"]);
  assert.deepEqual(r.approvalIds, ["a1"]);
  assert.deepEqual(messageActions(undefined), { connects: [], approvalIds: [] });
});

test("approvalPreview shows the important fields, trimmed", () => {
  const p = approvalPreview({ owner: "ana", repo: "app", title: "Bug", body: "x".repeat(700), labels: [] }, 600);
  assert.deepEqual(p.map(([k]) => k), ["owner", "repo", "title", "body"]);
  assert.equal(p[3][1].length, 601);
});

test("mergeToolActivity collapses repeated tools", () => {
  const a = mergeToolActivity([], { name: "github__list_repos", status: "ok" });
  const b = mergeToolActivity(a, { name: "github__list_repos", status: "error" });
  assert.deepEqual(b, [{ name: "github__list_repos", status: "error" }]);
  assert.equal(mergeToolActivity(b, { name: "request_connection", status: "ok" }).length, 2);
});

test("connect button text in both languages", () => {
  assert.equal(translate("pt", "chat.connect.button", { name: "GitHub" }), "Ligar GitHub");
  assert.equal(translate("en", "chat.connect.button", { name: "GitHub" }), "Connect GitHub");
});
