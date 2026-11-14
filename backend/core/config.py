"""Configuração validada do backend.

Tudo vem de variáveis de ambiente (ver .env.example). A aplicação recusa arrancar
com configuração inválida ou insegura — especialmente em produção.
"""

from __future__ import annotations

import base64
import binascii
from enum import Enum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

INSECURE_MARKERS = ("change-me", "changeme", "secret", "example", "dev-only")


class Environment(str, Enum):
    development = "development"
    test = "test"
    staging = "staging"
    production = "production"


class AuthProvider(str, Enum):
    local = "local"
    supabase = "supabase"


class EmailBackend(str, Enum):
    console = "console"  # escreve o email no log (desenvolvimento)
    memory = "memory"  # guarda em memória (testes)
    smtp = "smtp"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore")

    # Geral ------------------------------------------------------------------
    environment: Environment = Environment.development
    app_name: str = "CONNECTOR AI"
    api_base_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:3000"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    log_level: str = "INFO"
    # Só ativar atrás de um proxy de confiança (ex.: load balancer) que define X-Forwarded-For
    trust_forwarded_for: bool = False

    # Dados -------------------------------------------------------------------
    database_url: SecretStr
    database_pool_min: int = Field(default=1, ge=1, le=50)
    database_pool_max: int = Field(default=10, ge=1, le=200)
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")

    # Autenticação --------------------------------------------------------------
    auth_provider: AuthProvider = AuthProvider.local
    jwt_secret: SecretStr
    jwt_issuer: str = "connector-ai"
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_seconds: int = Field(default=60 * 60 * 24 * 14, ge=3600, le=60 * 60 * 24 * 90)
    email_verification_ttl_seconds: int = Field(default=60 * 60 * 24, ge=600)
    password_reset_ttl_seconds: int = Field(default=60 * 60, ge=300, le=60 * 60 * 6)
    require_email_verification: bool = True
    cookie_secure: bool = False
    cookie_domain: str | None = None
    auth_rate_limit_per_15min: int = Field(default=10, ge=1, le=1000)

    supabase_url: str | None = None
    supabase_anon_key: SecretStr | None = None
    supabase_jwt_secret: SecretStr | None = None

    # Email ------------------------------------------------------------------------
    email_backend: EmailBackend = EmailBackend.console
    email_from: str = "CONNECTOR AI <no-reply@localhost>"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = True

    # Cofre de credenciais de conectores (AES-256-GCM, 32 bytes em base64) ----------------
    credentials_encryption_key: SecretStr | None = None
    credentials_encryption_key_id: str = "k1"

    # Serviços externos das etapas seguintes (opcionais na Etapa 1) ---------------------
    openai_api_key: SecretStr | None = None  # Etapa 3
    openai_model: str = "gpt-4.1"
    browserbase_api_key: SecretStr | None = None  # Etapa 2
    browserbase_project_id: str | None = None  # Etapa 2
    temporal_address: str | None = None  # Etapa 5
    s3_endpoint_url: str | None = None  # Etapa 2/5
    s3_bucket: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None

    # Administração ----------------------------------------------------------------------
    # Emails (separados por vírgula) que ficam admin quando a conta tem o email confirmado.
    admin_emails: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["garenadario3@gmail.com"])

    # IA e chat (chaves dos fornecedores ficam cifradas na BD, definidas em /admin) ----------
    ai_provider_order: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["gemini", "openai"])
    gemini_default_model: str = "gemini-flash-latest"
    openai_default_model: str = "gpt-4o-mini"
    ai_request_timeout_seconds: float = Field(default=60, ge=5, le=300)
    chat_rate_limit_per_hour: int = Field(default=60, ge=1, le=10000)
    chat_history_messages: int = Field(default=20, ge=2, le=100)
    chat_max_message_chars: int = Field(default=8000, ge=100, le=32000)

    # Validadores ----------------------------------------------------------------------
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip().rstrip("/") for o in v.split(",") if o.strip()]
        return v

    @field_validator("admin_emails", mode="before")
    @classmethod
    def _split_admins(cls, v: object) -> object:
        if isinstance(v, str):
            return [e.strip().lower() for e in v.split(",") if e.strip()]
        if isinstance(v, list):
            return [str(e).strip().lower() for e in v if str(e).strip()]
        return v

    @field_validator("ai_provider_order", mode="before")
    @classmethod
    def _split_order(cls, v: object) -> object:
        items = [p.strip().lower() for p in v.split(",") if p.strip()] if isinstance(v, str) else v
        if isinstance(items, list):
            unknown = [p for p in items if p not in ("gemini", "openai")]
            if unknown:
                raise ValueError(f"AI_PROVIDER_ORDER só aceita gemini e openai (recebi: {', '.join(unknown)})")
            seen: list[str] = []
            for p in items:
                if p not in seen:
                    seen.append(p)
            for p in ("gemini", "openai"):  # os que faltarem ficam no fim
                if p not in seen:
                    seen.append(p)
            return seen
        return items

    @field_validator("frontend_url", "api_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("tem de começar por http:// ou https://")
        return v.rstrip("/")

    @field_validator("database_url")
    @classmethod
    def _check_db(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if not raw.startswith(("postgresql://", "postgres://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL tem de ser um URL PostgreSQL")
        return SecretStr(raw.replace("postgresql+asyncpg://", "postgresql://", 1))

    @field_validator("jwt_secret")
    @classmethod
    def _check_jwt(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET precisa de pelo menos 32 caracteres")
        return v

    @field_validator("credentials_encryption_key")
    @classmethod
    def _check_enc_key(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None or not v.get_secret_value():
            return None
        try:
            raw = base64.b64decode(v.get_secret_value(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("CREDENTIALS_ENCRYPTION_KEY tem de estar em base64") from exc
        if len(raw) != 32:
            raise ValueError("CREDENTIALS_ENCRYPTION_KEY tem de ter exatamente 32 bytes (AES-256)")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> "Settings":
        if self.database_pool_min > self.database_pool_max:
            raise ValueError("DATABASE_POOL_MIN não pode ser maior que DATABASE_POOL_MAX")

        if self.auth_provider is AuthProvider.supabase:
            missing = [
                name
                for name, val in (
                    ("SUPABASE_URL", self.supabase_url),
                    ("SUPABASE_ANON_KEY", self.supabase_anon_key),
                    ("SUPABASE_JWT_SECRET", self.supabase_jwt_secret),
                )
                if not val
            ]
            if missing:
                raise ValueError(f"AUTH_PROVIDER=supabase exige: {', '.join(missing)}")

        if self.email_backend is EmailBackend.smtp and not self.smtp_host:
            raise ValueError("EMAIL_BACKEND=smtp exige SMTP_HOST")

        if self.environment in (Environment.production, Environment.staging):
            problems: list[str] = []
            if any(m in self.jwt_secret.get_secret_value().lower() for m in INSECURE_MARKERS):
                problems.append("JWT_SECRET parece um valor de exemplo")
            if not self.cookie_secure:
                problems.append("COOKIE_SECURE tem de ser true")
            if self.email_backend is not EmailBackend.smtp and self.auth_provider is AuthProvider.local:
                problems.append("EMAIL_BACKEND tem de ser smtp (emails reais de verificação/recuperação)")
            if not self.require_email_verification:
                problems.append("REQUIRE_EMAIL_VERIFICATION não pode estar desligado")
            if self.credentials_encryption_key is None:
                problems.append("CREDENTIALS_ENCRYPTION_KEY é obrigatória")
            if not self.frontend_url.startswith("https://"):
                problems.append("FRONTEND_URL tem de usar https")
            if problems:
                raise ValueError("Configuração insegura para produção: " + "; ".join(problems))
        return self

    @property
    def allowed_origins(self) -> list[str]:
        origins = {self.frontend_url, *self.cors_origins}
        return sorted(origins)

    def is_admin_email(self, email: str | None) -> bool:
        return bool(email) and email.strip().lower() in self.admin_emails  # type: ignore[union-attr]

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.production


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
