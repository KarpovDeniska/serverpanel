"""OS image catalog — available operating systems per provider."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OSImage:
    id: str
    name: str
    family: str  # "debian" | "rhel" | "windows"
    version: str
    provider_meta: dict[str, dict] = field(default_factory=dict)


OS_IMAGES: list[OSImage] = [
    # Ubuntu
    OSImage("ubuntu-2404", "Ubuntu 24.04 LTS", "debian", "24.04",
            {"hetzner_dedicated": {"file": "Ubuntu-2404-noble-amd64-base.tar.gz"}}),
    OSImage("ubuntu-2204", "Ubuntu 22.04 LTS", "debian", "22.04",
            {"hetzner_dedicated": {"file": "Ubuntu-2204-jammy-amd64-base.tar.gz"}}),
    # Debian
    OSImage("debian-12", "Debian 12 Bookworm", "debian", "12",
            {"hetzner_dedicated": {"file": "Debian-1208-bookworm-amd64-base.tar.gz"}}),
    OSImage("debian-11", "Debian 11 Bullseye", "debian", "11",
            {"hetzner_dedicated": {"file": "Debian-1110-bullseye-amd64-base.tar.gz"}}),
    # RHEL family
    OSImage("rocky-9", "Rocky Linux 9", "rhel", "9",
            {"hetzner_dedicated": {"file": "Rocky-94-amd64-base.tar.gz"}}),
    OSImage("alma-9", "AlmaLinux 9", "rhel", "9",
            {"hetzner_dedicated": {"file": "Alma-9-amd64-base.tar.gz"}}),
    OSImage("centos-stream-9", "CentOS Stream 9", "rhel", "9",
            {"hetzner_dedicated": {"file": "CentOS-Stream-9-amd64-base.tar.gz"}}),
    # Windows
    OSImage("win-2022", "Windows Server 2022", "windows", "2022",
            {"hetzner_dedicated": {"file": "Windows-2022-standard-amd64-base.tar.gz"}}),
    OSImage("win-2019", "Windows Server 2019", "windows", "2019",
            {"hetzner_dedicated": {"file": "Windows-2019-standard-amd64-base.tar.gz"}}),
    OSImage("win-2022-dc", "Windows Server 2022 Datacenter", "windows", "2022",
            {"hetzner_dedicated": {"file": "Windows-2022-datacenter-amd64-base.tar.gz"}}),
]


def get_images_for_provider(provider_type: str) -> list[OSImage]:
    return [img for img in OS_IMAGES if provider_type in img.provider_meta]


def get_image_by_id(image_id: str) -> OSImage | None:
    return next((img for img in OS_IMAGES if img.id == image_id), None)
