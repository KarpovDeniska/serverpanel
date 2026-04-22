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
      - "HH:MM"                  — daily at time
      - "weekly:DAY@HH:MM"       — DAY in Mon/Tue/Wed/Thu/Fri/Sat/Sun (case-insensitive)
      - "monthly:D@HH:MM"        — D in 1..31 (day of month; Windows clamps to month end)
      - empty/None               — no schedule (manual only)
    """
    if not expr:
        return None
    expr = expr.strip()
    if not expr:
        return None
    low = expr.lower()
    if low.startswith("weekly:"):
        body = expr.split(":", 1)[1]
        day, time_part = body.split("@", 1)
        hh, mm = time_part.split(":", 1)
        return {
            "kind": "weekly",
            "day": day.strip().capitalize()[:3],
            "at": f"{int(hh):02d}:{int(mm):02d}",
        }
    if low.startswith("monthly:"):
        body = expr.split(":", 1)[1]
        day, time_part = body.split("@", 1)
        d = int(day.strip())
        if not 1 <= d <= 31:
            raise ValueError(f"monthly day must be 1..31, got {d}")
        hh, mm = time_part.split(":", 1)
        return {
            "kind": "monthly",
            "day": d,
            "at": f"{int(hh):02d}:{int(mm):02d}",
        }
    if ":" in expr and "@" not in expr:
        hh, mm = expr.split(":", 1)
        return {"kind": "daily", "at": f"{int(hh):02d}:{int(mm):02d}"}
    raise ValueError(f"Unsupported schedule format: {expr!r}")

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "static"
    / "scripts"
    / "backup.ps1"
)


BACKUP_PHASES = (
    "Сборка плана",
    "SSH подключение",
    "Загрузка скрипта и плана",
    "Выполнение бэкапа",
    "Чтение отчёта",
    "Готово",
)
PHASES_TOTAL = len(BACKUP_PHASES)


class BackupService:
    def __init__(self, db: AsyncSession, reporter: ProgressReporter | None = None):
        self.db = db
        self.reporter: ProgressReporter = reporter or NullProgressReporter()

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    async def _phase(self, history: BackupHistory, num: int) -> None:
        name = BACKUP_PHASES[num - 1]
        history.current_step = name
        history.progress = int(num / PHASES_TOTAL * 100)
        await self._flush(history)
        await self.reporter.progress(name, num, PHASES_TOTAL)

    async def run(self, config: BackupConfig, history: BackupHistory) -> None:
        history.started_at = datetime.datetime.now(datetime.UTC)
        history.status = BackupStatus.RUNNING
        history.details = {"log": [], "destinations": []}
        await self._flush(history)
        await self.reporter.status(BackupStatus.RUNNING.value)

        try:
            await self._phase(history, 1)
            plan = BackupPlan.model_validate(
                {"sources": config.sources, "destinations": config.destinations}
            )
            resolved = await self._build_remote_plan(config, plan)
            await self._append_log(history, f"Plan built: {len(plan.sources)} sources × {len(plan.destinations)} destinations")

            await self._phase(history, 2)
            async with self._open_ssh(config.server) as ssh:
                await self._persist_learned_host_key()
                await self._phase(history, 3)
                await self._deploy_script(ssh)
                await ssh.put_file(REMOTE_PLAN, json.dumps(resolved, ensure_ascii=False, indent=2))
                await self._append_log(history, "Script and plan uploaded")
                await self._phase(history, 4)

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

                await self._phase(history, 5)
                try:
                    report_bytes = await ssh.fetch_file(REMOTE_REPORT)
                    report = json.loads(report_bytes.decode("utf-8"))
                except Exception as e:
                    raise RuntimeError(
                        f"Cannot read remote report {REMOTE_REPORT}: {e}. "
                        f"Script exit={result.exit_code}, stderr tail: {result.stderr[-400:]}"
                    ) from e

            self._apply_report(history, report, script_exit=result.exit_code)
            await self._phase(history, 6)
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

        settings = get_settings()
        notifications: dict = {}
        if settings.telegram_bot_token and settings.telegram_chat_id:
            notifications["telegram"] = {
                "bot_token": settings.telegram_bot_token,
                "chat_id": settings.telegram_chat_id,
            }

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
            "notifications": notifications,
            "options": {
                "zip_level": settings.backup_zip_level,  # "fastest" | "optimal"
            },
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
        # Same JSON-identity-tracking gotcha as _append_log — build a new dict.
        existing = history.details or {"log": [], "destinations": []}
        history.details = {
            **existing,
            "destinations": dests,
            "script_exit": script_exit,
            "skipped": [d.get("index") for d in skipped],
        }

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
        # SQLAlchemy's JSON column tracks dirtiness by identity, not by deep
        # content. Mutating history.details["log"] in place + reassigning the
        # same dict reference keeps the attribute "clean" and the commit is a
        # no-op — on refresh the UI reads back the stale empty dict.
        # Build a fresh dict (and fresh list) so the attribute event fires.
        existing = history.details or {"log": [], "destinations": []}
        history.details = {
            **existing,
            "log": list(existing.get("log", [])) + [entry],
        }
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

            if trigger["kind"] == "monthly":
                # Monthly via schtasks.exe + a static .cmd wrapper: CIM
                # MSFT_TaskMonthlyTrigger.DaysOfMonth (uint32[]) refuses to be
                # assigned from PS 5.1's adapter, and XML register is fragile
                # in an ssh exec_command context. schtasks has worked since
                # Win2003; wrapping the PowerShell invocation in a .cmd lets
                # /tr accept a path without nested-quote hell.
                wrapper_path = base + r"\trigger.cmd"
                wrapper_body = (
                    "@echo off\r\n"
                    f'powershell.exe -ExecutionPolicy Bypass -NoProfile -File "{REMOTE_SCRIPT}" '
                    f'-PlanPath "{plan_path}" -ReportPath "{report_path}"\r\n'
                )
                await ssh.put_file(wrapper_path, wrapper_body)

                register_cmd = (
                    f'schtasks /create /tn "{task}" /tr "{wrapper_path}" '
                    f'/sc MONTHLY /d {trigger["day"]} /st {trigger["at"]} '
                    f'/ru SYSTEM /rl HIGHEST /f'
                )
                r = await ssh.execute(register_cmd, timeout=60)
                if r.exit_code != 0:
                    raise RuntimeError(
                        f"schtasks /create MONTHLY failed (exit {r.exit_code}): "
                        f"{(r.stderr or r.stdout)[-400:]}"
                    )
                return  # done, skip the Register-ScheduledTask path below

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

    async def sync_reports_from_server(self, server: Server) -> int:
        """Pull Task-Scheduler-driven backup reports from the target server
        into BackupHistory so the UI sees nightly runs (not just UI-triggered
        Run-now).

        Looks at `C:\\ProgramData\\serverpanel\\configs\\<id>\\last_report.json`
        for every configured backup, dedupes by the report's run_id (written
        in history.details.run_id) and creates a new BackupHistory row for
        each previously-unseen run.

        Returns: number of rows created.
        """
        if not server.ssh_key_encrypted:
            return 0

        from sqlalchemy import select as _select
        created = 0

        async with self._open_ssh(server) as ssh:
            await self._persist_learned_host_key()

            # List config-ids present under REMOTE_CONFIGS_DIR
            r = await ssh.execute(
                f'powershell -NoProfile -Command '
                f'"if (Test-Path \'{REMOTE_CONFIGS_DIR}\') '
                f'{{ Get-ChildItem \'{REMOTE_CONFIGS_DIR}\' -Directory | '
                f'Select-Object -ExpandProperty Name }}"',
                timeout=30,
            )
            if r.exit_code != 0:
                return 0
            raw_ids = [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]

            for cid_str in raw_ids:
                try:
                    cid = int(cid_str)
                except ValueError:
                    continue

                _, _, report_path = _scheduled_paths(cid)
                try:
                    report_bytes = await ssh.fetch_file(report_path)
                except Exception:
                    continue
                try:
                    report = json.loads(report_bytes.decode("utf-8"))
                except Exception:
                    continue

                run_id = report.get("run_id")
                if not run_id:
                    continue

                # Dedupe: if the most recent history for this config is the
                # same run_id, skip.
                existing = (await self.db.execute(
                    _select(BackupHistory)
                    .where(BackupHistory.backup_config_id == cid)
                    .order_by(BackupHistory.id.desc())
                    .limit(1)
                )).scalar_one_or_none()
                if existing and (existing.details or {}).get("run_id") == run_id:
                    continue

                # run_id is "YYYYMMDD_HHMMSS" in server-local time; run_at is ISO UTC.
                started_at = None
                try:
                    started_at = datetime.datetime.strptime(run_id, "%Y%m%d_%H%M%S")
                except Exception:
                    pass
                completed_at = None
                try:
                    run_at = report.get("run_at")
                    if run_at:
                        completed_at = datetime.datetime.fromisoformat(
                            run_at.replace("Z", "+00:00")
                        )
                except Exception:
                    pass

                dests = report.get("destinations", [])
                ok = [d for d in dests if d.get("status") == "success"]
                failed = [d for d in dests if d.get("status") == "failed"]

                if not dests:
                    status = BackupStatus.FAILED
                    err = report.get("error") or "empty report"
                elif failed and ok:
                    status = BackupStatus.PARTIAL
                    err = "; ".join(d.get("error", "?") for d in failed[:3])
                elif failed:
                    status = BackupStatus.FAILED
                    err = "; ".join(d.get("error", "?") for d in failed[:3])
                else:
                    status = BackupStatus.SUCCESS
                    err = None
                size_bytes = sum(int(d.get("size_bytes") or 0) for d in dests)

                hist = BackupHistory(
                    backup_config_id=cid,
                    started_at=started_at,
                    completed_at=completed_at,
                    status=status.value,
                    current_step="Готово",
                    progress=100,
                    size_bytes=size_bytes,
                    error_message=err,
                    details={
                        "log": [],  # live stdout not available for scheduled runs
                        "destinations": dests,
                        "run_id": run_id,
                        "source": "scheduled",  # mark origin for UI
                        "script_exit": report.get("script_exit"),
                    },
                )
                self.db.add(hist)
                created += 1

            if created:
                await self.db.commit()

        return created

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
