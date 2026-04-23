"""Value objects for byte-level backup progress.

`BackupProgress` is written by the remote `backup.ps1` to a file and later
ingested into `BackupHistory`. Keep this module free of SQLAlchemy / HTTP
concerns — it is pure domain.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass


class InvalidProgressError(ValueError):
    """Raised when a progress payload is structurally invalid."""


@dataclass(frozen=True)
class BackupProgress:
    """A single snapshot of a running backup's byte-level progress.

    Invariants (enforced in `__post_init__`):
        * bytes_total >= 0
        * 0 <= bytes_done <= bytes_total
        * updated_at is timezone-aware UTC
    """

    bytes_total: int
    bytes_done: int
    current_item: str
    updated_at: datetime.datetime

    def __post_init__(self) -> None:
        if self.bytes_total < 0:
            raise InvalidProgressError(f"bytes_total negative: {self.bytes_total}")
        if self.bytes_done < 0:
            raise InvalidProgressError(f"bytes_done negative: {self.bytes_done}")
        if self.bytes_done > self.bytes_total:
            raise InvalidProgressError(
                f"bytes_done {self.bytes_done} > bytes_total {self.bytes_total}"
            )
        if self.updated_at.tzinfo is None:
            raise InvalidProgressError("updated_at must be timezone-aware")

    @property
    def percent(self) -> float:
        if self.bytes_total == 0:
            return 0.0
        return round(100.0 * self.bytes_done / self.bytes_total, 1)

    @classmethod
    def from_json(cls, payload: dict) -> BackupProgress:
        try:
            raw_ts = str(payload["updated_at"])
            # `Z` suffix is valid ISO-8601 but `datetime.fromisoformat` only
            # accepts it on Python 3.11+. Normalise to `+00:00` for safety.
            ts = datetime.datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            return cls(
                bytes_total=int(payload["bytes_total"]),
                bytes_done=int(payload["bytes_done"]),
                current_item=str(payload["current_item"]),
                updated_at=ts,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise InvalidProgressError(f"bad progress payload: {e}") from e


def is_stalled(
    progress: BackupProgress | None,
    *,
    now: datetime.datetime,
    threshold_seconds: int,
) -> bool:
    """Pure function: decide whether a running backup is stalled.

    `progress is None` → not stalled (tracker has not written its first tick
    yet; caller must decide what to show — usually "preparing…").
    """
    if progress is None:
        return False
    if threshold_seconds <= 0:
        raise ValueError("threshold_seconds must be positive")
    age = (now - progress.updated_at).total_seconds()
    return age > threshold_seconds
