# История изменений serverpanel

Хронологический журнал крупных правок. Актуальное состояние — в `CLAUDE.md`.

---

## 2026-04-22 — disaster recovery для мака

- `scripts/bootstrap-mac.sh` — one-command setup: Xcode CLT, Homebrew, python@3.12, clone, venv, pip install, опциональная распаковка tar-архива через `SERVERPANEL_BACKUP_TAR=`, LaunchAgent, health-check. Идемпотентный. Генерит свежий `.env` если архив не задан.
- `scripts/restore.command` — GUI-обёртка для двойного клика: osascript-диалог выбора архива → git clone (если нет) → bootstrap. Лежит в репе + копия в iCloud рядом с архивами.
- **Репо переключён в public** (`gh repo edit ... --visibility public`). В коде/доках только публичные identifier-ы (IP, SB-логин, Robot server_number), ни паролей ни ключей в истории нет (`.env`, `data/`, `*.db` в .gitignore с начала). На новом маке `git clone` и `raw.githubusercontent.com` работают без авторизации.
- `docs/OPERATIONS.md` — §1.0-auto расписывает вариант A (restore.command) + вариант B (curl-one-liner). §1.0 — системные зависимости голого мака (`xcode-select --install`, brew, python@3.12). §4 обновлён.
- Проверено сквозное восстановление на текущем маке: `tar czf` свежего архива → полный снос (unload LaunchAgent, `rm -rf ~/projects/serverpanel ~/.ssh/serverpanel-seed`, чистка логов) → `git clone` + `bash bootstrap-mac.sh` с `SERVERPANEL_BACKUP_TAR=…` → панель на `127.0.0.1:5000`, один uvicorn-процесс под launchd, все конфиги/история/ключи на месте.
- Документация: ARCHITECTURE.md вынесен из CLAUDE.md (CLAUDE.md ужат до интро + ссылок + статуса).

## 2026-04-22 — видимость scheduled-run'ов в UI, CRUD-полнота, автозапуск

- `BackupService.sync_reports_from_server(server)`: по SSH читает `C:\ProgramData\serverpanel\configs\*\last_report.json`, дедупит по `run_id`, создаёт `BackupHistory` строки со статусом/размером/started_at/completed_at для каждого нового прогона. Task Scheduler теперь виден в панели.
- Фоновый поллер в `lifespan` (`_backup_sync_loop`, interval `Settings.backup_sync_interval_seconds` default 900) — раз в 15 мин проходит по всем серверам. `asyncio.create_task` + cancel на shutdown.
- Manual `⟲ sync` кнопки: на `/servers/{id}/backups` (крупная), на карточке «Бэкапы» в `/servers/{id}` (мелкая, обрамлена как button). Обе POST'ят в `/servers/{id}/backups/sync`, респектят hidden `next=...` для возврата на ту же страницу.
- Dashboard: per-server strip «Бэкапы: ✓1 ⚠0 ✕0 — 2 · 22.04 16:40», кликабельный → /backups. Цветной dot (red/yellow/blue/green) по доминантному статусу.
- Карточка «Бэкапы» на странице сервера: 3 конфига + счётчики + последний прогон + цветная рамка + status-dot. Пустой state `Создать первый →`.
- Backup list table: новая колонка «Последний запуск» — status-dot + timestamp. Single GROUP-BY-MAX subquery, не N+1.
- StorageConfig edit в UI (✎ рядом с ✕). Форма не рендерит расшифрованные секреты — пустые поля = сохранить старые.
- ProviderConfig edit (✎ возле «Провайдер» на детали сервера) + «Re-discover servers» — подхват новых серверов в том же Hetzner-аккаунте без дублей (матч по IP).
- LaunchAgent `~/Library/LaunchAgents/ru.gefest.serverpanel.plist` (RunAtLoad + KeepAlive). uvicorn больше не в foreground-терминале. Обновление → `launchctl kickstart -k gui/$UID/ru.gefest.serverpanel`. Логи в `~/Library/Logs/`.
- CLI: `set-robot-creds`, `sync-from-robot` (подтягивает numeric `server_number` из Robot API — фикс ошибки «requires numeric server id, got <IP>»), `export-keys`.
- Робот API webservice creds (`#ws+…`) настроены, API-статус `configured` + capabilities подтянуты.
- HISTORY: CLAUDE.md §Статус ужат до «что актуально сейчас», история переехала сюда.

