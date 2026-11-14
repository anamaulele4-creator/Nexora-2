import assert from "node:assert/strict";
import { test } from "node:test";

import { parseSseChunk } from "../lib/sse.ts";
import { errorText } from "../lib/errors.ts";
import { translate } from "../lib/i18n.ts";

test("parseSseChunk splits complete events and keeps the remainder", () => {
  const { events, rest } = parseSseChunk(': ping\n\nevent: delta\ndata: {"text":"Ol"}\n\nevent: delta\ndata: {"te');
  assert.deepEqual(events, [{ event: "delta", data: '{"text":"Ol"}' }]);
  assert.equal(rest, 'event: delta\ndata: {"te');
  const next = parseSseChunk(rest + 'xt":"á"}\n\ndata: [DONE]\n\n');
  assert.deepEqual(next.events, [{ event: "delta", data: '{"text":"á"}' }, { event: null, data: "[DONE]" }]);
  assert.equal(next.rest, "");
});

test("local engine errors show the server message; not-configured is translated", () => {
  const t = (k: Parameters<typeof translate>[1]) => translate("pt", k);
  assert.equal(errorText({ code: "local_engine_unreachable", message: "Motor local inacessível (ConnectError)" }, t),
    "Motor local inacessível (ConnectError)");
  assert.equal(errorText({ code: "local_engine_not_configured", message: "x" }, t), "Motor DÁRIO não configurado.");
  assert.equal(translate("pt", "admin.local.title"), "Motor DÁRIO / Modelos");
});
