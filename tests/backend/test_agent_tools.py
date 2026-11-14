"""Ferramentas do agente no chat: ciclo de ferramentas, botões de ligar contas e confirmação de escritas.

* Unitários: ``run_agent`` com um fornecedor falso; conversões de formato (Gemini, Ollama, OpenAI).
* Integração (BD e Redis reais, fornecedores e GitHub simulados com respx): o chat expõe só as
  ferramentas das contas ligadas, mostra "Ligar GitHub" quando falta a conta e só cria uma issue
  depois de o utilizador confirmar.
"""

from __future__ import annotations

import json
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from pydantic import SecretStr

from ai_core.agent import AgentEvent, ToolOutcome, run_agent
from ai_core.providers import AIReply, ChatMessage, ProviderError
from ai_core.system_prompt import build_system_prompt
from ai_core.tool_calling import (ToolCall, Turn, gemini_schema, to_gemini_contents, to_ollama_messages,
                                  to_openai_messages)
from conftest import FRONTEND, login, register_and_verify, unique_email
from local_helpers import OLLAMA, parse_sse, set_engine

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
GH = "https://api.github.com"
GH_TOKEN = "gho_fake_agent_test_token_0001"  # não é um token real


# --- unitários: ciclo de ferramentas ------------------------------------------------------------------
class FakeTurns:
    provider = "groq"
    model = "fake-model"

    def __init__(self, turns: list[Turn]) -> None:
        self.turns = list(turns)
        self.seen: list[tuple[list[dict], bool]] = []

    async def stream_turn(self, messages, tools, allow_tools=True):
        self.seen.append(([dict(m) for m in messages], allow_tools))
        turn = self.turns.pop(0) if self.turns else Turn(text="fim")
        if turn.text:
            yield turn.text
        yield Turn(text=turn.text, tool_calls=turn.tool_calls if allow_tools else [], model=self.model,
                   input_tokens=10, output_tokens=2)


class FakeToolbox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def definitions(self):
        return [{"type": "function", "function": {"name": "github__list_repos", "description": "x",
                                                  "parameters": {"type": "object", "properties": {}}}}]

    def label(self, name):
        return name

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "request_connection":
            return ToolOutcome({"ok": True}, events=[AgentEvent("connect", {"connector": "github"})])
        if name == "boom":
            raise RuntimeError("x")
        return ToolOutcome({"ok": True, "data": {"repos": ["ana/app"]}})


async def collect(gen):
    return [x async for x in gen]


async def test_run_agent_executes_tools_and_returns_final_text():
    turns = FakeTurns([Turn(text="", tool_calls=[ToolCall("c1", "github__list_repos", {"per_page": 5})]),
                       Turn(text="Tens 1 repositório: ana/app.")])
    tb = FakeToolbox()
    out = await collect(run_agent(turns, "SYS", [ChatMessage("user", "repos?")], tb))
    assert tb.calls == [("github__list_repos", {"per_page": 5})]
    reply = out[-1]
    assert isinstance(reply, AIReply) and reply.text == "Tens 1 repositório: ana/app."
    assert reply.input_tokens == 20 and reply.output_tokens == 4
    assert [e.data["status"] for e in out if isinstance(e, AgentEvent)] == ["ok"]
    second = turns.seen[1][0]
    assert second[0] == {"role": "system", "content": "SYS"}
    assert second[2]["tool_calls"][0]["name"] == "github__list_repos"
    assert second[3]["role"] == "tool" and json.loads(second[3]["content"])["data"]["repos"] == ["ana/app"]


async def test_run_agent_caps_iterations_and_forces_answer():
    loop_turn = Turn(text="", tool_calls=[ToolCall("c", "github__list_repos", {})])
    turns = FakeTurns([loop_turn] * 10 + [Turn(text="Resposta final")])
    tb = FakeToolbox()
    out = await collect(run_agent(turns, "S", [ChatMessage("user", "x")], tb, max_iterations=3))
    assert len(tb.calls) == 3
    assert turns.seen[-1][1] is False  # última volta sem ferramentas
    assert "Limite de ferramentas" in turns.seen[-1][0][-1]["content"]
    assert isinstance(out[-1], AIReply) and "limite de passos" in out[-1].text


