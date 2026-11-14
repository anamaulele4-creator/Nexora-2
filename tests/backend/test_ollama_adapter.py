"""Adaptador Ollama (sem BD): pedidos, opções, streaming, erros claros e catálogo."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ai_core import model_catalog
from ai_core.ollama import OllamaClient, OllamaConfig, OllamaManager, normalise_base_url, url_hint
from ai_core.providers import AIReply, ChatMessage, ProviderError
from local_helpers import OLLAMA, native_reply, native_stream, openai_stream, tags

MSGS = [ChatMessage("user", "Olá")]


def cfg(**kw) -> OllamaConfig:
    return OllamaConfig(**{"base_url": OLLAMA, "token": None, **kw})


@respx.mock
async def test_native_generate_sends_options_and_no_auth_without_token():
    route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=native_reply("Olá!"))
    client = OllamaClient(cfg(temperature=0.2, max_tokens=77, context_length=2048, think=False), "qwen3:1.7b")
    reply = await client.generate(MSGS, system="sê breve")
    assert reply == AIReply(text="Olá!", provider="ollama", model="qwen3:1.7b", input_tokens=12, output_tokens=5)
    req = route.calls[0].request
    assert "authorization" not in req.headers
    body = json.loads(req.content)
    assert body["stream"] is False and body["think"] is False
    assert body["options"] == {"temperature": 0.2, "num_predict": 77, "num_ctx": 2048}
    assert body["messages"][0] == {"role": "system", "content": "sê breve"}


@respx.mock
async def test_token_goes_in_bearer_header_only():
    route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=native_reply())
    await OllamaClient(cfg(token="tok-secret-123"), "m:1").generate(MSGS)
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer tok-secret-123"
    assert "tok-secret-123" not in str(req.url)


@respx.mock
async def test_openai_mode_uses_v1_chat_completions():
    route = respx.post(f"{OLLAMA}/v1/chat/completions").mock(return_value=httpx.Response(200, json={
        "model": "qwen3:1.7b", "choices": [{"message": {"content": "Olá (openai)"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2}}))
    reply = await OllamaClient(cfg(api_mode="openai", max_tokens=50), "qwen3:1.7b").generate(MSGS)
    assert reply.text == "Olá (openai)" and reply.output_tokens == 2
    body = json.loads(route.calls[0].request.content)
    assert body["max_tokens"] == 50 and body["reasoning_effort"] == "none" and body["stream"] is False


@respx.mock
async def test_native_stream_yields_pieces_then_reply():
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=native_stream(["Ol", "á", "!"]))
    items = [i async for i in OllamaClient(cfg(), "qwen3:1.7b").stream(MSGS)]
    assert items[:3] == ["Ol", "á", "!"]
    assert isinstance(items[-1], AIReply) and items[-1].text == "Olá!" and items[-1].output_tokens == 3


@respx.mock
async def test_openai_stream_yields_pieces_then_reply():
    respx.post(f"{OLLAMA}/v1/chat/completions").mock(return_value=openai_stream(["Bom ", "dia"]))
    items = [i async for i in OllamaClient(cfg(api_mode="openai"), "qwen3:1.7b").stream(MSGS)]
    assert items[:2] == ["Bom ", "dia"]
    assert items[-1].text == "Bom dia" and items[-1].input_tokens == 7


@respx.mock
async def test_unreachable_engine_gives_clear_error_never_fake_text():
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("recusado"))
    with pytest.raises(ProviderError) as exc:
        await OllamaClient(cfg(), "qwen3:1.7b").generate(MSGS)
    assert exc.value.code == "local_engine_unreachable"
    assert "inacessível" in exc.value.message and OLLAMA not in exc.value.message


@respx.mock
async def test_missing_model_and_bad_token_are_explicit():
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=[
        httpx.Response(404, json={"error": "model 'nada:1b' not found"}),
        httpx.Response(401, json={"error": "unauthorized"}),
    ])
    with pytest.raises(ProviderError) as e1:
        await OllamaClient(cfg(), "nada:1b").generate(MSGS)
    assert e1.value.code == "local_model_missing" and "nada:1b" in e1.value.message
    with pytest.raises(ProviderError) as e2:
        await OllamaClient(cfg(), "nada:1b").generate(MSGS)
    assert e2.value.code == "local_engine_auth"


@respx.mock
async def test_model_without_thinking_retries_without_think_flag():
    route = respx.post(f"{OLLAMA}/api/chat").mock(side_effect=[
        httpx.Response(400, json={"error": "\"gemma3:1b\" does not support thinking"}),
        native_reply("ok", model="gemma3:1b"),
    ])
    reply = await OllamaClient(cfg(think=True), "gemma3:1b").generate(MSGS)
    assert reply.text == "ok"
    assert "think" in json.loads(route.calls[0].request.content)
    assert "think" not in json.loads(route.calls[1].request.content)


@respx.mock
async def test_empty_reply_is_an_error():
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=native_reply(""))
    with pytest.raises(ProviderError) as exc:
        await OllamaClient(cfg(), "qwen3:1.7b").generate(MSGS)
    assert exc.value.retryable is False


@respx.mock
async def test_manager_native_endpoints():
    respx.get(f"{OLLAMA}/api/version").mock(return_value=httpx.Response(200, json={"version": "0.40.16"}))
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=tags("qwen3:1.7b"))
    respx.get(f"{OLLAMA}/api/ps").mock(return_value=httpx.Response(200, json={"models": [
        {"name": "qwen3:1.7b", "size": 1, "size_vram": 0, "context_length": 4096}]}))
    delete = respx.delete(f"{OLLAMA}/api/delete").mock(return_value=httpx.Response(200))
    pull_body = "\n".join(json.dumps(e) for e in [
        {"status": "pulling manifest"}, {"status": "downloading", "total": 100, "completed": 50},
        {"status": "success"}]) + "\n"
    respx.post(f"{OLLAMA}/api/pull").mock(return_value=httpx.Response(200, content=pull_body.encode()))
    mgr = OllamaManager(OLLAMA)
    assert await mgr.version() == "0.40.16"
    assert (await mgr.tags())[0]["name"] == "qwen3:1.7b"
    assert (await mgr.ps())[0]["context_length"] == 4096
    await mgr.delete("qwen3:1.7b")
    assert json.loads(delete.calls[0].request.content) == {"model": "qwen3:1.7b"}
    events = [e async for e in mgr.pull("qwen3:0.6b")]
    assert events[-1]["status"] == "success" and events[1]["completed"] == 50


@respx.mock
async def test_pull_error_event_raises():
    respx.post(f"{OLLAMA}/api/pull").mock(return_value=httpx.Response(
        200, content=b'{"status":"pulling manifest"}\n{"error":"pull model manifest: file does not exist"}\n'))
    with pytest.raises(ProviderError) as exc:
        _ = [e async for e in OllamaManager(OLLAMA).pull("naoexiste:1b")]
    assert "does not exist" in exc.value.message


def test_base_url_normalisation_and_hint():
    assert normalise_base_url("http://10.0.0.5:11434/") == "http://10.0.0.5:11434"
    assert normalise_base_url("https://ollama.exemplo.com/v1") == "https://ollama.exemplo.com"
    for bad in ("ftp://x", "localhost:11434", "http://user:pw@host", "http://host/?a=1"):
        with pytest.raises(ValueError):
            normalise_base_url(bad)
    assert url_hint("https://ollama.exemplo.com:8443/x") == "https://ollama.exemplo.com"


def test_catalog_entries_are_sane():
    names = [m.name for m in model_catalog.CATALOG]
    assert len(names) == len(set(names))
    for m in model_catalog.CATALOG:
        assert ":" in m.name and m.ram_gb > m.size_gb > 0 and m.license and m.capabilities
    assert model_catalog.find("qwen3:1.7b").recommended
    assert model_catalog.find("phi4-mini") is not None  # aceita sem a tag :latest
