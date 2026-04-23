"""`_apply_report` must propagate per-item integrity fields and flip a
destination to failed when backup.ps1 detected a size mismatch after scp.
"""

from serverpanel.application.services.backup_service import BackupService
from serverpanel.domain.enums import BackupStatus
from serverpanel.infrastructure.database.models import BackupHistory


def _history() -> BackupHistory:
    h = BackupHistory()
    h.details = {"log": [], "destinations": []}
    return h


def test_verified_item_keeps_destination_success():
    history = _history()
    report = {
        "destinations": [
            {
                "index": 0,
                "kind": "storage",
                "status": "success",
                "size_bytes": 12345,
                "items": [
                    {
                        "alias": "unf",
                        "status": "success",
                        "remote_path": "backups/daily/2026-04-23/unf.zip",
                        "size_bytes": 12345,
                        "remote_size": 12345,
                        "integrity": "verified",
                    }
                ],
            }
        ]
    }

    BackupService._apply_report(None, history, report, script_exit=0)  # type: ignore[arg-type]

    assert history.status == BackupStatus.SUCCESS
    assert history.size_bytes == 12345
    item = history.details["destinations"][0]["items"][0]
    assert item["integrity"] == "verified"
    assert item["remote_size"] == 12345


def test_size_mismatch_fails_destination_and_surfaces_error():
    history = _history()
    report = {
        "destinations": [
            {
                "index": 0,
                "kind": "storage",
                "status": "failed",
                "error": "integrity: size mismatch on backups/daily/2026-04-23/unf.zip (local=12345 remote=9999)",
                "size_bytes": 0,
                "items": [
                    {
                        "alias": "unf",
                        "status": "failed",
                        "error": "integrity: size mismatch on backups/daily/2026-04-23/unf.zip (local=12345 remote=9999)",
                    }
                ],
            }
        ]
    }

    BackupService._apply_report(None, history, report, script_exit=0)  # type: ignore[arg-type]

    assert history.status == BackupStatus.FAILED
    assert "size mismatch" in (history.error_message or "")


def test_skipped_dir_integrity_does_not_fail_success():
    history = _history()
    report = {
        "destinations": [
            {
                "index": 0,
                "kind": "storage",
                "status": "success",
                "size_bytes": 50000,
                "items": [
                    {
                        "alias": "c_drive",
                        "status": "success",
                        "remote_path": "backups/c",
                        "size_bytes": 50000,
                        "remote_size": None,
                        "integrity": "skipped_dir",
                    }
                ],
            }
        ]
    }

    BackupService._apply_report(None, history, report, script_exit=0)  # type: ignore[arg-type]

    assert history.status == BackupStatus.SUCCESS
    item = history.details["destinations"][0]["items"][0]
    assert item["integrity"] == "skipped_dir"
