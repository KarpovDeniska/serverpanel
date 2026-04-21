"""Software package catalog — pre-configured packages for post-install."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SoftwarePackage:
    id: str
    name: str
    description: str
    category: str
    install_commands: dict[str, list[str]]  # {os_family: [commands]}
    ports: list[int] = field(default_factory=list)


CATEGORY_LABELS = {
    "web": "Web-серверы",
    "databases": "Базы данных",
    "containers": "Контейнеры",
    "runtimes": "Рантаймы",
    "vpn": "VPN",
    "cloud": "Облачные сервисы",
    "enterprise": "Корпоративное ПО",
    "security": "Безопасность",
    "tools": "Утилиты",
}

SOFTWARE_CATALOG: list[SoftwarePackage] = [
    # Web
    SoftwarePackage(
        "nginx", "Nginx", "Высокопроизводительный веб-сервер и reverse proxy",
        "web",
        {"debian": ["apt-get install -y nginx", "systemctl enable nginx"],
         "rhel": ["dnf install -y nginx", "systemctl enable nginx"]},
        ports=[80, 443],
    ),
    SoftwarePackage(
        "apache", "Apache", "HTTP-сервер Apache",
        "web",
        {"debian": ["apt-get install -y apache2", "systemctl enable apache2"],
         "rhel": ["dnf install -y httpd", "systemctl enable httpd"]},
        ports=[80, 443],
    ),
    # Databases
    SoftwarePackage(
        "postgresql", "PostgreSQL", "Реляционная база данных",
        "databases",
        {"debian": ["apt-get install -y postgresql postgresql-client", "systemctl enable postgresql"],
         "rhel": ["dnf install -y postgresql-server postgresql", "postgresql-setup --initdb", "systemctl enable postgresql"],
         "windows": [
            "powershell -Command \"[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://get.enterprisedb.com/postgresql/postgresql-15.8-1-windows-x64.exe' -OutFile postgresql.exe; Start-Process ./postgresql.exe -ArgumentList '--mode unattended --superpassword postgres --serverport 5432 --install_runtimes 0' -Wait; Remove-Item postgresql.exe\"",
        ]},
        ports=[5432],
    ),
    SoftwarePackage(
        "mysql", "MySQL / MariaDB", "Реляционная СУБД",
        "databases",
        {"debian": ["apt-get install -y mariadb-server mariadb-client", "systemctl enable mariadb"],
         "rhel": ["dnf install -y mariadb-server mariadb", "systemctl enable mariadb"]},
        ports=[3306],
    ),
    SoftwarePackage(
        "redis", "Redis", "In-memory key-value хранилище",
        "databases",
        {"debian": ["apt-get install -y redis-server", "systemctl enable redis-server"],
         "rhel": ["dnf install -y redis", "systemctl enable redis"]},
        ports=[6379],
    ),
    SoftwarePackage(
        "mongodb", "MongoDB", "Документо-ориентированная БД",
        "databases",
        {"debian": [
            "curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg",
            "echo 'deb [signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] http://repo.mongodb.org/apt/debian bookworm/mongodb-org/7.0 main' > /etc/apt/sources.list.d/mongodb-org-7.0.list",
            "apt-get update && apt-get install -y mongodb-org",
            "systemctl enable mongod",
        ]},
        ports=[27017],
    ),
    # Containers
    SoftwarePackage(
        "docker", "Docker", "Контейнерный рантайм + Docker Compose",
        "containers",
        {"debian": ["curl -fsSL https://get.docker.com | sh", "systemctl enable docker"],
         "rhel": ["dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo",
                   "dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin",
                   "systemctl enable docker"]},
    ),
    # Runtimes
    SoftwarePackage(
        "nodejs", "Node.js 20 LTS", "JavaScript рантайм + npm",
        "runtimes",
        {"debian": ["curl -fsSL https://deb.nodesource.com/setup_20.x | bash -", "apt-get install -y nodejs"],
         "rhel": ["curl -fsSL https://rpm.nodesource.com/setup_20.x | bash -", "dnf install -y nodejs"]},
    ),
    SoftwarePackage(
        "python3", "Python 3 + pip", "Python 3 с pip и venv",
        "runtimes",
        {"debian": ["apt-get install -y python3 python3-pip python3-venv"],
         "rhel": ["dnf install -y python3 python3-pip"]},
    ),
    SoftwarePackage(
        "php", "PHP 8 + FPM", "PHP с модулями и FPM",
        "runtimes",
        {"debian": ["apt-get install -y php-fpm php-cli php-common php-mysql php-pgsql php-curl php-xml php-mbstring",
                     "systemctl enable php*-fpm"],
         "rhel": ["dnf install -y php-fpm php-cli php-common php-mysqlnd php-pgsql php-curl php-xml php-mbstring",
                   "systemctl enable php-fpm"]},
    ),
    SoftwarePackage(
        "dotnet", ".NET 8 SDK", "Microsoft .NET SDK и рантайм",
        "runtimes",
        {"debian": ["apt-get install -y dotnet-sdk-8.0"],
         "rhel": ["dnf install -y dotnet-sdk-8.0"]},
    ),
    # Security
    SoftwarePackage(
        "fail2ban", "Fail2Ban", "Защита от брутфорса SSH и сервисов",
        "security",
        {"debian": ["apt-get install -y fail2ban", "systemctl enable fail2ban"],
         "rhel": ["dnf install -y fail2ban", "systemctl enable fail2ban"]},
    ),
    SoftwarePackage(
        "ufw", "UFW", "Простой файрвол (Uncomplicated Firewall)",
        "security",
        {"debian": ["apt-get install -y ufw"]},
    ),
    # Tools
    SoftwarePackage(
        "htop", "htop + tools", "htop, curl, wget, git, mc, tmux",
        "tools",
        {"debian": ["apt-get install -y htop curl wget git mc tmux unzip"],
         "rhel": ["dnf install -y htop curl wget git mc tmux unzip"]},
    ),
    SoftwarePackage(
        "certbot", "Certbot", "Автоматические SSL-сертификаты Let's Encrypt",
        "tools",
        {"debian": ["apt-get install -y certbot"],
         "rhel": ["dnf install -y certbot"]},
        ports=[80, 443],
    ),
    # VPN
    SoftwarePackage(
        "3x-ui", "3X-UI", "Панель управления Xray — VLESS, Trojan, Shadowsocks с веб-интерфейсом",
        "vpn",
        {"debian": [
            "bash -c 'curl -fsSL https://raw.githubusercontent.com/MHSanaei/3x-ui/master/install.sh | bash'",
        ],
         "rhel": [
            "bash -c 'curl -fsSL https://raw.githubusercontent.com/MHSanaei/3x-ui/master/install.sh | bash'",
        ]},
        ports=[2053],
    ),
    SoftwarePackage(
        "xray", "Xray (standalone)", "Xray-core — VLESS + Reality + XHTTP, без панели",
        "vpn",
        {"debian": [
            "bash -c 'curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh | bash'",
            "bash -c 'cat > /usr/local/etc/xray/config.json << XRAYEOF\n"
            "{\n"
            "  \"log\": {\"loglevel\": \"warning\"},\n"
            "  \"inbounds\": [{\n"
            "    \"listen\": \"0.0.0.0\", \"port\": 8443, \"protocol\": \"vless\",\n"
            "    \"settings\": {\"clients\": [{\"id\": \"REPLACE-WITH-UUID\", \"flow\": \"\"}], \"decryption\": \"none\"},\n"
            "    \"streamSettings\": {\n"
            "      \"network\": \"xhttp\",\n"
            "      \"xhttpSettings\": {\"path\": \"/api/v1/data\", \"mode\": \"auto\"},\n"
            "      \"security\": \"reality\",\n"
            "      \"realitySettings\": {\n"
            "        \"dest\": \"github.com:443\", \"serverNames\": [\"github.com\"],\n"
            "        \"privateKey\": \"REPLACE-WITH-KEY\", \"shortIds\": [\"\", \"abcdef\"]\n"
            "      }\n"
            "    }\n"
            "  }],\n"
            "  \"outbounds\": [{\"protocol\": \"freedom\", \"tag\": \"direct\"}]\n"
            "}\n"
            "XRAYEOF'",
            "systemctl enable xray && systemctl restart xray",
        ],
         "rhel": [
            "bash -c 'curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh | bash'",
            "systemctl enable xray",
        ]},
        ports=[8443],
    ),
    SoftwarePackage(
        "wireguard", "WireGuard", "Классический VPN — быстрый, шифрование ChaCha20, UDP",
        "vpn",
        {"debian": [
            "apt-get install -y wireguard wireguard-tools qrencode",
            "umask 077 && wg genkey | tee /etc/wireguard/server.key | wg pubkey > /etc/wireguard/server.pub",
            "sysctl -w net.ipv4.ip_forward=1 && echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf",
        ],
         "rhel": [
            "dnf install -y wireguard-tools qrencode",
            "umask 077 && wg genkey | tee /etc/wireguard/server.key | wg pubkey > /etc/wireguard/server.pub",
            "sysctl -w net.ipv4.ip_forward=1 && echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf",
        ]},
        ports=[51820],
    ),
    # Cloud
    SoftwarePackage(
        "nextcloud", "Nextcloud", "Облако файлов, календарь, контакты, совместная работа (Docker)",
        "cloud",
        {"debian": [
            "curl -fsSL https://get.docker.com | sh && systemctl enable docker",
            "mkdir -p /opt/nextcloud && cd /opt/nextcloud && cat > docker-compose.yml << 'COMPOSE'\n"
            "services:\n"
            "  db:\n"
            "    image: mariadb:11\n"
            "    restart: always\n"
            "    environment:\n"
            "      MYSQL_ROOT_PASSWORD: nextcloud_root_pw\n"
            "      MYSQL_DATABASE: nextcloud\n"
            "      MYSQL_USER: nextcloud\n"
            "      MYSQL_PASSWORD: nextcloud_db_pw\n"
            "    volumes:\n"
            "      - db_data:/var/lib/mysql\n"
            "  app:\n"
            "    image: nextcloud:latest\n"
            "    restart: always\n"
            "    ports:\n"
            "      - '80:80'\n"
            "    environment:\n"
            "      MYSQL_HOST: db\n"
            "      MYSQL_DATABASE: nextcloud\n"
            "      MYSQL_USER: nextcloud\n"
            "      MYSQL_PASSWORD: nextcloud_db_pw\n"
            "    volumes:\n"
            "      - nc_data:/var/www/html\n"
            "    depends_on:\n"
            "      - db\n"
            "volumes:\n"
            "  db_data:\n"
            "  nc_data:\n"
            "COMPOSE",
            "cd /opt/nextcloud && docker compose up -d",
        ]},
        ports=[80, 443],
    ),
    # Enterprise
    SoftwarePackage(
        "1c-server", "Зависимости 1С", "Системные библиотеки, шрифты и ODBC для платформы 1С:Предприятие",
        "enterprise",
        {"debian": [
            "apt-get install -y imagemagick libfreetype6 libgsf-1-114 libgsf-1-common libglib2.0-0 libodbc2 unixodbc ttf-mscorefonts-installer libkrb5-3 libgssapi-krb5-2",
            "fc-cache -fv",
        ],
         "rhel": [
            "dnf install -y ImageMagick fontconfig freetype libgsf glib2 unixODBC krb5-libs",
            "fc-cache -fv",
        ],
         "windows": [
            "powershell -Command \"Set-Service -Name seclogon -StartupType Automatic; Start-Service seclogon\"",
        ]},
        ports=[1541, 1540],
    ),
    # Windows
    SoftwarePackage(
        "iis", "IIS", "Internet Information Services — веб-сервер Windows",
        "web",
        {"windows": [
            "powershell -Command \"Install-WindowsFeature -Name Web-Server -IncludeManagementTools\"",
        ]},
        ports=[80, 443],
    ),
    SoftwarePackage(
        "dotnet-hosting", ".NET Hosting Bundle", "ASP.NET Core Runtime + IIS интеграция",
        "runtimes",
        {"windows": [
            "powershell -Command \"Invoke-WebRequest -Uri 'https://dot.net/v1/dotnet-install.ps1' -OutFile dotnet-install.ps1; .\\dotnet-install.ps1 -Channel 8.0 -Runtime aspnetcore\"",
        ]},
    ),
    SoftwarePackage(
        "mssql", "SQL Server Express", "Microsoft SQL Server Express",
        "databases",
        {"windows": [
            "powershell -Command \"Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink//?linkid=866658' -OutFile SQLEXPR.exe; Start-Process ./SQLEXPR.exe -ArgumentList '/QS /IACCEPTSQLSERVERLICENSETERMS /ACTION=install /FEATURES=SQL /INSTANCENAME=SQLEXPRESS' -Wait\"",
        ]},
        ports=[1433],
    ),
    SoftwarePackage(
        "openssh-win", "OpenSSH Server", "SSH-сервер для Windows",
        "tools",
        {"windows": [
            "powershell -Command \"Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; Start-Service sshd; Set-Service -Name sshd -StartupType Automatic\"",
        ]},
        ports=[22],
    ),
    SoftwarePackage(
        "rdp", "Remote Desktop", "Включить RDP доступ",
        "tools",
        {"windows": [
            "powershell -Command \"Set-ItemProperty -Path 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' -Name 'fDenyTSConnections' -Value 0; Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'\"",
        ]},
        ports=[3389],
    ),
]


def get_software_grouped() -> dict[str, list[SoftwarePackage]]:
    grouped: dict[str, list[SoftwarePackage]] = {}
    for pkg in SOFTWARE_CATALOG:
        grouped.setdefault(pkg.category, []).append(pkg)
    return grouped


def get_software_by_ids(ids: list[str]) -> list[SoftwarePackage]:
    id_set = set(ids)
    return [pkg for pkg in SOFTWARE_CATALOG if pkg.id in id_set]
