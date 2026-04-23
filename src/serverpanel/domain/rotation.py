"""Rotation of dated backup folders.

Mirrors the logic in ``backup.ps1`` — local rotation (lines 283-293) and
remote rotation (lines 588-598). Kept here as a single Python source of
truth that the test suite pins down; any change to the PowerShell side
must be reflected here and the tests updated accordingly.

The contract is intentionally lenient on the input shape: local rotation
passes raw basenames (``Get-ChildItem`` ``.Name``), remote rotation
passes slash-separated paths (``sftp ls -1`` returns
``backups/daily/2026-04-04``). A single ``select_expired`` handles both
because remote listings used to be filtered by a regex that expected
basenames — and silently matched nothing on full paths, leaving rotation
a no-op from day one. That bug is the reason this module exists.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def compute_cutoff(today: _dt.date, rotation_days: int) -> str:
    """Return the cutoff date as ``YYYY-MM-DD``. Entries strictly less than
    this string are expired.
    """
    return (today - _dt.timedelta(days=rotation_days)).strftime("%Y-%m-%d")


def select_expired(names: Iterable[str | None], cutoff: str) -> list[str]:
    """Return basenames from ``names`` that look like ``YYYY-MM-DD`` and are
    strictly older than ``cutoff``.

    Accepts basenames and slash/backslash-separated paths interchangeably;
    blanks, ``None`` and non-date entries are silently ignored.
    """
    expired: list[str] = []
    for raw in names:
        if not raw:
            continue
        basename = raw.strip().replace("\\", "/").split("/")[-1]
        if not _DATE_RE.match(basename):
            continue
        if basename >= cutoff:
            continue
        expired.append(basename)
    return expired
