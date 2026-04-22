"""Self-backup: packs .env + data/serverpanel.db + ~/.ssh/serverpanel-seed/ into a tar.gz.

Stream-friendly: writes directly to a provided binary file handle so the caller
can send it to the browser as a download.
"""

from __future__ import annotations

import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from serverpanel.config import get_settings


class SelfBackupError(RuntimeError):
    pass


def _sqlite_path_from_url(url: str) -> Path | None:
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return None
    rest = url[len(prefix):]
    if rest.startswith("/"):
        return Path(rest)
    return Path.cwd() / rest


def suggested_filename() -> str:
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M")
    return f"serverpanel-backup-{stamp}.tar.gz"


def write_self_backup(fileobj: BinaryIO) -> list[str]:
    """Write tar.gz of app state into `fileobj`. Returns list of arcnames included."""
    cfg = get_settings()
    env_path = Path.cwd() / ".env"
    db_path = _sqlite_path_from_url(cfg.database_url)
    seed_dir = Path.home() / ".ssh/serverpanel-seed"

    included: list[tuple[Path, str]] = []
    if env_path.is_file():
        included.append((env_path, "projects/serverpanel/.env"))
    if db_path and db_path.is_file():
        included.append((db_path, "projects/serverpanel/data/serverpanel.db"))
    if seed_dir.is_dir():
        included.append((seed_dir, ".ssh/serverpanel-seed"))

    if not included:
        raise SelfBackupError(
            "Nothing to back up: .env, data/serverpanel.db, ~/.ssh/serverpanel-seed all missing"
        )

    with tarfile.open(fileobj=fileobj, mode="w:gz") as tar:
        for src, arcname in included:
            tar.add(src, arcname=arcname)

    return [arc for _, arc in included]