async def test_run_agent_bad_arguments_and_crashing_tools_do_not_break_chat():
    turns = FakeTurns([Turn(text="", tool_calls=[ToolCall("a", "github__list_repos", {}, arguments_error="json"),
                                                 ToolCall("b", "boom", {})]),
                       Turn(text="Desculpa, falhou.")])
    tb = FakeToolbox()
    out = await collect(run_agent(turns, "S", [ChatMessage("user", "x")], tb))
    assert tb.calls == [("boom", {})]  # argumentos inválidos nem chegam à ferramenta
    assert [e.data["status"] for e in out if isinstance(e, AgentEvent)] == ["error", "error"]
    assert out[-1].text == "Desculpa, falhou."


async def test_run_agent_fallback_text_when_model_is_silent_after_connect():
    turns = FakeTurns([Turn(text="", tool_calls=[ToolCall("a", "request_connection", {"connector": "github"})]),
                       Turn(text="")])
    out = await collect(run_agent(turns, "S", [ChatMessage("user", "abre o github")], FakeToolbox()))
    assert any(isinstance(e, AgentEvent) and e.kind == "connect" for e in out)
    assert "ligues a conta" in out[-1].text


async def test_run_agent_empty_answer_is_an_error():
    with pytest.raises(ProviderError):
        await collect(run_agent(FakeTurns([Turn(text="")]), "S", [ChatMessage("user", "x")], FakeToolbox()))


def test_message_conversions():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "github__get_me", "arguments": {"a": 1}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "github__get_me", "content": '{"ok": true}'},
            {"role": "tool", "tool_call_id": "c2", "name": "github__list_repos", "content": "[1]"}]
    oa = to_openai_messages(msgs)
    assert oa[2]["tool_calls"][0]["function"] == {"name": "github__get_me", "arguments": '{"a": 1}'}
    assert oa[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}
    ol = to_ollama_messages(msgs)
    assert ol[2]["tool_calls"][0]["function"]["arguments"] == {"a": 1} and ol[3]["tool_name"] == "github__get_me"
    system, contents = to_gemini_contents(msgs)
    assert system == "S"
    assert contents[1]["parts"][0]["functionCall"] == {"name": "github__get_me", "args": {"a": 1}}
    assert len(contents[2]["parts"]) == 2  # respostas de ferramentas juntas
    assert contents[2]["parts"][1]["functionResponse"]["response"] == {"result": [1]}


def test_gemini_schema_strips_unsupported_keys():
    from connectors.github.connector import TOOLS

    for spec in TOOLS:
        schema = gemini_schema(spec.to_openai_tool()["function"]["parameters"])

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "properties":
                        for sub in v.values():
                            walk(sub)
                        continue
                    assert k not in ("additionalProperties", "title", "pattern", "default", "maxLength", "$defs"), (spec.name, k)
                    walk(v)

        walk(schema)
        assert schema["type"] == "object"


def test_system_prompt_lists_connections_honestly():
    p = build_system_prompt({"github": "octo-ana"}, ["github"], tools_enabled=True)
    assert "DÁRIO" in p and "CONNECTOR AI" in p and "GitHub (octo-ana)" in p
    assert "request_connection" in p and "Confirmar" in p and "navegador" in p
    assert "Gmail" in p  # por ligar
    plain = build_system_prompt({"github": "octo-ana"}, ["github"], tools_enabled=False)
    assert "request_connection" not in plain and "não estão disponíveis" in plain
    none = build_system_prompt({}, [], tools_enabled=True)
    assert "ainda não ligou nenhuma conta" in none


# --- integração: chat + contas ligadas ----------------------------------------------------------------
@pytest.fixture(autouse=True)
async def _clean(db_conn, app):
    await db_conn.execute("delete from ai_providers")
    await db_conn.execute("delete from local_ai_engine")
    s = app.state.settings
    saved = (s.github_oauth_client_id, s.github_oauth_client_secret)
    s.github_oauth_client_id, s.github_oauth_client_secret = "Iv1.agenttest0001", SecretStr("fake-secret-agent-0001")
    yield
    s.github_oauth_client_id, s.github_oauth_client_secret = saved


