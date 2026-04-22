# ServerPanel — архитектура

Мульти-провайдерная панель управления выделенными серверами: инвентаризация, переустановка ОС, резервное копирование, восстановление, мониторинг. Новый провайдер подключается по единому `Protocol`-интерфейсу.

## Стек

- Python 3.12+, FastAPI + Uvicorn (port 5000).
- SQLAlchemy 2.0 async + Alembic. БД по умолчанию SQLite (`data/serverpanel.db`), опционально PostgreSQL (extra `postgres` → asyncpg).
- Pydantic v2 / pydantic-settings.
- Jinja2 + статика (серверный рендеринг).
- Paramiko — SSH, httpx — HTTP к провайдерам.
- cryptography (Fernet) — шифрование учёток; bcrypt — пароли; itsdangerous — сессии.
- OAuth опционально (extra `oauth` → authlib).

Запуск: `serverpanel` (CLI из [src/serverpanel/main.py](../src/serverpanel/main.py)) или `uvicorn serverpanel.main:app`.

## Слои (Clean-ish Architecture)

`src/serverpanel/`

- `domain/` — чистая модель без инфраструктуры.
  - `enums.py` — `Capability`, `ResetType`, `ServerStatusType`, `BackupStatus`, `UserRole`, `RecoveryStatus`.
  - `providers.py` — Protocol'ы `ServerProvider` и `StorageProvider` (контракт интеграций).
  - `models.py` — dataclass-ы (`ServerInfo`, `FileInfo`, `FirewallRule`…).
  - `backup.py` — Pydantic `BackupSource`/`BackupDestination`/`BackupPlan`.
  - `i18n.py` — плоский словарь ru/en, `t(key, **kwargs)`.
  - `progress.py` — `ProgressReporter` Protocol + `NullProgressReporter`.
- `application/` — сервисы + статические скрипты.
  - `services/install_service.py` — переустановка ОС (12 фаз).
  - `services/backup_service.py` — ручной `run()`, `install_schedule()`, `uninstall_schedule()`, `sync_reports_from_server()`.
  - `services/recovery_service.py` — `c_drive` / `d_drive` / `both`.
  - `importers/hetzner_recovery.py` — импорт legacy `config.yaml` + `_upsert_{daily,weekly,monthly}_backup`.
  - `importers/seed.py` — bootstrap через CLI.
  - `static/scripts/backup.ps1` — исполняется на целевом сервере.
  - `static/scripts/recovery/*` — партиционирование, applyimage, восстановление.
- `infrastructure/` — внешний мир.
  - `database/` — engine (`init_db` → alembic upgrade, `cleanup_stale_runs` без cutoff на startup), ORM-модели, репозитории.
  - `providers/hetzner/` — `ServerProvider` для Hetzner Robot (`robot_api.py` + `provider.py`). Регистрация через фабрику `create_provider`.
  - `providers/storage/hetzner_storagebox.py` — `StorageProvider` поверх SFTP.
  - `ssh/client.py` — `AsyncSSHClient` (Paramiko async-wrap, TOFU host-key pinning, `execute_stream` с callback).
  - `auth/backend.py`, `auth/oauth.py`.
  - `crypto.py` — Fernet-шифрование JSON-учёток.
- `presentation/` — HTTP/WebSocket.
  - `routers/` — `auth`, `dashboard`, `servers`, `install`, `backups`, `recovery`, `storages`, `settings`.
  - `middleware.py` (`AuthRedirectMiddleware`), `csrf.py`, `ratelimit.py`, `ws_auth.py`.
  - `websocket.py` — `ws_manager`. `progress.py` — `WsProgressReporter`.
  - `background.py` — `run_supervised(history_cls, history_id, worker, label)`.
  - `main.py` — lifespan (init_db → cleanup_stale_runs → фоновый `_backup_sync_loop`).
- `templates/`, `static/` — SSR UI. Visual builder sources/destinations ([static/js/backup_builder.js](../src/serverpanel/static/js/backup_builder.js)), live-stream backup stdout → WebSocket.

## Доменная модель (БД)

Таблицы — [infrastructure/database/models.py](../src/serverpanel/infrastructure/database/models.py):

