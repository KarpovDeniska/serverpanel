"""Fernet encryption for stored credentials."""

import json

from cryptography.fernet import Fernet, InvalidToken

from serverpanel.config import get_settings
from serverpanel.domain.exceptions import EncryptionError


def _get_fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise EncryptionError(
            "ENCRYPTION_KEY not set. Generate with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode())


def encrypt_json(data: dict) -> str:
    """Encrypt a dict to a Fernet token string."""
    plaintext = json.dumps(data).encode()
    return _get_fernet().encrypt(plaintext).decode()


def decrypt_json(token: str) -> dict:
    """Decrypt a Fernet token string back to a dict."""
    try:
        plaintext = _get_fernet().decrypt(token.encode())
        return json.loads(plaintext)
    except InvalidToken as e:
        raise EncryptionError("Failed to decrypt credentials — wrong encryption key?") from e
    except json.JSONDecodeError as e:
        raise EncryptionError("Decrypted data is not valid JSON") from e