async def user_with_conv(app, make_client):
    c = await make_client()
    email = unique_email("agent")
    user_id = await register_and_verify(c, app, email)
    await login(c, email)
    conv = (await c.post("/api/chat/conversations", json={})).json()["id"]
    return c, uuid.UUID(user_id), conv


async def link_github(app, user_id: uuid.UUID, login_name: str = "octo-ana") -> None:
    from connector_hub.vault import save_connection

    async with app.state.db.service() as conn:
        await save_connection(conn, app.state.cipher, user_id=user_id, connector_key="github", display_name="GitHub",
                              account_id="4242", account_label=login_name, credential_type="oauth_token",
                              payload={"access_token": GH_TOKEN, "scope": "repo read:user"}, expires_at=None,
                              metadata={"provider": "github"})


def native_tool_call(name: str, arguments: dict) -> httpx.Response:
    lines = [json.dumps({"model": "qwen3:1.7b", "message": {"role": "assistant", "content": "",
                                                           "tool_calls": [{"function": {"name": name, "arguments": arguments}}]},
                         "done": False}),
             json.dumps({"model": "qwen3:1.7b", "message": {"role": "assistant", "content": ""}, "done": True,
                         "prompt_eval_count": 50, "eval_count": 5})]
    return httpx.Response(200, content=("\n".join(lines) + "\n").encode(), headers={"Content-Type": "application/x-ndjson"})


def native_text(text: str) -> httpx.Response:
    lines = [json.dumps({"model": "qwen3:1.7b", "message": {"role": "assistant", "content": text}, "done": False}),
             json.dumps({"model": "qwen3:1.7b", "message": {"role": "assistant", "content": ""}, "done": True,
                         "prompt_eval_count": 60, "eval_count": 7})]
    return httpx.Response(200, content=("\n".join(lines) + "\n").encode(), headers={"Content-Type": "application/x-ndjson"})


def openai_tool_stream(name: str, arguments: dict) -> httpx.Response:
    args = json.dumps(arguments)
    chunks = [
        {"model": "gpt-4o-mini", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": name, "arguments": args[:5]}}]}}]},
        {"model": "gpt-4o-mini", "choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": args[5:]}}]}}]},
        {"model": "gpt-4o-mini", "choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 4}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"Content-Type": "text/event-stream"})


def openai_text_stream(text: str) -> httpx.Response:
    chunks = [{"model": "gpt-4o-mini", "choices": [{"index": 0, "delta": {"content": text}}]}]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"Content-Type": "text/event-stream"})


@respx.mock
async def test_connected_github_tools_run_in_stream_with_ollama_native(app, make_client, db_conn):
    await set_engine(app, db_conn)
    c, user_id, conv = await user_with_conv(app, make_client)
    await link_github(app, user_id)
    chat = respx.post(f"{OLLAMA}/api/chat").mock(side_effect=[
        native_tool_call("github__list_repos", {"per_page": 5}), native_text("Tens o repositório ana/app.")])
    repos = respx.get(f"{GH}/user/repos").mock(return_value=httpx.Response(200, json=[
        {"full_name": "ana/app", "private": False, "html_url": "https://github.com/ana/app"}]))

    r = await c.post(f"/api/chat/conversations/{conv}/messages/stream", json={"content": "Abre o GitHub"})
    assert r.status_code == 200
    events = parse_sse(r.text)
    kinds = [e for e, _ in events]
    assert "tool" in kinds and kinds[-1] == "done"
    tool = json.loads(next(d for e, d in events if e == "tool"))
    assert tool == {"name": "github__list_repos", "label": "GitHub · repositórios", "status": "ok"}
    done = json.loads(events[-1][1])
    assert done["assistant_message"]["content"] == "Tens o repositório ana/app."
    assert done["assistant_message"]["metadata"]["tools"][0]["name"] == "github__list_repos"

    assert repos.calls.last.request.headers["authorization"] == f"Bearer {GH_TOKEN}"
    first = json.loads(chat.calls[0].request.content)
    names = {t["function"]["name"] for t in first["tools"]}
    assert {"request_connection", "github__list_repos", "github__create_issue"} <= names
    system = first["messages"][0]["content"]
    assert "DÁRIO" in system and "GitHub (octo-ana)" in system
    second = json.loads(chat.calls[1].request.content)
    assert second["messages"][-1]["role"] == "tool" and second["messages"][-1]["tool_name"] == "github__list_repos"
    assert GH_TOKEN not in json.dumps(second)  # o token nunca vai para o modelo

    saved = (await c.get(f"/api/chat/conversations/{conv}/messages")).json()
    assert saved["items"][1]["metadata"]["tools"][0]["status"] == "ok"


