from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from authentication.tokens import Principal
from core.errors import ApiError


class AuthError(ApiError):
    pass


@dataclass(frozen=True)
class ClientInfo:
    ip: str | None
    user_agent: str | None


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    user_id: uuid.UUID


@dataclass(frozen=True)
class RegistrationResult:
    user_id: uuid.UUID
    verification_required: bool


class AuthProviderBase(ABC):
    """Contrato comum. As rotas /api/auth/* só falam com esta interface."""

    name: str

    @abstractmethod
    async def register(self, email: str, password: str, display_name: str | None, client: ClientInfo) -> RegistrationResult: ...

    @abstractmethod
    async def login(self, email: str, password: str, client: ClientInfo) -> TokenPair: ...

    @abstractmethod
    async def refresh(self, refresh_token: str, client: ClientInfo) -> TokenPair: ...

    @abstractmethod
    async def logout(self, principal: Principal | None, refresh_token: str | None, client: ClientInfo) -> None: ...

    @abstractmethod
    async def verify_email(self, token: str, client: ClientInfo) -> None: ...

    @abstractmethod
    async def resend_verification(self, email: str, client: ClientInfo) -> None: ...

    @abstractmethod
    async def request_password_reset(self, email: str, client: ClientInfo) -> None: ...

    @abstractmethod
    async def reset_password(self, token: str, new_password: str, client: ClientInfo) -> None: ...

    @abstractmethod
    async def authenticate(self, access_token: str) -> Principal: ...


def unauthorized(message: str = "Sessão inválida ou expirada") -> AuthError:
    return AuthError(401, "unauthorized", message, headers={"WWW-Authenticate": "Bearer"})
