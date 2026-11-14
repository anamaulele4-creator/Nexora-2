"""Teste ponta-a-ponta contra um Ollama REAL (sem mocks).

Só corre quando ``OLLAMA_E2E_URL`` está definido, por exemplo:

    ollama serve &  &&  ollama pull qwen3:1.7b
    OLLAMA_E2E_URL=http://127.0.0.1:11434 OLLAMA_E2E_MODEL=qwen3:1.7b pytest -s -k e2e

(Atrás de um proxy HTTP, acrescente NO_PROXY=127.0.0.1,localhost: o httpx respeita HTTP_PROXY.)

Verifica que o chat devolve tokens reais (não inventados), que o streaming chega em
vários pedaços, e que o mesmo motor funciona pelo /admin, pelo /chat e pela API v1.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from ai_core.ollama import OllamaClient, OllamaConfig, OllamaManager
from ai_core.providers import AIReply, ChatMessage
from local_helpers import admin_client, parse_sse, user_client

E2E_URL = os.environ.get("OLLAMA_E2E_URL")
E2E_MODEL = os.environ.get("OLLAMA_E2E_MODEL", "qwen3:1.7b")

pytestmark = pytest.mark.skipif(not E2E_URL, reason="defina OLLAMA_E2E_URL para correr contra um Ollama real")

PROMPT = [ChatMessage("user", "Em português, diz numa frase curta o que é Moçambique.")]


def _cfg(**kw) -> OllamaConfig:
    return OllamaConfig(**{"base_url": E2E_URL or "", "max_tokens": 80, "temperature": 0.2, "think": False,
                           "timeout": 300, **kw})


@pytest.fixture(autouse=True)
async def _clean(db_conn):
    await db_conn.execute("delete from local_ai_engine")
    await db_conn.execute("delete from ai_providers")
    yield
    await db_conn.execute("delete from local_ai_engine")


async def test_e2e_model_is_installed():
    mgr = OllamaManager(E2E_URL or "")
    names = [m["name"] for m in await mgr.tags()]
    print(f"\n[e2e] Ollama {await mgr.version()} · modelos instalados: {names}")
    assert E2E_MODEL in names


async def test_e2e_native_generate_real_tokens():
    t = time.perf_counter()
    reply = await OllamaClient(_cfg(), E2E_MODEL).generate(PROMPT)
    print(f"\n[e2e native] {reply.model} · {reply.input_tokens}→{reply.output_tokens} tokens · "
          f"{time.perf_counter() - t:.1f}s\n{reply.text}")
    assert reply.provider == "ollama" and reply.text.strip()
    assert reply.output_tokens and reply.output_tokens > 0 and reply.input_tokens


async def test_e2e_native_streaming_many_chunks():
    items = [i async for i in OllamaClient(_cfg(), E2E_MODEL).stream(PROMPT)]
    pieces = [i for i in items if isinstance(i, str)]
    final = items[-1]
    print(f"\n[e2e stream native] {len(pieces)} pedaços: {''.join(pieces)!r}")
    assert len(pieces) > 3 and isinstance(final, AIReply) and final.text == "".join(pieces).strip()


async def test_e2e_openai_compatible_streaming():
    items = [i async for i in OllamaClient(_cfg(api_mode="openai"), E2E_MODEL).stream(PROMPT)]
    pieces = [i for i in items if isinstance(i, str)]
    print(f"\n[e2e stream /v1] {len(pieces)} pedaços: {''.join(pieces)!r}")
    assert len(pieces) > 3 and isinstance(items[-1], AIReply)


async def test_e2e_full_app_admin_chat_and_api_v1(app, make_client, client, db_conn):
    admin = await admin_client(app, make_client)
    r = await admin.put("/api/admin/local-engine", json={
        "base_url": E2E_URL, "enabled": True, "default_model": E2E_MODEL, "max_tokens": 80, "temperature": 0.2,
        "context_length": 2048, "think": False})
    assert r.status_code == 200, r.text
    assert E2E_URL not in r.text
    test = (await admin.post("/api/admin/local-engine/test")).json()
    print(f"\n[e2e admin test] ok={test['ok']} · {test['latency_ms']} ms · resposta={test['reply']!r}")
    assert test["ok"] and test["reply"]

    # chat da app em streaming
    user = await user_client(app, make_client)
    conv = (await user.post("/api/chat/conversations", json={})).json()["id"]
    r = await user.post(f"/api/chat/conversations/{conv}/messages/stream",
                        json={"content": "Diz olá a Maputo numa frase curta."}, timeout=300)
    events = parse_sse(r.text)
    deltas = [json.loads(d)["text"] for e, d in events if e == "delta"]
    assert events[-1][0] == "done", events[-1]
    done = json.loads(events[-1][1])
    print(f"[e2e /chat stream] {len(deltas)} pedaços · {done['assistant_message']['provider']}/"
          f"{done['assistant_message']['model']}: {done['assistant_message']['content']!r}")
    assert len(deltas) > 1 and done["assistant_message"]["provider"] == "ollama"

    # API pública v1
    key = (await admin.post("/api/admin/api-keys", json={"name": "e2e"})).json()["token"]
    h = {"Authorization": f"Bearer {key}"}
    models = (await client.get("/api/v1/models", headers=h)).json()
    assert E2E_MODEL in [m["id"] for m in models["data"]]
    r = await client.post("/api/v1/chat/completions", headers=h, timeout=300, json={
        "model": E2E_MODEL, "max_tokens": 60, "messages": [{"role": "user", "content": "Responde só: olá"}]})
    body = r.json()
    print(f"[e2e /api/v1 json] {body['choices'][0]['message']['content']!r} · usage={body['usage']}")
    assert r.status_code == 200 and body["usage"]["completion_tokens"] > 0
    r = await client.post("/api/v1/chat/completions", headers=h, timeout=300, json={
        "model": "auto", "stream": True, "messages": [{"role": "user", "content": "Conta de 1 a 5."}]})
    events = parse_sse(r.text)
    chunks = [json.loads(d) for _, d in events if d != "[DONE]"]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
    print(f"[e2e /api/v1 stream] {len(chunks)} chunks: {text!r}")
    assert events[-1] == (None, "[DONE]") and len(chunks) > 3 and text.strip()
