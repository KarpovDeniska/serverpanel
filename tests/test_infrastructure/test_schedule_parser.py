"""BackupService._parse_schedule — format contract."""

import pytest

from serverpanel.application.services.backup_service import _parse_schedule


def test_empty_returns_none():
    assert _parse_schedule(None) is None
    assert _parse_schedule("") is None
    assert _parse_schedule("   ") is None


def test_daily_hhmm():
    assert _parse_schedule("03:00") == {"kind": "daily", "at": "03:00"}
    assert _parse_schedule("23:59") == {"kind": "daily", "at": "23:59"}


def test_daily_pads_single_digits():
    assert _parse_schedule("3:5") == {"kind": "daily", "at": "03:05"}


def test_weekly():
    assert _parse_schedule("weekly:Sun@04:00") == {
        "kind": "weekly",
        "day": "Sun",
        "at": "04:00",
    }


def test_weekly_case_insensitive():
    assert _parse_schedule("weekly:mon@02:30")["day"] == "Mon"


def test_invalid_format_raises():
    with pytest.raises(ValueError):
        _parse_schedule("cron: 0 3 * * *")
