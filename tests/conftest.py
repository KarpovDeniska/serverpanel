"""Shared pytest fixtures.

Ensure DEBUG=true so Settings validators don't block test runs on an
unset production SECRET_KEY.
"""

import os

os.environ.setdefault("DEBUG", "true")
os.environ.setdefault(
    "ENCRYPTION_KEY",
    # Deterministic test key — never use in production.
    "m9x2p3MqV3IY0vZvS1ZAAabSwyh1C4Md8zZKHSv_r64=",
)
os.environ.setdefault("SECRET_KEY", "test-secret-not-for-production")
