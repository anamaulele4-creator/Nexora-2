"""Motor local: admin (config cifrada, gestor de modelos), encaminhamento e chat em streaming."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from local_helpers import (FAKE_TOKEN, OLLAMA, admin_client, native_reply, native_stream, parse_sse, set_cloud,
                           set_engine, tags, user_client)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def gemini_ok(text: str = "Resposta da nuvem") -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def openai_ok(text: str = "Resposta paga") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


@pytest.fixture(autouse=True)
async def _clean(db_conn):
    await db_conn.execute("delete from ai_providers")
    await db_conn.execute("delete from local_ai_engine")
    yield
    await db_conn.execute("delete from local_ai_engine")


async def new_conv(c) -> str:
    return (await c.post("/api/chat/conversations", json={})).json()["id"]


# --- admin: acesso e configuração --------------------------------------------------------------------
LOCAL_ROUTES = [
    ("GET", "/api/admin/local-engine", None),
    ("PUT", "/api/admin/local-engine", {"temperature": 0.5}),
    ("GET", "/api/admin/local-engine/status", None),
    ("POST", "/api/admin/local-engine/test", None),
    ("GET", "/api/admin/local-engine/models", None),
    ("GET", "/api/admin/local-engine/catalog", None),
    ("POST", "/api/admin/local-engine/models/pull", {"name": "qwen3:0.6b"}),
    ("DELETE", "/api/admin/local-engine/models/qwen3:0.6b", None),
    ("POST", "/api/admin/local-engine/models/qwen3:0.6b/default", None),
    ("GET", "/api/admin/api-keys", None),
    ("POST", "/api/admin/api-keys", {"name": "x"}),
]


async def test_local_engine_routes_are_admin_only(app, make_client):
    anon = await make_client()
    plain = await user_client(app, make_client)
    for method, path, body in LOCAL_ROUTES:
        assert (await anon.request(method, path, json=body)).status_code == 401, path
        assert (await plain.request(method, path, json=body)).status_code == 403, path


async def test_config_is_encrypted_and_never_returned(app, make_client, db_conn):
    c = await admin_client(app, make_client)
    r = await c.get("/api/admin/local-engine")
    assert r.status_code == 200
    assert r.json()["configured"] is False and r.json()["warning"] == "Motor local não configurado"

    secret_url = "https://ollama-privado.exemplo.net:8443/v1"
    r = await c.put("/api/admin/local-engine", json={"base_url": secret_url, "token": FAKE_TOKEN,
                                                     "temperature": 0.3, "max_tokens": 512, "context_length": 8192})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] and body["token_configured"]
    assert body["base_url_hint"] == "https://ollama-privado.exemplo.net"
    assert body["token_masked"] == "••••" + FAKE_TOKEN[-4:]
    assert body["enabled"] is False and body["context_length"] == 8192
    text = r.text + (await c.get("/api/admin/local-engine")).text
    assert FAKE_TOKEN not in text and "8443" not in text

    row = await db_conn.fetchrow("select * from local_ai_engine")
    assert FAKE_TOKEN.encode() not in bytes(row["token_ciphertext"])
    assert b"ollama-privado" not in bytes(row["base_url_ciphertext"])

    # ativar exige modelo por defeito
    r = await c.put("/api/admin/local-engine", json={"enabled": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "default_model_required"
    r = await c.put("/api/admin/local-engine", json={"enabled": True, "default_model": "qwen3:1.7b"})
    assert r.status_code == 200 and r.json()["enabled"] and r.json()["warning"] is None
    # recurso pago sem recurso à nuvem é inválido
    r = await c.put("/api/admin/local-engine", json={"allow_paid_fallback": True})
    assert r.status_code == 422
    # limpar o token
    r = await c.put("/api/admin/local-engine", json={"clear_token": True})
    assert r.json()["token_configured"] is False
    audit = await db_conn.fetchval(
        "select count(*) from audit_logs where event_type = 'admin.local_engine_updated'")
    assert audit >= 3


async def test_invalid_url_rejected(app, make_client):
    c = await admin_client(app, make_client)
    r = await c.put("/api/admin/local-engine", json={"base_url": "http://user:pw@host:11434"})
    assert r.status_code == 422


@respx.mock
async def test_status_test_and_models(app, make_client, db_conn):
    await set_engine(app, db_conn)
    respx.get(f"{OLLAMA}/api/version").mock(return_value=httpx.Response(200, json={"version": "0.40.16"}))
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=tags("qwen3:1.7b", "gemma3:1b"))
    respx.get(f"{OLLAMA}/api/ps").mock(return_value=httpx.Response(200, json={"models": []}))
    chat = respx.post(f"{OLLAMA}/api/chat").mock(return_value=native_reply("ok"))
    c = await admin_client(app, make_client)

    st = (await c.get("/api/admin/local-engine/status")).json()
    assert st["reachable"] and st["version"] == "0.40.16" and st["warning"] is None

    r = await c.post("/api/admin/local-engine/test")
    assert r.status_code == 200 and r.json()["ok"] and r.json()["reply"] == "ok"
    assert chat.calls[0].request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    row = await db_conn.fetchrow("select last_test_ok from local_ai_engine")
    assert row["last_test_ok"] is True

    models = (await c.get("/api/admin/local-engine/models")).json()
    assert [m["name"] for m in models["items"]] == ["qwen3:1.7b", "gemma3:1b"]
    assert models["items"][0]["is_default"] and models["items"][0]["license"] == "Apache-2.0"

    cat = (await c.get("/api/admin/local-engine/catalog")).json()
    by = {m["name"]: m for m in cat["items"]}
    assert by["qwen3:1.7b"]["installed"] and not by["qwen3:8b"]["installed"]
    assert by["qwen3:8b"]["fits"] is False and by["qwen3:4b"]["fits"] is True  # limite 8 GB

    r = await c.post("/api/admin/local-engine/models/gemma3:1b/default")
    assert r.status_code == 200 and r.json()["default_model"] == "gemma3:1b"
    r = await c.post("/api/admin/local-engine/models/llama3.2:3b/default")
    assert r.status_code == 404


@respx.mock
async def test_unreachable_engine_reported_clearly(app, make_client, db_conn):
    await set_engine(app, db_conn)
    respx.get(f"{OLLAMA}/api/version").mock(side_effect=httpx.ConnectError("recusado"))
    respx.get(f"{OLLAMA}/api/tags").mock(side_effect=httpx.ConnectError("recusado"))
    c = await admin_client(app, make_client)
    st = (await c.get("/api/admin/local-engine/status")).json()
    assert st["reachable"] is False and "inacessível" in st["warning"]
    r = await c.get("/api/admin/local-engine/models")
    assert r.status_code == 503 and r.json()["error"]["code"] == "local_engine_unreachable"
    r = await c.post("/api/admin/local-engine/test")
    assert r.json()["ok"] is False


@respx.mock
async def test_pull_respects_ram_limit_and_streams_progress(app, make_client, db_conn):
    await set_engine(app, db_conn, ram_limit_gb=4)
    pull_body = "\n".join(json.dumps(e) for e in [
        {"status": "pulling manifest"}, {"status": "downloading", "total": 100, "completed": 100},
        {"status": "success"}]) + "\n"
    pull = respx.post(f"{OLLAMA}/api/pull").mock(return_value=httpx.Response(200, content=pull_body.encode()))
    c = await admin_client(app, make_client)

    r = await c.post("/api/admin/local-engine/models/pull", json={"name": "qwen3:8b"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ram_limit_exceeded"
    r = await c.post("/api/admin/local-engine/models/pull", json={"name": "modelo-desconhecido:7b"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ram_unknown"
    assert not pull.called  # nada foi descarregado sem confirmação

    r = await c.post("/api/admin/local-engine/models/pull", json={"name": "qwen3:0.6b"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert events[-1] == ("done", json.dumps({"name": "qwen3:0.6b"}, separators=(",", ":")))
    assert any(e == "progress" for e, _ in events)
    assert json.loads(pull.calls[0].request.content)["model"] == "qwen3:0.6b"

    r = await c.post("/api/admin/local-engine/models/pull", json={"name": "qwen3:8b", "confirm_over_limit": True})
    assert r.status_code == 200 and parse_sse(r.text)[-1][0] == "done"


@respx.mock
async def test_delete_model(app, make_client, db_conn):
    await set_engine(app, db_conn, default_model="qwen3:1.7b")
    respx.delete(f"{OLLAMA}/api/delete").mock(side_effect=[httpx.Response(200),
                                                           httpx.Response(404, json={"error": "model not found"})])
    c = await admin_client(app, make_client)
    r = await c.delete("/api/admin/local-engine/models/qwen3:1.7b")
    assert r.status_code == 409 and r.json()["error"]["code"] == "model_is_default"
    assert (await c.delete("/api/admin/local-engine/models/gemma3:1b")).json() == {"deleted": "gemma3:1b"}
    assert (await c.delete("/api/admin/local-engine/models/gemma3:1b")).status_code == 404


async def test_manager_requires_configuration(app, make_client):
    c = await admin_client(app, make_client)
    r = await c.get("/api/admin/local-engine/models")
    assert r.status_code == 409 and r.json()["error"]["message"] == "Motor local não configurado"
    cat = (await c.get("/api/admin/local-engine/catalog")).json()  # o catálogo funciona sem motor
    assert len(cat["items"]) >= 10 and cat["reachable"] is False


# --- encaminhamento ---------------------------------------------------------------------------------
@respx.mock
async def test_local_engine_has_priority_over_cloud(app, make_client, db_conn):
    await set_engine(app, db_conn)
    await set_cloud(app, db_conn, "gemini", "gemini-test")
    local = respx.post(f"{OLLAMA}/api/chat").mock(return_value=native_reply("Resposta local"))
    cloud = respx.post(GEMINI_URL).mock(return_value=gemini_ok())
    c = await user_client(app, make_client)
    r = await c.post(f"/api/chat/conversations/{await new_conv(c)}/messages", json={"content": "Olá"})
    assert r.status_code == 201, r.text
    msg = r.json()["assistant_message"]
    assert msg["content"] == "Resposta local" and msg["provider"] == "ollama" and msg["model"] == "qwen3:1.7b"
    assert local.called and not cloud.called


@respx.mock
async def test_no_cloud_fallback_unless_admin_allows(app, make_client, db_conn):
    await set_engine(app, db_conn, allow_cloud_fallback=False)
    await set_cloud(app, db_conn, "gemini", "gemini-test")
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("desligado"))
    cloud = respx.post(GEMINI_URL).mock(return_value=gemini_ok())
    c = await user_client(app, make_client)
    conv = await new_conv(c)
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "local_engine_unreachable"
    assert not cloud.called
    assert (await c.get(f"/api/chat/conversations/{conv}/messages")).json()["items"] == []

    # com autorização, passa para a nuvem gratuita
    await db_conn.execute("update local_ai_engine set allow_cloud_fallback = true")
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá"})
    assert r.status_code == 201 and r.json()["assistant_message"]["provider"] == "gemini"


@respx.mock
async def test_paid_provider_never_used_as_fallback_without_permission(app, make_client, db_conn):
    await set_engine(app, db_conn, allow_cloud_fallback=True, allow_paid_fallback=False)
    await set_cloud(app, db_conn, "openai", "gpt-test")
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("desligado"))
    paid = respx.post(OPENAI_URL).mock(return_value=openai_ok())
    c = await user_client(app, make_client)
    conv = await new_conv(c)
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá"})
    assert r.status_code == 503 and not paid.called
    await db_conn.execute("update local_ai_engine set allow_paid_fallback = true")
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá"})
    assert r.status_code == 201 and r.json()["assistant_message"]["provider"] == "openai"


@respx.mock
async def test_without_local_engine_cloud_chain_is_unchanged(app, make_client, db_conn):
    await set_cloud(app, db_conn, "gemini", "gemini-test")
    respx.post(GEMINI_URL).mock(return_value=gemini_ok("Nuvem"))
    c = await user_client(app, make_client)
    r = await c.post(f"/api/chat/conversations/{await new_conv(c)}/messages", json={"content": "Olá"})
    assert r.status_code == 201 and r.json()["assistant_message"]["content"] == "Nuvem"


async def test_explicit_local_choice_without_engine_warns(app, make_client):
    c = await user_client(app, make_client)
    r = await c.post(f"/api/chat/conversations/{await new_conv(c)}/messages",
                     json={"content": "Olá", "provider": "ollama", "model": "qwen3:1.7b"})
    assert r.status_code == 503
    assert r.json()["error"] == {**r.json()["error"], "code": "local_engine_not_configured",
                                 "message": "Motor local não configurado"}


@respx.mock
async def test_explicit_model_choice(app, make_client, db_conn):
    await set_engine(app, db_conn)
    await set_cloud(app, db_conn, "groq", "groq-model")
    local = respx.post(f"{OLLAMA}/api/chat").mock(return_value=native_reply("gemma diz olá", model="gemma3:1b"))
    groq = respx.post(GROQ_URL).mock(return_value=openai_ok("groq diz olá"))
    c = await user_client(app, make_client)
    conv = await new_conv(c)
    r = await c.post(f"/api/chat/conversations/{conv}/messages",
                     json={"content": "Olá", "provider": "ollama", "model": "gemma3:1b"})
    assert r.json()["assistant_message"]["model"] == "gemma3:1b"
    assert json.loads(local.calls[0].request.content)["model"] == "gemma3:1b"
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá", "provider": "groq"})
    assert r.json()["assistant_message"]["provider"] == "groq" and groq.called
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá", "provider": "xpto"})
    assert r.status_code == 422


# --- chat: modelos e streaming -------------------------------------------------------------------------
@respx.mock
async def test_chat_models_lists_local_and_cloud_without_secrets(app, make_client, db_conn):
    c = await user_client(app, make_client)
    body = (await c.get("/api/chat/models")).json()
    assert body["local"]["warning"] == "Motor local não configurado" and body["local"]["models"] == []

    await set_engine(app, db_conn)
    await set_cloud(app, db_conn, "gemini", "gemini-test")
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=tags("qwen3:1.7b"))
    r = await c.get("/api/chat/models")
    body = r.json()
    assert body["local"]["models"] == ["qwen3:1.7b"] and body["local"]["reachable"] is True
    assert body["cloud"] == [{"provider": "gemini", "model": "gemini-test"}]
    assert "ollama.test" not in r.text and FAKE_TOKEN not in r.text


@respx.mock
async def test_streaming_chat_sends_deltas_and_saves(app, make_client, db_conn):
    await set_engine(app, db_conn)
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=native_stream(["Olá", ", ", "mundo"]))
    c = await user_client(app, make_client)
    conv = await new_conv(c)
    r = await c.post(f"/api/chat/conversations/{conv}/messages/stream", json={"content": "Diz olá"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"].startswith("no-cache")
    events = parse_sse(r.text)
    deltas = [json.loads(d)["text"] for e, d in events if e == "delta"]
    assert deltas == ["Olá", ", ", "mundo"]
    done = json.loads(events[-1][1])
    assert events[-1][0] == "done" and done["assistant_message"]["content"] == "Olá, mundo"
    assert done["conversation"]["title"] == "Diz olá"
    saved = (await c.get(f"/api/chat/conversations/{conv}/messages")).json()["items"]
    assert [m["role"] for m in saved] == ["user", "assistant"] and saved[1]["provider"] == "ollama"


@respx.mock
async def test_streaming_errors(app, make_client, db_conn):
    c = await user_client(app, make_client)
    conv = await new_conv(c)
    # sem nenhum fornecedor: erro JSON antes de abrir o stream
    r = await c.post(f"/api/chat/conversations/{conv}/messages/stream", json={"content": "Olá"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "ai_unavailable"
    # motor inacessível: evento de erro, nada gravado
    await set_engine(app, db_conn)
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("desligado"))
    r = await c.post(f"/api/chat/conversations/{conv}/messages/stream", json={"content": "Olá"})
    events = parse_sse(r.text)
    assert events[-1][0] == "error" and json.loads(events[-1][1])["code"] == "local_engine_unreachable"
    assert (await c.get(f"/api/chat/conversations/{conv}/messages")).json()["items"] == []
