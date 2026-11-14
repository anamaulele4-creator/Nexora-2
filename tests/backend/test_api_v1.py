"""API pública v1: tokens (hash, revogação, âmbitos), modelos, chat/completions e registo de uso."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from local_helpers import OLLAMA, admin_client, native_reply, native_stream, parse_sse, set_engine, tags
from security.api_keys import hash_token


@pytest.fixture(autouse=True)
async def _clean(db_conn):
    await db_conn.execute("delete from ai_providers")
    await db_conn.execute("delete from local_ai_engine")
    await db_conn.execute("delete from api_usage_logs")
    await db_conn.execute("delete from api_keys")
    yield
    await db_conn.execute("delete from local_ai_engine")


async def new_token(app, make_client, **body) -> tuple[str, dict, object]:
    admin = await admin_client(app, make_client)
    r = await admin.post("/api/admin/api-keys", json={"name": "Integração teste", **body})
    assert r.status_code == 201, r.text
    data = r.json()
    return data["token"], data, admin


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_health_is_public_and_minimal(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["local_engine"] == "not_configured"


async def test_requires_valid_token(client):
    for path in ("/api/v1/models", "/api/v1/models/status"):
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=bearer("cai_naoexiste_000000000000000"))).status_code == 401
        assert (await client.get(path, headers=bearer("um-jwt-qualquer"))).status_code == 401
    r = await client.post("/api/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


async def test_token_shown_once_and_stored_hashed(app, make_client, db_conn):
    token, data, admin = await new_token(app, make_client)
    assert token.startswith("cai_") and len(token) > 40
    assert data["key_prefix"] == token[:12]
    row = await db_conn.fetchrow("select key_hash from api_keys where id = $1", data["id"])
    assert row["key_hash"] == hash_token(token)
    assert token not in json.dumps([dict(r) for r in await db_conn.fetch("select name, key_prefix, key_hash from api_keys")])
    listing = await admin.get("/api/admin/api-keys")
    assert token not in listing.text and "token" not in listing.json()["items"][0]


async def test_revoked_and_expired_tokens_fail(app, make_client, db_conn, client):
    token, data, admin = await new_token(app, make_client)
    assert (await client.get("/api/v1/models/status", headers=bearer(token))).status_code == 200
    r = await admin.delete(f"/api/admin/api-keys/{data['id']}")
    assert r.status_code == 200 and r.json()["revoked_at"]
    assert (await client.get("/api/v1/models/status", headers=bearer(token))).status_code == 401

    token2, data2, _ = await new_token(app, make_client)
    await db_conn.execute("update api_keys set expires_at = now() - interval '1 minute' where id = $1", data2["id"])
    r = await client.get("/api/v1/models/status", headers=bearer(token2))
    assert r.status_code == 401 and "expirado" in r.json()["error"]["message"]


async def test_scopes_are_enforced(app, make_client, client):
    token, _, _ = await new_token(app, make_client, scopes=["models"])
    r = await client.post("/api/v1/chat/completions", headers=bearer(token),
                          json={"messages": [{"role": "user", "content": "Olá"}]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"
    r = await (await admin_client(app, make_client)).post("/api/admin/api-keys", json={"name": "x", "scopes": ["admin"]})
    assert r.status_code == 422


async def test_chat_without_engine_returns_configuration_warning(app, make_client, client, db_conn):
    token, data, _ = await new_token(app, make_client)
    r = await client.post("/api/v1/chat/completions", headers=bearer(token),
                          json={"model": "qwen3:1.7b", "messages": [{"role": "user", "content": "Olá"}]})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "local_engine_not_configured"
    assert r.json()["error"]["message"] == "Motor local não configurado"
    log = await db_conn.fetchrow("select status_code from api_usage_logs where api_key_id = $1", data["id"])
    assert log["status_code"] == 503


@respx.mock
async def test_models_and_status(app, make_client, client, db_conn):
    await set_engine(app, db_conn)
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=tags("qwen3:1.7b"))
    respx.get(f"{OLLAMA}/api/version").mock(return_value=httpx.Response(200, json={"version": "0.40.16"}))
    respx.get(f"{OLLAMA}/api/ps").mock(return_value=httpx.Response(200, json={"models": [{"name": "qwen3:1.7b"}]}))
    token, _, _ = await new_token(app, make_client)
    r = await client.get("/api/v1/models", headers=bearer(token))
    assert r.json()["object"] == "list"
    assert [m["id"] for m in r.json()["data"]] == ["auto", "qwen3:1.7b"]
    st = (await client.get("/api/v1/models/status", headers=bearer(token))).json()["local_engine"]
    assert st["reachable"] and st["running"] == ["qwen3:1.7b"] and st["default_model"] == "qwen3:1.7b"
    assert (await client.get("/api/v1/health")).json()["local_engine"] == "ok"


@respx.mock
async def test_chat_completions_openai_format(app, make_client, client, db_conn):
    await set_engine(app, db_conn)
    route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=native_reply("Olá, sou local."))
    token, data, admin = await new_token(app, make_client)
    r = await client.post("/api/v1/chat/completions", headers=bearer(token), json={
        "model": "qwen3:1.7b", "temperature": 0.1, "max_tokens": 64, "user": "ignorado",
        "messages": [{"role": "system", "content": "És breve."}, {"role": "user", "content": "Olá"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion" and body["id"].startswith("chatcmpl-")
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "Olá, sou local."}
    assert body["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
    sent = json.loads(route.calls[0].request.content)
    assert sent["options"]["temperature"] == 0.1 and sent["options"]["num_predict"] == 64
    assert sent["messages"][0] == {"role": "system", "content": "És breve."}

    log = await db_conn.fetchrow("select * from api_usage_logs where api_key_id = $1 and endpoint = 'chat/completions'",
                                 data["id"])
    assert log["status_code"] == 200 and log["provider"] == "ollama" and log["output_tokens"] == 5
    listing = (await admin.get("/api/admin/api-keys")).json()["items"][0]
    assert listing["requests_30d"] == 1 and listing["tokens_30d"] == 17 and listing["last_used_at"]


@respx.mock
async def test_chat_completions_streaming(app, make_client, client, db_conn):
    await set_engine(app, db_conn)
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=native_stream(["Um", " dois", " três"]))
    token, data, _ = await new_token(app, make_client)
    r = await client.post("/api/v1/chat/completions", headers=bearer(token), json={
        "model": "auto", "stream": True, "messages": [{"role": "user", "content": "Conta"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert events[-1] == (None, "[DONE]")
    chunks = [json.loads(d) for _, d in events[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "Um dois três"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant" and chunks[0]["model"] == "qwen3:1.7b"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop" and chunks[-1]["usage"]["completion_tokens"] == 3
    import asyncio
    await asyncio.sleep(0.05)  # o registo de uso do stream é gravado numa tarefa à parte
    log = await db_conn.fetchrow("select streamed, status_code from api_usage_logs where api_key_id = $1", data["id"])
    assert log["streamed"] is True and log["status_code"] == 200


@respx.mock
async def test_missing_model_is_404_and_bad_messages_rejected(app, make_client, client, db_conn):
    await set_engine(app, db_conn)
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(404, json={"error": "model 'x:1b' not found"}))
    token, _, _ = await new_token(app, make_client)
    r = await client.post("/api/v1/chat/completions", headers=bearer(token),
                          json={"model": "x:1b", "messages": [{"role": "user", "content": "Olá"}]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
    r = await client.post("/api/v1/chat/completions", headers=bearer(token),
                          json={"messages": [{"role": "assistant", "content": "Olá"}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_messages"


async def test_rate_limit_per_key(app, make_client, client):
    token, _, _ = await new_token(app, make_client)
    app.state.settings.api_v1_rate_limit_per_minute = 2
    try:
        codes = [(await client.get("/api/v1/models/status", headers=bearer(token))).status_code for _ in range(3)]
    finally:
        app.state.settings.api_v1_rate_limit_per_minute = 60
    assert codes == [200, 200, 429]
