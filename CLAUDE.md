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

- **2026-04-21** — в serverpanel влита вся функциональность старого `hetzner-recovery`. Готово:
  - `StorageProvider` → [infrastructure/providers/storage/hetzner_storagebox.py](src/serverpanel/infrastructure/providers/storage/hetzner_storagebox.py) (SFTP, автоматически регистрируется в `main.lifespan`).
  - Pydantic-схемы `BackupSource`/`BackupDestination` → [domain/backup.py](src/serverpanel/domain/backup.py) (discriminated union `local` | `storage`).
  - Alembic baseline + `recovery_history` migration (`init_db` теперь прогоняет `alembic upgrade head`, легаси БД stamp'ится автоматически).
  - `BackupService` → [application/services/backup_service.py](src/serverpanel/application/services/backup_service.py): ручной `run()` + `install_schedule()` / `uninstall_schedule()` (Task Scheduler на целевом сервере).
  - `backup.ps1` → [application/static/scripts/backup.ps1](src/serverpanel/application/static/scripts/backup.ps1): VSS, robocopy, zip, local + SB, ротация. UTF-8 с BOM обязательно (PS 5.1 + cp1251 ломаются без BOM на кириллице).
  - `RecoveryService` + таблица `RecoveryHistory` → [application/services/recovery_service.py](src/serverpanel/application/services/recovery_service.py): 3 сценария (`c_drive`/`d_drive`/`both`), `_build_windows_config` → `config.json`.
  - Recovery-скрипты → [application/static/scripts/recovery/](src/serverpanel/application/static/scripts/recovery/): `partition_disk.sh`, `apply_windows.sh`, `inject_config.sh` (копирует PS из /tmp/, не качает с SB), `restore.ps1`, `restore_data.ps1`, `install_software.ps1`, `SetupComplete.cmd`. Все PS — UTF-8 BOM.
  - Роуты: `/servers/{id}/backups` (CRUD+install/uninstall+run+history+WS) → [routers/backups.py](src/serverpanel/presentation/routers/backups.py). `/servers/{id}/recovery` (wizard+progress+WS) → [routers/recovery.py](src/serverpanel/presentation/routers/recovery.py). Шаблоны — [templates/backups/](src/serverpanel/templates/backups/) + [templates/recovery/](src/serverpanel/templates/recovery/).
  - CLI-импорт: `serverpanel import-hetzner-recovery <yaml> --user-email X [--sb-private-key PATH] [--server-private-key PATH]` → [application/importers/hetzner_recovery.py](src/serverpanel/application/importers/hetzner_recovery.py). Создаёт `ProviderConfig` + `Server` + `StorageConfig` + `BackupConfig "legacy-daily"` (10 источников) + `BackupConfig "legacy-weekly-iis"`. Идемпотентно.

- **2026-04-21** — папка `hetzner-recovery/` перенесена в `archive/hetzner-recovery/`. В активной `projects/` её больше нет.

- **2026-04-21** — README.md создан (подключить сервер → настроить бэкап → тест восстановления).

- **2026-04-21** — промышленный аудит + правки. Закрыто:
  - **Безопасность**: fail-fast на дефолтный SECRET_KEY/ENCRYPTION_KEY в non-debug; WebSocket auth+ownership во всех 3 WS endpoint-ах ([ws_auth.py](src/serverpanel/presentation/ws_auth.py)); `shlex.quote` в scp/recovery SSH; whitelist валидация имени скрипта в `_upload_and_run`; host/password для Rename-Computer / hostnamectl тоже квотятся; CSRF middleware + double-submit токен в шаблонах ([csrf.py](src/serverpanel/presentation/csrf.py)); rate-limiter login/register + rotate session on login ([ratelimit.py](src/serverpanel/presentation/ratelimit.py)); регистрация открыта только до первого admin; `StorageConfigRepository.get_by_id_for_user` (IDOR).
  - **Надёжность**: `run_supervised` — фоновые задачи с обязательным status=failed ([background.py](src/serverpanel/presentation/background.py)); `cleanup_stale_runs` на startup — висящие `running` row'ы помечаются `failed` ([engine.py](src/serverpanel/infrastructure/database/engine.py)); `/health` endpoint (DB ping).
  - **Архитектура**: `ProgressReporter` Protocol ([domain/progress.py](src/serverpanel/domain/progress.py)) — убрал импорт `ws_manager` из application-слоя; `WsProgressReporter` адаптер в presentation; `RecoveryService` теперь использует `create_storage` фабрику вместо прямого `HetznerStorageBox`.
  - **Deployability**: `importlib.resources` для templates/static (работает из wheel); все роуты через общий [templates.py](src/serverpanel/presentation/templates.py); host/port в settings; upper bounds на все deps + `<major+1` каппы.
  - **Hygiene**: базовые тесты ([tests/test_domain/](tests/test_domain/), [tests/test_infrastructure/](tests/test_infrastructure/), [tests/test_presentation/test_health.py](tests/test_presentation/test_health.py)) — 18 зелёных; GitHub Actions CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)); ruff select E/F/I/B/UP/C4/S, конфиг в pyproject.

