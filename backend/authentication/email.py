"""Envio de emails transacionais (verificação e recuperação de palavra-passe)."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage as _MimeMessage
from urllib.parse import quote

log = logging.getLogger("connector_ai.email")


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    text: str
    kind: str
    link: str | None = None


class EmailSender:
    async def send(self, message: OutgoingEmail) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleEmailSender(EmailSender):
    """Desenvolvimento: o email (com o link) aparece no log do backend."""

    async def send(self, message: OutgoingEmail) -> None:
        log.info("email (consola)", extra={"fields": {"to": message.to, "subject": message.subject, "link": message.link}})


@dataclass
class MemoryEmailSender(EmailSender):
    """Testes: guarda os emails enviados."""

    outbox: list[OutgoingEmail] = field(default_factory=list)

    async def send(self, message: OutgoingEmail) -> None:
        self.outbox.append(message)


class SmtpEmailSender(EmailSender):
    def __init__(self, host: str, port: int, username: str | None, password: str | None,
                 starttls: bool, sender: str) -> None:
        self._host, self._port, self._user, self._pwd = host, port, username, password
        self._starttls, self._from = starttls, sender

    def _send_sync(self, message: OutgoingEmail) -> None:
        mime = _MimeMessage()
        mime["From"], mime["To"], mime["Subject"] = self._from, message.to, message.subject
        mime.set_content(message.text)
        with smtplib.SMTP(self._host, self._port, timeout=15) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._user and self._pwd:
                smtp.login(self._user, self._pwd)
            smtp.send_message(mime)

    async def send(self, message: OutgoingEmail) -> None:
        await asyncio.to_thread(self._send_sync, message)


def verification_email(to: str, token: str, frontend_url: str, app_name: str) -> OutgoingEmail:
    link = f"{frontend_url}/verify-email?token={quote(token)}"
    text = (
        f"Welcome to {app_name}.\n\nConfirm your email address by opening this link:\n{link}\n\n"
        "If you did not create this account, ignore this email."
    )
    return OutgoingEmail(to=to, subject=f"Confirm your email · {app_name}", text=text, kind="email_verification", link=link)


def password_reset_email(to: str, token: str, frontend_url: str, app_name: str) -> OutgoingEmail:
    link = f"{frontend_url}/reset-password?token={quote(token)}"
    text = (
        f"A password reset was requested for your {app_name} account.\n\n"
        f"Choose a new password here (valid for a limited time):\n{link}\n\n"
        "If you did not ask for this, ignore this email; your password stays the same."
    )
    return OutgoingEmail(to=to, subject=f"Reset your password · {app_name}", text=text, kind="password_reset", link=link)
