"""Rotation algorithm — pins down the contract that `backup.ps1` relies on.

Each test maps to a concrete bug class (past or hypothetical) to prevent
regressions like the day-one "silent no-op" where sftp `ls -1` returned
full paths and the regex never matched.
"""

import datetime as dt

import pytest

from serverpanel.domain.rotation import compute_cutoff, select_expired


def test_cutoff_is_today_minus_rotation_days():
    assert compute_cutoff(dt.date(2026, 4, 23), 14) == "2026-04-09"


def test_cutoff_zero_days_is_today():
    assert compute_cutoff(dt.date(2026, 4, 23), 0) == "2026-04-23"


def test_basenames_older_than_cutoff_are_selected():
    expired = select_expired(
        ["2026-04-01", "2026-04-09", "2026-04-10", "2026-04-23"],
        cutoff="2026-04-10",
    )
    # Cutoff itself is NOT expired (strictly-less semantics).
    assert expired == ["2026-04-01", "2026-04-09"]


def test_cutoff_boundary_is_not_expired():
    assert select_expired(["2026-04-10"], cutoff="2026-04-10") == []


def test_full_paths_are_stripped_to_basename():
    """Remote-rotation regression: sftp `ls -1` returns full paths."""
    expired = select_expired(
        [
            "backups/daily/2026-04-01",
            "backups/daily/2026-04-10",
            "backups/daily/2026-04-23",
        ],
        cutoff="2026-04-10",
    )
    assert expired == ["2026-04-01"]


def test_windows_backslashes_are_also_stripped():
    expired = select_expired(
        ["D:\\backups\\2026-04-01", "D:\\backups\\2026-04-23"],
        cutoff="2026-04-10",
    )
    assert expired == ["2026-04-01"]


def test_non_date_entries_are_ignored():
    expired = select_expired(
        ["2026-04-01", "README", "", "  ", "old_dir", "2026-04-xx"],
        cutoff="2026-04-10",
    )
    assert expired == ["2026-04-01"]


def test_none_values_are_safe():
    assert select_expired([None, "2026-04-01"], cutoff="2026-04-10") == ["2026-04-01"]


def test_all_entries_within_retention_means_nothing_expires():
    assert (
        select_expired(
            ["2026-04-10", "2026-04-15", "2026-04-23"], cutoff="2026-04-10"
        )
        == []
    )


def test_empty_listing_is_safe():
    assert select_expired([], cutoff="2026-04-10") == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  2026-04-01  ", ["2026-04-01"]),
        ("\t2026-04-01\n", ["2026-04-01"]),
        ("backups/daily/2026-04-01/", []),  # trailing slash -> empty basename
    ],
)
def test_whitespace_and_edge_shapes(raw, expected):
    assert select_expired([raw], cutoff="2026-04-10") == expected
