"""Conector GitHub (ferramentas do agente) contra uma API simulada com httpx.MockTransport.

Não usa BD nem rede: cada teste define as respostas do 'GitHub' e verifica os pedidos feitos.
"""

from __future__ import annotations

import base64
import json
import uuid

import httpx
import pytest

from connectors.base import ConnectorContext
from connectors.github import GitHubConnector
from connectors.github.connector import PERMISSIONS, TOOLS, describe_action

TOKEN = "gho_fake_unit_test_token_0001"  # não é um token real


def ctx(approval: bool = False, perms=PERMISSIONS) -> ConnectorContext:
    return ConnectorContext(user_id=uuid.uuid4(), workspace_id=uuid.uuid4(), connected_app_id=uuid.uuid4(),
                            granted_permissions=frozenset(perms), approval_id=uuid.uuid4() if approval else None)


def make(handler) -> tuple[GitHubConnector, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return GitHubConnector(TOKEN, http=http), seen


def test_tool_specs_follow_contract():
    names = {t.name for t in TOOLS}
    assert {"github.list_repos", "github.get_repo", "github.list_issues", "github.get_issue",
            "github.list_pull_requests", "github.read_file", "github.search_code", "github.create_issue",
            "github.comment_issue", "github.get_me"} == names
    for t in TOOLS:
        write = t.name in ("github.create_issue", "github.comment_issue")
        assert t.requires_approval is write
        assert t.openai_name() == t.name.replace(".", "__")
        assert t.to_openai_tool()["function"]["parameters"]["type"] == "object"


async def test_list_repos_sends_token_and_trims_fields():
    def handler(req):
        assert req.url.path == "/user/repos"
        return httpx.Response(200, json=[{"full_name": "ana/app", "private": True, "description": "x" * 1000,
                                          "language": "Python", "stargazers_count": 3, "html_url": "https://github.com/ana/app",
                                          "owner": {"login": "ana", "huge": "y" * 5000}}])

    gh, seen = make(handler)
    r = await gh.execute("github.list_repos", ctx(), {"per_page": 5})
    assert r.ok, r.error_message
    assert r.data["repos"][0]["full_name"] == "ana/app"
    assert "owner" not in r.data["repos"][0]  # campos inúteis ficam de fora
    assert len(r.data["repos"][0]["description"]) < 400
    req = seen[0]
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.url.params["per_page"] == "5" and req.url.params["sort"] == "updated"


async def test_list_issues_excludes_pull_requests():
    gh, _ = make(lambda req: httpx.Response(200, json=[
        {"number": 1, "title": "Bug", "state": "open", "user": {"login": "a"}, "labels": [{"name": "bug"}]},
        {"number": 2, "title": "PR", "state": "open", "pull_request": {"url": "x"}},
    ]))
    r = await gh.execute("github.list_issues", ctx(), {"owner": "ana", "repo": "app"})
    assert r.ok and [i["number"] for i in r.data["issues"]] == [1]
    assert r.data["issues"][0]["labels"] == ["bug"]


async def test_get_issue_with_comments():
    def handler(req):
        if req.url.path.endswith("/comments"):
            return httpx.Response(200, json=[{"user": {"login": "b"}, "body": "ok", "created_at": "2026-01-01"}])
        return httpx.Response(200, json={"number": 7, "title": "T", "state": "open", "comments": 1, "body": "corpo"})

    gh, seen = make(handler)
    r = await gh.execute("github.get_issue", ctx(), {"owner": "ana", "repo": "app", "number": 7})
    assert r.ok and r.data["comment_list"][0]["author"] == "b" and r.data["body"] == "corpo"
    assert [s.url.path for s in seen] == ["/repos/ana/app/issues/7", "/repos/ana/app/issues/7/comments"]


async def test_read_file_decodes_and_lists_dirs():
    content = base64.b64encode("print('olá')\n".encode()).decode()

    def handler(req):
        if req.url.path == "/repos/ana/app/contents/src/main.py":
            assert req.url.params.get("ref") == "dev"
            return httpx.Response(200, json={"type": "file", "size": 14, "encoding": "base64", "content": content})
        if req.url.path == "/repos/ana/app/contents/":
            return httpx.Response(200, json=[{"name": "src", "type": "dir", "size": 0}])
        return httpx.Response(404, json={"message": "Not Found"})

    gh, _ = make(handler)
    r = await gh.execute("github.read_file", ctx(), {"owner": "ana", "repo": "app", "path": "src/main.py", "ref": "dev"})
    assert r.ok and r.data["text"] == "print('olá')\n"
    r = await gh.execute("github.read_file", ctx(), {"owner": "ana", "repo": "app"})
    assert r.ok and r.data["type"] == "dir" and r.data["entries"][0]["name"] == "src"
    r = await gh.execute("github.read_file", ctx(), {"owner": "ana", "repo": "app", "path": "../etc/passwd"})
    assert not r.ok and r.error_code == "invalid_arguments"


async def test_search_code_scopes_query():
    gh, seen = make(lambda req: httpx.Response(200, json={"total_count": 1, "items": [
        {"path": "a.py", "html_url": "u", "repository": {"full_name": "ana/app"}}]}))
    r = await gh.execute("github.search_code", ctx(), {"query": "TODO", "owner": "ana", "repo": "app"})
    assert r.ok and r.data["items"][0]["repo"] == "ana/app"
    assert seen[0].url.params["q"] == "TODO repo:ana/app"


async def test_list_pull_requests():
    gh, _ = make(lambda req: httpx.Response(200, json=[{"number": 3, "title": "Feat", "state": "open", "draft": True,
                                                        "head": {"ref": "f"}, "base": {"ref": "main"}}]))
    r = await gh.execute("github.list_pull_requests", ctx(), {"owner": "ana", "repo": "app", "state": "all"})
    assert r.ok and r.data["pull_requests"][0] == {**r.data["pull_requests"][0], "number": 3, "head": "f", "base": "main"}


async def test_invalid_arguments_never_reach_github():
    gh, seen = make(lambda req: httpx.Response(200, json={}))
    r = await gh.execute("github.get_repo", ctx(), {"owner": "ana/../x", "repo": "app"})
    assert not r.ok and r.error_code == "invalid_arguments"
    r = await gh.execute("github.get_repo", ctx(), {"owner": "ana", "repo": "app", "extra": 1})
    assert not r.ok and r.error_code == "invalid_arguments"
    assert seen == []


async def test_writes_require_approval_and_permission():
    gh, seen = make(lambda req: httpx.Response(201, json={"number": 9, "title": "T", "html_url": "https://github.com/ana/app/issues/9"}))
    args = {"owner": "ana", "repo": "app", "title": "T", "body": "b"}
    r = await gh.execute("github.create_issue", ctx(approval=False), args)
    assert not r.ok and r.error_code == "approval_required" and seen == []
    r = await gh.execute("github.create_issue", ctx(approval=True, perms={"github.read"}), args)
    assert not r.ok and r.error_code == "permission_denied" and seen == []
    r = await gh.execute("github.create_issue", ctx(approval=True), args)
    assert r.ok and r.data["number"] == 9
    assert seen[0].method == "POST" and json.loads(seen[0].content) == {"title": "T", "body": "b"}


async def test_comment_issue():
    gh, seen = make(lambda req: httpx.Response(201, json={"id": 5, "html_url": "https://github.com/c"}))
    r = await gh.execute("github.comment_issue", ctx(approval=True), {"owner": "ana", "repo": "app", "number": 2, "body": "Olá"})
    assert r.ok and r.data["url"] == "https://github.com/c"
    assert seen[0].url.path == "/repos/ana/app/issues/2/comments"


@pytest.mark.parametrize("status,headers,code", [
    (401, {}, "token_invalid"),
    (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"}, "rate_limited"),
    (403, {}, "forbidden"),
    (404, {}, "not_found"),
    (422, {}, "invalid_arguments"),
    (502, {}, "upstream_5xx"),
])
async def test_error_mapping(status, headers, code):
    gh, _ = make(lambda req: httpx.Response(status, json={"message": "nope"}, headers=headers))
    r = await gh.execute("github.get_repo", ctx(), {"owner": "ana", "repo": "app"})
    assert not r.ok and r.error_code == code
    assert TOKEN not in (r.error_message or "")


async def test_network_error_is_retryable():
    def boom(req):
        raise httpx.ConnectError("down")

    gh, _ = make(boom)
    r = await gh.execute("github.get_me", ctx(), {})
    assert not r.ok and r.error_code == "connector_unreachable" and r.retryable


def test_describe_action():
    assert describe_action("github.create_issue", {"owner": "a", "repo": "b", "title": "Olá"}) == "Criar a issue «Olá» em a/b"
    assert "#4" in describe_action("github.comment_issue", {"owner": "a", "repo": "b", "number": 4})
