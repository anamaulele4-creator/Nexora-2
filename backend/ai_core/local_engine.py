"""Configuração do motor local (tabela ``local_ai_engine``, uma só linha).

O URL base e o token ficam cifrados com o mesmo CredentialCipher das chaves dos
fornecedores (AAD distintos: o texto cifrado do token não decifra como URL e vice-versa).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ai_core.keys import decrypt_api_key
from ai_core.ollama import OllamaConfig
from security.crypto import CredentialCipher, DecryptionError

log = logging.getLogger("connector_ai.ai")

URL_SECRET = "ollama_base_url"
TOKEN_SECRET = "ollama_token"

SELECT_ENGINE = """select e.*, u.email::text as updated_by_email
                   from public.local_ai_engine e left join public.users u on u.id = e.updated_by
                   where e.id = 1"""


@dataclass(frozen=True)
class LocalEngine:
    """Configuração decifrada. ``usable`` = ativo, com URL e modelo por defeito."""

    enabled: bool
    config: OllamaConfig | None
    default_model: str | None
    allow_cloud_fallback: bool
    allow_paid_fallback: bool
    ram_limit_gb: float
    error: str | None = None  # ex.: URL guardado não decifra

    @property
    def configured(self) -> bool:
        return self.config is not None

    @property
    def usable(self) -> bool:
        return self.enabled and self.config is not None and bool(self.default_model)


def engine_from_row(row: dict | None, cipher: CredentialCipher | None, timeout: float) -> LocalEngine | None:
    if row is None:
        return None
    config: OllamaConfig | None = None
    error: str | None = None
    if row.get("base_url_ciphertext") is not None:
        if cipher is None:
            error = "CREDENTIALS_ENCRYPTION_KEY não está configurada no servidor"
        else:
            try:
                url = decrypt_api_key(cipher, URL_SECRET, key_id=row["base_url_key_id"], nonce=row["base_url_nonce"],
                                      ciphertext=row["base_url_ciphertext"])
                token = None
                if row.get("token_ciphertext") is not None:
                    token = decrypt_api_key(cipher, TOKEN_SECRET, key_id=row["token_key_id"],
                                            nonce=row["token_nonce"], ciphertext=row["token_ciphertext"])
                config = OllamaConfig(
                    base_url=url, token=token, api_mode=row.get("api_mode") or "native",
                    temperature=float(row["temperature"]), max_tokens=int(row["max_tokens"]),
                    context_length=int(row["context_length"]), think=bool(row["think"]), timeout=timeout)
            except DecryptionError:
                log.error("configuração do motor local não decifra (CREDENTIALS_ENCRYPTION_KEY mudou?)")
                error = "O URL/token guardado não decifra (a CREDENTIALS_ENCRYPTION_KEY mudou?). Volta a guardá-los."
    return LocalEngine(
        enabled=bool(row.get("enabled")), config=config, default_model=row.get("default_model"),
        allow_cloud_fallback=bool(row.get("allow_cloud_fallback")),
        allow_paid_fallback=bool(row.get("allow_paid_fallback")),
        ram_limit_gb=float(row.get("ram_limit_gb") or 8), error=error)


async def load_engine(conn, cipher: CredentialCipher | None, timeout: float) -> LocalEngine | None:
    row = await conn.fetchrow(SELECT_ENGINE)
    return engine_from_row(dict(row) if row else None, cipher, timeout)
