"""Conector GitHub: ferramentas do agente sobre a API REST do GitHub (api.github.com).

Usa o token OAuth do próprio utilizador (decifrado do cofre pelo backend e passado ao
construtor — nunca vem do modelo nem de páginas web). Cada instância serve um só utilizador.

* Leitura (risco ``low``): perfil, repositórios, issues, pull requests, ficheiros e pesquisa
  de código. Correm logo.
* Escrita (risco ``high``, ``requires_approval``): criar issue e comentar. ``BaseConnector.execute``
  recusa-as sem ``approval_id`` no contexto, ou seja, sem um humano ter carregado em Confirmar.

As respostas são encurtadas (campos úteis, textos cortados) para caberem no contexto do modelo.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from connectors.base import (
    ApprovalCategory,
    BaseConnector,
    ConnectionResult,
    ConnectorContext,
    ConnectorError,
    HealthReport,
    HealthStatus,
    RateLimit,
    RetryPolicy,
    RiskLevel,
    ToolSpec,
    VerificationStrategy,
)

GITHUB_API = "https://api.github.com"
READ = "github.read"
WRITE = "github.write"
PERMISSIONS = frozenset({READ, WRITE})

_NAME = r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$"
MAX_FILE_CHARS = 20_000
MAX_TEXT = 4_000


def _cut(text: str | None, limit: int = MAX_TEXT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n…[cortado: {len(text) - limit} caracteres]"


# --- parâmetros -------------------------------------------------------------------------------------
class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Repo(_P):
    owner: str = Field(pattern=_NAME, description="Dono do repositório (utilizador ou organização)")
    repo: str = Field(pattern=_NAME, description="Nome do repositório")


class GetMeIn(_P):
    pass


class ListReposIn(_P):
    visibility: Literal["all", "public", "private"] = "all"
    sort: Literal["updated", "created", "pushed", "full_name"] = "updated"
    per_page: int = Field(default=20, ge=1, le=50)
    page: int = Field(default=1, ge=1, le=50)


class GetRepoIn(_Repo):
    pass


class ListIssuesIn(_Repo):
    state: Literal["open", "closed", "all"] = "open"
    per_page: int = Field(default=20, ge=1, le=50)
    page: int = Field(default=1, ge=1, le=50)


class GetIssueIn(_Repo):
    number: int = Field(ge=1, description="Número da issue ou pull request")


class ListPullsIn(_Repo):
    state: Literal["open", "closed", "all"] = "open"
    per_page: int = Field(default=20, ge=1, le=50)
    page: int = Field(default=1, ge=1, le=50)


class ReadFileIn(_Repo):
    path: str = Field(default="", max_length=500, description="Caminho do ficheiro ou pasta ('' = raiz)")
    ref: str = Field(default="", max_length=200, description="Branch, tag ou commit ('' = branch por defeito)")


class SearchCodeIn(_P):
    query: str = Field(min_length=1, max_length=256, description="Texto a procurar (sintaxe de pesquisa do GitHub)")
    owner: str = Field(default="", max_length=100, description="Limitar a este dono (opcional)")
    repo: str = Field(default="", max_length=100, description="Limitar a este repositório (exige owner)")
    per_page: int = Field(default=10, ge=1, le=30)


class CreateIssueIn(_Repo):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=20_000)
    labels: list[str] = Field(default_factory=list, max_length=10)


class CommentIssueIn(_Repo):
    number: int = Field(ge=1, description="Número da issue ou pull request")
    body: str = Field(min_length=1, max_length=20_000)


# --- respostas (genéricas: o conteúdo já vem encurtado) ------------------------------------------
class _R(BaseModel):
    model_config = ConfigDict(extra="allow")


def _spec(name: str, description: str, params: type[BaseModel], *, write: bool = False) -> ToolSpec:
    if write:
        return ToolSpec(
            name=f"github.{name}", description=description, parameters=params, response=_R,
            permissions=(WRITE,), risk=RiskLevel.high, requires_approval=True,
            approval_category=ApprovalCategory.publish, idempotent=False,
            rate_limit=RateLimit(max_calls=30, per_seconds=3600), retry=RetryPolicy(max_attempts=1),
            timeout_seconds=30, verification=VerificationStrategy.response_schema)
    return ToolSpec(
        name=f"github.{name}", description=description, parameters=params, response=_R,
        permissions=(READ,), risk=RiskLevel.low, requires_approval=False, idempotent=True,
        rate_limit=RateLimit(max_calls=300, per_seconds=3600), timeout_seconds=30,
        verification=VerificationStrategy.none)


TOOLS: tuple[ToolSpec, ...] = (
    _spec("get_me", "Perfil da conta GitHub ligada pelo utilizador (login, nome, nº de repositórios).", GetMeIn),
    _spec("list_repos", "Lista os repositórios a que a conta GitHub ligada tem acesso, mais recentes primeiro.",
          ListReposIn),
    _spec("get_repo", "Detalhes de um repositório (descrição, linguagem, estrelas, branch por defeito).", GetRepoIn),
    _spec("list_issues", "Lista issues de um repositório (sem pull requests).", ListIssuesIn),
    _spec("get_issue", "Lê uma issue ou pull request e os primeiros comentários.", GetIssueIn),
    _spec("list_pull_requests", "Lista pull requests de um repositório.", ListPullsIn),
    _spec("read_file", "Lê um ficheiro de texto de um repositório, ou lista uma pasta.", ReadFileIn),
    _spec("search_code", "Pesquisa código no GitHub (opcionalmente só num dono ou repositório).", SearchCodeIn),
    _spec("create_issue", "Cria uma issue num repositório. Só é executada depois de o utilizador confirmar.",
          CreateIssueIn, write=True),
    _spec("comment_issue", "Comenta uma issue ou pull request. Só é executada depois de o utilizador confirmar.",
          CommentIssueIn, write=True),
)


def _repo_out(r: dict) -> dict:
    return {
        "full_name": r.get("full_name"), "private": r.get("private"), "description": _cut(r.get("description"), 300),
        "language": r.get("language"), "stars": r.get("stargazers_count"), "forks": r.get("forks_count"),
        "open_issues": r.get("open_issues_count"), "default_branch": r.get("default_branch"),
        "updated_at": r.get("updated_at"), "url": r.get("html_url"),
    }


def _issue_out(i: dict, body_limit: int = 500) -> dict:
    return {
        "number": i.get("number"), "title": i.get("title"), "state": i.get("state"),
        "author": (i.get("user") or {}).get("login"), "comments": i.get("comments"),
        "labels": [lbl.get("name") for lbl in i.get("labels") or [] if isinstance(lbl, dict)],
        "created_at": i.get("created_at"), "updated_at": i.get("updated_at"), "url": i.get("html_url"),
        "is_pull_request": "pull_request" in i, "body": _cut(i.get("body"), body_limit),
    }


class GitHubConnector(BaseConnector):
    key = "github"
    tools = TOOLS

    def __init__(self, token: str, *, http: httpx.AsyncClient | None = None, timeout: float = 20,
                 base_url: str = GITHUB_API) -> None:
        if not token:
            raise ValueError("token em falta")
        self._token = token
        self._http = http
        self._timeout = timeout
        self._base = base_url.rstrip("/")

    # --- HTTP --------------------------------------------------------------------------------------
    async def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "CONNECTOR-AI"}
        url = f"{self._base}{path}"
        try:
            if self._http is not None:
                resp = await self._http.request(method, url, params=params, json=json, headers=headers,
                                                timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as http:
                    resp = await http.request(method, url, params=params, json=json, headers=headers)
        except httpx.TimeoutException as exc:
            raise ConnectorError("timeout", "O GitHub demorou demasiado a responder", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ConnectorError("connector_unreachable", "Erro de rede ao contactar o GitHub", retryable=True) from exc
        if resp.status_code >= 400:
            raise self._error(resp)
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise ConnectorError("invalid_response", "Resposta inválida do GitHub") from exc

    @staticmethod
    def _error(resp: httpx.Response) -> ConnectorError:
        try:
            msg = str((resp.json() or {}).get("message") or "")
        except ValueError:
            msg = ""
        msg = re.sub(r"\s+", " ", msg)[:200]
        status = resp.status_code
        if status == 401:
            return ConnectorError("token_invalid", "O GitHub recusou o token: volta a ligar a conta em /conectores")
        if status == 429 or (status == 403 and (resp.headers.get("x-ratelimit-remaining") == "0"
                                                or "rate limit" in msg.lower())):
            reset = resp.headers.get("x-ratelimit-reset") or resp.headers.get("retry-after")
            return ConnectorError("rate_limited", f"Limite de pedidos do GitHub atingido (reset: {reset or '?'})")
        if status == 403:
            return ConnectorError("forbidden", f"O GitHub recusou o acesso ({msg or 'sem permissão'})")
        if status == 404:
            return ConnectorError("not_found", "Não encontrado no GitHub (não existe ou a conta ligada não tem acesso)")
        if status == 422:
            return ConnectorError("invalid_arguments", f"O GitHub rejeitou o pedido: {msg or 'dados inválidos'}")
        if status >= 500:
            return ConnectorError("upstream_5xx", f"O GitHub respondeu HTTP {status}", retryable=True)
        return ConnectorError("github_error", f"O GitHub respondeu HTTP {status}: {msg}")

    # --- ciclo de vida ---------------------------------------------------------------------------
    async def connect(self, ctx: ConnectorContext, credentials: dict[str, Any]) -> ConnectionResult:
        me = await self._request("GET", "/user")
        return ConnectionResult(external_account_id=str(me.get("id")), external_account_label=str(me.get("login")))

    async def disconnect(self, ctx: ConnectorContext) -> None:
        return None  # o backend apaga o token cifrado e revoga no GitHub (connector_hub)

    async def health_check(self, ctx: ConnectorContext) -> HealthReport:
        try:
            await self._request("GET", "/user")
        except ConnectorError as exc:
            return HealthReport(HealthStatus.unhealthy, str(exc))
        return HealthReport(HealthStatus.healthy)

    # --- ferramentas -----------------------------------------------------------------------------
    async def _run(self, spec: ToolSpec, ctx: ConnectorContext, params: BaseModel) -> BaseModel:
        action = spec.name.split(".", 1)[1]
        handler = getattr(self, f"_t_{action}")
        return _R.model_validate(await handler(params))

    async def _t_get_me(self, p: GetMeIn) -> dict:
        me = await self._request("GET", "/user")
        return {"login": me.get("login"), "name": me.get("name"), "public_repos": me.get("public_repos"),
                "private_repos": me.get("total_private_repos"), "followers": me.get("followers"),
                "url": me.get("html_url")}

    async def _t_list_repos(self, p: ListReposIn) -> dict:
        data = await self._request("GET", "/user/repos", params={
            "visibility": p.visibility, "sort": p.sort, "per_page": p.per_page, "page": p.page})
        return {"count": len(data), "page": p.page, "repos": [_repo_out(r) for r in data]}

    async def _t_get_repo(self, p: GetRepoIn) -> dict:
        r = await self._request("GET", f"/repos/{p.owner}/{p.repo}")
        return {**_repo_out(r), "topics": r.get("topics") or [], "archived": r.get("archived")}

    async def _t_list_issues(self, p: ListIssuesIn) -> dict:
        data = await self._request("GET", f"/repos/{p.owner}/{p.repo}/issues",
                                   params={"state": p.state, "per_page": p.per_page, "page": p.page})
        issues = [_issue_out(i, 200) for i in data if "pull_request" not in i]
        return {"count": len(issues), "page": p.page, "issues": issues}

    async def _t_get_issue(self, p: GetIssueIn) -> dict:
        issue = await self._request("GET", f"/repos/{p.owner}/{p.repo}/issues/{p.number}")
        comments: list = []
        if issue.get("comments"):
            raw = await self._request("GET", f"/repos/{p.owner}/{p.repo}/issues/{p.number}/comments",
                                      params={"per_page": 20})
            comments = [{"author": (c.get("user") or {}).get("login"), "created_at": c.get("created_at"),
                         "body": _cut(c.get("body"), 1000)} for c in raw]
        return {**_issue_out(issue, MAX_TEXT), "comment_list": comments}

    async def _t_list_pull_requests(self, p: ListPullsIn) -> dict:
        data = await self._request("GET", f"/repos/{p.owner}/{p.repo}/pulls",
                                   params={"state": p.state, "per_page": p.per_page, "page": p.page})
        pulls = [{"number": x.get("number"), "title": x.get("title"), "state": x.get("state"),
                  "draft": x.get("draft"), "author": (x.get("user") or {}).get("login"),
                  "head": (x.get("head") or {}).get("ref"), "base": (x.get("base") or {}).get("ref"),
                  "merged_at": x.get("merged_at"), "updated_at": x.get("updated_at"), "url": x.get("html_url")}
                 for x in data]
        return {"count": len(pulls), "page": p.page, "pull_requests": pulls}

    async def _t_read_file(self, p: ReadFileIn) -> dict:
        path = p.path.strip().strip("/")
        if ".." in path.split("/"):
            raise ConnectorError("invalid_arguments", "Caminho inválido")
        data = await self._request("GET", f"/repos/{p.owner}/{p.repo}/contents/{path}",
                                   params={"ref": p.ref} if p.ref else None)
        if isinstance(data, list):
            return {"type": "dir", "path": path or "/", "entries": [
                {"name": e.get("name"), "type": e.get("type"), "size": e.get("size")} for e in data[:200]]}
        if data.get("type") != "file":
            return {"type": data.get("type"), "path": path}
        size = int(data.get("size") or 0)
        content = data.get("content")
        if content is None or data.get("encoding") != "base64":
            return {"type": "file", "path": path, "size": size, "text": None,
                    "note": "Ficheiro demasiado grande para ler pela API de conteúdos", "url": data.get("html_url")}
        raw = base64.b64decode(content)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"type": "file", "path": path, "size": size, "text": None, "note": "Ficheiro binário",
                    "url": data.get("html_url")}
        return {"type": "file", "path": path, "size": size, "text": _cut(text, MAX_FILE_CHARS),
                "url": data.get("html_url")}

    async def _t_search_code(self, p: SearchCodeIn) -> dict:
        q = p.query.strip()
        if p.repo:
            if not p.owner:
                raise ConnectorError("invalid_arguments", "Para limitar a um repositório indica também o owner")
            q += f" repo:{p.owner}/{p.repo}"
        elif p.owner:
            q += f" user:{p.owner}"
        data = await self._request("GET", "/search/code", params={"q": q, "per_page": p.per_page})
        items = [{"repo": (i.get("repository") or {}).get("full_name"), "path": i.get("path"),
                  "url": i.get("html_url")} for i in data.get("items") or []]
        return {"total": data.get("total_count"), "items": items}

    async def _t_create_issue(self, p: CreateIssueIn) -> dict:
        body: dict = {"title": p.title, "body": p.body}
        if p.labels:
            body["labels"] = p.labels
        i = await self._request("POST", f"/repos/{p.owner}/{p.repo}/issues", json=body)
        return {"number": i.get("number"), "title": i.get("title"), "url": i.get("html_url")}

    async def _t_comment_issue(self, p: CommentIssueIn) -> dict:
        c = await self._request("POST", f"/repos/{p.owner}/{p.repo}/issues/{p.number}/comments",
                                json={"body": p.body})
        return {"id": c.get("id"), "url": c.get("html_url")}


def describe_action(tool: str, args: dict) -> str:
    """Resumo legível de uma ação de escrita (mostrado no cartão de confirmação)."""
    repo = f"{args.get('owner')}/{args.get('repo')}"
    if tool == "github.create_issue":
        return f"Criar a issue «{_cut(str(args.get('title') or ''), 120)}» em {repo}"
    if tool == "github.comment_issue":
        return f"Comentar a issue/PR #{args.get('number')} em {repo}"
    return f"{tool} em {repo}"
