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
from serverpanel.domain.backup_progress import BackupProgress, InvalidProgressError
from serverpanel.domain.enums import BackupStatus
from serverpanel.domain.progress import NullProgressReporter, ProgressReporter
from serverpanel.infrastructure.crypto import decrypt_json
from serverpanel.infrastructure.database.repositories.backups import (
    StorageConfigRepository,
)
from serverpanel.infrastructure.ssh.client import AsyncSSHClient, SSHCommandError

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


def _progress_path(config_id: int) -> str:
    return f"{REMOTE_CONFIGS_DIR}\\{config_id}\\progress.json"


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
_WATCHDOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "static"
    / "scripts"
    / "watchdog.ps1"
)
REMOTE_WATCHDOG = REMOTE_DIR + r"\watchdog.ps1"


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
            # NOTE: no top-level "date_folder" — that used to be a frozen date
            # string read by backup.ps1, which pinned every nightly run into
            # the same subfolder and broke rotation. backup.ps1 now derives
            # the date live at run time. Destination-level `date_folder` is
            # still a bool flag and stays in resolved_destinations.
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
        """Upload backup.ps1 + watchdog.ps1 if remote hashes differ."""
        await ssh.execute(
            f'powershell -NoProfile -Command '
            f'"New-Item -ItemType Directory -Force -Path \'{REMOTE_DIR}\' | Out-Null"',
            timeout=30,
        )
        await self._deploy_one(ssh, _SCRIPT_PATH, REMOTE_SCRIPT)
        await self._deploy_one(ssh, _WATCHDOG_PATH, REMOTE_WATCHDOG)

    async def _deploy_one(
        self, ssh: AsyncSSHClient, local_path: Path, remote_path: str
    ) -> None:
        local = local_path.read_bytes()
        local_hash = hashlib.sha256(local).hexdigest()
        r = await ssh.execute(
            f'powershell -NoProfile -Command '
            f'"if (Test-Path \'{remote_path}\') '
            f'{{ (Get-FileHash -Algorithm SHA256 \'{remote_path}\').Hash.ToLower() }} '
            f'else {{ \'\' }}"',
            timeout=30,
        )
        remote_hash = (r.stdout or "").strip().lower()
        if remote_hash != local_hash:
            await ssh.put_file(remote_path, local)

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

            # All triggers go through a .cmd wrapper + schtasks /tr. Direct
            # Register-ScheduledTask / schtasks with the full powershell
            # invocation as /tr argument explodes on quote-escaping across the
            # ssh → cmd → powershell pipeline: the second half of the command
            # spills into <Arguments>/<WorkingDirectory> and the task fails at
            # launch with ERROR_DIRECTORY (0x8007010B).
            #
            # The wrapper does two things:
            #   1. Run backup.ps1. It sends its own ✅/⚠️/❌ Telegram when it
            #      finishes.
            #   2. Call watchdog.ps1. If the report is missing or older than
            #      this run, watchdog sends a "❌ killed by timeout" Telegram
            #      — otherwise it stays silent. `StartedEpoch` is captured
            #      BEFORE backup.ps1 runs, in wrapper-process local time, so
            #      watchdog can tell "was this run's report written?" without
            #      guessing.
            wrapper_path = base + r"\trigger.cmd"
            wrapper_body = (
                "@echo off\r\n"
                "for /f %%s in ('powershell -NoProfile -Command "
                "\"[int][double]::Parse(((Get-Date).ToUniversalTime() - "
                "[DateTime]'1970-01-01').TotalSeconds)\"') do set __sp_started=%%s\r\n"
                f'powershell.exe -ExecutionPolicy Bypass -NoProfile -File "{REMOTE_SCRIPT}" '
                f'-PlanPath "{plan_path}" -ReportPath "{report_path}"\r\n'
                f'powershell.exe -ExecutionPolicy Bypass -NoProfile -File "{REMOTE_WATCHDOG}" '
                f'-PlanPath "{plan_path}" -ReportPath "{report_path}" '
                f'-StartedEpoch %__sp_started%\r\n'
            )
            await ssh.put_file(wrapper_path, wrapper_body)

            if trigger["kind"] == "daily":
                sched_args = f'/sc DAILY /st {trigger["at"]}'
            elif trigger["kind"] == "weekly":
                sched_args = f'/sc WEEKLY /d {trigger["day"]} /st {trigger["at"]}'
            elif trigger["kind"] == "monthly":
                sched_args = f'/sc MONTHLY /d {trigger["day"]} /st {trigger["at"]}'
            else:
                raise ValueError(f"unreachable: {trigger}")

            register_cmd = (
                f'schtasks /create /tn "{task}" /tr "{wrapper_path}" '
                f'{sched_args} /ru SYSTEM /rl HIGHEST /f'
            )
            r = await ssh.execute(register_cmd, timeout=60)
            if r.exit_code != 0:
                raise RuntimeError(
                    f"schtasks /create {trigger['kind'].upper()} failed (exit {r.exit_code}): "
                    f"{(r.stderr or r.stdout)[-400:]}"
                )

            # Default ExecutionTimeLimit via `schtasks` is 72 h, which makes
            # "kill a stuck backup" meaningless. Clamp to 30 min — watchdog.ps1
            # then produces a Telegram alert on the kill.
            # Right after `schtasks /create`, Get-ScheduledTask can return a
            # stale CIM object; `Set-ScheduledTask -InputObject $t` then fails
            # with 0x80070057 ("The parameter is incorrect"). Re-read by
            # name, mutate a local Settings copy, and push via
            # `-TaskName + -Settings` which is the robust overload.
            clamp_cmd = (
                'powershell -NoProfile -Command "'
                "Start-Sleep -Milliseconds 500; "
                f"$t = Get-ScheduledTask -TaskName '{task}'; "
                "$s = $t.Settings; "
                "$s.ExecutionTimeLimit = 'PT30M'; "
                f"Set-ScheduledTask -TaskName '{task}' -Settings $s | Out-Null"
                '"'
            )
            cr = await ssh.execute(clamp_cmd, timeout=60)
            if cr.exit_code != 0:
                raise RuntimeError(
                    "Set-ScheduledTask (ExecutionTimeLimit=PT30M) failed "
                    f"(exit {cr.exit_code}): {(cr.stderr or cr.stdout)[-400:]}"
                )

            await self._validate_scheduled_task(ssh, task, wrapper_path)

    async def _validate_scheduled_task(
        self, ssh: AsyncSSHClient, task: str, wrapper_path: str
    ) -> None:
        """Read the just-registered task XML and assert that schtasks parsed
        the wrapper path as a single <Command> with no leaked <Arguments>
        or <WorkingDirectory>. Catches quote-escaping regressions like the
        one that silently broke serverpanel-backup-2 on 2026-04-22.
        """
        r = await ssh.execute(f'schtasks /query /xml /tn "{task}"', timeout=30)
        if r.exit_code != 0:
            raise RuntimeError(
                f"post-install validation: cannot read XML of '{task}' "
                f"(exit {r.exit_code}): {(r.stderr or r.stdout)[-400:]}"
            )
        xml = r.stdout or ""
        cmd_line = f"<Command>{wrapper_path}</Command>"
        if cmd_line not in xml:
            raise RuntimeError(
                f"post-install validation: task '{task}' has unexpected <Command> "
                f"(want {wrapper_path!r}). XML tail: {xml[-600:]}"
            )
        if "<Arguments>" in xml or "<WorkingDirectory>" in xml:
            raise RuntimeError(
                f"post-install validation: task '{task}' has leaked "
                f"<Arguments>/<WorkingDirectory>. XML tail: {xml[-600:]}"
            )

    async def fetch_live_progress(
        self, config: BackupConfig
    ) -> BackupProgress | None:
        """Read `progress.json` from the remote server for a single config.

        Returns:
            * `BackupProgress` when the file exists and parses.
            * `None` when the file is missing (run not started or already
              finished and `progress.json` was cleared).

        Raises only on SSH transport errors — JSON/shape errors return None
        (caller should not treat malformed progress as a hard failure).
        """
        if not config.server.ssh_key_encrypted:
            return None

        path = _progress_path(config.id)
        try:
            async with self._open_ssh(config.server) as ssh:
                await self._persist_learned_host_key()
                try:
                    raw = await ssh.fetch_file(path)
                except Exception:
                    return None
            try:
                payload = json.loads(raw.decode("utf-8"))
                return BackupProgress.from_json(payload)
            except (ValueError, InvalidProgressError):
                log.warning("malformed progress.json at %s", path)
                return None
        except Exception as e:
            raise SSHCommandError(f"fetch_live_progress: {e}") from e

    async def sync_progress_to_history(
        self, history: BackupHistory, config: BackupConfig
    ) -> BackupProgress | None:
        """Pull progress.json and write byte-level fields into `history`.

        Commits inside. Returns the ingested progress (or None).
        """
        from serverpanel.infrastructure.database.models import (
            BackupHistory as _BackupHistory,
        )
        # Re-attach through the session to avoid stale ORM state
        _BackupHistory  # noqa: B018

        progress = await self.fetch_live_progress(config)
        if progress is None:
            return None

        history.bytes_total = progress.bytes_total
        history.bytes_done = progress.bytes_done
        history.current_item = progress.current_item
        history.progress_updated_at = progress.updated_at.replace(tzinfo=None)
        self.db.add(history)
        await self.db.commit()
        return progress

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

        from serverpanel.infrastructure.database.models import (
            BackupHistory as _BackupHistory,
        )
        BackupHistory = _BackupHistory  # noqa: N806 — shadow TYPE_CHECKING alias at runtime
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
