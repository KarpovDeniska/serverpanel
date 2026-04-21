"""Fernet round-trip and error handling."""

import pytest

from serverpanel.domain.exceptions import EncryptionError
from serverpanel.infrastructure.crypto import decrypt_json, encrypt_json


def test_roundtrip_simple_dict():
    payload = {"user": "alice", "password": "hunter2"}
    token = encrypt_json(payload)
    assert isinstance(token, str)
    assert "alice" not in token  # ciphertext not plain
    assert decrypt_json(token) == payload


def test_roundtrip_unicode():
    payload = {"name": "кириллица", "emoji": "🔐"}
    assert decrypt_json(encrypt_json(payload)) == payload


def test_decrypt_invalid_token():
    with pytest.raises(EncryptionError):
        decrypt_json("not-a-valid-fernet-token")
