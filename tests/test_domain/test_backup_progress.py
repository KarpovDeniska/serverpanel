"""Tests for BackupProgress VO and is_stalled."""

from __future__ import annotations

import datetime

import pytest

from serverpanel.domain.backup_progress import (
    BackupProgress,
    InvalidProgressError,
    is_stalled,
)

UTC = datetime.UTC


def _p(total: int = 1000, done: int = 100, *, item: str = "UNF",
       ts: datetime.datetime | None = None) -> BackupProgress:
    return BackupProgress(
        bytes_total=total,
        bytes_done=done,
        current_item=item,
        updated_at=ts or datetime.datetime(2026, 4, 23, 10, 0, tzinfo=UTC),
    )


class TestInvariants:
    def test_negative_total_rejected(self):
        with pytest.raises(InvalidProgressError):
            _p(total=-1, done=0)

    def test_negative_done_rejected(self):
        with pytest.raises(InvalidProgressError):
            _p(total=100, done=-5)

    def test_done_gt_total_rejected(self):
        with pytest.raises(InvalidProgressError):
            _p(total=100, done=200)

    def test_naive_datetime_rejected(self):
        with pytest.raises(InvalidProgressError):
            BackupProgress(
                bytes_total=100, bytes_done=50, current_item="x",
                updated_at=datetime.datetime(2026, 4, 23, 10, 0),
            )

    def test_done_equals_total_ok(self):
        p = _p(total=100, done=100)
        assert p.percent == 100.0

    def test_zero_total_percent(self):
        p = _p(total=0, done=0)
        assert p.percent == 0.0


class TestFromJson:
    def test_valid_payload(self):
        p = BackupProgress.from_json({
            "bytes_total": 2_000_000_000,
            "bytes_done": 500_000_000,
            "current_item": "UNF",
            "updated_at": "2026-04-23T10:30:00Z",
        })
        assert p.bytes_total == 2_000_000_000
        assert p.percent == 25.0
        assert p.current_item == "UNF"
        assert p.updated_at.tzinfo is not None

    def test_plus_zero_offset_accepted(self):
        p = BackupProgress.from_json({
            "bytes_total": 10,
            "bytes_done": 3,
            "current_item": "x",
            "updated_at": "2026-04-23T10:30:00+00:00",
        })
        assert p.percent == 30.0

    def test_missing_field_raises(self):
        with pytest.raises(InvalidProgressError):
            BackupProgress.from_json({"bytes_total": 1, "bytes_done": 0})

    def test_malformed_timestamp_raises(self):
        with pytest.raises(InvalidProgressError):
            BackupProgress.from_json({
                "bytes_total": 1, "bytes_done": 0,
                "current_item": "x", "updated_at": "not-a-date",
            })


class TestIsStalled:
    def test_none_progress_not_stalled(self):
        assert is_stalled(None, now=datetime.datetime.now(UTC), threshold_seconds=30) is False

    def test_fresh_update_not_stalled(self):
        now = datetime.datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
        p = _p(ts=now - datetime.timedelta(seconds=10))
        assert is_stalled(p, now=now, threshold_seconds=30) is False

    def test_stale_update_is_stalled(self):
        now = datetime.datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
        p = _p(ts=now - datetime.timedelta(seconds=200))
        assert is_stalled(p, now=now, threshold_seconds=120) is True

    def test_exact_threshold_not_stalled(self):
        now = datetime.datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
        p = _p(ts=now - datetime.timedelta(seconds=120))
        # "> threshold" — inclusive not-stalled, strict becomes-stalled at +1
        assert is_stalled(p, now=now, threshold_seconds=120) is False

    def test_zero_threshold_rejected(self):
        with pytest.raises(ValueError):
            is_stalled(_p(), now=datetime.datetime.now(UTC), threshold_seconds=0)
