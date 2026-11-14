import assert from "node:assert/strict";
import { test } from "node:test";

import { errorKey, passwordProblems } from "../lib/errors.ts";
import { dictionaries, translate } from "../lib/i18n.ts";

test("every English key has a non-empty Portuguese translation", () => {
  for (const key of Object.keys(dictionaries.en)) {
    const value = (dictionaries.pt as Record<string, string>)[key];
    assert.ok(value && value.trim().length > 0, `missing pt translation: ${key}`);
  }
});

test("translate interpolates variables", () => {
  assert.equal(translate("en", "dash.welcome", { name: "Ana" }), "Welcome, Ana");
  assert.equal(translate("pt", "dash.connectors", { available: 0, planned: 6 }), "Conectores: 0 disponíveis · 6 planeados");
});

test("password rules match the backend", () => {
  assert.deepEqual(passwordProblems("Correct-Horse-42"), []);
  assert.ok(passwordProblems("short").includes("min_length"));
  assert.ok(passwordProblems("alllowercase123").includes("mixed_case"));
  assert.ok(passwordProblems("NoDigitsHereAtAll").includes("digit"));
});

test("unknown error codes fall back to a generic message", () => {
  assert.equal(errorKey("email_taken"), "error.email_taken");
  assert.equal(errorKey("something_else"), "error.generic");
  assert.equal(errorKey("network"), "error.network");
  assert.equal(errorKey("ai_unavailable"), "error.ai_unavailable");
  assert.equal(errorKey("forbidden"), "error.forbidden");
});
