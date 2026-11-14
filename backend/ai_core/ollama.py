"""Motor local: adaptador Ollama.

Duas formas de conversar com o servidor, escolhidas pelo admin (``api_mode``):

* ``native`` (por defeito): ``POST /api/chat`` (NDJSON). É a única que respeita
  ``num_ctx`` (contexto), ``num_predict``, ``temperature`` e ``think`` — o endpoint
  OpenAI-compatível do Ollama ignora ``options`` (verificado com Ollama 0.40).
* ``openai``: ``POST /v1/chat/completions`` (SSE). Útil para servidores
  OpenAI-compatíveis que não são Ollama (llama.cpp, vLLM, LM Studio); o contexto
  fica a cargo do servidor.

A gestão de modelos usa sempre a API nativa: ``/api/tags``, ``/api/ps``, ``/api/pull``,
``/api/delete``, ``/api/show`` e ``/api/version``.

O URL base e o token nunca saem do servidor e nunca aparecem em mensagens de erro.
Se o motor não responder, o erro é explícito: nunca se inventa uma resposta.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

from ai_core.providers import AIReply, ChatMessage, ProviderError, StreamItem, _BaseClient, _error_message

NAME = "ollama"
NOT_CONFIGURED_MESSAGE = "Motor local não configurado"
UNREACHABLE_CODE = "local_engine_unreachable"


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str
    token: str | None = None
    api_mode: Literal["native", "openai"] = "native"
    temperature: float = 0.7
    max_tokens: int = 1024
    context_length: int = 4096
    think: bool = False
    timeout: float = 120


def normalise_base_url(url: str) -> str:
    """Valida e normaliza o URL base (sem barra final, sem /v1, sem credenciais no URL)."""
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("O URL tem de começar por http:// ou https:// e ter um anfitrião")
    if parts.username or parts.password:
        raise ValueError("Não ponhas credenciais no URL; usa o campo do token")
    if parts.query or parts.fragment:
        raise ValueError("O URL não pode ter parâmetros (?…) nem fragmentos (#…)")
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return f"{parts.scheme}://{parts.netloc}{path}"


def url_hint(url: str) -> str:
    """Pista segura para mostrar no admin: esquema + anfitrião (sem caminho nem porta)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if len(host) > 40:
        host = host[:18] + "…" + host[-18:]
    return f"{parts.scheme}://{host}"


def _headers(token: str | None) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _unreachable(exc: Exception) -> ProviderError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(NAME, "Motor local sem resposta (tempo esgotado)", code=UNREACHABLE_CODE)
    return ProviderError(NAME, f"Motor local inacessível ({type(exc).__name__})", code=UNREACHABLE_CODE)


def _ollama_error(resp: httpx.Response, model: str | None = None) -> ProviderError:
    msg = _error_message(resp)
    low = msg.lower()
    if resp.status_code in (401, 403):
        return ProviderError(NAME, "O motor local recusou o token (401/403). Verifica o token no /admin.",
                             status=resp.status_code, code="local_engine_auth")
    if resp.status_code == 404 and model and ("not found" in low or "model" in low):
        return ProviderError(NAME, f"O modelo '{model}' não está instalado no motor local",
                             status=404, retryable=True, code="local_model_missing")
    return ProviderError(NAME, msg, status=resp.status_code)


