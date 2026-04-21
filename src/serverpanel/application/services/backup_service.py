"""Backup service — build plan, ship it to the target server, execute, collect report.

Flow (one SSH session per run):
1. Load BackupConfig + Server + StorageConfigs referenced in destinations.
2. Validate and merge the pydantic `BackupPlan` with resolved storage credentials.
3. SFTP upload: backup.ps1 (if missing/changed) + plan.json.
4. Execute PowerShell on the server, stream stdout to BackupHistory.log.
5. Fetch report.json from the server, aggregate per-destination statuses.
6. Write result into BackupHistory (success / partial / failed).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from serverpanel.config import get_settings
from serverpanel.domain.backup import (
    BackupPlan,
    LocalDestination,
    StorageDestination,
)
from serverpanel.domain.enums import BackupStatus
from serverpanel.domain.progress import NullProgressReporter, ProgressReporter
from serverpanel.infrastructure.crypto import decrypt_json
from serverpanel.infrastructure.database.repositories.backups import (
    StorageConfigRepository,
)
from serverpanel.infrastructure.ssh.client import AsyncSSHClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from serverpanel.infrastructure.database.models import (
        BackupConfig,
        BackupHistory,
        Server,
    )

log = logging.getLogger(__name__)

REMOTE_DIR = r"C:\ProgramData\serverpanel"
REMOTE_SCRIPT = REMOTE_DIR + r"\backup.ps1"
REMOTE_PLAN = REMOTE_DIR + r"\plan.json"
REMOTE_REPORT = REMOTE_DIR + r"\report.json"

# Scheduled (per-config) — task invokes backup.ps1 against these frozen paths.
REMOTE_CONFIGS_DIR = REMOTE_DIR + r"\configs"


def _task_name(config_id: int) -> str:
    return f"serverpanel-backup-{config_id}"


def _scheduled_paths(config_id: int) -> tuple[str, str, str]:
    base = f"{REMOTE_CONFIGS_DIR}\\{config_id}"
    return base, base + r"\plan.json", base + r"\last_report.json"


def _parse_schedule(expr: str | None) -> dict | None:
    """Parse BackupConfig.schedule to Task Scheduler trigger params.

    Formats:
      - "HH:MM"                 — daily at time
      - "weekly:DAY@HH:MM"      — DAY in Mon/Tue/Wed/Thu/Fri/Sat/Sun (case-insensitive)
      - empty/None              — no schedule (manual only)
    """
    if not expr:
        return None
    expr = expr.strip()
    if not expr:
        return None
    if ":" in expr and "@" not in expr and not expr.lower().startswith("weekly"):
        hh, mm = expr.split(":", 1)
        return {"kind": "daily", "at": f"{int(hh):02d}:{int(mm):02d}"}
    if expr.lower().startswith("weekly:"):
        body = expr.split(":", 1)[1]
        day, time_part = body.split("@", 1)
        hh, mm = time_part.split(":", 1)
        return {
            "kind": "weekly",
            "day": day.strip().capitalize()[:3],
            "at": f"{int(hh):02d}:{int(mm):02d}",
        }
    raise ValueError(f"Unsupported schedule format: {expr!r}")

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "static"
    / "scripts"
    / "backup.ps1"
)


class BackupService:
    def __init__(self, db: AsyncSession, reporter: ProgressReporter | None = None):
        self.db = db
        self.reporter: ProgressReporter = reporter or NullProgressReporter()

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    async def run(self, config: BackupConfig, history: BackupHistory) -> None:
        history.started_at = datetime.datetime.now(datetime.UTC)
        history.status = BackupStatus.RUNNING
        history.details = {"log": [], "destinations": []}
        await self._flush(history)
        await self.reporter.status(BackupStatus.RUNNING.value)

        try:
            plan = BackupPlan.model_validate(
                {"sources": config.sources, "destinations": config.destinations}
            )
            resolved = await self._build_remote_plan(config, plan)
            await self._append_log(history, f"Plan built: {len(plan.sources)} sources × {len(plan.destinations)} destinations")

            async with self._open_ssh(config.server) as ssh:
                await self._persist_learned_host_key()
                await self._deploy_script(ssh)
                await ssh.put_file(REMOTE_PLAN, json.dumps(resolved, ensure_ascii=False, indent=2))
                await self._append_log(history, "Script and plan uploaded")

                cmd = (
                    f'powershell -ExecutionPolicy Bypass -NoProfile '
                    f'-File "{REMOTE_SCRIPT}" -PlanPath "{REMOTE_PLAN}" '
                    f'-ReportPath "{REMOTE_REPORT}"'
                )

                # Live-stream stdout to WS and to history.log as lines arrive.
                buffer: list[str] = []
                pending_lines: list[str] = []

                def _on_chunk(chunk: str) -> None:
                    buffer.append(chunk)
                    text = "".join(buffer)
                    *complete, tail = text.split("\n")
                    buffer.clear()
                    if tail:
                        buffer.append(tail)
                    for line in complete:
                        line = line.rstrip()
                        if line:
                            pending_lines.append(line)

                async def _drain() -> None:
                    while pending_lines:
                        await self._append_log(history, pending_lines.pop(0))

                # Drain in background while the SSH exec streams — paramiko callback
                # is sync, so we buffer lines and flush them via a periodic task.
                import asyncio as _asyncio

                async def _pump() -> None:
                    while True:
                        await _asyncio.sleep(1.0)
                        await _drain()

                pump_task = _asyncio.create_task(_pump())
                try:
                    result = await ssh.execute_stream(
                        cmd,
                        on_output=_on_chunk,
                        timeout=get_settings().backup_run_timeout,
                    )
                finally:
                    pump_task.cancel()
                    try:
                        await pump_task
                    except _asyncio.CancelledError:
                        pass
                    except Exception:
                        log.debug("backup live-stream pump ended with error", exc_info=True)

                # Flush any tail line left in buffer
                if buffer:
                    tail = "".join(buffer).rstrip()
                    if tail:
                        pending_lines.append(tail)
                await _drain()

                try:
                    report_bytes = await ssh.fetch_file(REMOTE_REPORT)
                    report = json.loads(report_bytes.decode("utf-8"))
                except Exception as e:
                    raise RuntimeError(
                        f"Cannot read remote report {REMOTE_REPORT}: {e}. "
                        f"Script exit={result.exit_code}, stderr tail: {result.stderr[-400:]}"
                    ) from e

            self._apply_report(history, report, script_exit=result.exit_code)
            status_str = history.status.value if hasattr(history.status, "value") else str(history.status)
            await self.reporter.status(status_str)

        except Exception as e:
            log.exception("Backup failed for config %s", config.id)
            history.status = BackupStatus.FAILED
            history.error_message = str(e)
            await self._append_log(history, f"ERROR: {e}", level="error")
            await self.reporter.status(BackupStatus.FAILED.value)
        finally:
            history.completed_at = datetime.datetime.now(datetime.UTC)
            await self._flush(history)

    # ------------------------------------------------------------------
    # Plan assembly
    # ------------------------------------------------------------------

    async def _build_remote_plan(self, config: BackupConfig, plan: BackupPlan) -> dict:
        now = datetime.datetime.now(datetime.UTC)
        resolved_destinations: list[dict] = []

        storage_repo = StorageConfigRepository(self.db)
        for idx, dest in enumerate(plan.destinations):
            rotation = dest.rotation_days or config.rotation_days
            if isinstance(dest, LocalDestination):
                resolved_destinations.append({
                    "index": idx,
                    "kind": "local",
                    "base_path": dest.base_path,
                    "aliases": dest.aliases,
                    "rotation_days": rotation,
                    "date_folder": dest.date_folder,
                })
            elif isinstance(dest, StorageDestination):
                storage = await storage_repo.get_by_id(dest.storage_config_id)
                if storage is None:
                    raise RuntimeError(
                        f"destination[{idx}]: StorageConfig id={dest.storage_config_id} not found"
                    )
                connection = decrypt_json(storage.connection_encrypted)
                resolved_destinations.append({
                    "index": idx,
                    "kind": "storage",
                    "storage_type": storage.storage_type,
                    "connection": connection,
                    "base_path": dest.base_path,
                    "aliases": dest.aliases,
                    "rotation_days": rotation,
                    "date_folder": dest.date_folder,
                    "frequency": dest.frequency,
                })
            else:
                raise RuntimeError(f"Unknown destination kind at index {idx}")

        return {
            "schema_version": 1,
            "config_id": config.id,
            "config_name": config.name,
            "run_at": now.isoformat() + "Z",
            "date_folder": now.strftime("%Y-%m-%d"),
            "day_of_week": now.strftime("%A"),
            "global_rotation_days": config.rotation_days,
            "sources": [s.model_dump() for s in plan.sources],
            "destinations": resolved_destinations,
        }

    # ------------------------------------------------------------------
    # SSH
    # ------------------------------------------------------------------

    def _open_ssh(self, server: Server) -> AsyncSSHClient:
        creds: dict = {}
        if server.ssh_key_encrypted:
            creds = decrypt_json(server.ssh_key_encrypted)

        # TOFU: on first connect store the presented host key back on Server.
        learned_holder: dict[str, str] = {}

        def _on_learned(line: str) -> None:
            learned_holder["line"] = line

        client = AsyncSSHClient(
            host=server.ip_address,
            username=server.ssh_username or "Administrator",
            port=server.ssh_port or 22,
            password=creds.get("password"),
            private_key=creds.get("private_key"),
            key_passphrase=creds.get("passphrase"),
            timeout=30.0,
            known_host_key=server.ssh_host_key_pub,
            on_host_key_learned=_on_learned if not server.ssh_host_key_pub else None,
        )
        # Post-connect: if we learned a new key, persist it. Stored here so
        # background-task session picks it up via commit.
        self._ssh_learn_sink = (server, learned_holder)
        return client

    async def _persist_learned_host_key(self) -> None:
        sink = getattr(self, "_ssh_learn_sink", None)
        if not sink:
            return
        server, holder = sink
        line = holder.get("line")
        if line and not server.ssh_host_key_pub:
            server.ssh_host_key_pub = line
            self.db.add(server)
            await self.db.commit()
            log.info("Pinned SSH host key for server %s", server.id)

    async def _deploy_script(self, ssh: AsyncSSHClient) -> None:
        """Upload backup.ps1 if remote hash differs. Creates remote dir if missing."""
        local = _SCRIPT_PATH.read_bytes()
        local_hash = hashlib.sha256(local).hexdigest()

        await ssh.execute(
            f'powershell -NoProfile -Command '
            f'"New-Item -ItemType Directory -Force -Path \'{REMOTE_DIR}\' | Out-Null"',
            timeout=30,
        )

        r = await ssh.execute(
            f'powershell -NoProfile -Command '
            f'"if (Test-Path \'{REMOTE_SCRIPT}\') '
            f'{{ (Get-FileHash -Algorithm SHA256 \'{REMOTE_SCRIPT}\').Hash.ToLower() }} '
            f'else {{ \'\' }}"',
            timeout=30,
        )
        remote_hash = (r.stdout or "").strip().lower()
        if remote_hash == local_hash:
            return
        await ssh.put_file(REMOTE_SCRIPT, local)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _apply_report(
        self, history: BackupHistory, report: dict, script_exit: int
    ) -> None:
        """Merge remote report.json into BackupHistory.details, compute aggregate status."""
        dests = report.get("destinations", [])
        ok = [d for d in dests if d.get("status") == "success"]
        failed = [d for d in dests if d.get("status") == "failed"]
        skipped = [d for d in dests if d.get("status") == "skipped"]

        if not dests:
            history.status = BackupStatus.FAILED
            history.error_message = report.get("error") or f"empty report (exit={script_exit})"
        elif failed and ok:
            history.status = BackupStatus.PARTIAL
            history.error_message = "; ".join(d.get("error", "?") for d in failed[:3])
        elif failed:
            history.status = BackupStatus.FAILED
            history.error_message = "; ".join(d.get("error", "?") for d in failed[:3])
        else:
            history.status = BackupStatus.SUCCESS

        history.size_bytes = sum(int(d.get("size_bytes") or 0) for d in dests)
        details = history.details or {"log": [], "destinations": []}
        details["destinations"] = dests
        details["script_exit"] = script_exit
        details["skipped"] = [d.get("index") for d in skipped]
        history.details = details

    # ------------------------------------------------------------------
    # Log helpers
    # ------------------------------------------------------------------

    async def _append_log(
        self, history: BackupHistory, message: str, level: str = "info"
    ) -> None:
        entry = {
            "time": datetime.datetime.now(datetime.UTC).isoformat(),
            "message": message,
            "level": level,
        }
        details = history.details or {"log": [], "destinations": []}
        details.setdefault("log", []).append(entry)
        history.details = details
        await self._flush(history)
        await self.reporter.log(message, level)

    # ------------------------------------------------------------------
    # Scheduled install / uninstall on target server (Task Scheduler)
    # ------------------------------------------------------------------

    async def install_schedule(self, config: BackupConfig) -> None:
        """Freeze plan.json on the target server and register a Task Scheduler task.

        Creds in the frozen plan are resolved at install time. Re-run
        `install_schedule` to refresh after rotating keys or editing the config.
        """
        trigger = _parse_schedule(config.schedule)
        if trigger is None:
            raise ValueError(
                "BackupConfig.schedule is empty; nothing to schedule. "
                "Either set a schedule or call run() for manual backups."
            )

        plan = BackupPlan.model_validate(
            {"sources": config.sources, "destinations": config.destinations}
        )
        resolved = await self._build_remote_plan(config, plan)

        base, plan_path, report_path = _scheduled_paths(config.id)
        task = _task_name(config.id)

        async with self._open_ssh(config.server) as ssh:
            await self._persist_learned_host_key()
            await self._deploy_script(ssh)
            await ssh.execute(
                f'powershell -NoProfile -Command '
                f'"New-Item -ItemType Directory -Force -Path \'{base}\' | Out-Null"',
                timeout=30,
            )
            await ssh.put_file(plan_path, json.dumps(resolved, ensure_ascii=False, indent=2))

            ps_args = (
                f'-ExecutionPolicy Bypass -NoProfile -File \\"{REMOTE_SCRIPT}\\" '
                f'-PlanPath \\"{plan_path}\\" -ReportPath \\"{report_path}\\"'
            )

            if trigger["kind"] == "daily":
                trigger_cmd = f"New-ScheduledTaskTrigger -Daily -At '{trigger['at']}'"
            elif trigger["kind"] == "weekly":
                trigger_cmd = (
                    f"New-ScheduledTaskTrigger -Weekly -DaysOfWeek {trigger['day']} "
                    f"-At '{trigger['at']}'"
                )
            else:
                raise ValueError(f"unreachable: {trigger}")

            register_cmd = (
                f'powershell -NoProfile -Command "'
                f"$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument \\\"{ps_args}\\\"; "
                f"$trigger = {trigger_cmd}; "
                f"$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
                f"-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew; "
                f"Register-ScheduledTask -TaskName '{task}' -Action $action -Trigger $trigger "
                f"-Settings $settings -User 'SYSTEM' -RunLevel Highest -Force | Out-Null"
                f'"'
            )
            r = await ssh.execute(register_cmd, timeout=60)
            if r.exit_code != 0:
                raise RuntimeError(
                    f"Register-ScheduledTask failed (exit {r.exit_code}): "
                    f"{(r.stderr or r.stdout)[-400:]}"
                )

    async def uninstall_schedule(self, config: BackupConfig) -> None:
        """Remove the Task Scheduler task and its frozen plan folder."""
        base, _, _ = _scheduled_paths(config.id)
        task = _task_name(config.id)
        async with self._open_ssh(config.server) as ssh:
            await self._persist_learned_host_key()
            await ssh.execute(
                f'powershell -NoProfile -Command '
                f'"Unregister-ScheduledTask -TaskName \'{task}\' -Confirm:$false -ErrorAction SilentlyContinue"',
                timeout=30,
            )
            await ssh.execute(
                f'powershell -NoProfile -Command '
                f'"Remove-Item -LiteralPath \'{base}\' -Recurse -Force -ErrorAction SilentlyContinue"',
                timeout=30,
            )

    async def _flush(self, history: BackupHistory) -> None:
        try:
            self.db.add(history)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
