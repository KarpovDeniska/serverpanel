"""Async SSH client wrapping paramiko via run_in_executor.

Host-key policy:
  - If `known_host_key` is provided, only that key is accepted. Mismatch raises
    `SSHHostKeyMismatch` (MITM or host re-provisioned).
  - Else if `on_host_key_learned` callback is provided, the first seen key is
    passed to the callback (caller persists it) — Trust-On-First-Use.
  - Else (rescue / bootstrap) any key is accepted (old behavior). Only legitimate
    use case: rescue Linux regenerates host keys every boot.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from io import StringIO

import paramiko

from serverpanel.domain.exceptions import SSHCommandError, SSHConnectionError

log = logging.getLogger(__name__)


class SSHHostKeyMismatch(SSHConnectionError):
    """Remote host presented a key different from the pinned one."""


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


def _host_key_line(key: paramiko.PKey) -> str:
    """Format a paramiko PKey as an OpenSSH 'known_hosts'-style single line
    ('<type> <base64-blob>') for stable string comparison."""
    return f"{key.get_name()} {base64.b64encode(key.asbytes()).decode('ascii')}"


class _PinnedPolicy(paramiko.MissingHostKeyPolicy):
    """Reject any missing key — used when we have a pinned key to enforce."""

    def missing_host_key(self, client, hostname, key):
        raise SSHHostKeyMismatch(
            f"Host key for {hostname} not in known hosts (expected pinned key)"
        )


class _CapturePolicy(paramiko.MissingHostKeyPolicy):
    """Accept any key and call `learned(line)` with its OpenSSH line form."""

    def __init__(self, learned: Callable[[str], None]):
        self._learned = learned

    def missing_host_key(self, client, hostname, key):
        try:
            self._learned(_host_key_line(key))
        except Exception:
            log.exception("host-key learn callback raised")


class AsyncSSHClient:
    """Async wrapper around paramiko SSHClient.

    All blocking paramiko calls run in a thread-pool executor.
    """

    def __init__(
        self,
        host: str,
        username: str = "root",
        password: str | None = None,
        port: int = 22,
        timeout: float = 30.0,
        private_key: str | None = None,
        key_passphrase: str | None = None,
        *,
        known_host_key: str | None = None,
        on_host_key_learned: Callable[[str], None] | None = None,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.private_key = private_key
        self.key_passphrase = key_passphrase
        self.known_host_key = known_host_key
        self.on_host_key_learned = on_host_key_learned
        self._client: paramiko.SSHClient | None = None

    async def _run(self, fn: Callable, *args, **kwargs):
        """Run a blocking function in executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, functools.partial(fn, *args, **kwargs)
        )

    def _load_pkey(self) -> paramiko.PKey | None:
        """Parse PEM private key (tries Ed25519, RSA, ECDSA, DSS if present)."""
        if not self.private_key:
            return None

        # DSSKey removed in paramiko 4.x — include only if available.
        pk_cls: list[type[paramiko.PKey]] = [
            paramiko.Ed25519Key,
            paramiko.RSAKey,
            paramiko.ECDSAKey,
        ]
        dss = getattr(paramiko, "DSSKey", None)
        if dss is not None:
            pk_cls.append(dss)

        last_err: Exception | None = None
        for cls in pk_cls:
            try:
                return cls.from_private_key(
                    StringIO(self.private_key), password=self.key_passphrase
                )
            except paramiko.SSHException as e:
                last_err = e
        raise SSHConnectionError(f"Cannot parse SSH private key: {last_err}")

    def _install_host_key_policy(self, client: paramiko.SSHClient) -> None:
        if self.known_host_key:
            host_keys = client.get_host_keys()
            try:
                key_type, blob_b64 = self.known_host_key.strip().split(None, 1)
                pkey_blob = base64.b64decode(blob_b64.split()[0])
            except Exception as e:
                raise SSHConnectionError(
                    f"Invalid stored host key format: {e}"
                ) from e
            key_cls = {
                "ssh-rsa": paramiko.RSAKey,
                "ssh-ed25519": paramiko.Ed25519Key,
                "ecdsa-sha2-nistp256": paramiko.ECDSAKey,
                "ecdsa-sha2-nistp384": paramiko.ECDSAKey,
                "ecdsa-sha2-nistp521": paramiko.ECDSAKey,
            }.get(key_type)
            if key_cls is None:
                raise SSHConnectionError(f"Unsupported host key type: {key_type}")
            try:
                pkey = key_cls(data=pkey_blob)
            except Exception as e:
                raise SSHConnectionError(f"Cannot reconstruct pinned host key: {e}") from e
            host_keys.add(self.host, key_type, pkey)
            if self.port != 22:
                host_keys.add(f"[{self.host}]:{self.port}", key_type, pkey)
            client.set_missing_host_key_policy(_PinnedPolicy())
        elif self.on_host_key_learned:
            client.set_missing_host_key_policy(_CapturePolicy(self.on_host_key_learned))
        else:
            # Bootstrap / rescue path. Logged once so ops can audit.
            log.warning(
                "SSH connect to %s without host-key verification (bootstrap mode)",
                self.host,
            )
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # noqa: S507

    async def connect(self) -> None:
        """Establish SSH connection."""
        try:
            client = paramiko.SSHClient()
            self._install_host_key_policy(client)
            pkey = self._load_pkey()
            await self._run(
                client.connect,
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                pkey=pkey,
                timeout=self.timeout,
                look_for_keys=False,
                allow_agent=False,
            )
            self._client = client
            log.info("SSH connected to %s@%s:%d", self.username, self.host, self.port)
        except SSHHostKeyMismatch:
            raise
        except Exception as e:
            raise SSHConnectionError(f"SSH connection failed to {self.host}: {e}") from e

    async def execute(
        self,
        command: str,
        timeout: float = 300.0,
    ) -> CommandResult:
        """Execute a command and return result."""
        if not self._client:
            raise SSHConnectionError("Not connected")

        try:
            def _exec():
                stdin, stdout, stderr = self._client.exec_command(
                    command, timeout=timeout
                )
                exit_code = stdout.channel.recv_exit_status()
                return CommandResult(
                    exit_code=exit_code,
                    stdout=stdout.read().decode("utf-8", errors="replace"),
                    stderr=stderr.read().decode("utf-8", errors="replace"),
                )

            return await self._run(_exec)
        except SSHConnectionError:
            raise
        except Exception as e:
            raise SSHCommandError(f"Command failed: {e}") from e

    async def execute_stream(
        self,
        command: str,
        on_output: Callable[[str], None] | None = None,
        timeout: float = 600.0,
    ) -> CommandResult:
        """Execute command with streaming output via callback."""
        if not self._client:
            raise SSHConnectionError("Not connected")

        try:
            def _exec_stream():
                transport = self._client.get_transport()
                channel = transport.open_session()
                channel.settimeout(timeout)
                channel.exec_command(command)

                output_lines = []
                while True:
                    if channel.recv_ready():
                        data = channel.recv(4096).decode("utf-8", errors="replace")
                        if data:
                            output_lines.append(data)
                            if on_output:
                                on_output(data)
                    if channel.exit_status_ready():
                        while channel.recv_ready():
                            data = channel.recv(4096).decode("utf-8", errors="replace")
                            if data:
                                output_lines.append(data)
                                if on_output:
                                    on_output(data)
                        break

                exit_code = channel.recv_exit_status()
                stderr = ""
                while channel.recv_stderr_ready():
                    stderr += channel.recv_stderr(4096).decode("utf-8", errors="replace")
                channel.close()

                return CommandResult(
                    exit_code=exit_code,
                    stdout="".join(output_lines),
                    stderr=stderr,
                )

            return await self._run(_exec_stream)
        except SSHConnectionError:
            raise
        except Exception as e:
            raise SSHCommandError(f"Command failed: {e}") from e

    async def put_file(self, remote_path: str, data: bytes | str) -> None:
        """Upload bytes or text to a remote path via SFTP."""
        if not self._client:
            raise SSHConnectionError("Not connected")

        if isinstance(data, str):
            data = data.encode("utf-8")

        def _op():
            sftp = self._client.open_sftp()
            try:
                with sftp.file(remote_path, "wb") as f:
                    f.write(data)
            finally:
                sftp.close()

        try:
            await self._run(_op)
        except Exception as e:
            raise SSHCommandError(f"SFTP upload failed ({remote_path}): {e}") from e

    async def fetch_file(self, remote_path: str) -> bytes:
        """Download a remote file via SFTP."""
        if not self._client:
            raise SSHConnectionError("Not connected")

        def _op() -> bytes:
            sftp = self._client.open_sftp()
            try:
                with sftp.file(remote_path, "rb") as f:
                    return f.read()
            finally:
                sftp.close()

        try:
            return await self._run(_op)
        except Exception as e:
            raise SSHCommandError(f"SFTP fetch failed ({remote_path}): {e}") from e

    async def close(self) -> None:
        """Close SSH connection. Failures during close are expected
        (connection already dropped by remote reboot) — log and move on."""
        if self._client:
            try:
                await self._run(self._client.close)
            except Exception as e:
                log.debug("SSH close() ignored error: %s", e)
            self._client = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()