class OllamaClient(_BaseClient):
    name = NAME  # type: ignore[assignment]

    def __init__(self, config: OllamaConfig, model: str, *, http: httpx.AsyncClient | None = None) -> None:
        if not model:
            raise ValueError("modelo em falta")
        self.config = config
        self.api_key = config.token or ""
        self.model = model
        self.timeout = config.timeout
        self._http = http

    # --- pedidos -------------------------------------------------------------------------
    def _messages(self, messages: list[ChatMessage], system: str | None) -> list[dict]:
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend({"role": m.role, "content": m.content} for m in messages)
        return msgs

    def _native_body(self, messages: list[ChatMessage], system: str | None, stream: bool, think: bool | None) -> dict:
        body: dict = {
            "model": self.model,
            "messages": self._messages(messages, system),
            "stream": stream,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
                "num_ctx": self.config.context_length,
            },
        }
        if think is not None:
            body["think"] = think
        return body

    def _openai_body(self, messages: list[ChatMessage], system: str | None, stream: bool, think: bool | None) -> dict:
        body: dict = {
            "model": self.model,
            "messages": self._messages(messages, system),
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if think is False:
            body["reasoning_effort"] = "none"
        return body

    def _url(self) -> str:
        if self.config.api_mode == "openai":
            return f"{self.config.base_url}/v1/chat/completions"
        return f"{self.config.base_url}/api/chat"

    def _body(self, messages, system, stream: bool, think: bool | None) -> dict:  # type: ignore[override]
        if self.config.api_mode == "openai":
            return self._openai_body(messages, system, stream, think)
        return self._native_body(messages, system, stream, think)

    @staticmethod
    def _think_unsupported(exc: ProviderError) -> bool:
        return exc.status == 400 and ("think" in exc.message.lower() or "reasoning" in exc.message.lower())

    async def _post_json(self, body: dict) -> dict:
        try:
            if self._http is not None:
                resp = await self._http.post(self._url(), headers=_headers(self.config.token), json=body,
                                             timeout=self.timeout)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as http:
                    resp = await http.post(self._url(), headers=_headers(self.config.token), json=body)
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc
        if resp.status_code >= 400:
            raise _ollama_error(resp, self.model)
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(NAME, "Resposta inválida do motor local") from exc

    async def generate(self, messages: list[ChatMessage], system: str | None = None) -> AIReply:
        think: bool | None = self.config.think
        try:
            data = await self._post_json(self._body(messages, system, False, think))
        except ProviderError as exc:
            if not self._think_unsupported(exc):
                raise
            data = await self._post_json(self._body(messages, system, False, None))  # modelo sem "thinking"
        if self.config.api_mode == "openai":
            choices = data.get("choices") or []
            text = str((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
            usage = data.get("usage") or {}
            tin, tout = usage.get("prompt_tokens"), usage.get("completion_tokens")
        else:
            text = str((data.get("message") or {}).get("content") or "").strip()
            tin, tout = data.get("prompt_eval_count"), data.get("eval_count")
        if not text:
            raise ProviderError(NAME, "Resposta vazia do motor local (aumenta max_tokens ou desliga 'think')",
                                retryable=False)
        return AIReply(text=text, provider=NAME, model=str(data.get("model") or self.model),  # type: ignore[arg-type]
                       input_tokens=tin, output_tokens=tout)

    async def _lines(self, body: dict) -> AsyncIterator[str]:
        http = self._http or httpx.AsyncClient(timeout=self.timeout)
        try:
            async with http.stream("POST", self._url(), headers=_headers(self.config.token), json=body,
                                   timeout=self.timeout) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _ollama_error(resp, self.model)
                async for line in resp.aiter_lines():
                    if line:
                        yield line
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc
        finally:
            if self._http is None:
                await http.aclose()

    async def stream(self, messages: list[ChatMessage], system: str | None = None) -> AsyncIterator[StreamItem]:
        think: bool | None = self.config.think
        started = False
        try:
            async for item in self._stream_once(messages, system, think):
                started = True
                yield item
        except ProviderError as exc:
            if started or not self._think_unsupported(exc):
                raise
            async for item in self._stream_once(messages, system, None):
                yield item

    async def _stream_once(self, messages, system, think: bool | None) -> AsyncIterator[StreamItem]:
        parts: list[str] = []
        tin = tout = None
        model = self.model
        body = self._body(messages, system, True, think)
        async for line in self._lines(body):
            if self.config.api_mode == "openai":
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if chunk.get("error"):
                    err = chunk["error"]
                    raise ProviderError(NAME, str(err.get("message") if isinstance(err, dict) else err)[:300])
                model = str(chunk.get("model") or model)
                if chunk.get("usage"):
                    tin = chunk["usage"].get("prompt_tokens")
                    tout = chunk["usage"].get("completion_tokens")
                for choice in chunk.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        parts.append(piece)
                        yield piece
            else:
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise ProviderError(NAME, str(chunk["error"])[:300])
                model = str(chunk.get("model") or model)
                piece = (chunk.get("message") or {}).get("content")
                if piece:
                    parts.append(piece)
                    yield piece
                if chunk.get("done"):
                    tin, tout = chunk.get("prompt_eval_count"), chunk.get("eval_count")
                    break
        text = "".join(parts).strip()
        if not text:
            raise ProviderError(NAME, "Resposta vazia do motor local (aumenta max_tokens ou desliga 'think')",
                                retryable=False)
        yield AIReply(text=text, provider=NAME, model=model, input_tokens=tin, output_tokens=tout)  # type: ignore[arg-type]


class OllamaManager:
    """Gestão de modelos via API nativa do Ollama (só usada por rotas de admin)."""

    def __init__(self, base_url: str, token: str | None = None, *, timeout: float = 15,
                 http: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url
        self.token = token
        self.timeout = timeout
        self._http = http

    async def _request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        t = timeout or self.timeout
        try:
            if self._http is not None:
                resp = await self._http.request(method, f"{self.base_url}{path}", headers=_headers(self.token),
                                                json=body, timeout=t)
            else:
                async with httpx.AsyncClient(timeout=t) as http:
                    resp = await http.request(method, f"{self.base_url}{path}", headers=_headers(self.token),
                                              json=body)
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc
        if resp.status_code >= 400:
            raise _ollama_error(resp, (body or {}).get("model"))
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(NAME, "Resposta inválida do motor local") from exc
        return data if isinstance(data, dict) else {}

    async def version(self) -> str | None:
        return (await self._request("GET", "/api/version")).get("version")

    async def tags(self) -> list[dict]:
        models = (await self._request("GET", "/api/tags")).get("models") or []
        out = []
        for m in models:
            d = m.get("details") or {}
            out.append({
                "name": m.get("name") or m.get("model"),
                "size_bytes": m.get("size"),
                "modified_at": m.get("modified_at"),
                "digest": (m.get("digest") or "")[:12] or None,
                "family": d.get("family"),
                "parameter_size": d.get("parameter_size"),
                "quantization": d.get("quantization_level"),
            })
        return out

    async def ps(self) -> list[dict]:
        models = (await self._request("GET", "/api/ps")).get("models") or []
        return [{"name": m.get("name") or m.get("model"), "size_bytes": m.get("size"),
                 "size_vram_bytes": m.get("size_vram"), "context_length": m.get("context_length"),
                 "expires_at": m.get("expires_at")} for m in models]

    async def show(self, name: str) -> dict:
        data = await self._request("POST", "/api/show", {"model": name})
        return {"capabilities": data.get("capabilities") or [], "license": (data.get("license") or "")[:200] or None,
                "details": data.get("details") or {}}

    async def delete(self, name: str) -> None:
        await self._request("DELETE", "/api/delete", {"model": name})

    async def pull(self, name: str, *, timeout: float = 3600) -> AsyncIterator[dict]:
        """Descarrega um modelo e devolve o progresso (status, total, completed)."""
        http = self._http or httpx.AsyncClient(timeout=timeout)
        try:
            async with http.stream("POST", f"{self.base_url}/api/pull", headers=_headers(self.token),
                                   json={"model": name, "stream": True},
                                   timeout=httpx.Timeout(timeout, connect=15)) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _ollama_error(resp)
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("error"):
                        raise ProviderError(NAME, str(ev["error"])[:300], retryable=False)
                    yield {"status": ev.get("status"), "total": ev.get("total"), "completed": ev.get("completed")}
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc
        finally:
            if self._http is None:
                await http.aclose()
