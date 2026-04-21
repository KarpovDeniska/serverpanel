"""Install service — orchestrates OS installation and post-install setup.

Transactional contract:
  `run()` is a long-running job; it commits DB checkpoints between phases so
  the progress page reflects state even without WebSocket. The caller (the
  background task wrapper) owns the final commit and the failure fallback.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import secrets
import shlex
import socket
from typing import TYPE_CHECKING

from serverpanel.application.catalogs.os_images import get_image_by_id
from serverpanel.application.catalogs.software import get_software_by_ids
from serverpanel.domain.i18n import t
from serverpanel.domain.progress import NullProgressReporter, ProgressReporter
from serverpanel.infrastructure.crypto import decrypt_json
from serverpanel.infrastructure.providers import create_provider
from serverpanel.infrastructure.ssh.client import AsyncSSHClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from serverpanel.infrastructure.database.models import InstallHistory, Server

log = logging.getLogger(__name__)

TOTAL_PHASES = 12


class InstallService:
    """Orchestrates full server OS installation."""

    def __init__(self, db: AsyncSession, reporter: ProgressReporter | None = None):
        self.db = db
        self.reporter: ProgressReporter = reporter or NullProgressReporter()

    async def run(self, server: Server, history: InstallHistory) -> None:
        config = history.config
        os_image = get_image_by_id(config["os_image_id"])
        hostname = config.get("hostname", "server")
        ssh_keys = config.get("ssh_keys", [])
        software_ids = config.get("software_ids", [])
        enable_firewall = config.get("enable_firewall", False)
        open_ports = config.get("open_ports", [22])

        temp_password = secrets.token_urlsafe(24)

        try:
            history.status = "running"
            history.started_at = datetime.datetime.now(datetime.UTC)
            await self._flush(history)
            await self.reporter.status("running")

            # === PHASE 1: OS INSTALL ===

            await self._step(history, t("install.activate_rescue"), 1)
            rescue_password = await self._activate_rescue(server)
            await self._log(history, t("install.rescue_ready"))

            await self._step(history, t("install.hard_reset"), 2)
            await self._reset_server(server)
            await self._log(history, t("install.reset_sent"))

            await self._step(history, t("install.wait_rescue_ssh"), 3)
            await self._wait_for_ssh(server.ip_address, timeout=300)
            await self._log(history, t("install.ssh_ready"))

            async with AsyncSSHClient(
                host=server.ip_address, username="root", password=rescue_password
            ) as ssh:
                await self._step(history, t("install.prepare_config"), 4)
                provider_type = server.provider_config.provider_type
                installimage_file = os_image.provider_meta[provider_type]["file"]
                autosetup = self._build_autosetup(hostname, installimage_file)
                await ssh.execute(f"cat > /autosetup << 'EOF'\n{autosetup}\nEOF")
                await self._log(history, t("install.image_line", name=os_image.name))
                await self._log(history, t("install.hostname_line", name=hostname))

                await self._step(history, t("install.installing", name=os_image.name), 5)
                result = await ssh.execute("installimage -a -c /autosetup", timeout=900)
                if result.exit_code != 0:
                    await self._log(history, result.stderr or result.stdout, "error")
                    raise RuntimeError(f"installimage failed (exit {result.exit_code})")
                for line in result.stdout.strip().split("\n")[-10:]:
                    await self.reporter.log(line, "output")
                await self._log(history, t("install.installimage_done"))

                await self._step(history, t("install.inject_creds"), 6)
                await self._inject_credentials(ssh, ssh_keys, temp_password)
                await self._log(history, t("install.creds_done"))

                await self._step(history, t("install.reboot_new_os"), 7)
                try:
                    await ssh.execute("reboot", timeout=10)
                except Exception as e:
                    # reboot closes SSH connection abruptly — expected.
                    log.debug("reboot SSH closed as expected: %s", e)

            # === PHASE 2: POST-INSTALL ===

            await self._step(history, t("install.wait_new_os"), 8)
            await asyncio.sleep(20)  # Let rescue shut down
            await self._wait_for_ssh(server.ip_address, timeout=300)
            await self._log(history, t("install.new_os_ready"))

            async with AsyncSSHClient(
                host=server.ip_address, username="root", password=temp_password
            ) as ssh:
                await self._step(history, t("install.update_system"), 9)
                if os_image.family == "windows":
                    ps = f"Rename-Computer -NewName {shlex.quote(hostname)} -Force"
                    await ssh.execute(
                        f'powershell -NoProfile -Command {shlex.quote(ps)}',
                        timeout=30,
                    )
                    await self._log(history, t("install.hostname_done"))
                else:
                    await ssh.execute(
                        f"hostnamectl set-hostname {shlex.quote(hostname)}", timeout=30
                    )
                    pkg = "apt-get" if os_image.family == "debian" else "dnf"
                    env = "export DEBIAN_FRONTEND=noninteractive && " if os_image.family == "debian" else ""
                    await ssh.execute(
                        f"{env}{pkg} update -y && {pkg} upgrade -y", timeout=600
                    )
                    await self._log(history, t("install.system_updated"))

                await self._step(history, t("install.install_software"), 10)
                software = get_software_by_ids(software_ids)
                for pkg_info in software:
                    commands = pkg_info.install_commands.get(os_image.family, [])
                    if not commands:
                        continue
                    await self._log(history, t("install.installing_pkg", name=pkg_info.name))
                    for cmd in commands:
                        if os_image.family in ("debian", "rhel"):
                            env_prefix = "export DEBIAN_FRONTEND=noninteractive && " if os_image.family == "debian" else ""
                            r = await ssh.execute(f"{env_prefix}{cmd}", timeout=300)
                        else:
                            r = await ssh.execute(cmd, timeout=300)
                        if r.exit_code != 0:
                            await self._log(
                                history,
                                t("install.pkg_warn", name=pkg_info.name, err=r.stderr[:200]),
                                "error",
                            )
                    await self._log(history, t("install.pkg_done", name=pkg_info.name))

                await self._step(history, t("install.configure_firewall"), 11)
                if enable_firewall:
                    await self._configure_firewall(ssh, os_image.family, open_ports)
                    await self._log(history, t("install.firewall_done", ports=open_ports))
                else:
                    await self._log(history, t("install.firewall_skipped"))

            await self._step(history, t("install.done"), 12)
            history.status = "success"
            history.completed_at = datetime.datetime.now(datetime.UTC)
            history.progress = 100
            await self._flush(history)

            # Update server in DB
            server.os_type = os_image.name
            self.db.add(server)
            await self.db.commit()

            await self.reporter.status("success")
            await self._log(history, t("install.final", name=os_image.name))

        except Exception as e:
            log.exception("Install failed for server %s", server.id)
            history.status = "failed"
            history.completed_at = datetime.datetime.now(datetime.UTC)
            history.error_message = str(e)
            await self._flush(history)
            await self.reporter.status("failed")
            await self.reporter.log(t("install.error", msg=str(e)), "error")

    # --- Firewall ---

    async def _configure_firewall(
        self, ssh: AsyncSSHClient, family: str, open_ports: list[int]
    ) -> None:
        # Open ports arrive from HTTP form parsed to int — safe to interpolate.
        ports = [int(p) for p in open_ports]
        if family == "debian":
            await ssh.execute("apt-get install -y ufw", timeout=60)
            await ssh.execute("ufw default deny incoming", timeout=10)
            await ssh.execute("ufw default allow outgoing", timeout=10)
            for port in ports:
                await ssh.execute(f"ufw allow {port}/tcp", timeout=10)
            await ssh.execute("echo 'y' | ufw enable", timeout=10)
        elif family == "rhel":
            await ssh.execute("systemctl enable --now firewalld", timeout=30)
            for port in ports:
                await ssh.execute(
                    f"firewall-cmd --permanent --add-port={port}/tcp", timeout=10
                )
            await ssh.execute("firewall-cmd --reload", timeout=10)
        elif family == "windows":
            for port in ports:
                ps = (
                    f"New-NetFirewallRule -DisplayName 'Port {port}' "
                    f"-Direction Inbound -LocalPort {port} -Protocol TCP -Action Allow"
                )
                await ssh.execute(
                    f"powershell -NoProfile -Command {shlex.quote(ps)}", timeout=30
                )

    # --- Provider operations ---

    async def _activate_rescue(self, server: Server) -> str:
        provider = self._get_provider(server)
        try:
            rescue = await provider.activate_rescue(server.provider_server_id)
            return rescue.password
        finally:
            await provider.close()

    async def _reset_server(self, server: Server) -> None:
        provider = self._get_provider(server)
        try:
            await provider.reset_server(server.provider_server_id, "hw")
        finally:
            await provider.close()

    def _get_provider(self, server: Server):
        credentials = decrypt_json(server.provider_config.credentials_encrypted)
        return create_provider(server.provider_config.provider_type, credentials)

    # --- SSH helpers ---

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
        raise TimeoutError(f"SSH недоступен после {timeout}s")

    def _build_autosetup(self, hostname: str, image_file: str) -> str:
        # installimage config — not a shell, but we still strip newlines
        # from hostname so a malicious value can't inject extra directives.
        safe_host = hostname.replace("\n", "").replace("\r", "").strip()
        return f"""DRIVE1 /dev/sda
