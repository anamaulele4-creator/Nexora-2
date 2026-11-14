"""Autenticação própria (AUTH_PROVIDER=local).

* Palavras-passe com argon2id; bloqueio temporário após 5 falhas seguidas.
* Access token JWT curto (15 min) + refresh token opaco guardado só como hash SHA-256.
* Rotação do refresh token a cada uso; reutilizar um token já rodado revoga todas as
  sessões do utilizador (deteção de roubo).
* Cada pedido confirma que a sessão continua ativa -> revogação imediata.
* Tokens de verificação de email e de recuperação: aleatórios, uso único, só hash na BD.
* Respostas que não revelam se um email existe (recuperação e reenvio).
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg

from authentication.accounts import create_account
from authentication.email import EmailSender, password_reset_email, verification_email
from authentication.passwords import hash_password, password_problems, verify_password
from authentication.providers.base import (
    AuthError,
    AuthProviderBase,
    ClientInfo,
    RegistrationResult,
    TokenPair,
    unauthorized,
)
from authentication.tokens import (
    InvalidToken,
    Principal,
    decode_access_token,
    issue_access_token,
    new_opaque_token,
    sha256_hex,
)
from core.config import Settings
from core.db import Database
from security.audit import record_audit

log = logging.getLogger("connector_ai.auth")

MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15


def _ip(client: ClientInfo) -> str | None:
    try:
        return str(ipaddress.ip_address(client.ip)) if client.ip else None
    except ValueError:
        return None


class LocalAuthProvider(AuthProviderBase):
    name = "local"

    def __init__(self, db: Database, settings: Settings, email_sender: EmailSender) -> None:
        self.db = db
        self.settings = settings
        self.email = email_sender

    # -- auxiliares ---------------------------------------------------------------
    async def _issue_one_time_token(self, conn: asyncpg.Connection, user_id: uuid.UUID, purpose: str, ttl: int) -> str:
        # um só token válido por finalidade: invalida os anteriores
        await conn.execute(
            "update local_auth.one_time_tokens set used_at = now() where user_id = $1 and purpose = $2 and used_at is null",
            user_id, purpose,
        )
        token = new_opaque_token()
        await conn.execute(
            """insert into local_auth.one_time_tokens (user_id, purpose, token_hash, expires_at)
               values ($1, $2, $3, now() + make_interval(secs => $4))""",
            user_id, purpose, sha256_hex(token), ttl,
        )
        return token

    async def _consume_one_time_token(self, conn: asyncpg.Connection, token: str, purpose: str) -> uuid.UUID:
        row = await conn.fetchrow(
            """update local_auth.one_time_tokens set used_at = now()
               where token_hash = $1 and purpose = $2 and used_at is null and expires_at > now()
               returning user_id""",
            sha256_hex(token), purpose,
        )
        if row is None:
            raise AuthError(400, "invalid_or_expired_token", "Link inválido ou expirado")
        return row["user_id"]

    async def _create_session(self, conn: asyncpg.Connection, user_id: uuid.UUID, email: str, client: ClientInfo) -> TokenPair:
        refresh = new_opaque_token()
        session_id = await conn.fetchval(
            """insert into local_auth.sessions (user_id, refresh_token_hash, expires_at, ip, user_agent)
               values ($1, $2, now() + make_interval(secs => $3), $4::inet, $5) returning id""",
            user_id, sha256_hex(refresh), self.settings.refresh_token_ttl_seconds, _ip(client),
            (client.user_agent or "")[:500] or None,
        )
        access = issue_access_token(
            user_id=user_id, email=email, session_id=session_id,
            secret=self.settings.jwt_secret.get_secret_value(), issuer=self.settings.jwt_issuer,
            ttl_seconds=self.settings.access_token_ttl_seconds,
        )
        return TokenPair(access, refresh, self.settings.access_token_ttl_seconds, user_id)

    # -- contrato -------------------------------------------------------------------
    async def register(self, email: str, password: str, display_name: str | None, client: ClientInfo) -> RegistrationResult:
        problems = password_problems(password)
        if problems:
            raise AuthError(422, "weak_password", "Palavra-passe fraca: " + "; ".join(problems))
        pw_hash = hash_password(password)
        try:
            async with self.db.service() as conn:
                user_id = await create_account(conn, email=email, display_name=display_name,
                                               email_verified=not self.settings.require_email_verification)
                await conn.execute("insert into local_auth.credentials (user_id, password_hash) values ($1, $2)", user_id, pw_hash)
                token = None
                if self.settings.require_email_verification:
                    token = await self._issue_one_time_token(conn, user_id, "email_verification",
                                                             self.settings.email_verification_ttl_seconds)
                await record_audit(conn, "auth.register", user_id=user_id, ip=_ip(client), user_agent=client.user_agent)
        except asyncpg.UniqueViolationError as exc:
            raise AuthError(409, "email_taken", "Já existe uma conta com este email") from exc
        if token:
            await self.email.send(verification_email(email, token, self.settings.frontend_url, self.settings.app_name))
        return RegistrationResult(user_id, self.settings.require_email_verification)

    async def login(self, email: str, password: str, client: ClientInfo) -> TokenPair:
        # Os erros são lançados DEPOIS do commit, para que contadores de falhas e
        # auditoria fiquem gravados mesmo quando o login é recusado.
        failure: AuthError | None = None
        pair: TokenPair | None = None
        async with self.db.service() as conn:
            row = await conn.fetchrow(
                """select u.id, u.email, u.status, u.email_verified_at, c.password_hash, c.failed_attempts, c.locked_until
                   from public.users u left join local_auth.credentials c on c.user_id = u.id
                   where u.email = $1 for update of u""",
                email,
            )
            now = datetime.now(UTC)
            if row and row["locked_until"] and row["locked_until"] > now:
                await record_audit(conn, "auth.login_blocked", user_id=row["id"], ip=_ip(client), user_agent=client.user_agent)
                failure = AuthError(423, "account_locked", "Demasiadas tentativas. Tenta de novo mais tarde.")
            elif not verify_password(row["password_hash"] if row else None, password):
                if row and row["password_hash"]:
                    attempts = row["failed_attempts"] + 1
                    lock = now + timedelta(minutes=LOCK_MINUTES) if attempts >= MAX_FAILED_ATTEMPTS else None
                    await conn.execute(
                        "update local_auth.credentials set failed_attempts = $2, locked_until = $3 where user_id = $1",
                        row["id"], 0 if lock else attempts, lock,
                    )
                await record_audit(conn, "auth.login_failed", user_id=row["id"] if row else None,
                                   actor_type="user" if row else "anonymous", ip=_ip(client),
                                   user_agent=client.user_agent)
                failure = AuthError(401, "invalid_credentials", "Email ou palavra-passe incorretos")
            elif row["status"] != "active":
                failure = AuthError(403, "account_disabled", "Esta conta está desativada")
            elif self.settings.require_email_verification and row["email_verified_at"] is None:
                failure = AuthError(403, "email_not_verified", "Confirma o teu email antes de entrar")
            else:
                await conn.execute(
                    "update local_auth.credentials set failed_attempts = 0, locked_until = null where user_id = $1", row["id"])
                await conn.execute("update public.users set last_login_at = now() where id = $1", row["id"])
                pair = await self._create_session(conn, row["id"], row["email"], client)
                await record_audit(conn, "auth.login", user_id=row["id"], ip=_ip(client), user_agent=client.user_agent)
        if failure is not None:
            raise failure
        assert pair is not None
        return pair

    async def refresh(self, refresh_token: str, client: ClientInfo) -> TokenPair:
        reuse_detected = False
        async with self.db.service() as conn:
            row = await conn.fetchrow(
                """select s.id, s.user_id, s.revoked_at, s.expires_at, u.email, u.status
                   from local_auth.sessions s join public.users u on u.id = s.user_id
                   where s.refresh_token_hash = $1 for update of s""",
                sha256_hex(refresh_token),
            )
            if row is None:
                raise unauthorized()
            if row["revoked_at"] is not None:
                # token já rodado/revogado a ser reutilizado: possível roubo -> fecha todas as sessões
                await conn.execute(
                    """update local_auth.sessions set revoked_at = now(), revoked_reason = 'refresh_reuse'
                       where user_id = $1 and revoked_at is null""", row["user_id"])
                await record_audit(conn, "auth.refresh_reuse_detected", user_id=row["user_id"], ip=_ip(client),
                                   user_agent=client.user_agent)
                reuse_detected = True
            elif row["expires_at"] <= datetime.now(UTC) or row["status"] != "active":
                raise unauthorized()
            else:
                await conn.execute(
                    "update local_auth.sessions set revoked_at = now(), revoked_reason = 'rotated' where id = $1", row["id"])
                return await self._create_session(conn, row["user_id"], row["email"], client)
        assert reuse_detected
        raise unauthorized()

    async def logout(self, principal: Principal | None, refresh_token: str | None, client: ClientInfo) -> None:
        async with self.db.service() as conn:
            user_id = principal.user_id if principal else None
            if principal and principal.session_id:
                await conn.execute(
                    """update local_auth.sessions set revoked_at = now(), revoked_reason = 'logout'
                       where id = $1 and revoked_at is null""", principal.session_id)
            if refresh_token:
                uid = await conn.fetchval(
                    """update local_auth.sessions set revoked_at = now(), revoked_reason = 'logout'
                       where refresh_token_hash = $1 and revoked_at is null returning user_id""", sha256_hex(refresh_token))
                user_id = user_id or uid
            if user_id:
                await record_audit(conn, "auth.logout", user_id=user_id, ip=_ip(client), user_agent=client.user_agent)

    async def verify_email(self, token: str, client: ClientInfo) -> None:
        async with self.db.service() as conn:
            user_id = await self._consume_one_time_token(conn, token, "email_verification")
            await conn.execute(
                "update public.users set email_verified_at = coalesce(email_verified_at, now()) where id = $1", user_id)
            await record_audit(conn, "auth.email_verified", user_id=user_id, ip=_ip(client), user_agent=client.user_agent)

    async def resend_verification(self, email: str, client: ClientInfo) -> None:
        async with self.db.service() as conn:
            row = await conn.fetchrow(
                "select id, email from public.users where email = $1 and email_verified_at is null and status = 'active'", email)
            if row is None:
                return  # não revela se o email existe
            token = await self._issue_one_time_token(conn, row["id"], "email_verification",
                                                     self.settings.email_verification_ttl_seconds)
        await self.email.send(verification_email(row["email"], token, self.settings.frontend_url, self.settings.app_name))

    async def request_password_reset(self, email: str, client: ClientInfo) -> None:
        async with self.db.service() as conn:
            row = await conn.fetchrow("select id, email from public.users where email = $1 and status = 'active'", email)
            if row is None:
                await record_audit(conn, "auth.password_reset_unknown_email", actor_type="anonymous", ip=_ip(client),
                                   user_agent=client.user_agent)
                return
            token = await self._issue_one_time_token(conn, row["id"], "password_reset",
                                                     self.settings.password_reset_ttl_seconds)
            await record_audit(conn, "auth.password_reset_requested", user_id=row["id"], ip=_ip(client),
                               user_agent=client.user_agent)
        await self.email.send(password_reset_email(row["email"], token, self.settings.frontend_url, self.settings.app_name))

    async def reset_password(self, token: str, new_password: str, client: ClientInfo) -> None:
        problems = password_problems(new_password)
        if problems:
            raise AuthError(422, "weak_password", "Palavra-passe fraca: " + "; ".join(problems))
        pw_hash = hash_password(new_password)
        async with self.db.service() as conn:
            user_id = await self._consume_one_time_token(conn, token, "password_reset")
            await conn.execute(
                """insert into local_auth.credentials (user_id, password_hash) values ($1, $2)
                   on conflict (user_id) do update set password_hash = excluded.password_hash,
                       password_updated_at = now(), failed_attempts = 0, locked_until = null""",
                user_id, pw_hash,
            )
            # quem consegue abrir o link controla o email: conta como verificado
            await conn.execute(
                "update public.users set email_verified_at = coalesce(email_verified_at, now()) where id = $1", user_id)
            await conn.execute(
                """update local_auth.sessions set revoked_at = now(), revoked_reason = 'password_reset'
                   where user_id = $1 and revoked_at is null""", user_id)
            await record_audit(conn, "auth.password_reset", user_id=user_id, ip=_ip(client), user_agent=client.user_agent)

    async def authenticate(self, access_token: str) -> Principal:
        try:
            claims = decode_access_token(access_token, secret=self.settings.jwt_secret.get_secret_value(),
                                         issuer=self.settings.jwt_issuer)
            user_id = uuid.UUID(claims["sub"])
            session_id = uuid.UUID(claims["sid"])
        except (InvalidToken, KeyError, ValueError) as exc:
            raise unauthorized() from exc
        async with self.db.service() as conn:
            active = await conn.fetchval(
                """select true from local_auth.sessions s join public.users u on u.id = s.user_id
                   where s.id = $1 and s.user_id = $2 and s.revoked_at is null and s.expires_at > now()
                     and u.status = 'active'""",
                session_id, user_id,
            )
        if not active:
            raise unauthorized()
        return Principal(user_id=user_id, email=claims.get("email", ""), session_id=session_id)