- `users` / `oauth_accounts` — auth, роли `admin` / `user`.
- `provider_configs` — зашифрованные Robot-креды (`hetzner_dedicated`).
- `servers` — `provider_server_id`, IP, OS, SSH creds, **`ssh_host_key_pub`** (TOFU pin), `check_ports`, `extra`.
- `storage_configs` — `hetzner_storagebox` / `sftp` / `s3` (connection_encrypted + base_path).
- `install_history`, `recovery_history`, `backup_history` — журналы с `status` / `current_step` / `progress` / `details` JSON.
- `backup_configs` — `sources` / `destinations` / `schedule` / `rotation_days`.
- `monitored_services`, `audit_log`.

Все credentials (Robot, SSH, StorageBox) шифруются Fernet через `Settings.encryption_key` до записи.

## Провайдерная абстракция

`ServerProvider` (Protocol в `domain/providers.py`) описывает контракт: info / power / rescue / network / firewall / ssh_keys / traffic / storage_box. Гранулярно — через `Capability`; UI и сервисы спрашивают `provider.supports(cap)` перед вызовом, неподдержанные операции бросают `NotImplementedError`.

Добавить провайдера — положить модуль в `infrastructure/providers/<name>/`, реализовать Protocol, зарегистрировать в `create_provider` (импорт пакета в `main.lifespan` триггерит авто-регистрацию).

`StorageProvider` — аналогично для файловых хранилищ (sftp-like / s3-like).

## Конфигурация

[src/serverpanel/config.py](../src/serverpanel/config.py) (pydantic-settings, `.env`):

- **Обязательные в production**: `SECRET_KEY`, `ENCRYPTION_KEY` (Fernet base64). `model_validator` fail-fast-ит на дефолтах если `debug=false`.
- **База**: `DATABASE_URL` (`sqlite+aiosqlite:///./data/serverpanel.db` по умолчанию).
- **Сессии**: `session_lifetime_hours`, `session_cookie_secure`, `session_cookie_samesite`.
- **Бэкапы**: `backup_run_timeout` (3ч), `backup_zip_level` (`fastest` / `optimal`), `backup_sync_interval_seconds` (900, 0 = выкл).
- **Timeouts**: `install_*`, `recovery_*`, `ssh_*`, `stale_run_timeout_minutes`.
- **Алерты**: `telegram_bot_token`, `telegram_chat_id` (пусто — тишина).
- **OAuth**: `google_client_id`/`secret`, `github_client_id`/`secret` (опционально).
- **i18n**: `language` = `ru` | `en`.

## Ключевые инженерные решения

- **Slots без WS в домене**: application-слой принимает `ProgressReporter` (Protocol), конкретный `WsProgressReporter` инжектится из роутера. Сервисы тестируются без WebSocket.
- **run_supervised**: фоновый таск с гарантированным финальным `commit` + `status=failed` при исключении. Голый `asyncio.create_task` запрещён для истории-таблиц.
- **JSON-мутации в SQLAlchemy**: `history.details` пересоздаётся новым dict на каждую запись (адаптер не ловит in-place изменения списков → refresh давал пустой лог).
- **Host-key pinning (TOFU)**: `Server.ssh_host_key_pub` + `known_host_key=` / `on_host_key_learned=` callback в `AsyncSSHClient`.
- **CSRF**: double-submit через meta-тег + JS-инжект в формы. Явный `{{ get_csrf_token(request) }}` для server-render.
- **Rate-limit**: in-memory `RateLimiter` на `/login` (10/5мин) и `/register` (5/час). Для мульти-воркера нужен Redis.
- **UI JSON-payload**: всегда `<script type="application/json">`, не `data-*=""` (двойные кавычки рвали атрибут).

## Тесты и CI

[`tests/`](../tests) — pytest + pytest-asyncio (`asyncio_mode=auto`). Разбиение `test_domain` / `test_infrastructure` / `test_presentation`. **27 passed**, ruff `select E/F/I/B/UP/C4/S` чистый. CI — `.github/workflows/ci.yml`.

## Alembic

`alembic.ini` + `alembic/`. `init_db` в lifespan прогоняет `alembic upgrade head`; legacy-БД без `alembic_version` стампится к head на первом старте (таблицы, созданные create_all, не перетираются).
