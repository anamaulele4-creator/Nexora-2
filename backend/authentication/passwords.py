from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id com parâmetros recomendados pela biblioteca
_DUMMY_HASH = _hasher.hash("connector-ai-dummy-password")

MIN_LENGTH = 10
MAX_LENGTH = 128


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append(f"mínimo {MIN_LENGTH} caracteres")
    if len(password) > MAX_LENGTH:
        problems.append(f"máximo {MAX_LENGTH} caracteres")
    if password.lower() == password or password.upper() == password:
        problems.append("usa maiúsculas e minúsculas")
    if not any(c.isdigit() for c in password):
        problems.append("inclui pelo menos um número")
    return problems


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Verifica em tempo semelhante exista ou não o utilizador."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
