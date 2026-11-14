"""Chaves dos fornecedores de IA: cifra com o CredentialCipher existente e máscara.

O texto cifrado fica ligado ao fornecedor pelos dados associados (AAD): a chave da
OpenAI copiada para a linha do Gemini não decifra.
"""

from __future__ import annotations

from security.crypto import CredentialCipher, EncryptedBlob

_SYSTEM_OWNER = "system"


def _credential_id(provider: str) -> str:
    return f"ai_provider:{provider}"


def encrypt_api_key(cipher: CredentialCipher, provider: str, api_key: str) -> EncryptedBlob:
    return cipher.encrypt(api_key.encode("utf-8"), user_id=_SYSTEM_OWNER, credential_id=_credential_id(provider))


def decrypt_api_key(cipher: CredentialCipher, provider: str, *, key_id: str, nonce: bytes, ciphertext: bytes) -> str:
    blob = EncryptedBlob(key_id=key_id, nonce=bytes(nonce), ciphertext=bytes(ciphertext))
    return cipher.decrypt(blob, user_id=_SYSTEM_OWNER, credential_id=_credential_id(provider)).decode("utf-8")


def last4(api_key: str) -> str:
    return api_key[-4:] if len(api_key) >= 8 else ""


def mask(last_four: str | None) -> str | None:
    """Representação segura para mostrar: só os últimos 4 caracteres."""
    if last_four is None:
        return None
    return "••••" + last_four if last_four else "••••"


def build_cipher(key_b64: str | None, key_id: str) -> CredentialCipher | None:
    if not key_b64:
        return None
    return CredentialCipher.from_base64(key_b64, key_id)
