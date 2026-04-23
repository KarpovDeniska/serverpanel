"""Application settings from environment variables."""

from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent

_INSECURE_SECRET_KEY = "CHANGE-ME-IN-PRODUCTION"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # App
    app_name: str = "ServerPanel"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 5000
    secret_key: str = _INSECURE_SECRET_KEY
    language: str = "ru"  # "ru" | "en" — used by domain.i18n.t()

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/serverpanel.db"

    # Auth
    session_lifetime_hours: int = 24
    session_cookie_secure: bool = False  # set True behind HTTPS
    session_cookie_samesite: str = "lax"

    # Encryption key for stored credentials (Fernet, base64-encoded 32 bytes)
    encryption_key: str = ""

    # Operations
    stale_run_timeout_minutes: int = 360  # sweep 'running' rows older than this on startup
    ssh_connect_timeout: float = 30.0
    ssh_command_timeout: float = 300.0

    # Long-running phase timeouts (seconds) — exposed so operators can tune for slow networks
    install_installimage_timeout: float = 900.0
    install_package_timeout: float = 300.0
    install_wait_ssh_timeout: float = 300.0
    install_post_reboot_sleep: float = 20.0
    recovery_iso_download_timeout: float = 1800.0
    recovery_wimapply_timeout: float = 1200.0
    recovery_poll_status_timeout: float = 3600.0
    recovery_wait_rescue_timeout: float = 600.0
    recovery_poll_interval: float = 15.0
    backup_run_timeout: float = 3 * 3600.0
    ssh_wait_interval: float = 10.0

    # OAuth (future)
    google_client_id: str = ""
    google_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""

    # Notifications — sent from inside backup.ps1 on the target server.
    # Empty = no alerts (silent). Token format: "123456:ABC-DEF...".
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Backup zip compression level — "fastest" | "optimal".
    # Fastest is ~2-3x faster at the cost of ~15-20% larger archives; for
    # 1C file-based DB (binary .1CD) the ratio is still ~55-60% of original.
    # Optimal is only worth it if storage space is a hard constraint.
    backup_zip_level: str = "fastest"

    # Background sync of scheduled-run reports from target servers into
    # BackupHistory. 0 = disabled (manual Sync-now button still works).
    backup_sync_interval_seconds: int = 15 * 60

    @model_validator(mode="after")
    def _validate_secrets(self) -> "Settings":
        if self.debug:
            return self
        if self.secret_key == _INSECURE_SECRET_KEY or not self.secret_key:
            raise ValueError(
                "SECRET_KEY must be set to a strong value in production (debug=False). "
                "Generate with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        if not self.encryption_key:
            raise ValueError(
                "ENCRYPTION_KEY must be set in production (debug=False). "
                "Generate with: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        # Fernet() parses the key and raises ValueError on malformed input
        # (wrong length, non-base64 chars, trailing whitespace). Catching it
        # at startup surfaces .env typos immediately instead of at the first
        # decrypt_json call — where the same error looks like "credentials
        # corrupted" in the UI and wastes time.
        try:
            Fernet(self.encryption_key.encode())
        except ValueError as e:
            raise ValueError(
                f"ENCRYPTION_KEY is not a valid Fernet key ({e}). "
                "Must be urlsafe-base64 of 32 bytes. Generate with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from e
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