BOOTLOADER grub
HOSTNAME {safe_host}
PART /boot ext4 1G
PART lvm vg0 all
LV vg0 root / ext4 all
IMAGE /root/.oldroot/nfs/images/{image_file}
"""

    async def _inject_credentials(
        self, ssh: AsyncSSHClient, ssh_keys: list[str], temp_password: str
    ) -> None:
        """Inject SSH keys and temp password into installed OS before reboot."""
        # Find the root volume (try common paths)
        for dev in ["/dev/vg0/root", "/dev/sda1", "/dev/md1"]:
            r = await ssh.execute(f"mount {dev} /mnt 2>/dev/null", timeout=10)
            if r.exit_code == 0:
                break
        else:
            await ssh.execute("vgscan && vgchange -ay", timeout=10)
            await ssh.execute("mount /dev/vg0/root /mnt", timeout=10)

        try:
            # SSH keys — upload via SFTP to avoid shell quoting of arbitrary pubkey text.
            if ssh_keys:
                await ssh.execute("mkdir -p /mnt/root/.ssh && chmod 700 /mnt/root/.ssh", timeout=10)
                keys_text = "\n".join(k.strip() for k in ssh_keys if k and k.strip()) + "\n"
                await ssh.put_file("/mnt/root/.ssh/authorized_keys", keys_text)
                await ssh.execute("chmod 600 /mnt/root/.ssh/authorized_keys", timeout=10)

            # Temp password for post-install SSH — pipe via stdin so value never
            # touches the shell command line. put_file + chpasswd -m with the file
            # is simpler; but chpasswd reads stdin, so we use a here-doc guarded
            # by shlex.quote on the user part only.
            pw_line = f"root:{temp_password}"
            await ssh.put_file("/mnt/root/.chpasswd_input", pw_line)
            await ssh.execute(
                "chroot /mnt bash -c 'chpasswd < /root/.chpasswd_input && rm -f /root/.chpasswd_input'",
                timeout=10,
            )
        finally:
            await ssh.execute("umount /mnt", timeout=10)

    # --- Progress helpers ---

    async def _step(self, history: InstallHistory, name: str, num: int) -> None:
        history.current_step = name
        history.progress = int((num / TOTAL_PHASES) * 100)
        await self._flush(history)
        await self.reporter.progress(name, num, TOTAL_PHASES)

    async def _log(self, history: InstallHistory, msg: str, level: str = "info") -> None:
        entry = {"time": datetime.datetime.now(datetime.UTC).isoformat(), "message": msg, "level": level}
        history.log = [*(history.log or []), entry]
        await self._flush(history)
        await self.reporter.log(msg, level)

    async def _flush(self, history: InstallHistory) -> None:
        """Checkpoint commit between phases."""
        try:
            self.db.add(history)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
