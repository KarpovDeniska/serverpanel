"""SQLAlchemy 2.0 ORM models."""

from __future__ import annotations

import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ============================================================
# Auth
# ============================================================


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(50), default="user")  # admin / user
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    oauth_accounts: Mapped[list[OAuthAccount]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    provider_configs: Mapped[list[ProviderConfig]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class OAuthAccount(Base):
    __tablename__ = "oauth_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(50))  # google, github
    provider_user_id: Mapped[str] = mapped_column(String(255))
    access_token: Mapped[str | None] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="oauth_accounts")


# ============================================================
# Providers & Servers
# ============================================================


class ProviderConfig(Base):
    __tablename__ = "provider_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider_type: Mapped[str] = mapped_column(String(50))  # hetzner_dedicated
    name: Mapped[str] = mapped_column(String(255))
    credentials_encrypted: Mapped[str] = mapped_column(Text)  # Fernet-encrypted JSON
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="provider_configs")
    servers: Mapped[list[Server]] = relationship(
        back_populates="provider_config", cascade="all, delete-orphan"
    )


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_config_id: Mapped[int] = mapped_column(
        ForeignKey("provider_configs.id", ondelete="CASCADE")
    )
    provider_server_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    os_type: Mapped[str | None] = mapped_column(String(50))

    ssh_username: Mapped[str | None] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_key_encrypted: Mapped[str | None] = mapped_column(Text)  # Fernet-encrypted
    # Pinned host key in OpenSSH line format ("ssh-ed25519 AAAA..." base64).
    # Populated on first successful SSH connect (TOFU). If set, subsequent
    # connects reject any other key; rescue-mode connects skip pinning since
    # the rescue OS rotates its host key on every boot.
    ssh_host_key_pub: Mapped[str | None] = mapped_column(Text)

    check_ports: Mapped[dict] = mapped_column(JSON, default=list)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    provider_config: Mapped[ProviderConfig] = relationship(back_populates="servers")
    storage_configs: Mapped[list[StorageConfig]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
    install_history: Mapped[list[InstallHistory]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
    backup_configs: Mapped[list[BackupConfig]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
    recovery_history: Mapped[list[RecoveryHistory]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
    monitored_services: Mapped[list[MonitoredService]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )


# ============================================================
# Storage
# ============================================================


class StorageConfig(Base):
    __tablename__ = "storage_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    storage_type: Mapped[str] = mapped_column(String(50))  # hetzner_storagebox, sftp, s3
    name: Mapped[str] = mapped_column(String(255))
    connection_encrypted: Mapped[str] = mapped_column(Text)  # Fernet-encrypted JSON
    base_path: Mapped[str] = mapped_column(String(500), default="/")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    server: Mapped[Server] = relationship(back_populates="storage_configs")


# ============================================================
# Install
# ============================================================


class InstallHistory(Base):
    __tablename__ = "install_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    current_step: Mapped[str | None] = mapped_column(String(255))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    log: Mapped[dict] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSON)  # {os_image_id, software_ids, hostname, ...}

    server: Mapped[Server] = relationship(back_populates="install_history")


# ============================================================
# Backups
# ============================================================


class BackupConfig(Base):
    __tablename__ = "backup_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    sources: Mapped[dict] = mapped_column(JSON)
    destinations: Mapped[dict] = mapped_column(JSON)
    schedule: Mapped[str | None] = mapped_column(String(100))
    rotation_days: Mapped[int] = mapped_column(Integer, default=14)
    stall_threshold_seconds: Mapped[int] = mapped_column(Integer, default=120)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    server: Mapped[Server] = relationship(back_populates="backup_configs")
    history: Mapped[list[BackupHistory]] = relationship(
        back_populates="backup_config", cascade="all, delete-orphan"
    )


class BackupHistory(Base):
    __tablename__ = "backup_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    backup_config_id: Mapped[int] = mapped_column(
        ForeignKey("backup_configs.id", ondelete="CASCADE")
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    current_step: Mapped[str | None] = mapped_column(String(255))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    # Byte-level progress, polled from remote `progress.json` during a run.
    # Null until the tracker writes its first tick; non-null after that.
    bytes_total: Mapped[int | None] = mapped_column(BigInteger)
    bytes_done: Mapped[int | None] = mapped_column(BigInteger)
    current_item: Mapped[str | None] = mapped_column(String(255))
    progress_updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)

    backup_config: Mapped[BackupConfig] = relationship(back_populates="history")


# ============================================================
# Recovery
# ============================================================


class RecoveryHistory(Base):
    __tablename__ = "recovery_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    scenario: Mapped[str] = mapped_column(String(50))   # c_drive, d_drive, both
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    current_step: Mapped[str | None] = mapped_column(String(255))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    log: Mapped[dict] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSON)          # {storage_config_id, software_ids, ...}

    server: Mapped[Server] = relationship(back_populates="recovery_history")


# ============================================================
# Monitoring
# ============================================================


class MonitoredService(Base):
    __tablename__ = "monitored_services"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    service_type: Mapped[str] = mapped_column(String(50))  # windows_service, systemd, process
    service_identifier: Mapped[str] = mapped_column(String(255))
    label: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    server: Mapped[Server] = relationship(back_populates="monitored_services")


# ============================================================
# Audit Log
# ============================================================


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(255))
    target_type: Mapped[str | None] = mapped_column(String(100))  # server, provider, backup...
    target_id: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
