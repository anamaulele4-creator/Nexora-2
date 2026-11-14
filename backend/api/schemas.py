from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegisterIn(_In):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=120)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class LoginIn(_In):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class EmailIn(_In):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class TokenIn(_In):
    token: str = Field(min_length=16, max_length=512)


class RefreshIn(_In):
    refresh_token: str | None = Field(default=None, min_length=16, max_length=512)


class ResetPasswordIn(_In):
    token: str = Field(min_length=16, max_length=512)
    new_password: str = Field(min_length=1, max_length=256)


class RegisterOut(BaseModel):
    user_id: uuid.UUID
    verification_required: bool


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    plan: str


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    email_verified: bool
    is_admin: bool
    display_name: str | None
    locale: str
    timezone: str
    theme: str
    created_at: datetime
    default_workspace: WorkspaceOut | None


class SessionOut(BaseModel):
    expires_in: int
    user: MeOut
    access_token: str | None = None
    refresh_token: str | None = None
