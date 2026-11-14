"""BaseConnector: contrato que todos os conectores implementam (Etapa 4)."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from connectors.base.tools import ToolSpec


class HealthStatus(str, Enum):
    healthy = "healthy"
    degraded = "degraded"
    unhealthy = "unhealthy"


@dataclass(frozen=True)
class ConnectorContext:
    """Quem pede e com que permissões. Nunca vem de conteúdo de páginas web."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    connected_app_id: uuid.UUID
    granted_permissions: frozenset[str]
    task_id: uuid.UUID | None = None
    approval_id: uuid.UUID | None = None  # presente só quando um humano aprovou esta ação


@dataclass(frozen=True)
class ConnectionResult:
    external_account_id: str
    external_account_label: str
    granted_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class HealthReport:
    status: HealthStatus
    detail: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False


class ConnectorError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class BaseConnector(ABC):
    """Cada subclasse declara ``key`` e ``tools`` e implementa os quatro métodos abstratos."""

    key: ClassVar[str]
    tools: ClassVar[tuple[ToolSpec, ...]] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        for spec in cls.tools:
            if spec.connector_key != cls.key:
                raise TypeError(f"{spec.name} não pertence ao conector {cls.key}")

    # -- ciclo de vida ---------------------------------------------------------------
    @abstractmethod
    async def connect(self, ctx: ConnectorContext, credentials: dict[str, Any]) -> ConnectionResult: ...

    @abstractmethod
    async def disconnect(self, ctx: ConnectorContext) -> None: ...

    @abstractmethod
    async def health_check(self, ctx: ConnectorContext) -> HealthReport: ...

    @abstractmethod
    async def _run(self, spec: ToolSpec, ctx: ConnectorContext, params: BaseModel) -> BaseModel: ...

    # -- execução com as regras do contrato ------------------------------------------
    def get_tool(self, name: str) -> ToolSpec:
        for spec in self.tools:
            if spec.name == name:
                return spec
        raise ConnectorError("unknown_tool", f"Ferramenta não registada: {name}")

    async def execute(self, tool_name: str, ctx: ConnectorContext, arguments: dict[str, Any]) -> ToolResult:
        spec = self.get_tool(tool_name)
        missing = [p for p in spec.permissions if p not in ctx.granted_permissions]
        if missing:
            return ToolResult(False, error_code="permission_denied", error_message=f"Permissões em falta: {', '.join(missing)}")
        if spec.requires_approval and ctx.approval_id is None:
            return ToolResult(False, error_code="approval_required", error_message="Esta ação exige aprovação humana")
        try:
            params = spec.parameters.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(False, error_code="invalid_arguments", error_message=str(exc))
        try:
            result = await self._run(spec, ctx, params)
            validated = spec.response.model_validate(result.model_dump() if isinstance(result, BaseModel) else result)
        except ConnectorError as exc:
            return ToolResult(False, error_code=exc.code, error_message=str(exc), retryable=exc.retryable)
        except ValidationError as exc:
            return ToolResult(False, error_code="invalid_response", error_message=str(exc))
        return ToolResult(True, data=validated.model_dump(mode="json"))
