import assert from "node:assert/strict";
import { test } from "node:test";

import {
  callbackFeedback,
  connectionState,
  describeUserAgent,
  isTrustedAuthorizeUrl,
  normalizeShopDomain,
} from "../lib/connectors.ts";
import { errorKey } from "../lib/errors.ts";
import { dictionaries, translate, type MessageKey } from "../lib/i18n.ts";

test("OAuth callback feedback: success, known errors and unknown errors", () => {
  assert.deepEqual(callbackFeedback("?connected=github"), {
    tone: "success", key: "conn.callback.connected", connector: "github",
  });
  assert.deepEqual(callbackFeedback("?error=access_denied&connector=gmail"), {
    tone: "error", key: "conn.callback.access_denied", connector: "gmail",
  });
  assert.deepEqual(callbackFeedback("?error=oauth_state_invalid"), {
    tone: "error", key: "conn.callback.oauth_state_invalid", connector: null,
  });
  // códigos desconhecidos e valores estranhos não passam para a interface
  assert.deepEqual(callbackFeedback("?error=<script>&connector=../x"), {
    tone: "error", key: "conn.callback.generic", connector: null,
  });
  assert.equal(callbackFeedback(""), null);
  assert.equal(callbackFeedback("?connected=Bad Key!"), null);
});

test("every callback message key exists in both languages", () => {
  for (const code of ["connected", "access_denied", "oauth_state_invalid", "oauth_exchange_failed",
    "oauth_provider_error", "oauth_not_configured", "encryption_not_configured", "connector_unreachable",
    "token_invalid", "connector_error", "unknown_provider", "generic"]) {
    const key = `conn.callback.${code}` as MessageKey;
    assert.ok(dictionaries.en[key], `en: ${key}`);
    assert.ok(dictionaries.pt[key], `pt: ${key}`);
  }
  assert.equal(translate("pt", "conn.callback.connected", { name: "GitHub" }), "GitHub ligado.");
});

test("only provider authorize URLs are followed", () => {
  assert.ok(isTrustedAuthorizeUrl("https://github.com/login/oauth/authorize?client_id=x"));
  assert.ok(isTrustedAuthorizeUrl("https://accounts.google.com/o/oauth2/v2/auth?x=1"));
  assert.ok(!isTrustedAuthorizeUrl("http://github.com/login/oauth/authorize"));
  assert.ok(!isTrustedAuthorizeUrl("https://github.com.evil.com/login"));
  assert.ok(!isTrustedAuthorizeUrl("javascript:alert(1)"));
  assert.ok(!isTrustedAuthorizeUrl("not a url"));
});

test("shop domain normalization matches the backend", () => {
  assert.equal(normalizeShopDomain("Loja-Teste"), "loja-teste.myshopify.com");
  assert.equal(normalizeShopDomain(" https://loja-teste.myshopify.com/admin/products "), "loja-teste.myshopify.com");
  assert.equal(normalizeShopDomain("loja.com"), null);
  assert.equal(normalizeShopDomain("-x.myshopify.com"), null);
  assert.equal(normalizeShopDomain(""), null);
});

test("user agent description for the sessions list", () => {
  const chromeAndroid = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36";
  const safariIos = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";
  const edgeWin = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 Edg/130.0";
  assert.equal(describeUserAgent(chromeAndroid), "Chrome · Android");
  assert.equal(describeUserAgent(safariIos), "Safari · iOS");
  assert.equal(describeUserAgent(edgeWin), "Edge · Windows");
  assert.equal(describeUserAgent("python-httpx/0.28.1"), "API");
  assert.equal(describeUserAgent(null), "—");
});

test("connection state falls back to none", () => {
  assert.equal(connectionState({ status: "connected" }), "connected");
  assert.equal(connectionState({ status: "expired" }), "expired");
  assert.equal(connectionState({ status: "weird" }), "none");
  assert.equal(connectionState(null), "none");
});

test("new backend error codes have translations", () => {
  for (const code of ["invalid_current_password", "same_password", "paid_provider_disabled", "not_supported",
    "cannot_revoke_current", "oauth_not_configured", "invalid_shop_domain", "invalid_shopify_token",
    "connector_not_found", "not_connected"]) {
    const key = errorKey(code);
    assert.equal(key, `error.${code}`);
    assert.ok(dictionaries.pt[key] && dictionaries.en[key], key);
  }
});