## 2026-04-22 — прод-ready бэкапы на hetzner-windows

- 3 Task-Scheduler конфига: `legacy-daily` (03:00, 14d), `legacy-weekly-iis` (Sun 04:00, 180d), `legacy-monthly` (1-е число 05:00, 365d). Источники: UNF через VSS + 1C + IIS + tools/xray целиком.
- Telegram heartbeat на каждом прогоне (success/partial/failed) — молчание = тревога. Креды бота/chat_id в `.env` (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`), подкладываются в `plan.json.notifications.telegram` при build'е plan'а.
- Zip через `System.IO.Compression.ZipFile.CreateFromDirectory` (Zip64, нет 2 GB баги `Compress-Archive`). Уровень по дефолту `Fastest` — `Settings.backup_zip_level`.
- Monthly schedule через `schtasks.exe` + `.cmd`-wrapper (CIM MSFT_TaskMonthlyTrigger в PS 5.1 сломан). Формат `monthly:D@HH:MM`.
- Новые CLI: [`seed-legacy-backups`](../src/serverpanel/main.py), [`export-keys`](../src/serverpanel/main.py) (материализует SSH-ключи в `~/.ssh/serverpanel-seed/`), [`set-robot-creds`](../src/serverpanel/main.py), [`sync-from-robot`](../src/serverpanel/main.py) (подтягивает numeric `server_number` из Robot API).
- UI: toast-feedback на Install/Uninstall; форма [provider_edit.html](../src/serverpanel/templates/servers/provider_edit.html) с проверкой кредов против Robot API перед сохранением + кнопка «Re-discover servers» под тем же провайдером (матчится по IP, без дублей).
- Фиксы устойчивости: `cleanup_stale_runs` без cutoff (все `running` → `failed` при startup); JSON-мутации в `BackupHistory.details` пересоздаются новым dict (иначе SQLAlchemy не видит dirty → refresh давал пустой лог); builder-IIFE читает JSON из `<script type="application/json">` (было в `data-*=""` с `tojson` — двойные кавычки рвали атрибут); `[Console]::Out.WriteLine + Flush()` вместо `Write-Host` + `[Console]::OutputEncoding = UTF8 без BOM` — stdout стримится в UI/TG без буферизации и без cp1251-мойсиака.
- Документация: [docs/OPERATIONS.md](OPERATIONS.md) (setup с нуля, Telegram, emergency restore, CLI), [docs/RESTORE_TEST.md](RESTORE_TEST.md) (квартальный чеклист — в `C:\restore_test\` на том же hetzner-windows).
- **27 passed** (+2 теста monthly schedule), ruff чистый.

---

## 2026-04-21 — второй проход по «отложенному»

- **Host-key pinning (TOFU)**: миграция [c3d4e5f6a7b8](../alembic/versions/c3d4e5f6a7b8_server_host_key.py) добавляет `Server.ssh_host_key_pub`. [`AsyncSSHClient`](../src/serverpanel/infrastructure/ssh/client.py) принимает `known_host_key=` + `on_host_key_learned=` callback. `BackupService._open_ssh` и `RecoveryService._recover_d_drive` используют/пополняют пин; `_recover_c_drive` стирает ключ после `both`-сценария.
- **UI — StorageConfig CRUD**: [routers/storages.py](../src/serverpanel/presentation/routers/storages.py) + [templates/storages/edit.html](../src/serverpanel/templates/storages/edit.html). На [servers/detail.html](../src/serverpanel/templates/servers/detail.html) — секция «Хранилища».
- **UI — визуальный builder sources/destinations**: [static/js/backup_builder.js](../src/serverpanel/static/js/backup_builder.js).
- **Live-stream `backup.ps1`**: `ssh.execute_stream` с callback, фоновая `_pump()` задача раз в 1с шлёт строки в `_append_log` → WS.
- **i18n**: [domain/i18n.py](../src/serverpanel/domain/i18n.py) — словарь ru/en, `Settings.language` (default "ru").
- **Таймауты в Settings**: `install_*`, `recovery_*`, `backup_run_timeout`, `ssh_*` в [config.py](../src/serverpanel/config.py).
- `datetime.utcnow()` → `datetime.now(datetime.UTC)` во всех 5 местах.
- Тесты: ssh host-key line roundtrip, i18n completeness, storage CRUD auth gate. **25 passed**, ruff чистый.

---

## 2026-04-21 — промышленный аудит + правки

- **Безопасность**: fail-fast на дефолтный SECRET_KEY/ENCRYPTION_KEY; WebSocket auth+ownership ([ws_auth.py](../src/serverpanel/presentation/ws_auth.py)); `shlex.quote` в scp/recovery SSH; whitelist валидация имени скрипта в `_upload_and_run`; CSRF middleware + double-submit токен ([csrf.py](../src/serverpanel/presentation/csrf.py)); rate-limiter login/register + rotate session ([ratelimit.py](../src/serverpanel/presentation/ratelimit.py)); регистрация открыта только до первого admin; `StorageConfigRepository.get_by_id_for_user` (IDOR).
- **Надёжность**: `run_supervised` ([background.py](../src/serverpanel/presentation/background.py)); `cleanup_stale_runs` на startup ([engine.py](../src/serverpanel/infrastructure/database/engine.py)); `/health` endpoint.
- **Архитектура**: `ProgressReporter` Protocol ([domain/progress.py](../src/serverpanel/domain/progress.py)) — убрал импорт `ws_manager` из application-слоя; `WsProgressReporter` адаптер в presentation; `RecoveryService` через `create_storage` фабрику.
- **Deployability**: `importlib.resources` для templates/static; общий [templates.py](../src/serverpanel/presentation/templates.py); host/port в settings; upper bounds на все deps.
- **Hygiene**: базовые тесты — **18 зелёных**; GitHub Actions CI ([.github/workflows/ci.yml](../.github/workflows/ci.yml)); ruff select E/F/I/B/UP/C4/S.

---

## 2026-04-21 — миграция из hetzner-recovery

Вся функциональность старого Flask-проекта `hetzner-recovery` влита в `serverpanel` v2.

- `StorageProvider` → [infrastructure/providers/storage/hetzner_storagebox.py](../src/serverpanel/infrastructure/providers/storage/hetzner_storagebox.py) (SFTP, авторегистрация в `main.lifespan`).
- Pydantic-схемы `BackupSource`/`BackupDestination` → [domain/backup.py](../src/serverpanel/domain/backup.py) (discriminated union `local` | `storage`).
- Alembic baseline + `recovery_history` migration (`init_db` прогоняет `alembic upgrade head`, легаси БД stamp'ится).
- `BackupService` → [application/services/backup_service.py](../src/serverpanel/application/services/backup_service.py): ручной `run()` + `install_schedule()` / `uninstall_schedule()`.
- `backup.ps1` → [application/static/scripts/backup.ps1](../src/serverpanel/application/static/scripts/backup.ps1): VSS, robocopy, zip, local + SB, ротация.
- `RecoveryService` + таблица `RecoveryHistory` → [application/services/recovery_service.py](../src/serverpanel/application/services/recovery_service.py): 3 сценария (`c_drive`/`d_drive`/`both`).
- Recovery-скрипты → [application/static/scripts/recovery/](../src/serverpanel/application/static/scripts/recovery/).
- Роуты: `/servers/{id}/backups` + `/servers/{id}/recovery` + WS-прогресс.
- CLI-импорт: `serverpanel import-hetzner-recovery <yaml> --user-email X`.

Папка `hetzner-recovery/` перенесена в `archive/hetzner-recovery/`. README.md создан (подключить сервер → настроить бэкап → тест восстановления).