- **2026-04-22** — прод-ready бэкапы на hetzner-windows:
  - 3 Task-Scheduler конфига: `legacy-daily` (03:00, 14d), `legacy-weekly-iis` (Sun 04:00, 180d), `legacy-monthly` (1-е число 05:00, 365d). Все с правильными источниками (UNF через VSS + 1C + IIS + tools/xray целиком).
  - Telegram heartbeat на каждом прогоне (success/partial/failed) — молчание = тревога. Креды бота и chat_id в `.env` (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`), подкладываются в `plan.json.notifications.telegram` при build'е plan'а.
  - Zip через `System.IO.Compression.ZipFile.CreateFromDirectory` (Zip64, нет 2 GB баги `Compress-Archive`). Уровень по дефолту `Fastest` — `Settings.backup_zip_level`.
  - Monthly schedule через `schtasks.exe` + `.cmd`-wrapper (CIM MSFT_TaskMonthlyTrigger в PS 5.1 сломан). Формат `monthly:D@HH:MM` в парсере.
  - Новые CLI: [`seed-legacy-backups`](src/serverpanel/main.py), [`export-keys`](src/serverpanel/main.py) (материализует SSH-ключи в `~/.ssh/serverpanel-seed/`), [`set-robot-creds`](src/serverpanel/main.py), [`sync-from-robot`](src/serverpanel/main.py) (подтягивает numeric `server_number` из Robot API).
  - UI: toast-feedback на Install/Uninstall ([templates/backups/detail.html](src/serverpanel/templates/backups/detail.html)); форма [provider_edit.html](src/serverpanel/templates/servers/provider_edit.html) с проверкой кредов против Robot API перед сохранением + кнопка «Re-discover servers» под тем же провайдером (матчится по IP — без дублей).
  - Фиксы устойчивости: `cleanup_stale_runs` без cutoff (все `running` → `failed` при startup); JSON-мутации в `BackupHistory.details` пересоздаются новым dict (иначе SQLAlchemy не видит dirty → refresh давал пустой лог); builder-IIFE читает JSON из `<script type="application/json">` (было в `data-*=""` с `tojson` — двойные кавычки рвали атрибут); `[Console]::Out.WriteLine + Flush()` вместо `Write-Host` + `[Console]::OutputEncoding = UTF8 без BOM` — stdout теперь стримится в UI/TG без буферизации и без cp1251-мойсиака.
  - Документация: [docs/OPERATIONS.md](docs/OPERATIONS.md) (setup с нуля, Telegram, emergency restore, CLI), [docs/RESTORE_TEST.md](docs/RESTORE_TEST.md) (квартальный чеклист — тестируем в `C:\restore_test\` на том же hetzner-windows).
  - Итого: **27 passed** (+2 теста monthly schedule), ruff чистый.

- **2026-04-21** — второй проход по «отложенному», закрыто всё:
  - **Host-key pinning (TOFU)**: миграция [c3d4e5f6a7b8](alembic/versions/c3d4e5f6a7b8_server_host_key.py) добавляет `Server.ssh_host_key_pub`. [`AsyncSSHClient`](src/serverpanel/infrastructure/ssh/client.py) принимает `known_host_key=` + `on_host_key_learned=` callback: если ключ есть — `_PinnedPolicy` отвергает любой другой, если нет — TOFU-захват при первом коннекте. `BackupService._open_ssh` и `RecoveryService._recover_d_drive` используют/пополняют пин; `_recover_c_drive` стирает ключ после `both`-сценария (Windows переустановлен).
  - **UI — StorageConfig CRUD**: [routers/storages.py](src/serverpanel/presentation/routers/storages.py) + [templates/storages/edit.html](src/serverpanel/templates/storages/edit.html). На [servers/detail.html](src/serverpanel/templates/servers/detail.html) — секция «Хранилища» со списком и + кнопкой. Подключается в `servers.router.include_router` (45 роутов всего).
  - **UI — визуальный builder sources/destinations**: [static/js/backup_builder.js](src/serverpanel/static/js/backup_builder.js). Вместо JSON-textarea — отдельные поля на каждый source/destination с выпадашками типов/compress/frequency и выбором StorageConfig по id. Hidden `sources`/`destinations` сериализуются на submit, бэкенд не переписывался.
  - **Live-stream `backup.ps1`**: [backup_service.py](src/serverpanel/application/services/backup_service.py) использует `ssh.execute_stream` с callback, буфер разбивается по `\n`, фоновая `_pump()` задача раз в 1с шлёт строки в `_append_log` (который через `self.reporter.log` летит в WS).
  - **i18n**: [domain/i18n.py](src/serverpanel/domain/i18n.py) — плоский словарь ru/en, `Settings.language` (default "ru"), сервисы install/recovery полностью переведены через `t(key, **kwargs)`. Тест `test_english_translation_table_complete` гарантирует, что все ключи имеют `en`.
  - **Таймауты в Settings**: `install_*`, `recovery_*`, `backup_run_timeout`, `ssh_*` в [config.py](src/serverpanel/config.py), сервисы читают через `get_settings()`.
  - **`datetime.utcnow()` → `datetime.now(datetime.UTC)`** во всех 5 местах.
  - Добавлены тесты: ssh host-key line roundtrip, i18n completeness, storage CRUD auth gate. Итого **25 passed**, ruff чистый.