@respx.mock
async def test_missing_connection_shows_connect_button(app, make_client, db_conn):
    await set_engine(app, db_conn)
    c, _, conv = await user_with_conv(app, make_client)
    chat = respx.post(f"{OLLAMA}/api/chat").mock(side_effect=[
        native_tool_call("request_connection", {"connector": "github", "reason": "ver os teus repositórios"}),
        native_text("Preciso que ligues o GitHub: carrega no botão.")])
    r = await c.post(f"/api/chat/conversations/{conv}/messages/stream", json={"content": "Abre o GitHub"})
    events = parse_sse(r.text)
    connect = json.loads(next(d for e, d in events if e == "connect"))
    assert connect == {"type": "connect", "connector": "github", "name": "GitHub", "method": "oauth",
                       "tools_available": True, "reason": "ver os teus repositórios"}
    first = json.loads(chat.calls[0].request.content)
    assert [t["function"]["name"] for t in first["tools"]] == ["request_connection"]  # sem conta, sem ferramentas GitHub
    assert "ainda não ligou nenhuma conta" in first["messages"][0]["content"]
    done = json.loads(events[-1][1])
    assert done["assistant_message"]["metadata"]["actions"][0]["connector"] == "github"
    tool_msg = json.loads(chat.calls[1].request.content)["messages"][-1]
    assert "AINDA NÃO está ligada" in tool_msg["content"]


