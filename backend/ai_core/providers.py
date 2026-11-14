"""Clientes HTTP dos fornecedores de IA (Gemini e OpenAI), sem SDKs: só httpx.

Cada cliente recebe o histórico como uma lista de ``ChatMessage`` (role user/assistant)
e devolve um ``AIReply``. Erros de rede, de autenticação ou de quota tornam-se
``ProviderError`` com uma mensagem curta e segura (a chave nunca aparece: vai num
cabeçalho, não no URL).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

ProviderName = Literal["gemini", "openai"]
PROVIDERS: tuple[ProviderName, ...] = ("gemini", "openai")

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class AIReply:
    text: str
    provider: ProviderName
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProviderError(Exception):
    """Falha ao falar com um fornecedor. ``retryable`` indica se faz sentido tentar outro."""

    def __init__(self, provider: str, message: str, *, status: int | None = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.provider = provider
        self.message = message
        self.status = status
        self.retryable = retryable


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = None
    msg = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("status") or err.get("code")
        elif isinstance(err, str):
            msg = err
    text = str(msg or resp.reason_phrase or "erro desconhecido")
    return f"HTTP {resp.status_code}: {text[:300]}"


class _BaseClient:
    name: ProviderName

    def __init__(self, api_key: str, model: str, *, timeout: float = 60, http: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("chave em falta")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._http = http

    async def _post(self, url: str, headers: dict[str, str], body: dict) -> dict:
        try:
            if self._http is not None:
                resp = await self._http.post(url, headers=headers, json=body, timeout=self.timeout)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as http:
                    resp = await http.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, "Tempo de resposta esgotado") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"Erro de rede: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            # 400/401/403/404: chave ou modelo errados -> tentar outro fornecedor ainda faz sentido
            raise ProviderError(self.name, _error_message(resp), status=resp.status_code)
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(self.name, "Resposta inválida do fornecedor") from exc

    async def generate(self, messages: list[ChatMessage], system: str | None = None) -> AIReply:  # pragma: no cover
        raise NotImplementedError


class GeminiClient(_BaseClient):
    name: ProviderName = "gemini"

    async def generate(self, messages: list[ChatMessage], system: str | None = None) -> AIReply:
        body: dict = {
            "contents": [
                {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
                for m in messages
            ],
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{GEMINI_BASE_URL}/models/{self.model.removeprefix('models/')}:generateContent"
        data = await self._post(url, {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}, body)
        candidates = data.get("candidates") or []
        text = ""
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought")).strip()
        if not text:
            reason = (data.get("promptFeedback") or {}).get("blockReason") or (
                candidates[0].get("finishReason") if candidates else None)
            raise ProviderError(self.name, f"Resposta vazia do Gemini ({reason or 'sem texto'})", retryable=False)
        usage = data.get("usageMetadata") or {}
        return AIReply(text=text, provider=self.name, model=self.model,
                       input_tokens=usage.get("promptTokenCount"), output_tokens=usage.get("candidatesTokenCount"))


class OpenAIClient(_BaseClient):
    name: ProviderName = "openai"

    async def generate(self, messages: list[ChatMessage], system: str | None = None) -> AIReply:
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend({"role": m.role, "content": m.content} for m in messages)
        # Pedido mínimo (sem temperature/max_tokens) para funcionar também com modelos de raciocínio.
        data = await self._post(
            f"{OPENAI_BASE_URL}/chat/completions",
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            {"model": self.model, "messages": msgs},
        )
        choices = data.get("choices") or []
        text = ""
        if choices:
            text = str((choices[0].get("message") or {}).get("content") or "").strip()
        if not text:
            raise ProviderError(self.name, "Resposta vazia da OpenAI", retryable=False)
        usage = data.get("usage") or {}
        return AIReply(text=text, provider=self.name, model=str(data.get("model") or self.model),
                       input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"))


def make_client(provider: str, api_key: str, model: str, *, timeout: float = 60,
                http: httpx.AsyncClient | None = None) -> _BaseClient:
    if provider == "gemini":
        return GeminiClient(api_key, model, timeout=timeout, http=http)
    if provider == "openai":
        return OpenAIClient(api_key, model, timeout=timeout, http=http)
    raise ValueError(f"fornecedor desconhecido: {provider}")
