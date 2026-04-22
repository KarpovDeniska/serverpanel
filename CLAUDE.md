# ServerPanel

Веб-панель для управления выделенными серверами (dedicated): инвентаризация, переустановка ОС, настройка софта, резервное копирование, мониторинг. Построена как мульти-провайдерная — конкретный провайдер (Hetzner Robot и др.) подключается по единому Protocol-интерфейсу.

## Стек

- Python 3.12+, FastAPI + Uvicorn (port 5000)
- SQLAlchemy 2.0 async + Alembic
- БД по умолчанию: SQLite (`data/serverpanel.db`), опционально PostgreSQL (extra `postgres` → asyncpg)
- Pydantic v2 / pydantic-settings
- Jinja2 + статика (серверный рендеринг)
- Paramiko — SSH-клиент, httpx — HTTP к провайдерам
- cryptography (Fernet) — шифрование учёток; bcrypt — пароли; itsdangerous — сессии
- OAuth опционально (extra `oauth` → authlib), для Google / GitHub

Запуск: `serverpanel` (CLI из `src/serverpanel/main.py:cli`) или `uvicorn serverpanel.main:app`.

## Архитектура (слои)

`src/serverpanel/`

- `domain/` — чистая доменная модель без зависимостей от инфраструктуры
  - `enums.py` — `Capability`, `ResetType`, `ServerStatusType`, `BackupStatus`, `UserRole`, `RecoveryStatus`
  - `providers.py` — Protocol'ы `ServerProvider` и `StorageProvider` (контракт для интеграций)
  - `models.py` — dataclass-ы домена (`ServerInfo`, `FileInfo`, `FirewallRule` и т.п.)
  - `exceptions.py`
- `application/` — прикладные сервисы и статические каталоги
  - `services/install_service.py` — оркестрация переустановки ОС (12 фаз: rescue → installimage → post-install)
  - `catalogs/` — предопределённые OS-образы, шаблоны серверов, софт
- `infrastructure/` — внешний мир
  - `database/` — engine, ORM-модели, репозитории (`users`, `servers`, `install`)
  - `providers/hetzner/` — реализация `ServerProvider` для Hetzner Robot (`robot_api.py` + `provider.py`). Провайдеры регистрируются через фабрику `create_provider`
  - `ssh/client.py` — `AsyncSSHClient` (обёртка над Paramiko)
  - `auth/` — `backend.py` (сессии/пароли), `oauth.py`
  - `crypto.py` — Fernet-шифрование JSON-учёток (encryption_key из settings)
- `presentation/` — HTTP/WebSocket
  - `routers/` — `auth`, `dashboard`, `servers`, `install`, `settings`
  - `middleware.py` — `AuthRedirectMiddleware`
  - `websocket.py` — `ws_manager` для прогресс-бара установки
  - `dependencies.py`
- `templates/`, `static/` — SSR UI

## Доменная модель (БД)

Таблицы ([infrastructure/database/models.py](src/serverpanel/infrastructure/database/models.py)):

- `users` / `oauth_accounts` — auth, роли `admin` / `user`
- `provider_configs` — зашифрованные учётки провайдеров (сейчас `hetzner_dedicated`)
- `servers` — инвентарь: `provider_server_id`, IP, OS, SSH (username/port/ключ-шифрованный), `check_ports`, `extra`
- `storage_configs` — хранилища для бэкапов: `hetzner_storagebox`, `sftp`, `s3` (connection_encrypted + base_path)
- `install_history` — журнал переустановок с прогрессом и логом
- `backup_configs` — конфиги бэкапов: `sources` (JSON), `destinations` (JSON), `schedule` (строка, пока без раннера), `rotation_days=14`
- `backup_history` — запуски: `started_at/completed_at`, `status`, `size_bytes`, `details`, `error_message`
- `monitored_services` — службы для мониторинга (`windows_service` / `systemd` / `process`)
- `audit_log` — действия пользователей

Учётные данные провайдеров, storage и SSH-ключи шифруются Fernet до записи в БД.

## Провайдерная абстракция

