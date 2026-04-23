"""`_validate_scheduled_task` is the last line of defense against silent
regressions in schtasks / Set-ScheduledTask CIM overloads. Every invariant
it checks maps to a real bug class that hit production before.
"""

from unittest.mock import AsyncMock

import pytest

from serverpanel.application.services.backup_service import (
    TASK_EXECUTION_TIME_LIMIT,
    BackupService,
)
from serverpanel.infrastructure.ssh.client import CommandResult

WRAPPER_PATH = r"C:\ProgramData\serverpanel\configs\2\trigger.cmd"


def _xml(
    *,
    command: str = WRAPPER_PATH,
    include_args: bool = False,
    include_workdir: bool = False,
    exec_limit: str | None = TASK_EXECUTION_TIME_LIMIT,
) -> str:
    parts = ["<Task>", "<Actions>", "<Exec>", f"<Command>{command}</Command>"]
    if include_args:
        parts.append("<Arguments>--leaked</Arguments>")
    if include_workdir:
        parts.append("<WorkingDirectory>C:\\</WorkingDirectory>")
    parts += ["</Exec>", "</Actions>", "<Settings>"]
    if exec_limit is not None:
        parts.append(f"<ExecutionTimeLimit>{exec_limit}</ExecutionTimeLimit>")
    parts += ["</Settings>", "</Task>"]
    return "".join(parts)


def _mock_ssh(xml: str, exit_code: int = 0) -> AsyncMock:
    ssh = AsyncMock()
    ssh.execute = AsyncMock(return_value=CommandResult(exit_code=exit_code, stdout=xml, stderr=""))
    return ssh


@pytest.mark.asyncio
async def test_happy_path_passes():
    svc = BackupService.__new__(BackupService)  # skip __init__
    ssh = _mock_ssh(_xml())
    await svc._validate_scheduled_task(ssh, "serverpanel-backup-2", WRAPPER_PATH)


@pytest.mark.asyncio
async def test_missing_execution_time_limit_fails():
    svc = BackupService.__new__(BackupService)
    ssh = _mock_ssh(_xml(exec_limit=None))
    with pytest.raises(RuntimeError, match="ExecutionTimeLimit"):
        await svc._validate_scheduled_task(ssh, "serverpanel-backup-2", WRAPPER_PATH)


@pytest.mark.asyncio
async def test_wrong_execution_time_limit_fails():
    """If the clamp step runs but is somehow stuck on the default (72 h = PT72H),
    the validator must catch it. PT72H is the schtasks default; seeing it
    would mean our clamp never took effect."""
    svc = BackupService.__new__(BackupService)
    ssh = _mock_ssh(_xml(exec_limit="PT72H"))
    with pytest.raises(RuntimeError, match="ExecutionTimeLimit"):
        await svc._validate_scheduled_task(ssh, "serverpanel-backup-2", WRAPPER_PATH)


@pytest.mark.asyncio
async def test_wrong_command_fails():
    svc = BackupService.__new__(BackupService)
    ssh = _mock_ssh(_xml(command=r"C:\Windows\System32\cmd.exe"))
    with pytest.raises(RuntimeError, match="unexpected <Command>"):
        await svc._validate_scheduled_task(ssh, "serverpanel-backup-2", WRAPPER_PATH)


@pytest.mark.asyncio
async def test_leaked_arguments_fail():
    svc = BackupService.__new__(BackupService)
    ssh = _mock_ssh(_xml(include_args=True))
    with pytest.raises(RuntimeError, match="Arguments"):
        await svc._validate_scheduled_task(ssh, "serverpanel-backup-2", WRAPPER_PATH)


@pytest.mark.asyncio
async def test_leaked_workdir_fails():
    svc = BackupService.__new__(BackupService)
    ssh = _mock_ssh(_xml(include_workdir=True))
    with pytest.raises(RuntimeError, match="WorkingDirectory"):
        await svc._validate_scheduled_task(ssh, "serverpanel-backup-2", WRAPPER_PATH)


@pytest.mark.asyncio
async def test_schtasks_query_failure_surfaces():
    svc = BackupService.__new__(BackupService)
    ssh = AsyncMock()
    ssh.execute = AsyncMock(
        return_value=CommandResult(exit_code=1, stdout="", stderr="task not found")
    )
    with pytest.raises(RuntimeError, match="cannot read XML"):
        await svc._validate_scheduled_task(ssh, "serverpanel-backup-2", WRAPPER_PATH)
