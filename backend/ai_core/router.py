"""Escolha do fornecedor de IA.

Lê ``ai_providers`` (modo serviço), fica com os ativos que têm chave, ordena-os por
``AI_PROVIDER_ORDER`` (Gemini primeiro por defeito) e usa o primeiro. Se esse falhar
com um erro recuperável (rede, quota, chave/modelo inválidos), tenta o seguinte.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ai_core.keys import decrypt_api_key
from ai_core.providers import AIReply, ChatMessage, ProviderError, make_client
from core.config import Settings
from core.db import Database
from security.crypto import CredentialCipher, DecryptionError

log = logging.getLogger("connector_ai.ai")

NO_PROVIDER_MESSAGE = "Nenhum fornecedor de IA ativo"


class NoProviderError(Exception):
    def __init__(self, message: str = NO_PROVIDER_MESSAGE) -> None:
        super().__init__(message)
        self.message = message


class AllProvidersFailed(Exception):
    def __init__(self, errors: list[ProviderError]) -> None:
        self.errors = errors
        last = errors[-1]
        super().__init__(f"{last.provider}: {last.message}")
        self.message = "; ".join(f"{e.provider}: {e.message}" for e in errors)


@dataclass(frozen=True)
class ActiveProvider:
    provider: str
    model: str
    api_key: str


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

    async def generate(self, messages: list[ChatMessage], system: str | None = None) -> AIReply:
        providers = await self.active_providers()
        if not providers:
            raise NoProviderError()
        errors: list[ProviderError] = []
        for p in providers:
            client = make_client(p.provider, p.api_key, p.model,
                                 timeout=self.settings.ai_request_timeout_seconds, http=self.http)
            try:
                return await client.generate(messages, system)
            except ProviderError as exc:
                log.warning("fornecedor de IA falhou", extra={"fields": {"provider": p.provider, "status": exc.status}})
                errors.append(exc)
                if not exc.retryable:
                    break
        raise AllProvidersFailed(errors)
