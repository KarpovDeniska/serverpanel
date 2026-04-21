"""Server templates — pre-configured deployment scenarios."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServerTemplate:
    id: str
    name: str
    icon: str
    short_desc: str
    description: str  # detailed HTML-safe description
    recommended_os: str  # os_image id
    software_ids: list[str] = field(default_factory=list)
    software_by_family: dict[str, list[str]] = field(default_factory=dict)  # overrides software_ids per OS family
    default_ports: list[int] = field(default_factory=list)
    firewall: bool = True


SERVER_TEMPLATES: list[ServerTemplate] = [
    ServerTemplate(
        id="vpn",
        name="VPN-сервер",
        icon="shield",
        short_desc="Xray VLESS + Reality — обход блокировок",
        description=(
            "<b>3X-UI</b> (рекомендуется) — веб-панель управления Xray. "
            "Поддерживает VLESS, Trojan, Shadowsocks. "
            "Веб-интерфейс на порту 2053 для управления пользователями "
            "и протоколами.<br><br>"
            "<b>Xray standalone</b> — ядро Xray без панели. "
            "VLESS + Reality + XHTTP — маскировка под обычный HTTPS-трафик "
            "(github.com). Не определяется DPI. Порт 8443.<br><br>"
            "<b>Протоколы:</b><br>"
            "&#8226; <b>VLESS + Reality</b> — без собственного TLS-сертификата, "
            "использует TLS отпечаток реального сайта. Не блокируется.<br>"
            "&#8226; <b>XHTTP</b> — транспорт поверх HTTP, мультиплексирование.<br>"
            "&#8226; <b>WireGuard</b> — классический VPN (UDP:51820), быстрый, "
            "но легко детектируется и блокируется."
        ),
        recommended_os="ubuntu-2404",
        software_ids=["3x-ui"],
        default_ports=[22, 2053, 8443],
        firewall=True,
    ),
    ServerTemplate(
        id="cloud",
        name="Файловое облако",
        icon="cloud",
        short_desc="Nextcloud — своё облачное хранилище",
        description=(
            "<b>Nextcloud</b> — полноценная замена Google Drive / Dropbox. "
            "Файлы, календарь, контакты, совместное редактирование.<br><br>"
            "Устанавливается в Docker-контейнерах: Nextcloud + MariaDB. "
            "Веб-интерфейс на порту 80. Рекомендуется настроить SSL "
            "(certbot) после установки.<br><br>"
            "Клиенты: веб-браузер, Windows, macOS, Linux, iOS, Android."
        ),
        recommended_os="ubuntu-2404",
        software_ids=["nextcloud", "certbot"],
        default_ports=[22, 80, 443],
        firewall=True,
    ),
    ServerTemplate(
        id="1c",
        name="Сервер 1С",
        icon="building",
        short_desc="1С:Предприятие + PostgreSQL или MS SQL",
        description=(
            "Инфраструктура для <b>1С:Предприятие</b>:<br><br>"
            "<b>Windows:</b> зависимости 1С + MS SQL + IIS<br>"
            "<b>Linux:</b> зависимости 1С + PostgreSQL + Apache<br><br>"
            "Состав предвыбирается автоматически по ОС. "
            "Можно заменить: MS SQL на PostgreSQL, Apache на Nginx и т.д.<br><br>"
            "После установки загрузите пакеты 1С с releases.1c.ru."
        ),
        recommended_os="",
        software_ids=["1c-server"],
        software_by_family={
            "debian": ["1c-server", "postgresql", "apache"],
            "rhel": ["1c-server", "postgresql", "apache"],
            "windows": ["1c-server", "mssql", "iis"],
        },
        default_ports=[22, 80, 443, 1541],
        firewall=True,
    ),
    ServerTemplate(
        id="custom",
        name="Свой сервер",
        icon="settings",
        short_desc="Ручной выбор ОС и компонентов",
        description=(
            "Полностью ручная настройка: выберите операционную систему "
            "и любые компоненты из каталога."
        ),
        recommended_os="",
        software_ids=[],
        default_ports=[22, 80, 443],
        firewall=True,
    ),
]


def get_templates() -> list[ServerTemplate]:
    return SERVER_TEMPLATES


def get_template_by_id(template_id: str) -> ServerTemplate | None:
    return next((t for t in SERVER_TEMPLATES if t.id == template_id), None)
