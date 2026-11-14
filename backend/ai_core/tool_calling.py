"""Chamada de ferramentas (function calling) para cada fornecedor, num formato comum.

Formato interno das mensagens (estilo OpenAI, em dicionários):

* ``{"role": "system" | "user" | "assistant", "content": str}``
* resposta do modelo com ferramentas:
  ``{"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments": dict}], "_raw": ...}``
* resultado de uma ferramenta: ``{"role": "tool", "tool_call_id", "name", "content": str (JSON)}``

Cada adaptador tem ``stream_turn(messages, tools, allow_tools)``: devolve pedaços de texto (str) à
medida que chegam e, no fim, um ``Turn`` (texto completo, pedidos de ferramentas, uso de tokens).

* OpenAI / Groq / OpenRouter e Ollama em modo ``openai``: ``/chat/completions`` com ``tools``
  e streaming (os pedaços de ``tool_calls`` são juntados por ``index``).
* Ollama nativo: ``/api/chat`` com ``tools`` (NDJSON).
* Gemini: ``generateContent`` com ``functionDeclarations`` (sem streaming, como o chat normal).

Se o modelo/fornecedor não suportar ferramentas, levanta ``ProviderError`` com
``code="tools_unsupported"`` antes de qualquer texto: o chamador volta ao chat simples.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ai_core.ollama import OllamaConfig, _headers as ollama_headers, _ollama_error, _unreachable
from ai_core.providers import GEMINI_BASE_URL, ProviderError, _error_message

TOOLS_UNSUPPORTED = "tools_unsupported"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict
    arguments_error: str | None = None  # JSON inválido vindo do modelo


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw: Any = None  # partes originais (Gemini: repetidas tal e qual, com thoughtSignature)


TurnItem = str | Turn


def _tools_unsupported(provider: str, exc: ProviderError) -> ProviderError:
    """Converte um 400 'modelo não suporta ferramentas' num erro reconhecível."""
    low = exc.message.lower()
    if exc.status == 400 and ("tool" in low or "function" in low) and (
            "support" in low or "not enabled" in low or "not available" in low or "unknown" in low):
        return ProviderError(provider, exc.message, status=400, code=TOOLS_UNSUPPORTED)
    return exc


def _parse_args(raw: Any) -> tuple[dict, str | None]:
    if isinstance(raw, dict):
        return raw, None
    if raw in (None, ""):
        return {}, None
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}, "argumentos não são JSON válido"
    return (val, None) if isinstance(val, dict) else ({}, "argumentos têm de ser um objeto JSON")


async def _stream_lines(provider: str, http: httpx.AsyncClient | None, timeout: float, url: str,
                        headers: dict, body: dict, *, ollama_model: str | None = None) -> AsyncIterator[str]:
    client = http or httpx.AsyncClient(timeout=timeout)
    try:
        async with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                if ollama_model is not None:
                    raise _tools_unsupported(provider, _ollama_error(resp, ollama_model))
                raise _tools_unsupported(provider, ProviderError(provider, _error_message(resp),
                                                                 status=resp.status_code))
            async for line in resp.aiter_lines():
                if line:
                    yield line
    except httpx.HTTPError as exc:
        if ollama_model is not None:
            raise _unreachable(exc) from exc
        if isinstance(exc, httpx.TimeoutException):
            raise ProviderError(provider, "Tempo de resposta esgotado") from exc
        raise ProviderError(provider, f"Erro de rede: {type(exc).__name__}") from exc
    finally:
        if http is None:
            await client.aclose()


async def _post_json(provider: str, http: httpx.AsyncClient | None, timeout: float, url: str, headers: dict,
                     body: dict, *, ollama_model: str | None = None) -> dict:
    try:
        if http is not None:
            resp = await http.post(url, headers=headers, json=body, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        if ollama_model is not None:
            raise _unreachable(exc) from exc
        if isinstance(exc, httpx.TimeoutException):
            raise ProviderError(provider, "Tempo de resposta esgotado") from exc
        raise ProviderError(provider, f"Erro de rede: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        if ollama_model is not None:
            raise _tools_unsupported(provider, _ollama_error(resp, ollama_model))
        raise _tools_unsupported(provider, ProviderError(provider, _error_message(resp), status=resp.status_code))
    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderError(provider, "Resposta inválida do fornecedor") from exc
    return data if isinstance(data, dict) else {}


# --- OpenAI e compatíveis ----------------------------------------------------------------------------
def to_openai_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


class OpenAICompatTurns:
    def __init__(self, provider: str, url: str, headers: dict[str, str], model: str, *,
                 http: httpx.AsyncClient | None = None, timeout: float = 60, extra: dict | None = None,
                 stream: bool = True) -> None:
        self.stream = stream
        self.provider = provider
        self.url = url
        self.headers = headers
        self.model = model
        self.http = http
        self.timeout = timeout
        self.extra = extra or {}

    async def stream_turn(self, messages: list[dict], tools: list[dict], allow_tools: bool = True
                          ) -> AsyncIterator[TurnItem]:
        body: dict = {"model": self.model, "messages": to_openai_messages(messages), **self.extra}
        if tools:
            body["tools"] = tools
            if not allow_tools:
                body["tool_choice"] = "none"
        if not self.stream:
            data = await _post_json(self.provider, self.http, self.timeout, self.url, self.headers, body)
            choices = data.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices else {}
            text = str(msg.get("content") or "")
            calls = []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args, err = _parse_args(fn.get("arguments"))
                calls.append(ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                                      name=str(fn.get("name") or ""), arguments=args, arguments_error=err))
            if text:
                yield text
            usage = data.get("usage") or {}
            yield Turn(text=text, tool_calls=calls if allow_tools else [], model=str(data.get("model") or self.model),
                       input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"))
            return
        body.update({"stream": True, "stream_options": {"include_usage": True}})
        parts: list[str] = []
        calls: dict[int, dict] = {}
        usage: dict = {}
        model = self.model
        async for line in _stream_lines(self.provider, self.http, self.timeout, self.url, self.headers, body):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if chunk.get("error"):
                err = chunk["error"]
                raise ProviderError(self.provider, str(err.get("message") if isinstance(err, dict) else err)[:300])
            model = str(chunk.get("model") or model)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    parts.append(piece)
                    yield piece
                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index") or 0)
                    acc = calls.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["name"] += fn["name"]
                    if fn.get("arguments"):
                        acc["arguments"] += fn["arguments"] if isinstance(fn["arguments"], str) else json.dumps(
                            fn["arguments"])
        tool_calls = []
        for idx in sorted(calls):
            c = calls[idx]
            args, err = _parse_args(c["arguments"])
            tool_calls.append(ToolCall(id=c["id"] or f"call_{uuid.uuid4().hex[:12]}", name=c["name"],
                                       arguments=args, arguments_error=err))
        if not allow_tools:
            tool_calls = []
        yield Turn(text="".join(parts), tool_calls=tool_calls, model=model,
                   input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"))


# --- Ollama nativo -----------------------------------------------------------------------------------
def to_ollama_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [
                {"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"], "tool_name": m["name"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


class OllamaNativeTurns:
    provider = "ollama"

    def __init__(self, config: OllamaConfig, model: str, *, http: httpx.AsyncClient | None = None,
                 stream: bool = True) -> None:
        self.stream = stream
        self.config = config
        self.model = model
        self.http = http

    def _body(self, messages: list[dict], tools: list[dict], allow_tools: bool, think: bool | None) -> dict:
        body: dict = {"model": self.model, "messages": to_ollama_messages(messages), "stream": self.stream,
                      "options": {"temperature": self.config.temperature, "num_predict": self.config.max_tokens,
                                  "num_ctx": self.config.context_length}}
        if tools and allow_tools:
            body["tools"] = tools
        if think is not None:
            body["think"] = think
        return body

    async def stream_turn(self, messages: list[dict], tools: list[dict], allow_tools: bool = True
                          ) -> AsyncIterator[TurnItem]:
        think: bool | None = self.config.think
        started = False
        try:
            async for item in self._once(messages, tools, allow_tools, think):
                started = True
                yield item
        except ProviderError as exc:
            low = exc.message.lower()
            if started or not (exc.status == 400 and ("think" in low or "reasoning" in low)):
                raise
            async for item in self._once(messages, tools, allow_tools, None):
                yield item

    async def _lines(self, body: dict) -> AsyncIterator[str]:
        url, headers = f"{self.config.base_url}/api/chat", ollama_headers(self.config.token)
        if self.stream:
            async for line in _stream_lines("ollama", self.http, self.config.timeout, url, headers, body,
                                            ollama_model=self.model):
                yield line
        else:
            data = await _post_json("ollama", self.http, self.config.timeout, url, headers, body,
                                    ollama_model=self.model)
            yield json.dumps({**data, "done": True})

    async def _once(self, messages, tools, allow_tools, think) -> AsyncIterator[TurnItem]:
        parts: list[str] = []
        calls: list[ToolCall] = []
        tin = tout = None
        model = self.model
        async for line in self._lines(self._body(messages, tools, allow_tools, think)):
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            if chunk.get("error"):
                raise _tools_unsupported("ollama", ProviderError("ollama", str(chunk["error"])[:300], status=400))
            model = str(chunk.get("model") or model)
            msg = chunk.get("message") or {}
            piece = msg.get("content")
            if piece:
                parts.append(piece)
                yield piece
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args, err = _parse_args(fn.get("arguments"))
                calls.append(ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:12]}", name=str(fn.get("name") or ""),
                                      arguments=args, arguments_error=err))
            if chunk.get("done"):  # sem break: o gerador do stream fecha-se sozinho (evita aclose fora de contexto)
                tin, tout = chunk.get("prompt_eval_count"), chunk.get("eval_count")
        yield Turn(text="".join(parts), tool_calls=calls, model=model, input_tokens=tin, output_tokens=tout)


# --- Gemini --------------------------------------------------------------------------------------------
_GEMINI_SCHEMA_KEYS = {"type", "description", "properties", "required", "enum", "items", "minimum", "maximum",
                       "format", "nullable", "minItems", "maxItems"}


def gemini_schema(schema: Any) -> Any:
    """Reduz um JSON Schema (Pydantic) ao subconjunto que o Gemini aceita."""
    if isinstance(schema, list):
        return [gemini_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    if "anyOf" in schema:  # Optional[...] -> primeiro tipo não nulo
        options = [s for s in schema["anyOf"] if s.get("type") != "null"]
        merged = {**(options[0] if options else {}), **{k: v for k, v in schema.items() if k != "anyOf"}}
        return gemini_schema(merged)
    out: dict = {}
    for k, v in schema.items():
        if k not in _GEMINI_SCHEMA_KEYS:
            continue
        if k == "properties":
            out[k] = {name: gemini_schema(s) for name, s in v.items()}
        elif k == "items":
            out[k] = gemini_schema(v)
        elif k == "format" and v not in ("enum", "date-time"):
            continue
        else:
            out[k] = v
    if out.get("type") == "object" and not out.get("properties"):
        out.pop("required", None)
        out["properties"] = {}
    return out


def to_gemini_contents(messages: list[dict]) -> tuple[str | None, list[dict]]:
    system = None
    contents: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            system = (system + "\n\n" if system else "") + m["content"]
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": m["content"]}]})
        elif role == "assistant":
            if m.get("_raw"):
                contents.append({"role": "model", "parts": m["_raw"]})
            else:
                parts: list[dict] = []
                if m.get("content"):
                    parts.append({"text": m["content"]})
                for c in m.get("tool_calls") or []:
                    parts.append({"functionCall": {"name": c["name"], "args": c["arguments"]}})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        elif role == "tool":
            try:
                response = json.loads(m["content"])
            except ValueError:
                response = {"result": m["content"]}
            if not isinstance(response, dict):
                response = {"result": response}
            part = {"functionResponse": {"name": m["name"], "response": response}}
            # respostas seguidas de ferramentas vão juntas numa só mensagem
            if contents and contents[-1]["role"] == "user" and all("functionResponse" in p
                                                                   for p in contents[-1]["parts"]):
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
    return system, contents


class GeminiTurns:
    provider = "gemini"

    def __init__(self, api_key: str, model: str, *, http: httpx.AsyncClient | None = None, timeout: float = 60) -> None:
        self.api_key = api_key
        self.model = model
        self.http = http
        self.timeout = timeout

    async def stream_turn(self, messages: list[dict], tools: list[dict], allow_tools: bool = True
                          ) -> AsyncIterator[TurnItem]:
        system, contents = to_gemini_contents(messages)
        body: dict = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t["function"]["name"], "description": t["function"]["description"],
                 "parameters": gemini_schema(t["function"]["parameters"])} for t in tools]}]
            body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO" if allow_tools else "NONE"}}
        url = f"{GEMINI_BASE_URL}/models/{self.model.removeprefix('models/')}:generateContent"
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        try:
            if self.http is not None:
                resp = await self.http.post(url, headers=headers, json=body, timeout=self.timeout)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as http:
                    resp = await http.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderError("gemini", "Tempo de resposta esgotado") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("gemini", f"Erro de rede: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise _tools_unsupported("gemini", ProviderError("gemini", _error_message(resp), status=resp.status_code))
        data = resp.json()
        candidates = data.get("candidates") or []
        parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        calls = []
        for p in parts:
            fc = p.get("functionCall") if isinstance(p, dict) else None
            if fc:
                args, err = _parse_args(fc.get("args"))
                calls.append(ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=str(fc.get("name") or ""),
                                      arguments=args, arguments_error=err))
        if text:
            yield text
        if not text and not calls:
            reason = (data.get("promptFeedback") or {}).get("blockReason") or (
                candidates[0].get("finishReason") if candidates else None)
            raise ProviderError("gemini", f"Resposta vazia do Gemini ({reason or 'sem texto'})", retryable=False)
        usage = data.get("usageMetadata") or {}
        yield Turn(text=text, tool_calls=calls if allow_tools else [], model=self.model,
                   input_tokens=usage.get("promptTokenCount"), output_tokens=usage.get("candidatesTokenCount"),
                   raw=parts)
