"""Escolha do fornecedor de IA.

Ordem automática ("auto"):

1. **Motor local (Ollama)** — prioridade 1 sempre que está ativo, com URL e modelo por defeito.
2. Fornecedores na nuvem de ``ai_providers`` ativos e com chave, por ``AI_PROVIDER_ORDER``.

Regras de recurso (fallback):

* Se o motor local está ativo, só se passa para a nuvem quando ele falha **e** o admin
  ligou ``allow_cloud_fallback``; fornecedores pagos (OpenAI) exigem ainda
  ``allow_paid_fallback``. Sem isso devolve-se o erro do motor local — nunca uma
  resposta inventada.
* Sem motor local ativo, o comportamento é o de sempre (cadeia de fornecedores na nuvem).
* Escolha explícita (``ModelSelection`` com fornecedor) usa só esse fornecedor, sem recurso.

No streaming, o recurso só acontece antes do primeiro pedaço de texto.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

import httpx

from ai_core.keys import decrypt_api_key
from ai_core.local_engine import LocalEngine, load_engine
from ai_core.ollama import NOT_CONFIGURED_MESSAGE, OllamaClient, OllamaConfig
from ai_core.providers import (PAID_PROVIDERS, PROVIDERS, AIReply, ChatMessage, ProviderError, StreamItem,
                               _BaseClient, make_client)
from core.config import Settings
from core.db import Database
from security.crypto import CredentialCipher, DecryptionError

log = logging.getLogger("connector_ai.ai")

NO_PROVIDER_MESSAGE = "Nenhum fornecedor de IA ativo"


class NoProviderError(Exception):
    def __init__(self, message: str = NO_PROVIDER_MESSAGE, code: str = "ai_unavailable") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class AllProvidersFailed(Exception):
    def __init__(self, errors: list[ProviderError]) -> None:
        self.errors = errors
        last = errors[-1]
        super().__init__(f"{last.provider}: {last.message}")
        self.message = "; ".join(f"{e.provider}: {e.message}" for e in errors)

    @property
    def code(self) -> str | None:
        return self.errors[-1].code


@dataclass(frozen=True)
class ModelSelection:
    """Escolha do utilizador. ``provider=None`` = automático."""

    provider: str | None = None
    model: str | None = None
    # Ajustes por pedido (API v1). Só o motor local os aplica; a nuvem usa os seus defeitos.
    temperature: float | None = None
    max_tokens: int | None = None
    # Preferência do utilizador (Definições): fornecedores a tentar se o escolhido falhar
    # ou não estiver disponível. Só se aplica quando ``fallback`` não está vazio.
    fallback: tuple[str, ...] = ()

    def apply(self, config: OllamaConfig) -> OllamaConfig:
        changes: dict = {}
        if self.temperature is not None:
            changes["temperature"] = self.temperature
        if self.max_tokens is not None:
            changes["max_tokens"] = max(16, self.max_tokens)
        return replace(config, **changes) if changes else config


@dataclass(frozen=True)
class ActiveProvider:
    provider: str
    model: str
    api_key: str
    ollama: OllamaConfig | None = None


def order_providers(rows: list[dict], order: list[str]) -> list[dict]:
    """Ativos com chave, pela ordem configurada (desconhecidos no fim)."""
    usable = [r for r in rows if r.get("enabled") and r.get("api_key_ciphertext") is not None]
    rank = {p: i for i, p in enumerate(order)}
    return sorted(usable, key=lambda r: rank.get(r["provider"], len(rank)))


class AIRouter:
    def __init__(self, db: Database, settings: Settings, cipher: CredentialCipher | None,
                 http: httpx.AsyncClient | None = None) -> None:
        self.db = db
        self.settings = settings
        self.cipher = cipher
        self.http = http  # injetável (testes)

    # --- leitura da configuração -------------------------------------------------------------
    async def local_engine(self) -> LocalEngine | None:
        async with self.db.service() as conn:
            return await load_engine(conn, self.cipher, self.settings.local_engine_timeout_seconds)

    async def active_providers(self) -> list[ActiveProvider]:
        async with self.db.service() as conn:
            rows = [dict(r) for r in await conn.fetch(
                """select provider, model, enabled, api_key_ciphertext, api_key_nonce, api_key_key_id
                   from public.ai_providers""")]
        ordered = order_providers(rows, self.settings.ai_provider_order)
        if not ordered or self.cipher is None:
            return []
        out: list[ActiveProvider] = []
        for r in ordered:
            try:
                key = decrypt_api_key(self.cipher, r["provider"], key_id=r["api_key_key_id"],
                                      nonce=r["api_key_nonce"], ciphertext=r["api_key_ciphertext"])
            except DecryptionError:
                log.error("chave do fornecedor não decifra (CREDENTIALS_ENCRYPTION_KEY mudou?)",
                          extra={"fields": {"provider": r["provider"]}})
                continue
            out.append(ActiveProvider(r["provider"], r["model"], key))
        return out

    async def candidates(self, selection: ModelSelection | None = None) -> list[ActiveProvider]:
        sel = selection or ModelSelection()
        if sel.fallback:
            return await self._with_fallback(sel)
        if sel.provider == "ollama":
            engine = await self.local_engine()
            if engine is None or not engine.enabled or engine.config is None:
                raise NoProviderError(engine.error if engine and engine.error else NOT_CONFIGURED_MESSAGE,
                                      code="local_engine_not_configured")
            model = sel.model or engine.default_model
            if not model:
                raise NoProviderError("O motor local não tem modelo por defeito; escolhe um em /admin",
                                      code="local_engine_not_configured")
            return [ActiveProvider("ollama", model, "", sel.apply(engine.config))]
        if sel.provider is not None:
            if sel.provider not in PROVIDERS:
                raise NoProviderError(f"Fornecedor desconhecido: {sel.provider}", code="unknown_provider")
            match = [p for p in await self.active_providers() if p.provider == sel.provider]
            if not match:
                raise NoProviderError(f"O fornecedor {sel.provider} não está ativo")
            p = match[0]
            return [ActiveProvider(p.provider, sel.model or p.model, p.api_key)]

        engine = await self.local_engine()
        cloud = await self.active_providers()
        if engine is not None and engine.usable and engine.config is not None:
            local = ActiveProvider("ollama", engine.default_model or "", "", sel.apply(engine.config))
            if not engine.allow_cloud_fallback:
                return [local]
            allowed = [p for p in cloud if engine.allow_paid_fallback or p.provider not in PAID_PROVIDERS]
            return [local, *allowed]
        return cloud

    async def _with_fallback(self, sel: ModelSelection) -> list[ActiveProvider]:
        """Escolhido primeiro; depois cada recurso disponível (sem repetir fornecedores)."""
        out: list[ActiveProvider] = []
        first_error: NoProviderError | None = None
        try:
            out.extend(await self.candidates(replace(sel, fallback=())))
        except NoProviderError as exc:
            first_error = exc
        for provider in sel.fallback:
            if provider == sel.provider or any(p.provider == provider for p in out):
                continue
            try:
                extra = await self.candidates(ModelSelection(provider=provider))
            except NoProviderError:
                continue
            out.extend(p for p in extra if not any(o.provider == p.provider for o in out))
        if not out and first_error is not None:
            raise first_error
        return out

    # --- geração -----------------------------------------------------------------------------
    def _client(self, p: ActiveProvider) -> _BaseClient:
        if p.provider == "ollama":
            assert p.ollama is not None
            return OllamaClient(p.ollama, p.model, http=self.http)
        return make_client(p.provider, p.api_key, p.model, timeout=self.settings.ai_request_timeout_seconds,
                           http=self.http)

    async def generate(self, messages: list[ChatMessage], system: str | None = None,
                       selection: ModelSelection | None = None) -> AIReply:
        providers = await self.candidates(selection)
        if not providers:
            raise NoProviderError()
        errors: list[ProviderError] = []
        for p in providers:
            try:
                return await self._client(p).generate(messages, system)
            except ProviderError as exc:
                log.warning("fornecedor de IA falhou", extra={"fields": {"provider": p.provider, "status": exc.status}})
                errors.append(exc)
                if not exc.retryable:
                    break
        raise AllProvidersFailed(errors)

    async def stream(self, messages: list[ChatMessage], system: str | None = None,
                     selection: ModelSelection | None = None) -> AsyncIterator[StreamItem]:
        """Pedaços de texto e, no fim, o AIReply completo. Recurso só antes do 1.º pedaço."""
        providers = await self.candidates(selection)
        if not providers:
            raise NoProviderError()
        errors: list[ProviderError] = []
        for p in providers:
            started = False
            try:
                async for item in self._client(p).stream(messages, system):
                    started = True
                    yield item
                return
            except ProviderError as exc:
                log.warning("fornecedor de IA falhou (stream)",
                            extra={"fields": {"provider": p.provider, "status": exc.status}})
                errors.append(exc)
                if started or not exc.retryable:
                    break
        raise AllProvidersFailed(errors)
