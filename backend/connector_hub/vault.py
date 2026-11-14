"""Ligações de conectores e o cofre dos seus tokens.

* ``connected_apps``: uma linha por utilizador e conector (estado, conta externa, metadados).
* ``encrypted_credentials``: o token, cifrado com o ``CredentialCipher`` existente
  (AES-256-GCM, CREDENTIALS_ENCRYPTION_KEY). O AAD liga o texto cifrado ao dono e ao id da
  credencial: copiado para outro utilizador ou outra linha, não decifra.
* Desligar apaga a credencial (o texto cifrado deixa de existir) e marca a ligação como revogada.

Estas funções correm em modo serviço (``db.service()``): ``encrypted_credentials`` não tem
acesso para o papel authenticated. Todas filtram explicitamente por ``user_id``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from security.crypto import CredentialCipher, DecryptionError, EncryptedBlob


class VaultError(Exception):
    pass


@dataclass(frozen=True)
class StoredConnection:
    id: uuid.UUID
    status: str
    credential_id: uuid.UUID | None
    payload: dict | None


def _credential_aad_id(credential_id: uuid.UUID) -> str:
    return f"connector_credential:{credential_id}"


def encrypt_payload(cipher: CredentialCipher, user_id: uuid.UUID, credential_id: uuid.UUID, payload: dict) -> EncryptedBlob:
    return cipher.encrypt(json.dumps(payload).encode("utf-8"), user_id=str(user_id),
                          credential_id=_credential_aad_id(credential_id))


def decrypt_payload(cipher: CredentialCipher, user_id: uuid.UUID, credential_id: uuid.UUID, *, key_id: str,
                    nonce: bytes, ciphertext: bytes) -> dict:
    blob = EncryptedBlob(key_id=key_id, nonce=bytes(nonce), ciphertext=bytes(ciphertext))
    raw = cipher.decrypt(blob, user_id=str(user_id), credential_id=_credential_aad_id(credential_id))
    return json.loads(raw.decode("utf-8"))


async def default_workspace(conn: asyncpg.Connection, user_id: uuid.UUID) -> uuid.UUID:
    ws = await conn.fetchval("select default_workspace_id from public.user_profiles where user_id = $1", user_id)
    if ws is None:
        ws = await conn.fetchval(
            "select id from public.workspaces where owner_user_id = $1 and deleted_at is null order by created_at limit 1",
            user_id)
    if ws is None:
        raise VaultError("utilizador sem workspace")
    return ws


async def save_connection(conn: asyncpg.Connection, cipher: CredentialCipher, *, user_id: uuid.UUID,
                          connector_key: str, display_name: str, account_id: str, account_label: str,
                          credential_type: str, payload: dict, expires_at: datetime | None,
                          metadata: dict) -> uuid.UUID:
    """Cria ou atualiza a ligação e substitui a credencial cifrada. Devolve o id da ligação."""
    definition_id = await conn.fetchval("select id from public.connector_definitions where key = $1", connector_key)
    if definition_id is None:
        raise VaultError(f"conector desconhecido: {connector_key}")
    ws = await default_workspace(conn, user_id)
    app_id = await conn.fetchval(
        """insert into public.connected_apps (user_id, workspace_id, connector_definition_id, display_name, status,
                                              external_account_id, external_account_label, last_health_check_at,
                                              last_health_status, revoked_at, metadata)
           values ($1, $2, $3, $4, 'connected', $5, $6, now(), 'healthy', null, $7)
           on conflict (user_id, connector_definition_id) do update set
             workspace_id = excluded.workspace_id, display_name = excluded.display_name, status = 'connected',
             external_account_id = excluded.external_account_id,
             external_account_label = excluded.external_account_label,
             last_health_check_at = now(), last_health_status = 'healthy', revoked_at = null,
             metadata = excluded.metadata
           returning id""",
        user_id, ws, definition_id, display_name[:200], account_id[:200], account_label[:200], metadata)
    # uma só credencial ativa por ligação: a anterior é apagada (crypto-shredding)
    await conn.execute("delete from public.encrypted_credentials where connected_app_id = $1 and user_id = $2",
                       app_id, user_id)
    cred_id = uuid.uuid4()
    blob = encrypt_payload(cipher, user_id, cred_id, payload)
    await conn.execute(
        """insert into public.encrypted_credentials (id, user_id, workspace_id, connected_app_id, credential_type,
                                                     key_id, nonce, ciphertext, expires_at)
           values ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
        cred_id, user_id, ws, app_id, credential_type, blob.key_id, blob.nonce, blob.ciphertext, expires_at)
    return app_id


async def load_connection(conn: asyncpg.Connection, cipher: CredentialCipher | None, *, user_id: uuid.UUID,
                          connector_key: str) -> StoredConnection | None:
    row = await conn.fetchrow(
        """select a.id, a.status, c.id as cred_id, c.key_id, c.nonce, c.ciphertext
           from public.connected_apps a
           join public.connector_definitions d on d.id = a.connector_definition_id
           left join public.encrypted_credentials c on c.connected_app_id = a.id and c.user_id = a.user_id
                                                    and c.revoked_at is null
           where a.user_id = $1 and d.key = $2
           order by c.created_at desc nulls last limit 1""",
        user_id, connector_key)
    if row is None:
        return None
    payload = None
    if row["cred_id"] is not None and cipher is not None:
        try:
            payload = decrypt_payload(cipher, user_id, row["cred_id"], key_id=row["key_id"], nonce=row["nonce"],
                                      ciphertext=row["ciphertext"])
        except DecryptionError:
            payload = None
    return StoredConnection(id=row["id"], status=row["status"], credential_id=row["cred_id"], payload=payload)


async def update_payload(conn: asyncpg.Connection, cipher: CredentialCipher, *, user_id: uuid.UUID,
                         credential_id: uuid.UUID, payload: dict, expires_at: datetime | None) -> None:
    """Grava um token renovado (mesma credencial, novo nonce)."""
    blob = encrypt_payload(cipher, user_id, credential_id, payload)
    await conn.execute(
        """update public.encrypted_credentials set key_id = $3, nonce = $4, ciphertext = $5, expires_at = $6,
                  rotated_at = now()
           where id = $1 and user_id = $2""",
        credential_id, user_id, blob.key_id, blob.nonce, blob.ciphertext, expires_at)


async def record_health(conn: asyncpg.Connection, *, user_id: uuid.UUID, app_id: uuid.UUID, ok: bool,
                        status: str | None = None) -> None:
    await conn.execute(
        """update public.connected_apps set last_health_check_at = now(),
                  last_health_status = case when $3 then 'healthy' else 'unhealthy' end,
                  status = coalesce($4, status)
           where id = $1 and user_id = $2""",
        app_id, user_id, ok, status)


async def delete_connection(conn: asyncpg.Connection, *, user_id: uuid.UUID, app_id: uuid.UUID) -> None:
    await conn.execute("delete from public.encrypted_credentials where connected_app_id = $1 and user_id = $2",
                       app_id, user_id)
    await conn.execute(
        """update public.connected_apps set status = 'revoked', revoked_at = now(), last_health_status = null
           where id = $1 and user_id = $2""", app_id, user_id)


def epoch_to_dt(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value else None
