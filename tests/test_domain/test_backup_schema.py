"""Pydantic validation of BackupPlan."""

import pytest

from serverpanel.domain.backup import BackupPlan


def test_valid_plan_with_local_and_storage_dests():
    plan = BackupPlan.model_validate({
        "sources": [
            {"type": "dir", "alias": "users", "path": "C:/Users"},
            {"type": "vss_dir", "alias": "unf", "path": "D:/1c/UNF"},
        ],
        "destinations": [
            {"kind": "local", "base_path": "D:/backups", "aliases": []},
            {
                "kind": "storage",
                "storage_config_id": 1,
                "base_path": "backups/daily",
                "frequency": "daily",
            },
        ],
    })
    assert len(plan.sources) == 2
    assert len(plan.destinations) == 2
    assert plan.destinations[0].kind == "local"
    assert plan.destinations[1].kind == "storage"


def test_unknown_destination_kind_rejected():
    with pytest.raises(Exception):
        BackupPlan.model_validate({
            "sources": [{"alias": "a", "path": "C:/x"}],
            "destinations": [{"kind": "ftp", "host": "x"}],
        })


def test_storage_destination_requires_storage_config_id():
    with pytest.raises(Exception):
        BackupPlan.model_validate({
            "sources": [{"alias": "a", "path": "C:/x"}],
            "destinations": [{"kind": "storage"}],
        })


def test_defaults_applied():
    plan = BackupPlan.model_validate({
        "sources": [{"alias": "a", "path": "C:/x"}],
        "destinations": [{"kind": "storage", "storage_config_id": 7}],
    })
    dest = plan.destinations[0]
    assert dest.base_path == "backups/daily"
    assert dest.frequency == "daily"
    assert dest.date_folder is True
