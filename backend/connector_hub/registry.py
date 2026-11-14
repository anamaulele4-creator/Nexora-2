"""Registo central de conectores e ferramentas.

É a ÚNICA fonte das ferramentas expostas ao modelo. Um nome que não esteja aqui
nunca é executado, venha de onde vier (modelo, página web, pedido).
"""

from __future__ import annotations

from dataclasses import dataclass

from connectors.base import BaseConnector, ConnectorError, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, BaseConnector] = {}
        self._tools: dict[str, ToolSpec] = {}
        self._by_openai_name: dict[str, str] = {}

    def register(self, connector: BaseConnector) -> None:
        if connector.key in self._connectors:
            raise ValueError(f"conector duplicado: {connector.key}")
        for spec in connector.tools:
            if spec.name in self._tools:
                raise ValueError(f"ferramenta duplicada: {spec.name}")
        self._connectors[connector.key] = connector
        for spec in connector.tools:
            self._tools[spec.name] = spec
            self._by_openai_name[spec.openai_name()] = spec.name

    def resolve(self, name: str) -> tuple[BaseConnector, ToolSpec]:
        canonical = self._by_openai_name.get(name, name)
        spec = self._tools.get(canonical)
        if spec is None:
            raise ConnectorError("unknown_tool", f"Ferramenta não registada: {name}")
        return self._connectors[spec.connector_key], spec

    def openai_tools(self, allowed_permissions: set[str] | None = None) -> list[dict]:
        specs = self._tools.values()
        if allowed_permissions is not None:
            specs = [s for s in specs if set(s.permissions) <= allowed_permissions]
        return [s.to_openai_tool() for s in specs]

    def names(self) -> list[str]:
        return sorted(self._tools)


# --- Ligação de contas (Etapa 4) ------------------------------------------------------------
# Como cada conector do catálogo (connector_definitions) liga a conta do utilizador.
# Os escopos OAuth vêm da própria definição na BD; aqui só se diz o mecanismo.

@dataclass(frozen=True)
class ConnectionSpec:
    key: str  # = connector_definitions.key
    method: str  # "oauth" | "api_key"
    oauth_provider: str | None = None  # "github" | "google"
    # escopos extra pedidos sempre (identificar a conta ligada)
    identity_scopes: tuple[str, ...] = ()


CONNECTION_SPECS: dict[str, ConnectionSpec] = {
    "github": ConnectionSpec("github", "oauth", "github"),
    "gmail": ConnectionSpec("gmail", "oauth", "google", ("openid", "email")),
    "google_drive": ConnectionSpec("google_drive", "oauth", "google", ("openid", "email")),
    "shopify": ConnectionSpec("shopify", "api_key"),
}

OAUTH_PROVIDERS: tuple[str, ...] = ("github", "google")


def connection_spec(key: str) -> ConnectionSpec | None:
    """Conectores que já sabem ligar uma conta. Os restantes do catálogo continuam só 'planned'."""
    return CONNECTION_SPECS.get(key)


def connectors_for_provider(provider: str) -> list[str]:
    return [s.key for s in CONNECTION_SPECS.values() if s.oauth_provider == provider]
