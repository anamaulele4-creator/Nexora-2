"""ai_core: cifra/máscara de chaves, ordem dos fornecedores e formato dos pedidos HTTP."""

from __future__ import annotations

import base64
import json
import os

import httpx
import pytest
import respx

from ai_core.keys import decrypt_api_key, encrypt_api_key, last4, mask
from ai_core.providers import ChatMessage, GeminiClient, OpenAIClient, ProviderError
from ai_core.router import order_providers
from security.crypto import CredentialCipher, DecryptionError

FAKE_KEY = "test-fake-key-0000-ABCD"  # não é uma chave real


def _cipher():
    return CredentialCipher.from_base64(base64.b64encode(os.urandom(32)).decode(), "k1")


def test_api_key_encryption_roundtrip_and_binding():
    c = _cipher()
    blob = encrypt_api_key(c, "gemini", FAKE_KEY)
    assert FAKE_KEY.encode() not in blob.ciphertext
    assert decrypt_api_key(c, "gemini", key_id=blob.key_id, nonce=blob.nonce, ciphertext=blob.ciphertext) == FAKE_KEY
    # cifrado da linha do gemini não decifra como openai
    with pytest.raises(DecryptionError):
        decrypt_api_key(c, "openai", key_id=blob.key_id, nonce=blob.nonce, ciphertext=blob.ciphertext)


def test_masking_shows_only_last_four():
    assert last4(FAKE_KEY) == "ABCD"
    assert mask("ABCD") == "••••ABCD"
    assert last4("short") == ""  # chaves curtas não revelam nada
    assert mask(None) is None


def test_order_providers_gemini_first_and_skips_unusable():
    rows = [
        {"provider": "openai", "enabled": True, "api_key_ciphertext": b"x"},
        {"provider": "gemini", "enabled": True, "api_key_ciphertext": b"y"},
    ]
    assert [r["provider"] for r in order_providers(rows, ["gemini", "openai"])] == ["gemini", "openai"]
    assert [r["provider"] for r in order_providers(rows, ["openai", "gemini"])] == ["openai", "gemini"]
    rows[1]["enabled"] = False
    assert [r["provider"] for r in order_providers(rows, ["gemini", "openai"])] == ["openai"]
    rows[0]["api_key_ciphertext"] = None
    assert order_providers(rows, ["gemini", "openai"]) == []


def test_provider_order_setting_validation():
    from conftest import make_settings

    s = make_settings(ai_provider_order="openai")
    assert s.ai_provider_order == ["openai", "gemini", "groq", "openrouter"]
    with pytest.raises(Exception):
        make_settings(ai_provider_order="gemini,claude")
    s = make_settings(admin_emails=" A@Example.com, b@example.com ")
    assert s.admin_emails == ["a@example.com", "b@example.com"]
    assert s.is_admin_email("B@example.com") and not s.is_admin_email("c@example.com")


def test_default_admin_email():
    from conftest import make_settings

    assert make_settings().admin_emails == ["garenadario3@gmail.com"]


@respx.mock
async def test_gemini_request_shape_and_parsing():
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
    ).mock(return_value=httpx.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": [{"text": "Olá!"}]}}],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3},
    }))
    client = GeminiClient(FAKE_KEY, "gemini-flash-latest")
    reply = await client.generate(
        [ChatMessage("user", "oi"), ChatMessage("assistant", "olá"), ChatMessage("user", "tudo bem?")], system="sys")
    assert reply.text == "Olá!" and reply.provider == "gemini"
    assert (reply.input_tokens, reply.output_tokens) == (12, 3)
    req = route.calls.last.request
    assert req.headers["x-goog-api-key"] == FAKE_KEY
    assert FAKE_KEY not in str(req.url)  # a chave nunca vai no URL
    body = json.loads(req.content)
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"


@respx.mock
async def test_openai_request_shape_and_error():
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(return_value=httpx.Response(200, json={
        "model": "gpt-4o-mini", "choices": [{"message": {"role": "assistant", "content": "Oi"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }))
    reply = await OpenAIClient(FAKE_KEY, "gpt-4o-mini").generate([ChatMessage("user", "oi")], system="sys")
    assert reply.text == "Oi" and reply.provider == "openai"
    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(req.content)
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"][0] == {"role": "system", "content": "sys"}

    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}}))
    with pytest.raises(ProviderError) as exc:
        await OpenAIClient(FAKE_KEY, "gpt-4o-mini").generate([ChatMessage("user", "oi")])
    assert exc.value.status == 401 and "Incorrect API key" in exc.value.message
    assert FAKE_KEY not in exc.value.message
