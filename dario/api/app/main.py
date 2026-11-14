"""DÁRIO API — a inteligência própria da CONNECTOR AI.

* Espelha a API nativa do Ollama (``/api/*``) e a compatível com OpenAI (``/v1/*``), **com token**.
  Assim o CONNECTOR AI liga-se ao DÁRIO sem mudar código: no /admin → Ollama / Modelos basta pôr o
  URL da DÁRIO API e o token.
* Model Manager: ``model: "dario"`` escolhe o modelo pela tarefa (código, raciocínio, geral) entre os instalados.
* Memória semântica (PostgreSQL + pgvector) em ``/dario/v1/memory``.
* Diagnóstico do servidor e recomendações de modelos em ``/dario/v1/diagnostics``.

O Ollama nunca fica exposto: só esta API fala com ele, pela rede privada do Docker.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app import diagnostics, model_manager
from app.auth import require_admin, require_user
from app.config import Settings, get_settings
from app.memory import Memory

log = logging.getLogger("dario")

USER_PATHS = {
    ("POST", "api/chat"), ("POST", "api/generate"), ("POST", "api/embed"), ("POST", "api/embeddings"),
    ("GET", "api/tags"), ("GET", "api/ps"), ("POST", "api/show"), ("GET", "api/version"),
    ("POST", "v1/chat/completions"), ("POST", "v1/completions"), ("POST", "v1/embeddings"),
    ("GET", "v1/models"),
}
ADMIN_PATHS = {("POST", "api/pull"), ("DELETE", "api/delete"), ("POST", "api/copy"), ("POST", "api/create")}
CHAT_PATHS = {"api/chat", "api/generate", "v1/chat/completions", "v1/completions"}
VIRTUAL_TAG = {"name": "dario:auto", "model": "dario:auto", "size": 0, "digest": "",
               "details": {"family": "dario", "parameter_size": "auto", "quantization_level": "auto"}}
HOP_HEADERS = {"content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding"}


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = httpx.AsyncClient(base_url=settings.ollama_url, transport=transport,
                                           timeout=httpx.Timeout(settings.request_timeout, connect=10))
        app.state.memory = None
        if settings.database_url:
            try:
                app.state.memory = await Memory.connect(settings.database_url, app.state.http, settings.ollama_url,
                                                        settings.embed_model, settings.embed_dim)
            except Exception as exc:  # a API arranca na mesma; /ready mostra o problema
                log.error("Memória indisponível: %s", exc.__class__.__name__)
        if not settings.api_token and not settings.admin_token:
            log.warning("Sem DARIO_API_TOKEN nem DARIO_ADMIN_TOKEN: todos os pedidos serão recusados")
        yield
        if app.state.memory:
            await app.state.memory.close()
        await app.state.http.aclose()

    app = FastAPI(title="DÁRIO API", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.dependency_overrides[get_settings] = lambda: settings

    async def installed_models(http: httpx.AsyncClient) -> list[str]:
        resp = await http.get("/api/tags")
        resp.raise_for_status()
        return [m.get("name") or m.get("model") for m in resp.json().get("models", [])]

    # ------------------------------------------------------------------ saúde
    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "dario"}

    @app.get("/ready")
    async def ready(request: Request):
        checks = {}
        try:
            r = await request.app.state.http.get("/api/version", timeout=5)
            checks["ollama"] = r.json().get("version") if r.status_code == 200 else f"http {r.status_code}"
        except httpx.HTTPError:
            checks["ollama"] = None
        checks["memory"] = bool(request.app.state.memory) if settings.database_url else "desligada"
        ok = bool(checks["ollama"]) and checks["memory"] is not False
        return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)

    # ------------------------------------------------------------ diagnóstico
    @app.get("/dario/v1/diagnostics", dependencies=[Depends(require_user)])
    async def diag(request: Request):
        hw = diagnostics.hardware("/models")
        http = request.app.state.http
        info: dict = {"hardware": hw, "recommendation": model_manager.recommend(hw["vram_gb"], hw["ram_gb"])}
        try:
            info["ollama_version"] = (await http.get("/api/version")).json().get("version")
            info["installed"] = await installed_models(http)
            info["loaded"] = [m.get("name") for m in (await http.get("/api/ps")).json().get("models", [])]
        except httpx.HTTPError:
            info["ollama_version"] = None
            info["error"] = "Ollama inacessível"
        return info

    @app.get("/dario/v1/models/route", dependencies=[Depends(require_user)])
    async def route(request: Request, q: str = "", model: str = "dario"):
        installed = await installed_models(request.app.state.http)
        chosen, task = model_manager.resolve(model, [{"role": "user", "content": q}], installed, settings.preferences)
        return {"model": chosen, "task": task, "installed": installed}

    # ------------------------------------------------------------------ memória
    class MemoryIn(BaseModel):
        namespace: str = Field(min_length=1, max_length=200)
        content: str = Field(min_length=1, max_length=20_000)
        metadata: dict = Field(default_factory=dict)

    class MemoryQuery(BaseModel):
        namespace: str = Field(min_length=1, max_length=200)
        query: str = Field(min_length=1, max_length=5_000)
        k: int = Field(default=5, ge=1, le=50)

    def memory_or_503(request: Request) -> Memory:
        mem = request.app.state.memory
        if not mem:
            raise HTTPException(503, "Memória desligada (define DARIO_DATABASE_URL)")
        return mem

    @app.post("/dario/v1/memory", dependencies=[Depends(require_user)])
    async def memory_add(body: MemoryIn, mem: Memory = Depends(memory_or_503)):
        return {"id": await mem.add(body.namespace, body.content, body.metadata)}

    @app.post("/dario/v1/memory/search", dependencies=[Depends(require_user)])
    async def memory_search(body: MemoryQuery, mem: Memory = Depends(memory_or_503)):
        return {"results": await mem.search(body.namespace, body.query, body.k)}

    @app.delete("/dario/v1/memory/{namespace}/{memory_id}", dependencies=[Depends(require_user)])
    async def memory_delete(namespace: str, memory_id: int, mem: Memory = Depends(memory_or_503)):
        if not await mem.delete(namespace, memory_id):
            raise HTTPException(404, "Memória não encontrada")
        return {"deleted": memory_id}

    # ------------------------------------------- espelho autenticado do Ollama
    async def proxy(request: Request, path: str) -> Response:
        key = (request.method, path)
        if key in ADMIN_PATHS:
            require_admin(require_user(request, settings))
        elif key in USER_PATHS:
            require_user(request, settings)
        else:
            raise HTTPException(404, "Não encontrado")

        raw = await request.body()
        if len(raw) > settings.max_body_bytes:
            raise HTTPException(413, "Pedido demasiado grande")
        http: httpx.AsyncClient = request.app.state.http
        headers = {"content-type": "application/json"}
        routed = None

        if path in CHAT_PATHS and raw:
            try:
                body = json.loads(raw)
            except ValueError:
                raise HTTPException(400, "JSON inválido")
            requested = body.get("model")
            if requested and requested.lower().startswith("dario"):
                try:
                    installed = await installed_models(http)
                except httpx.HTTPError:
                    raise HTTPException(502, "Motor local inacessível")
                msgs = body.get("messages") or [{"role": "user", "content": body.get("prompt", "")}]
                chosen, task = model_manager.resolve(requested, msgs, installed, settings.preferences)
                if not chosen:
                    raise HTTPException(503, "Nenhum modelo instalado no DÁRIO")
                body["model"] = chosen
                routed = f"{chosen};task={task}"
                raw = json.dumps(body).encode()

        # Modelos virtuais: "dario:auto" aparece na lista e pode ser o modelo por defeito no /admin
        if key == ("GET", "api/tags"):
            try:
                tags = (await http.get("/api/tags")).json()
            except (httpx.HTTPError, ValueError):
                raise HTTPException(502, "Motor local inacessível")
            tags.setdefault("models", []).insert(0, VIRTUAL_TAG)
            return JSONResponse(tags)
        if key == ("GET", "v1/models"):
            try:
                listed = (await http.get("/v1/models")).json()
            except (httpx.HTTPError, ValueError):
                raise HTTPException(502, "Motor local inacessível")
            listed.setdefault("data", []).insert(0, {"id": "dario:auto", "object": "model", "owned_by": "dario"})
            return JSONResponse(listed)
        if key == ("POST", "api/show"):
            try:
                name = str(json.loads(raw or b"{}").get("model", ""))
            except ValueError:
                raise HTTPException(400, "JSON inválido")
            if name.lower().startswith("dario"):
                return JSONResponse({"capabilities": ["completion"], "license": "DÁRIO · encaminhamento automático",
                                     "details": VIRTUAL_TAG["details"]})

        started = time.monotonic()
        upstream = http.build_request(request.method, "/" + path, content=raw or None, headers=headers,
                                      params=request.query_params)
        try:
            resp = await http.send(upstream, stream=True)
        except httpx.HTTPError:
            raise HTTPException(502, "Motor local inacessível")
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_HEADERS}
        if routed:
            out_headers["x-dario-model"] = routed
        log.info("%s /%s -> %s (%s) %.0fms", request.method, path, resp.status_code, routed or "-",
                 (time.monotonic() - started) * 1000)
        async def body():
            if resp.is_stream_consumed:  # resposta já lida (ex.: transporte de testes)
                yield resp.content
                return
            async for chunk in resp.aiter_raw():
                yield chunk

        return StreamingResponse(body(), status_code=resp.status_code, headers=out_headers,
                                 background=BackgroundTask(resp.aclose))

    def mirror(prefix: str):
        async def endpoint(request: Request, rest: str) -> Response:
            return await proxy(request, f"{prefix}/{rest}")
        return endpoint

    for prefix in ("api", "v1"):
        app.add_api_route(f"/{prefix}/{{rest:path}}", mirror(prefix), methods=["GET", "POST", "DELETE"],
                          include_in_schema=False)
    return app
