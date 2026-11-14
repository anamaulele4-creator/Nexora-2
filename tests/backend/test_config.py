from __future__ import annotations

import base64
import os

import pytest
from pydantic import ValidationError

from conftest import make_settings

GOOD_KEY = base64.b64encode(os.urandom(32)).decode()


def test_valid_development_settings():
    s = make_settings(environment="development")
    assert s.allowed_origins == ["http://localhost:3000"]


def test_short_jwt_secret_rejected():
    with pytest.raises(ValidationError, match="32 caracteres"):
        make_settings(jwt_secret="short")


def test_non_postgres_url_rejected():
    with pytest.raises(ValidationError, match="PostgreSQL"):
        make_settings(database_url="mysql://x")


def test_encryption_key_must_be_32_bytes():
    with pytest.raises(ValidationError, match="32 bytes"):
        make_settings(credentials_encryption_key=base64.b64encode(b"abc").decode())


def test_supabase_mode_requires_keys():
    with pytest.raises(ValidationError, match="SUPABASE_URL"):
        make_settings(auth_provider="supabase")


def test_production_rejects_insecure_defaults():
    with pytest.raises(ValidationError) as exc:
        make_settings(environment="production", jwt_secret="change-me-change-me-change-me-change-me")
    msg = str(exc.value)
    for fragment in ("JWT_SECRET", "COOKIE_SECURE", "EMAIL_BACKEND", "CREDENTIALS_ENCRYPTION_KEY", "https"):
        assert fragment in msg


def test_production_accepts_secure_settings():
    s = make_settings(
        environment="production",
        jwt_secret="a8f1c29e7b4d40e6b2f9c1d3e5a7b9c0d2e4f6a8",
        cookie_secure=True,
        email_backend="smtp",
        smtp_host="smtp.example.net",
        credentials_encryption_key=GOOD_KEY,
        frontend_url="https://app.connector.ai",
    )
    assert s.is_production
