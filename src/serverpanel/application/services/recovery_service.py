"""Recovery service — three scenarios for bringing a dead/partially-dead server back.

Scenarios:
  - `c_drive` — system disk lost. Boot rescue Linux, repartition, wimapply Windows ISO
                from Storage Box, inject autounattend + post-install scripts, reboot.
  - `d_drive` — data disk lost. SSH to live Windows, initialize new disk, pull data
                from Storage Box.
  - `both`    — run `c_drive` with a flag that tells restore.ps1 to also rebuild D:.

Scripts live in `application/static/scripts/recovery/` — uploaded to rescue Linux
or new Windows over SSH. This service orchestrates the flow; it does not know
about the Windows specifics (those are in the shipped scripts).

Transactional contract:
  `run()` is a long-running job. It commits DB checkpoints between phases so
  the progress page reflects state even without WebSocket. The caller (the
  background task wrapper) owns the *final* commit and the failure fallback.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import shlex
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from serverpanel.config import get_settings
from serverpanel.domain.i18n import t
from serverpanel.domain.progress import NullProgressReporter, ProgressReporter
from serverpanel.infrastructure.crypto import decrypt_json
from serverpanel.infrastructure.providers import create_provider
from serverpanel.infrastructure.providers.storage import create_storage
from serverpanel.infrastructure.ssh.client import AsyncSSHClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from serverpanel.infrastructure.database.models import (
        RecoveryHistory,
        Server,
        StorageConfig,
    )

log = logging.getLogger(__name__)

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "static" / "scripts" / "recovery"

PHASES_C = 11
PHASES_D = 9


class RecoveryService:
    def __init__(
        self,
        db: AsyncSession,
        reporter: ProgressReporter | None = None,
    ):
        self.db = db
        self.reporter: ProgressReporter = reporter or NullProgressReporter()

    # =================================================================
    # Entry
    # =================================================================

    async def run(
        self, server: Server, history: RecoveryHistory, storage: StorageConfig
    ) -> None:
        """Dispatch on `history.scenario` and run it with uniform reporting."""
        history.started_at = datetime.datetime.now(datetime.UTC)
        history.status = "running"
        history.log = []
        await self._flush(history)
        await self.reporter.status("running")

        scenario = history.scenario
        try:
            if scenario == "c_drive":
                await self._recover_c_drive(server, history, storage, recover_d=False)
            elif scenario == "d_drive":
                await self._recover_d_drive(server, history, storage)
            elif scenario == "both":
                await self._recover_c_drive(server, history, storage, recover_d=True)
            else:
                raise ValueError(f"Unknown scenario: {scenario!r}")

            history.status = "success"
            history.progress = 100
            history.completed_at = datetime.datetime.now(datetime.UTC)
            await self._flush(history)
            await self.reporter.status("success")

        except Exception as e:
            log.exception("Recovery %s failed for server %s", scenario, server.id)
            history.status = "failed"
            history.error_message = str(e)
            history.completed_at = datetime.datetime.now(datetime.UTC)
            await self._flush(history)
            await self._log(history, f"ERROR: {e}", "error")
            await self.reporter.status("failed")

    # =================================================================
    # Scenario C — system disk
    # =================================================================

    async def _recover_c_drive(
        self,
        server: Server,
        history: RecoveryHistory,
        storage: StorageConfig,
        recover_d: bool,
    ) -> None:
        sb_conn = decrypt_json(storage.connection_encrypted)
        provider = self._get_provider(server)

        try:
            await self._step(history, t("install.activate_rescue"), 1, PHASES_C)
            rescue = await provider.activate_rescue(
                server.provider_server_id, os="linux", arch=64
            )
            rescue_password = rescue.password
            await self._log(history, t("recovery.rescue_ready"))

            await self._step(history, t("install.hard_reset"), 2, PHASES_C)
            await provider.reset_server(server.provider_server_id, "hw")
            await self._log(history, t("recovery.reset_sent"))
        finally:
            await provider.close()

        await self._step(history, t("recovery.wait_rescue_boot"), 3, PHASES_C)
        await self._wait_for_ssh(server.ip_address, timeout=get_settings().recovery_wait_rescue_timeout)

        async with AsyncSSHClient(
            host=server.ip_address, username="root", password=rescue_password
        ) as ssh:
            await self._step(history, t("recovery.install_tools"), 4, PHASES_C)
            r = await ssh.execute("apt-get update && apt-get install -y wimtools ntfs-3g", timeout=600)
            if r.exit_code != 0:
                raise RuntimeError(f"apt-get failed: {r.stderr[-400:]}")

            await self._step(history, t("recovery.iso_download"), 5, PHASES_C)
            iso_src = history.config.get("iso_remote_path", "/backups/software/windows_server_2022.iso")
            iso_local = "/tmp/win.iso"
            await self._sb_scp_to_rescue(ssh, sb_conn, iso_src, iso_local, timeout=get_settings().recovery_iso_download_timeout)
            await self._log(history, t("recovery.iso_ok", path=iso_local))

            await self._step(history, t("recovery.partition"), 6, PHASES_C)
            await self._upload_and_run(ssh, "partition_disk.sh", args=["/dev/sda"])

            await self._step(history, t("recovery.wimapply"), 7, PHASES_C)
            await self._upload_and_run(ssh, "apply_windows.sh", timeout=get_settings().recovery_wimapply_timeout)

            await self._step(history, t("recovery.inject"), 8, PHASES_C)
            config_json = self._build_windows_config(history, storage, sb_conn, recover_d)
            await ssh.put_file("/tmp/config.json", config_json)
            # inject_config.sh копирует эти файлы в /mnt/win/Windows/Setup/Scripts/
            for name in (
                "SetupComplete.cmd",
                "restore.ps1",
                "restore_data.ps1",
                "install_software.ps1",
            ):
                await ssh.put_file(
                    f"/tmp/{name}",
                    (_SCRIPTS_DIR / name).read_bytes(),
                )
            await self._upload_and_run(ssh, "inject_config.sh")

            await self._step(history, t("recovery.bcd"), 9, PHASES_C)
            bcd_src = history.config.get("bcd_remote_path", "/backups/software/BCD")
            await self._sb_scp_to_rescue(ssh, sb_conn, bcd_src, "/mnt/boot/Boot/BCD")

            await self._step(history, t("recovery.disable_rescue"), 10, PHASES_C)

        # reboot the box back into the freshly-installed Windows
        provider = self._get_provider(server)
        try:
            await provider.deactivate_rescue(server.provider_server_id)
            await provider.reset_server(server.provider_server_id, "hw")
        finally:
            await provider.close()

        # Windows was reinstalled → the old pinned host key is no longer valid.
        # Clear it so the next SSH connect re-pins via TOFU.
        if server.ssh_host_key_pub:
            server.ssh_host_key_pub = None
            self.db.add(server)
            await self._flush(history)

        await self._step(history, t("recovery.wait_windows"), 11, PHASES_C)
        await self._poll_restore_status(history, storage, sb_conn, timeout=get_settings().recovery_poll_status_timeout)

    # =================================================================
    # Scenario D — data disk
    # =================================================================

    async def _recover_d_drive(
        self,
        server: Server,
        history: RecoveryHistory,
        storage: StorageConfig,
    ) -> None:
        sb_conn = decrypt_json(storage.connection_encrypted)
        server_ssh = decrypt_json(server.ssh_key_encrypted) if server.ssh_key_encrypted else {}

        learned: dict[str, str] = {}

        async with AsyncSSHClient(
            host=server.ip_address,
            username=server.ssh_username or "Administrator",
            port=server.ssh_port or 22,
            password=server_ssh.get("password"),
            private_key=server_ssh.get("private_key"),
            key_passphrase=server_ssh.get("passphrase"),
            known_host_key=server.ssh_host_key_pub,
            on_host_key_learned=(
                (lambda line: learned.__setitem__("line", line))
                if not server.ssh_host_key_pub else None
            ),
        ) as ssh:
            if learned.get("line") and not server.ssh_host_key_pub:
                server.ssh_host_key_pub = learned["line"]
                self.db.add(server)
                await self.db.commit()
            await self._step(history, t("recovery.init_d"), 1, PHASES_D)
            await ssh.execute(
                'powershell -NoProfile -Command '
                '"Get-Disk | Where-Object {$_.PartitionStyle -eq \'RAW\'} | '
                'Initialize-Disk -PartitionStyle MBR -PassThru | '
                'New-Partition -UseMaximumSize -DriveLetter D | '
                'Format-Volume -FileSystem NTFS -NewFileSystemLabel Data -Confirm:$false"',
                timeout=120,
            )

            await self._step(history, t("recovery.upload_restore"), 2, PHASES_D)
            restore_script = _SCRIPTS_DIR / "restore_data.ps1"
            await ssh.put_file(r"C:\ProgramData\serverpanel\restore_data.ps1",
                               restore_script.read_bytes())
            restore_config = json.dumps({
                "storage_box": sb_conn,
                "sources": history.config.get("sources", []),
                "daily_folder": history.config.get("daily_folder", "latest"),
            }, ensure_ascii=False)
            await ssh.put_file(r"C:\ProgramData\serverpanel\restore.json", restore_config)

            await self._step(history, t("recovery.restore_data"), 3, PHASES_D)
            r = await ssh.execute(
                r'powershell -NoProfile -ExecutionPolicy Bypass -File '
                r'C:\ProgramData\serverpanel\restore_data.ps1 '
                r'-ConfigPath C:\ProgramData\serverpanel\restore.json',
                timeout=3600,
            )
            if r.exit_code != 0:
                raise RuntimeError(f"restore_data.ps1 exit={r.exit_code}: {r.stderr[-400:]}")
            for line in (r.stdout or "").splitlines()[-30:]:
                if line.strip():
                    await self._log(history, line.strip())

            await self._step(history, t("recovery.reinstall_backup_cfg"), 4, PHASES_D)
            await self._log(history, t("recovery.reinstall_hint"))

            await self._step(history, t("recovery.done"), PHASES_D, PHASES_D)

    # =================================================================
    # Helpers
    # =================================================================

    def _get_provider(self, server: Server):
        creds = decrypt_json(server.provider_config.credentials_encrypted)
        return create_provider(server.provider_config.provider_type, creds)

    def _build_windows_config(
        self,
        history: RecoveryHistory,
        storage: StorageConfig,
        sb_conn: dict,
        recover_d: bool,
    ) -> str:
        """Build config.json consumed by restore.ps1 on the freshly-installed Windows."""
        cfg = history.config
        return json.dumps({
            "storage_box": {
                "host": sb_conn.get("host"),
                "user": sb_conn.get("user"),
                "port": sb_conn.get("port", 23),
                "private_key": sb_conn.get("private_key"),
            },
            "windows": {
                "product_key": cfg.get("product_key"),
                "admin_password": cfg.get("admin_password"),
                "hostname": cfg.get("hostname", "server"),
            },
            "software": cfg.get("software", {}),
            "recover_d_drive": recover_d,
            "daily_folder": cfg.get("daily_folder", "latest"),
        }, ensure_ascii=False, indent=2)

    async def _upload_and_run(
        self,
        ssh: AsyncSSHClient,
        script_name: str,
        args: list[str] | None = None,
        timeout: float = 600,
    ) -> None:
        path = _SCRIPTS_DIR / script_name
        if not path.exists():
            raise FileNotFoundError(f"recovery script missing: {path}")
        if "/" in script_name or "\\" in script_name or ".." in script_name:
            raise ValueError(f"invalid script name: {script_name!r}")
        remote = f"/tmp/{script_name}"
        await ssh.put_file(remote, path.read_bytes())
        quoted_args = " ".join(shlex.quote(a) for a in (args or []))
        cmd = f"chmod +x {shlex.quote(remote)} && {shlex.quote(remote)}"
        if quoted_args:
            cmd += " " + quoted_args
        r = await ssh.execute(cmd, timeout=timeout)
        if r.exit_code != 0:
            raise RuntimeError(
                f"{script_name} failed (exit {r.exit_code}): {r.stderr[-400:]}"
            )

    async def _sb_scp_to_rescue(
        self,
        ssh: AsyncSSHClient,
        sb_conn: dict,
        remote_src: str,
        local_dst: str,
        timeout: float = 1800,
    ) -> None:
        """Run scp on the rescue Linux to pull a file from Storage Box."""
        if sb_conn.get("private_key"):
            await ssh.execute("mkdir -p /root/.ssh && chmod 700 /root/.ssh", timeout=10)
            await ssh.put_file("/root/.ssh/sb_key", sb_conn["private_key"])
            await ssh.execute("chmod 600 /root/.ssh/sb_key", timeout=10)
            id_args = ["-i", "/root/.ssh/sb_key"]
        else:
            id_args = []

        port = int(sb_conn.get("port", 23))
        user = str(sb_conn["user"])
        host = str(sb_conn["host"])
        remote_spec = f"{user}@{host}:{remote_src}"

        parts = [
            "scp",
            "-P", str(port),
            *id_args,
            "-o", "StrictHostKeyChecking=accept-new",
            remote_spec,
            local_dst,
        ]
        cmd = " ".join(shlex.quote(p) for p in parts)
        r = await ssh.execute(cmd, timeout=timeout)
        if r.exit_code != 0:
            raise RuntimeError(f"scp from SB failed: {r.stderr[-400:]}")

    async def _poll_restore_status(
        self,
        history: RecoveryHistory,
        storage: StorageConfig,
        sb_conn: dict,
        timeout: float = 3600,
    ) -> None:
        """Poll recovery_status.json on Storage Box while the fresh Windows runs restore.ps1."""
        box = create_storage(storage.storage_type, sb_conn)
        deadline = asyncio.get_event_loop().time() + timeout
        last_progress = -1
        try:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    data = await box.read_file("/backups/status/recovery_status.json")
                    status = json.loads(data.decode("utf-8"))
                    step = status.get("step", "")
                    progress = int(status.get("progress", 0))
                    message = status.get("message", "")
                    if progress != last_progress:
                        await self._log(history, f"[Windows] {step}: {message} ({progress}%)")
                        last_progress = progress
                    if step == "done" or progress >= 100:
                        return
                    if step == "error":
                        raise RuntimeError(f"restore.ps1 error: {message}")
                except FileNotFoundError:
                    pass
                except Exception as e:
                    log.debug("poll restore_status: %s", e)
                await asyncio.sleep(15)
            raise TimeoutError("restore.ps1 did not report completion within timeout")
        finally:
            await box.close()

    async def _wait_for_ssh(
        self, ip: str, port: int = 22, timeout: float = 300, interval: float = 10
    ) -> None:
        elapsed = 0.0
        while elapsed < timeout:
            try:
                sock = socket.create_connection((ip, port), timeout=5)
                sock.close()
                return
            except (TimeoutError, OSError):
                await self.reporter.log(t("recovery.ssh_wait", sec=int(elapsed)), "info")
                await asyncio.sleep(interval)
                elapsed += interval
        raise TimeoutError(f"SSH unreachable after {timeout}s")

    # --- progress / log helpers ---

    async def _step(
        self, history: RecoveryHistory, name: str, num: int, total: int
    ) -> None:
        history.current_step = name
        history.progress = int((num / total) * 100)
        await self._flush(history)
        await self.reporter.progress(name, num, total)

    async def _log(
        self, history: RecoveryHistory, msg: str, level: str = "info"
    ) -> None:
        entry = {
            "time": datetime.datetime.now(datetime.UTC).isoformat(),
            "message": msg,
            "level": level,
        }
        history.log = [*(history.log or []), entry]
        await self._flush(history)
        await self.reporter.log(msg, level)

    async def _flush(self, history: RecoveryHistory) -> None:
        """Checkpoint commit — long-running job persists progress between phases."""
        try:
            self.db.add(history)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
