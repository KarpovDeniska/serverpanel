"""Settings validators must fail fast in production mode."""

import pytest

from serverpanel.config import Settings


def test_prod_requires_secret_key():
    # DEBUG=False, SECRET_KEY=default sentinel → must raise.
    with pytest.raises(Exception):
        Settings(
            _env_file=None,
            debug=False,
            secret_key="CHANGE-ME-IN-PRODUCTION",
            encryption_key="a" * 44,
        )


def test_prod_requires_encryption_key():
    with pytest.raises(Exception):
        Settings(
            _env_file=None,
            debug=False,
            secret_key="a-strong-secret-key",
            encryption_key="",
        )


def test_prod_ok_when_both_set():
    s = Settings(
        _env_file=None,
        debug=False,
        secret_key="a-strong-secret-key",
        encryption_key="a" * 44,
    )
    assert s.debug is False


def test_debug_mode_allows_defaults():
    s = Settings(
        _env_file=None,
        debug=True,
        secret_key="CHANGE-ME-IN-PRODUCTION",
        encryption_key="",
    )
    assert s.debug is True
