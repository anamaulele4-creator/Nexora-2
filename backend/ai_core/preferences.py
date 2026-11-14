"""Preferência de IA de cada utilizador (Definições) e política global do admin.

* Por defeito: motor local (Ollama) e Groq como recurso.
* Fornecedores pagos (``PAID_PROVIDERS``, ex.: OpenAI) só podem ser escolhidos quando o
  admin os autoriza em ``ai_policy.allow_paid_providers``. Se o admin desligar a autorização
  depois, a preferência paga é ignorada: usa-se o recurso escolhido ou o modo automático.
* Sem preferência gravada o chat usa o modo automático de sempre (``AIRouter``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import asyncpg

from ai_core.providers import PAID_PROVIDERS, PROVIDERS
from ai_core.router import ModelSelection

DEFAULT_PROVIDER = "ollama"
DEFAULT_FALLBACK = "groq"
PROVIDER_CHOICES: tuple[str, ...] = ("auto", "ollama", *PROVIDERS)
PROVIDER_LABELS = {"auto": "Automático", "ollama": "Motor local (Ollama)", "gemini": "Google Gemini",
                   "groq": "Groq", "openrouter": "OpenRouter", "openai": "OpenAI"}


@dataclass(frozen=True)
class AIPreference:
    provider: str = DEFAULT_PROVIDER
    model: str | None = None
    fallback_provider: str | None = DEFAULT_FALLBACK
    saved: bool = False


def is_paid(provider: str | None) -> bool:
    return provider in PAID_PROVIDERS


async def allow_paid_providers(conn: asyncpg.Connection) -> bool:
    """Lida em modo serviço (ai_policy é tabela de sistema)."""
    return bool(await conn.fetchval("select allow_paid_providers from public.ai_policy where id = 1"))


async def load_preference(conn: asyncpg.Connection, user_id: uuid.UUID) -> AIPreference:
    row = await conn.fetchrow(
        "select provider, model, fallback_provider from public.user_ai_preferences where user_id = $1", user_id)
    if row is None:
        return AIPreference()
    return AIPreference(row["provider"], row["model"], row["fallback_provider"], saved=True)


def selection_for(pref: AIPreference, allow_paid: bool) -> ModelSelection | None:
    """ModelSelection para o chat, ou None (modo automático de sempre)."""
    if not pref.saved:
        return None
    provider = pref.provider
    model = pref.model
    fallback = pref.fallback_provider
    if fallback and is_paid(fallback) and not allow_paid:
        fallback = None
    if is_paid(provider) and not allow_paid:
        # a escolha paga deixou de ser permitida: usa o recurso escolhido, ou o modo automático
        provider, model, fallback = (fallback, None, None) if fallback else ("auto", None, None)
    if provider == "auto":
        return ModelSelection(fallback=(fallback,) if fallback else ())
    return ModelSelection(provider=provider, model=model, fallback=(fallback,) if fallback else ())
