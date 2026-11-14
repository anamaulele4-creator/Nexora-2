"""Cifra de credenciais de conectores (AES-256-GCM).

O texto cifrado fica ligado ao dono e ao registo através de dados associados (AAD):
um ciphertext copiado para outro utilizador ou outra credencial não decifra.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class DecryptionError(Exception):
    pass


@dataclass(frozen=True)
class EncryptedBlob:
    key_id: str
    nonce: bytes
    ciphertext: bytes
    algorithm: str = "AES-256-GCM"


class CredentialCipher:
    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None:
        if active_key_id not in keys:
            raise ValueError("chave ativa desconhecida")
        for kid, key in keys.items():
            if len(key) != 32:
                raise ValueError(f"chave {kid} não tem 32 bytes")
        self._keys = keys
        self._active = active_key_id

    @classmethod
    def from_base64(cls, key_b64: str, key_id: str) -> "CredentialCipher":
        return cls({key_id: base64.b64decode(key_b64)}, key_id)

    @staticmethod
    def _aad(user_id: str, credential_id: str) -> bytes:
        return f"connector-ai:v1:{user_id}:{credential_id}".encode()

    def encrypt(self, plaintext: bytes, *, user_id: str, credential_id: str) -> EncryptedBlob:
        nonce = os.urandom(12)
        ct = AESGCM(self._keys[self._active]).encrypt(nonce, plaintext, self._aad(user_id, credential_id))
        return EncryptedBlob(key_id=self._active, nonce=nonce, ciphertext=ct)

    def decrypt(self, blob: EncryptedBlob, *, user_id: str, credential_id: str) -> bytes:
        key = self._keys.get(blob.key_id)
        if key is None:
            raise DecryptionError("chave desconhecida")
        try:
            return AESGCM(key).decrypt(blob.nonce, blob.ciphertext, self._aad(user_id, credential_id))
        except InvalidTag as exc:
            raise DecryptionError("credencial inválida ou adulterada") from exc