@respx.mock
async def test_write_tool_waits_for_confirmation_then_runs_once(app, make_client, db_conn):
    await set_engine(app, db_conn)
    c, user_id, conv = await user_with_conv(app, make_client)
    await link_github(app, user_id)
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=[
        native_tool_call("github__create_issue", {"owner": "ana", "repo": "app", "title": "Bug no login", "body": "Passos…"}),
        native_text("Preparei a issue; confirma no cartão.")])
    create = respx.post(f"{GH}/repos/ana/app/issues").mock(return_value=httpx.Response(
        201, json={"number": 12, "title": "Bug no login", "html_url": "https://github.com/ana/app/issues/12"}))

    r = await c.post(f"/api/chat/conversations/{conv}/messages/stream", json={"content": "Cria uma issue"})
    events = parse_sse(r.text)
    approval = json.loads(next(d for e, d in events if e == "approval"))
    assert approval["status"] == "pending" and approval["tool"] == "github.create_issue"
    assert approval["summary"] == "Criar a issue «Bug no login» em ana/app"
    assert approval["arguments"]["title"] == "Bug no login"
    assert create.call_count == 0  # nada foi criado sem confirmação
    done = json.loads(events[-1][1])
    assert done["approvals"][approval["id"]]["status"] == "pending"
    assert await db_conn.fetchval("select status from tasks where id = (select task_id from approvals where id = $1)",
                                  uuid.UUID(approval["id"])) == "waiting_approval"

    # outro utilizador não vê nem decide
    other, _, _ = await user_with_conv(app, make_client)
    assert (await other.post(f"/api/chat/approvals/{approval['id']}", json={"decision": "approve"})).status_code == 404

    r = await c.post(f"/api/chat/approvals/{approval['id']}", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["approval"]["status"] == "approved"
    assert body["approval"]["task_status"] == "completed"
    assert "https://github.com/ana/app/issues/12" in body["message"]["content"]
    assert create.call_count == 1
    assert json.loads(create.calls.last.request.content) == {"title": "Bug no login", "body": "Passos…"}

    again = await c.post(f"/api/chat/approvals/{approval['id']}", json={"decision": "approve"})
    assert again.status_code == 409 and create.call_count == 1  # nunca executa duas vezes

    listed = (await c.get(f"/api/chat/conversations/{conv}/messages")).json()
    assert listed["approvals"][approval["id"]]["result"]["number"] == 12
    assert listed["items"][-1]["content"].startswith("Feito:")
    logs = await db_conn.fetch("select actor, outcome from action_logs where task_id = (select task_id from approvals where id = $1) order by created_at",
                               uuid.UUID(approval["id"]))
    assert [(r["actor"], r["outcome"]) for r in logs] == [("agent", "pending"), ("user", "success")]


@respx.mock
async def test_reject_and_expired_approvals_never_call_github(app, make_client, db_conn):
    await set_engine(app, db_conn)
    c, user_id, conv = await user_with_conv(app, make_client)
    await link_github(app, user_id)
    args = {"owner": "ana", "repo": "app", "number": 3, "body": "Olá"}
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=[
        native_tool_call("github__comment_issue", args), native_text("Confirma o comentário."),
        native_tool_call("github__comment_issue", args), native_text("Confirma o comentário.")])
    comment = respx.post(f"{GH}/repos/ana/app/issues/3/comments").mock(return_value=httpx.Response(201, json={"id": 1}))

    ids = []
    for _ in range(2):
        r = await c.post(f"/api/chat/conversations/{conv}/messages/stream", json={"content": "Comenta"})
        ids.append(json.loads(next(d for e, d in parse_sse(r.text) if e == "approval"))["id"])

    r = await c.post(f"/api/chat/approvals/{ids[0]}", json={"decision": "reject"})
    assert r.status_code == 200 and r.json()["ok"] is None and r.json()["approval"]["status"] == "rejected"
    assert r.json()["message"]["content"].startswith("Ação cancelada")

    await db_conn.execute("update approvals set expires_at = now() - interval '1 minute' where id = $1", uuid.UUID(ids[1]))
    r = await c.post(f"/api/chat/approvals/{ids[1]}", json={"decision": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "approval_expired"
    assert await db_conn.fetchval("select status from approvals where id = $1", uuid.UUID(ids[1])) == "expired"
    assert comment.call_count == 0
    assert (await c.post(f"/api/chat/approvals/{uuid.uuid4()}", json={"decision": "approve"})).status_code == 404
    assert (await c.post(f"/api/chat/approvals/{ids[0]}", json={"decision": "talvez"})).status_code == 422


@respx.mock
async def test_model_without_tools_falls_back_to_plain_chat(app, make_client, db_conn):
    await set_engine(app, db_conn)
    c, user_id, conv = await user_with_conv(app, make_client)
    await link_github(app, user_id)
    chat = respx.post(f"{OLLAMA}/api/chat").mock(side_effect=[
        httpx.Response(400, json={"error": "registry.ollama.ai/library/gemma:2b does not support tools"}),
        native_text("Este modelo não usa ferramentas.")])
    r = await c.post(f"/api/chat/conversations/{conv}/messages/stream", json={"content": "Abre o GitHub"})
    events = parse_sse(r.text)
    assert events[-1][0] == "done", r.text
    plain = json.loads(chat.calls[1].request.content)
    assert "tools" not in plain
    assert "não estão disponíveis" in plain["messages"][0]["content"]


@respx.mock
async def test_non_stream_endpoint_uses_tools_too(app, make_client, db_conn):
    await set_engine(app, db_conn)
    c, user_id, conv = await user_with_conv(app, make_client)
    await link_github(app, user_id)
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=[
        httpx.Response(200, json={"model": "qwen3:1.7b", "message": {"role": "assistant", "content": "",
                                  "tool_calls": [{"function": {"name": "github__get_me", "arguments": {}}}]}, "done": True}),
        httpx.Response(200, json={"model": "qwen3:1.7b", "message": {"role": "assistant", "content": "És o octo-ana."},
                                  "done": True, "prompt_eval_count": 5, "eval_count": 3})])
    respx.get(f"{GH}/user").mock(return_value=httpx.Response(200, json={"login": "octo-ana", "public_repos": 2}))
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Quem sou no GitHub?"})
    assert r.status_code == 201, r.text
    assert r.json()["assistant_message"]["content"] == "És o octo-ana."
    assert r.json()["assistant_message"]["metadata"]["tools"][0]["name"] == "github__get_me"


@respx.mock
async def test_openai_compatible_streamed_tool_calls(app, make_client, db_conn):
    from ai_core.keys import encrypt_api_key, last4

    key = "fake-openai-test-key-0001"
    blob = encrypt_api_key(app.state.cipher, "openai", key)
    await db_conn.execute(
        """insert into ai_providers (provider, api_key_ciphertext, api_key_nonce, api_key_key_id, api_key_last4, model, enabled)
           values ('openai', $1, $2, $3, $4, 'gpt-4o-mini', true)""", blob.ciphertext, blob.nonce, blob.key_id, last4(key))
    c, user_id, conv = await user_with_conv(app, make_client)
    await link_github(app, user_id)
    route = respx.post(OPENAI_URL).mock(side_effect=[
        openai_tool_stream("github__get_repo", {"owner": "ana", "repo": "app"}), openai_text_stream("Repo ana/app.")])
    respx.get(f"{GH}/repos/ana/app").mock(return_value=httpx.Response(200, json={"full_name": "ana/app"}))
    r = await c.post(f"/api/chat/conversations/{conv}/messages/stream", json={"content": "Mostra ana/app"})
    events = parse_sse(r.text)
    assert events[-1][0] == "done", r.text
    second = json.loads(route.calls[1].request.content)
    assert second["messages"][-2]["tool_calls"][0]["function"]["arguments"] == '{"owner": "ana", "repo": "app"}'
    assert second["messages"][-1] == {**second["messages"][-1], "role": "tool", "tool_call_id": "call_1"}


@respx.mock
async def test_connect_from_chat_returns_to_chat(app, make_client):
    c, _, conv = await user_with_conv(app, make_client)
    r = await c.post(f"/api/connectors/github/connect?return_to=chat&conversation={conv}")
    assert r.status_code == 200, r.text
    state = parse_qs(urlparse(r.json()["authorize_url"]).query)["state"][0]
    from connector_hub import oauth

    respx.post(oauth.GITHUB_TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": GH_TOKEN, "token_type": "bearer", "scope": "repo,read:user"}))
    respx.get(f"{oauth.GITHUB_API}/user").mock(return_value=httpx.Response(200, json={"id": 1, "login": "octo"}))
    r = await c.get("/api/connectors/oauth/github/callback", params={"code": "abc", "state": state})
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(f"{FRONTEND}/chat?"), loc
    q = parse_qs(urlparse(loc).query)
    assert q["connected"] == ["github"] and q["conversation"] == [conv]
    # destinos desconhecidos são recusados
    assert (await c.post("/api/connectors/github/connect?return_to=https://evil.example")).status_code == 422


@respx.mock
async def test_gemini_function_calling_round_trip():
    from ai_core.tool_calling import GeminiTurns

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
    raw_parts = [{"functionCall": {"name": "github__get_me", "args": {}}, "thoughtSignature": "sig123"}]
    route = respx.post(url).mock(side_effect=[
        httpx.Response(200, json={"candidates": [{"content": {"parts": raw_parts}}],
                                  "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 2}}),
        httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "És o octo."}]}}]}),
    ])
    tb = FakeToolbox()
    out = await collect(run_agent(GeminiTurns("fake-key", "gemini-test"), "SYS", [ChatMessage("user", "quem sou?")], tb))
    assert tb.calls == [("github__get_me", {})] and out[-1].text == "És o octo."
    first = json.loads(route.calls[0].request.content)
    assert first["tools"][0]["functionDeclarations"][0]["name"] == "github__list_repos"
    assert first["systemInstruction"]["parts"][0]["text"] == "SYS"
    second = json.loads(route.calls[1].request.content)
    assert second["contents"][1] == {"role": "model", "parts": raw_parts}  # thoughtSignature devolvida tal e qual
    assert second["contents"][2]["parts"][0]["functionResponse"]["name"] == "github__get_me"