`ServerProvider` (Protocol, `domain/providers.py`) описывает весь контракт интеграции: info/power/rescue/network/firewall/ssh_keys/traffic. Возможности гранулярны через `Capability` — UI и сервисы спрашивают `provider.supports(cap)` перед вызовом, неподдержанные операции бросают `NotImplementedError`. Добавить нового провайдера — положить модуль в `infrastructure/providers/<name>/`, реализовать Protocol, зарегистрировать в `create_provider` (импортируется в `main.lifespan`).

`StorageProvider` — аналогично для файловых хранилищ/снапшотов.

## Конфигурация

`src/serverpanel/config.py` (pydantic-settings, `.env`):

- `database_url`, `secret_key`, `encryption_key` (Fernet base64, обязателен для production)
- `session_lifetime_hours`
- OAuth-креды (Google/GitHub) — опционально
- `debug` включает `/api/docs` и auto-reload

## Бэкапы — текущее состояние

- Схема БД готова: `BackupConfig` (с полем `schedule`) + `BackupHistory` + `StorageConfig`
- Enum `BackupStatus`: `pending / running / success / failed`
- **Раннера/планировщика бэкапов в коде пока нет** — нет сервиса, который читал бы `schedule` и запускал `BackupHistory`. Роутер `backups` не подключён в `main.create_app`

## Тесты

`tests/` — pytest + pytest-asyncio (`asyncio_mode=auto`). Разбиение: `test_domain`, `test_infrastructure`, `test_presentation`.

## Alembic

`alembic.ini` + `alembic/` на месте, папка `versions/` пока пустая — миграции не заведены (таблицы создаются через `init_db` в lifespan).

## Полезные точки входа

- [src/serverpanel/main.py](src/serverpanel/main.py) — фабрика приложения и CLI
- [src/serverpanel/domain/providers.py](src/serverpanel/domain/providers.py) — контракт провайдеров
- [src/serverpanel/infrastructure/database/models.py](src/serverpanel/infrastructure/database/models.py) — вся схема БД
- [src/serverpanel/application/services/install_service.py](src/serverpanel/application/services/install_service.py) — сценарий переустановки ОС

## Статус

Прод-ready бэкапы 1С-сервера hetzner-windows через Task Scheduler + Telegram heartbeat.

- **Сервер**: hetzner-windows (Hetzner dedicated, Windows Server 2022, Intel Xeon E3-1246v3, IP `148.251.46.106`, Robot `server_number=910543`). Провайдерский конфиг + SSH-ключ + Robot webservice creds заведены.
- **Хранилище**: Hetzner Storage Box (`u571198@u571198.your-storagebox.de:23`), SFTP, SSH-ключ.
- **Три Task-Scheduler конфига** на сервере:
  - `legacy-daily` (03:00, 14d rotation, 9 источников: UNF через VSS+zip, 1c_license, ibases, 1c_settings, 1c_files, 1c_obrabotki, rutoken, 1c_licenses_archive, tools_xray).
  - `legacy-weekly-iis` (Sun 04:00, 180d rotation, C:\inetpub + C:\win-acme).
  - `legacy-monthly` (1-е число 05:00, 365d rotation, тот же набор что daily — долгохранящийся снимок).
- **Telegram-алерты**: на каждый прогон → ✅ / ⚠️ / ❌. Молчание = сигнал тревоги. Токен и chat_id в `.env`.
- **Emergency backup самой serverpanel**: `.env` + `data/serverpanel.db` + `~/.ssh/serverpanel-seed/` (ключи экспортированы через `serverpanel export-keys`). tar-команда для ежемесячной перекладки в iCloud — в [docs/OPERATIONS.md §4](docs/OPERATIONS.md).
- **Документация**: [README.md](README.md), [docs/OPERATIONS.md](docs/OPERATIONS.md) (setup с нуля, Telegram, emergency restore, CLI), [docs/RESTORE_TEST.md](docs/RESTORE_TEST.md) (квартальный чеклист), [docs/HISTORY.md](docs/HISTORY.md) (хронология правок).
- **Тесты**: 27 passed, ruff чистый.

**Что осталось** (не срочное):
- Нет теста восстановления на реальных данных (RESTORE_TEST.md написан, но первый прогон не сделан — запланирован Q2 2026).
- Recovery-flow `both` (полная переустановка Windows с нуля через rescue) — код есть, но без ISO/unattend артефактов на SB не протестирован.
- S3-provider — только stub, реализован только `hetzner_storagebox`.
