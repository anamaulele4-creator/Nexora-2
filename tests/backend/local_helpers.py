"""Ajudas partilhadas pelos testes do motor local e da API v1."""

from __future__ import annotations

import json

import httpx

from ai_core.keys import encrypt_api_key, last4
from ai_core.local_engine import TOKEN_SECRET, URL_SECRET
from conftest import ADMIN_EMAIL, STRONG_PASSWORD, login, unique_email, register_and_verify

OLLAMA = "http://ollama.test:11434"  # host fictício (respx intercepta)
FAKE_TOKEN = "fake-ollama-token-0001"  # não é um segredo real


async def admin_client(app, make_client):
    """Cliente com sessão de admin (regista a conta de admin uma vez por execução)."""
    c = await make_client()
    r = await c.post("/api/auth/register", json={"email": ADMIN_EMAIL, "password": STRONG_PASSWORD})
    if r.status_code == 201:
        msg = [m for m in app.state.email_sender.outbox if m.to == ADMIN_EMAIL and m.kind == "email_verification"][-1]
        assert (await c.post("/api/auth/verify-email", json={"token": msg.link.split("token=")[1]})).status_code == 204
    await login(c, ADMIN_EMAIL)
    return c


async def user_client(app, make_client):
    c = await make_client()
    email = unique_email("local")
    await register_and_verify(c, app, email)
    await login(c, email)
    return c


async def set_engine(app, db_conn, *, base_url: str = OLLAMA, token: str | None = FAKE_TOKEN, enabled: bool = True,
                     default_model: str | None = "qwen3:1.7b", allow_cloud_fallback: bool = False,
                     allow_paid_fallback: bool = False, api_mode: str = "native", think: bool = False,
                     ram_limit_gb: float = 8, max_tokens: int = 1024) -> None:
    cipher = app.state.cipher
    url = encrypt_api_key(cipher, URL_SECRET, base_url)
    tok = encrypt_api_key(cipher, TOKEN_SECRET, token) if token else None
    await db_conn.execute("delete from local_ai_engine")
    await db_conn.execute(
        """insert into local_ai_engine (id, base_url_ciphertext, base_url_nonce, base_url_key_id, base_url_hint,
               token_ciphertext, token_nonce, token_key_id, token_last4, enabled, default_model,
               allow_cloud_fallback, allow_paid_fallback, api_mode, think, ram_limit_gb, max_tokens)
           values (1, $1, $2, $3, 'http://ollama.test', $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)""",
        url.ciphertext, url.nonce, url.key_id, tok.ciphertext if tok else None, tok.nonce if tok else None,
        tok.key_id if tok else None, last4(token) if token else None, enabled, default_model,
        allow_cloud_fallback, allow_paid_fallback, api_mode, think, ram_limit_gb, max_tokens)


async def set_cloud(app, db_conn, provider: str, model: str, enabled: bool = True) -> None:
    key = f"fake-{provider}-test-key-0001"
    blob = encrypt_api_key(app.state.cipher, provider, key)
    await db_conn.execute(
        """insert into ai_providers (provider, api_key_ciphertext, api_key_nonce, api_key_key_id, api_key_last4, model, enabled)
           values ($1, $2, $3, $4, $5, $6, $7)""",
        provider, blob.ciphertext, blob.nonce, blob.key_id, last4(key), model, enabled)


def native_reply(text: str = "Olá do motor local", model: str = "qwen3:1.7b") -> httpx.Response:
    return httpx.Response(200, json={"model": model, "message": {"role": "assistant", "content": text},
                                     "done": True, "prompt_eval_count": 12, "eval_count": 5})


def native_stream(pieces: list[str], model: str = "qwen3:1.7b") -> httpx.Response:
    lines = [json.dumps({"model": model, "message": {"role": "assistant", "content": p}, "done": False})
             for p in pieces]
    lines.append(json.dumps({"model": model, "message": {"role": "assistant", "content": ""}, "done": True,
                             "prompt_eval_count": 9, "eval_count": len(pieces)}))
    return httpx.Response(200, content=("\n".join(lines) + "\n").encode(),
                          headers={"Content-Type": "application/x-ndjson"})


def openai_stream(pieces: list[str], model: str = "qwen3:1.7b") -> httpx.Response:
    chunks = [{"model": model, "choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]}
              for p in pieces]
    chunks.append({"model": model, "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": len(pieces)}})
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"Content-Type": "text/event-stream"})


def tags(*names: str) -> httpx.Response:
    return httpx.Response(200, json={"models": [
        {"name": n, "model": n, "size": 1_359_293_444, "digest": "8f68893c685c3ddff2aa",
         "details": {"family": "qwen3", "parameter_size": "2.0B", "quantization_level": "Q4_K_M"}} for n in names]})


def parse_sse(text: str) -> list[tuple[str | None, str]]:
    """[(evento, dados)] de um corpo SSE (ignora comentários/pings)."""
    out: list[tuple[str | None, str]] = []
    for block in text.split("\n\n"):
        event, data = None, []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            out.append((event, "\n".join(data)))
    return out
