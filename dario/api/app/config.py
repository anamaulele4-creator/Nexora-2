"""Configuração da DÁRIO API (tudo por variáveis de ambiente; nada de segredos no código)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DARIO_", env_file=".env", extra="ignore")

    # Ollama só na rede privada do Docker (nunca exposto à Internet)
    ollama_url: str = "http://ollama:11434"
    # Token de utilização (chat, modelos, memória) e token de administração (pull/delete)
    api_token: str = ""
    admin_token: str = ""
    # PostgreSQL + pgvector para a memória semântica (vazio = memória desligada)
    database_url: str = ""
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    # Preferências do Model Manager (primeiro modelo instalado da lista ganha)
    general_models: str = "qwen3:8b,llama3.1:8b,mistral:7b,qwen3:4b"
    code_models: str = "qwen2.5-coder:7b,qwen3:8b,llama3.1:8b"
    reasoning_models: str = "qwen3:14b,qwen3:8b,llama3.1:8b"
    request_timeout: float = 600
    max_body_bytes: int = 4_000_000

    @staticmethod
    def _list(value: str) -> list[str]:
        return [v.strip() for v in value.split(",") if v.strip()]

    @property
    def preferences(self) -> dict[str, list[str]]:
        return {
            "general": self._list(self.general_models),
            "code": self._list(self.code_models),
            "reasoning": self._list(self.reasoning_models),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
