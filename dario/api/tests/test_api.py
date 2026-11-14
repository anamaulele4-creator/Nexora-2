"""Testes da DÁRIO API com um Ollama simulado (httpx.MockTransport)."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.model_manager import classify, recommend, resolve

USER = {"Authorization": "Bearer user-token"}
ADMIN = {"Authorization": "Bearer admin-token"}
seen: list[httpx.Request] = []


def fake_ollama(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    path = request.url.path
    if path == "/api/version":
        return httpx.Response(200, json={"version": "0.12.0"})
    if path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "qwen2.5-coder:7b"},
                                                    {"name": "nomic-embed-text:latest"}]})
    if path == "/v1/models":
        return httpx.Response(200, json={"object": "list", "data": [{"id": "qwen3:8b", "object": "model"}]})
    if path == "/api/ps":
        return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
    if path == "/api/chat":
        body = json.loads(request.content)
        lines = [{"model": body["model"], "message": {"role": "assistant", "content": "Olá"}, "done": False},
                 {"model": body["model"], "done": True}]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines) + "\n",
                              headers={"content-type": "application/x-ndjson"})
    if path == "/api/pull":
        return httpx.Response(200, json={"status": "success"})
    return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def client():
    seen.clear()
    settings = Settings(api_token="user-token", admin_token="admin-token", database_url="",
                        ollama_url="http://ollama:11434")
    with TestClient(create_app(settings, transport=httpx.MockTransport(fake_ollama))) as c:
        yield c


def test_health_is_public(client):
    assert client.get("/health").json()["status"] == "ok"


def test_ready_reports_ollama(client):
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["checks"]["ollama"] == "0.12.0"


def test_mirror_requires_token(client):
    assert client.get("/api/tags").status_code == 401
    assert client.get("/api/tags", headers={"Authorization": "Bearer errado"}).status_code == 401
    r = client.get("/api/tags", headers=USER)
    assert r.status_code == 200 and r.json()["models"][0]["name"] == "dario:auto"


def test_show_virtual_model(client):
    r = client.post("/api/show", headers=USER, json={"model": "dario:auto"})
    assert r.status_code == 200 and r.json()["details"]["family"] == "dario"


def test_pull_needs_admin(client):
    assert client.post("/api/pull", json={"model": "qwen3:8b"}, headers=USER).status_code == 403
    assert client.post("/api/pull", json={"model": "qwen3:8b"}, headers=ADMIN).status_code == 200


def test_unknown_path_is_404(client):
    assert client.post("/api/unknown", headers=USER).status_code == 404


def test_explicit_model_passes_through(client):
    r = client.post("/api/chat", headers=USER, json={"model": "qwen3:8b", "messages": [
        {"role": "user", "content": "Olá"}], "stream": True})
    assert r.status_code == 200 and "x-dario-model" not in r.headers
    assert json.loads(r.text.splitlines()[0])["message"]["content"] == "Olá"


def test_auto_model_routes_code_to_coder(client):
    r = client.post("/api/chat", headers=USER, json={"model": "dario", "messages": [
        {"role": "user", "content": "Corrige este código Python: ```print(1```"}]})
    assert r.headers["x-dario-model"].startswith("qwen2.5-coder:7b")
    chat = [q for q in seen if q.url.path == "/api/chat"][-1]
    assert json.loads(chat.content)["model"] == "qwen2.5-coder:7b"


def test_memory_disabled_returns_503(client):
    r = client.post("/dario/v1/memory", headers=USER, json={"namespace": "u1", "content": "x"})
    assert r.status_code == 503


def test_diagnostics(client):
    r = client.get("/dario/v1/diagnostics", headers=USER)
    assert r.status_code == 200 and r.json()["ollama_version"] == "0.12.0"
    assert "qwen3:8b" in r.json()["loaded"]


def test_classify_and_resolve():
    prefs = Settings().preferences
    assert classify([{"role": "user", "content": "escreve uma função em javascript"}]) == "code"
    assert classify([{"role": "user", "content": "Bom dia!"}]) == "general"
    assert resolve("dario:reasoning", [], ["qwen3:8b"], prefs) == ("qwen3:8b", "reasoning")
    assert resolve("llama3.1:8b", [], [], prefs) == ("llama3.1:8b", None)
    assert resolve("dario", [], [], prefs) == (None, "general")


def test_recommend_for_12gb_vram():
    rec = recommend(12, 16)
    assert rec["runs_on"] == "gpu" and rec["profile"] == "intermédia"
    assert "qwen3:14b" in rec["fits"] and "qwen3:32b" in rec["too_big"]
    assert rec["start_with"] == ["qwen3:8b", "qwen2.5-coder:7b", "nomic-embed-text"]


def test_recommend_cpu_only():
    rec = recommend(None, 16)
    assert rec["runs_on"] == "cpu" and "qwen3:14b" in rec["too_big"]


def test_v1_models_lists_dario(client):
    ids = [m["id"] for m in client.get("/v1/models", headers=USER).json()["data"]]
    assert ids == ["dario:auto", "qwen3:8b"]


def test_memory_routes_take_json_body():
    """Regressão: os modelos do corpo têm de ser visíveis ao FastAPI (antes caíam como query)."""
    app = create_app(Settings(api_token="user-token", database_url=""), transport=httpx.MockTransport(fake_ollama))
    route = next(r for r in app.routes if getattr(r, "path", "") == "/dario/v1/memory")
    assert [p.name for p in route.dependant.body_params] == ["body"]
