from __future__ import annotations

import base64
import os

import pytest

from security.crypto import CredentialCipher, DecryptionError


def _cipher():
    return CredentialCipher.from_base64(base64.b64encode(os.urandom(32)).decode(), "k1")


def test_roundtrip_and_no_plaintext():
    c = _cipher()
    blob = c.encrypt(b"oauth-refresh-token-value", user_id="u1", credential_id="c1")
    assert b"oauth-refresh-token-value" not in blob.ciphertext
    assert len(blob.nonce) == 12
    assert c.decrypt(blob, user_id="u1", credential_id="c1") == b"oauth-refresh-token-value"


def test_ciphertext_bound_to_owner():
    c = _cipher()
    blob = c.encrypt(b"secret", user_id="u1", credential_id="c1")
    with pytest.raises(DecryptionError):
        c.decrypt(blob, user_id="u2", credential_id="c1")


def test_tampering_detected():
    c = _cipher()
    blob = c.encrypt(b"secret", user_id="u1", credential_id="c1")
    tampered = type(blob)(blob.key_id, blob.nonce, bytes([blob.ciphertext[0] ^ 1]) + blob.ciphertext[1:])
    with pytest.raises(DecryptionError):
        c.decrypt(tampered, user_id="u1", credential_id="c1")
