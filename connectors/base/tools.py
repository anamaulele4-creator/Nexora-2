"""Contrato de ferramentas (tools) do CONNECTOR AI.

O modelo de IA só pode chamar ferramentas declaradas e registadas no backend.
Cada ferramenta declara: nome, descrição, parâmetros (modelo Pydantic -> JSON Schema),
resposta, permissões exigidas, nível de risco, se exige aprovação humana, limite de uso,
política de repetição, timeout e estratégia de verificação do resultado.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}\.[a-z][a-z0-9_]{1,40}$")
PERMISSION_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_.]+$")


class RiskLevel(str, Enum):
    low = "low"  # leitura sem efeitos (listar, pesquisar)
    medium = "medium"  # escrita reversível (criar rascunho, etiqueta)
    high = "high"  # efeito externo visível (enviar, publicar, alterar dados importantes)
    critical = "critical"  # dinheiro, apagar, aceitar termos, irreversível


class ApprovalCategory(str, Enum):
    payment = "payment"
    purchase = "purchase"
    send_message = "send_message"
    publish = "publish"
    delete_data = "delete_data"
    modify_data = "modify_data"
    accept_terms = "accept_terms"
    irreversible = "irreversible"
    other = "other"


class VerificationStrategy(str, Enum):
    none = "none"  # só permitido em risco low
    response_schema = "response_schema"  # a resposta valida contra o modelo declarado
    read_back = "read_back"  # reler o recurso depois de escrever
    browser_assertion = "browser_assertion"  # confirmar no DOM/screenshot
    human = "human"  # confirmação humana


class RateLimit(BaseModel):
    model_config = ConfigDict(frozen=True)
    max_calls: int = Field(ge=1, le=100_000)
    per_seconds: int = Field(ge=1, le=86_400)


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_seconds: float = Field(default=1.0, ge=0, le=300)
    backoff_multiplier: float = Field(default=2.0, ge=1, le=10)
    retry_on: tuple[str, ...] = ("timeout", "rate_limited", "upstream_5xx")

    def delay_for(self, attempt: int) -> float:
        """Espera antes da tentativa ``attempt`` (1 = primeira, sem espera)."""
        if attempt <= 1:
            return 0.0
        return self.backoff_seconds * (self.backoff_multiplier ** (attempt - 2))


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    description: str = Field(min_length=10, max_length=1024)
    parameters: type[BaseModel]
    response: type[BaseModel]
    permissions: tuple[str, ...]
    risk: RiskLevel
    requires_approval: bool
    approval_category: ApprovalCategory | None = None
    idempotent: bool = False
    rate_limit: RateLimit
    retry: RetryPolicy = RetryPolicy()
    timeout_seconds: float = Field(default=30, gt=0, le=900)
    verification: VerificationStrategy

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not TOOL_NAME_RE.match(v):
            raise ValueError("nome tem de ser '<conector>.<acao>' em minúsculas, ex.: gmail.search_messages")
        return v

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("toda a ferramenta declara pelo menos uma permissão")
        for p in v:
            if not PERMISSION_RE.match(p):
                raise ValueError(f"permissão inválida: {p}")
        return v

    @model_validator(mode="after")
    def _safety_rules(self) -> "ToolSpec":
        if self.risk in (RiskLevel.high, RiskLevel.critical) and not self.requires_approval:
            raise ValueError("ferramentas de risco high/critical exigem aprovação humana")
        if self.requires_approval and self.approval_category is None:
            raise ValueError("ferramenta com aprovação tem de indicar approval_category")
        if self.verification is VerificationStrategy.none and self.risk is not RiskLevel.low:
            raise ValueError("só ferramentas de risco low podem dispensar verificação")
        if self.retry.max_attempts > 1 and not self.idempotent and self.risk is not RiskLevel.low:
            raise ValueError("ferramentas com efeitos e não idempotentes não podem repetir automaticamente")
        if self.parameters.model_config.get("extra") != "forbid":
            raise ValueError("o modelo de parâmetros tem de usar extra='forbid'")
        return self

    @property
    def connector_key(self) -> str:
        return self.name.split(".", 1)[0]

    def openai_name(self) -> str:
        # A API da OpenAI só aceita [a-zA-Z0-9_-] no nome da função
        return self.name.replace(".", "__")

    def to_openai_tool(self) -> dict[str, Any]:
        schema = self.parameters.model_json_schema()
        schema.setdefault("additionalProperties", False)
        return {
            "type": "function",
            "function": {
                "name": self.openai_name(),
                "description": self.description,
                "parameters": schema,
                "strict": False,
            },
        }

    def manifest(self) -> dict[str, Any]:
        """Descrição serializável (guardada em connector_definitions.tools)."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters.model_json_schema(),
            "response": self.response.model_json_schema(),
            "permissions": list(self.permissions),
            "risk": self.risk.value,
            "requires_approval": self.requires_approval,
            "approval_category": self.approval_category.value if self.approval_category else None,
            "idempotent": self.idempotent,
            "rate_limit": self.rate_limit.model_dump(),
            "retry": self.retry.model_dump(),
            "timeout_seconds": self.timeout_seconds,
            "verification": self.verification.value,
        }
