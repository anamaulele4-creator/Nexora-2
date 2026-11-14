"""Chat: conversas, mensagens, histórico, isolamento, limites e escolha do fornecedor."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ai_core.keys import encrypt_api_key, last4
from conftest import login, register_and_verify, unique_email

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


@pytest.fixture(autouse=True)
async def _clean_providers(db_conn):
    await db_conn.execute("delete from ai_providers")
    yield


async def set_provider(app, db_conn, provider: str, model: str, enabled: bool = True) -> None:
    key = f"fake-{provider}-test-key-0001"
    blob = encrypt_api_key(app.state.cipher, provider, key)
    await db_conn.execute(
        """insert into ai_providers (provider, api_key_ciphertext, api_key_nonce, api_key_key_id, api_key_last4, model, enabled)
           values ($1, $2, $3, $4, $5, $6, $7)""",
        provider, blob.ciphertext, blob.nonce, blob.key_id, last4(key), model, enabled)


def gemini_ok(text: str = "Resposta da IA") -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}],
                                     "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4}})


async def new_user(app, make_client):
    c = await make_client()
    email = unique_email("chat")
    await register_and_verify(c, app, email)
    await login(c, email)
    return c


async def test_chat_requires_login(make_client):
    c = await make_client()
    assert (await c.get("/api/chat/conversations")).status_code == 401
    assert (await c.post("/api/chat/conversations", json={})).status_code == 401


async def test_no_active_provider_gives_clear_error(app, make_client, db_conn):
    c = await new_user(app, make_client)
    conv = (await c.post("/api/chat/conversations", json={})).json()
    r = await c.post(f"/api/chat/conversations/{conv['id']}/messages", json={"content": "Olá"})
    assert r.status_code == 503
    assert r.json()["error"] == {**r.json()["error"], "code": "ai_unavailable", "message": "Nenhum fornecedor de IA ativo"}
    # desativado também conta como inexistente
    await set_provider(app, db_conn, "gemini", "gemini-test", enabled=False)
    r = await c.post(f"/api/chat/conversations/{conv['id']}/messages", json={"content": "Olá"})
    assert r.status_code == 503
    msgs = (await c.get(f"/api/chat/conversations/{conv['id']}/messages")).json()["items"]
    assert msgs == []  # nada gravado quando a IA não responde


@respx.mock
async def test_full_chat_flow_with_history(app, make_client, db_conn):
    await set_provider(app, db_conn, "gemini", "gemini-test")
    route = respx.post(GEMINI_URL).mock(side_effect=[gemini_ok("Olá! Como posso ajudar?"), gemini_ok("Claro.")])
    c = await new_user(app, make_client)

    r = await c.post("/api/chat/conversations", json={})
    assert r.status_code == 201
    conv_id = r.json()["id"]
    assert r.json()["title"] is None

    r = await c.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "Olá, quem és tu?"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["assistant_message"]["content"] == "Olá! Como posso ajudar?"
    assert body["assistant_message"]["provider"] == "gemini" and body["assistant_message"]["model"] == "gemini-test"
    assert body["user_message"]["content"] == "Olá, quem és tu?"
    assert body["conversation"]["title"] == "Olá, quem és tu?"

    r = await c.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "Explica melhor"})
    assert r.status_code == 201
    sent = json.loads(route.calls.last.request.content)
    assert [x["role"] for x in sent["contents"]] == ["user", "model", "user"]
    assert sent["contents"][-1]["parts"][0]["text"] == "Explica melhor"
    assert "CONNECTOR AI" in sent["systemInstruction"]["parts"][0]["text"]

    msgs = (await c.get(f"/api/chat/conversations/{conv_id}/messages")).json()["items"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    convs = (await c.get("/api/chat/conversations")).json()["items"]
    assert [x["id"] for x in convs] == [conv_id] and convs[0]["last_message_at"]

    # tokens contam no uso do mês (Dashboard)
    summary = (await c.get("/api/dashboard/summary")).json()
    assert summary["usage_this_month"]["llm_input_tokens"] == 20
    assert summary["usage_this_month"]["llm_output_tokens"] == 8


@respx.mock
async def test_history_is_limited(app, make_client, db_conn):
    await set_provider(app, db_conn, "gemini", "gemini-test")
    route = respx.post(GEMINI_URL).mock(return_value=gemini_ok())
    app.state.settings.chat_history_messages = 4
    c = await new_user(app, make_client)
    conv_id = (await c.post("/api/chat/conversations", json={"title": "Limite"})).json()["id"]
    for i in range(4):
        assert (await c.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": f"m{i}"})).status_code == 201
    sent = json.loads(route.calls.last.request.content)["contents"]
    assert len(sent) == 5  # 4 do histórico + a nova
    assert sent[0]["parts"][0]["text"] == "m1" and sent[-1]["parts"][0]["text"] == "m3"


@respx.mock
async def test_conversations_are_isolated_between_users(app, make_client, db_conn):
    await set_provider(app, db_conn, "gemini", "gemini-test")
    respx.post(GEMINI_URL).mock(return_value=gemini_ok())
    alice = await new_user(app, make_client)
    bob = await new_user(app, make_client)
    conv_id = (await alice.post("/api/chat/conversations", json={"title": "Privada"})).json()["id"]
    await alice.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "segredo"})

    assert (await bob.get("/api/chat/conversations")).json()["items"] == []
    assert (await bob.get(f"/api/chat/conversations/{conv_id}/messages")).status_code == 404
    r = await bob.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "x"})
    assert r.status_code == 404
    assert (await bob.delete(f"/api/chat/conversations/{conv_id}")).status_code == 404
    assert len((await alice.get(f"/api/chat/conversations/{conv_id}/messages")).json()["items"]) == 2


@respx.mock
async def test_delete_conversation_removes_messages(app, make_client, db_conn):
    await set_provider(app, db_conn, "gemini", "gemini-test")
    respx.post(GEMINI_URL).mock(return_value=gemini_ok())
    c = await new_user(app, make_client)
    conv_id = (await c.post("/api/chat/conversations", json={})).json()["id"]
    await c.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "olá"})
    assert (await c.delete(f"/api/chat/conversations/{conv_id}")).status_code == 204
    assert await db_conn.fetchval("select count(*) from messages where conversation_id = $1::uuid", conv_id) == 0
    assert (await c.get(f"/api/chat/conversations/{conv_id}/messages")).status_code == 404
    assert await db_conn.fetchval(
        "select count(*) from audit_logs where event_type = 'chat.conversation_deleted' and details->>'conversation_id' = $1",
        conv_id) == 1


@respx.mock
async def test_chat_rate_limit_per_user(app, make_client, db_conn):
    await set_provider(app, db_conn, "gemini", "gemini-test")
    respx.post(GEMINI_URL).mock(return_value=gemini_ok())
    app.state.settings.chat_rate_limit_per_hour = 2
    c = await new_user(app, make_client)
    conv_id = (await c.post("/api/chat/conversations", json={})).json()["id"]
    for _ in range(2):
        assert (await c.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "a"})).status_code == 201
    r = await c.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "a"})
    assert r.status_code == 429 and "Retry-After" in r.headers
    # outro utilizador não é afetado
    other = await new_user(app, make_client)
    conv2 = (await other.post("/api/chat/conversations", json={})).json()["id"]
    assert (await other.post(f"/api/chat/conversations/{conv2}/messages", json={"content": "a"})).status_code == 201


@respx.mock
async def test_router_falls_back_to_next_provider(app, make_client, db_conn):
    await set_provider(app, db_conn, "gemini", "gemini-test")
    await set_provider(app, db_conn, "openai", "gpt-4o-mini")
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(429, json={"error": {"message": "quota"}}))
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json={
        "model": "gpt-4o-mini", "choices": [{"message": {"content": "Resposta OpenAI"}}]}))
    c = await new_user(app, make_client)
    conv_id = (await c.post("/api/chat/conversations", json={})).json()["id"]
    r = await c.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "olá"})
    assert r.status_code == 201, r.text
    assert r.json()["assistant_message"]["provider"] == "openai"


@respx.mock
async def test_router_prefers_gemini_and_respects_order(app, db_conn):
    from ai_core.providers import ChatMessage

    await set_provider(app, db_conn, "openai", "gpt-4o-mini")
    await set_provider(app, db_conn, "gemini", "gemini-test")
    g = respx.post(GEMINI_URL).mock(return_value=gemini_ok("G"))
    o = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": "O"}}]}))
    router = app.state.ai_router
    assert (await router.generate([ChatMessage("user", "x")])).provider == "gemini"
    assert g.call_count == 1 and o.call_count == 0
    app.state.settings.ai_provider_order = ["openai", "gemini"]
    try:
        assert (await router.generate([ChatMessage("user", "x")])).provider == "openai"
    finally:
        app.state.settings.ai_provider_order = ["gemini", "openai"]


@respx.mock
async def test_all_providers_failing_returns_502_and_stores_nothing(app, make_client, db_conn):
    await set_provider(app, db_conn, "gemini", "gemini-test")
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(500, json={"error": {"message": "boom"}}))
    c = await new_user(app, make_client)
    conv_id = (await c.post("/api/chat/conversations", json={})).json()["id"]
    r = await c.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "olá"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "ai_provider_error"
    assert "boom" in r.json()["error"]["message"]
    assert (await c.get(f"/api/chat/conversations/{conv_id}/messages")).json()["items"] == []


async def test_message_validation(app, make_client):
    c = await new_user(app, make_client)
    conv_id = (await c.post("/api/chat/conversations", json={})).json()["id"]
    assert (await c.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "   "})).status_code == 422
    r = await c.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "x" * 9000})
    assert r.status_code == 422 and r.json()["error"]["code"] == "message_too_long"
    assert (await c.get("/api/chat/conversations/not-a-uuid/messages")).status_code == 422
