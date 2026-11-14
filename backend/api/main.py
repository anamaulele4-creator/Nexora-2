"""Ponto de entrada da API.

Arrancar (a partir de backend/):  uvicorn --factory api.main:create_app
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

# permite importar o pacote "connectors" da raiz do monorepo
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from ai_core.keys import build_cipher  # noqa: E402
from ai_core.router import AIRouter  # noqa: E402
from api.routes import admin, api_keys, auth, chat, connectors, dashboard, health, local_engine, public_v1  # noqa: E402
from authentication.email import ConsoleEmailSender, EmailSender, MemoryEmailSender, SmtpEmailSender  # noqa: E402
from authentication.providers.base import AuthProviderBase  # noqa: E402
from core.config import AuthProvider, EmailBackend, Settings, get_settings  # noqa: E402
from core.db import Database  # noqa: E402
from core.errors import register_error_handlers  # noqa: E402
from core.redis import create_redis  # noqa: E402
from monitoring.logging import RequestIdMiddleware, configure_logging  # noqa: E402
from security.headers import SecurityHeadersMiddleware  # noqa: E402
from security.origin import OriginCheckMiddleware  # noqa: E402
from security.rate_limit import RateLimiter  # noqa: E402


def build_email_sender(settings: Settings) -> EmailSender:
    if settings.email_backend is EmailBackend.smtp:
        return SmtpEmailSender(
            settings.smtp_host or "", settings.smtp_port, settings.smtp_username,
            settings.smtp_password.get_secret_value() if settings.smtp_password else None,
            settings.smtp_starttls, settings.email_from,
        )
    if settings.email_backend is EmailBackend.memory:
        return MemoryEmailSender()
    return ConsoleEmailSender()


def build_auth_provider(settings: Settings, db: Database, email: EmailSender) -> AuthProviderBase:
    if settings.auth_provider is AuthProvider.supabase:
        from authentication.providers.supabase import SupabaseAuthProvider

        return SupabaseAuthProvider(db, settings)
    from authentication.providers.local import LocalAuthProvider

    return LocalAuthProvider(db, settings, email)


def create_app(settings: Settings | None = None, email_sender: EmailSender | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    db = Database(settings.database_url.get_secret_value(), settings.database_pool_min, settings.database_pool_max)
    redis = create_redis(settings.redis_url.get_secret_value())
    sender = email_sender or build_email_sender(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await db.connect()
        try:
            yield
        finally:
            await db.close()
            await redis.aclose()

    app = FastAPI(
        title="CONNECTOR AI API",
        version=health.VERSION,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings
    app.state.db = db
    app.state.redis = redis
    app.state.email_sender = sender
    app.state.rate_limiter = RateLimiter(redis)
    app.state.auth = build_auth_provider(settings, db, sender)
    app.state.cipher = build_cipher(
        settings.credentials_encryption_key.get_secret_value() if settings.credentials_encryption_key else None,
        settings.credentials_encryption_key_id,
    )
    app.state.ai_router = AIRouter(db, settings, app.state.cipher)

    # Ordem: o último adicionado é o mais exterior.
    app.add_middleware(OriginCheckMiddleware, allowed_origins=settings.allowed_origins)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Client-Type"],
    )
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    for r in (health.router, auth.router, dashboard.router, connectors.router, admin.router, chat.router,
              local_engine.router, api_keys.router, public_v1.router):
        app.include_router(r)
    return app
