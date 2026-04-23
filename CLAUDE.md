# ServerPanel

Веб-панель для управления выделенными серверами (dedicated): инвентаризация, переустановка ОС, настройка софта, резервное копирование, мониторинг. Мульти-провайдерная — конкретный провайдер (Hetzner Robot и др.) подключается по единому Protocol-интерфейсу.

## Документация

- [README.md](README.md) — быстрый старт
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — стек, слои, доменная модель, провайдеры, конфиг, инженерные решения
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — setup с нуля, Telegram, emergency restore, CLI, LaunchAgent
- [docs/RESTORE_TEST.md](docs/RESTORE_TEST.md) — квартальный чеклист теста восстановления
- [docs/HISTORY.md](docs/HISTORY.md) — хронология крупных правок

## Статус

Прод-ready бэкапы 1С-сервера hetzner-windows через Task Scheduler + Telegram heartbeat.

- **Сервер**: hetzner-windows (Windows Server 2022, IP `148.251.46.106`, Robot `server_number=910543`). Провайдерский конфиг + SSH-ключ + Robot webservice creds заведены.
- **Хранилище**: Hetzner Storage Box (`u571198@u571198.your-storagebox.de:23`), SFTP, SSH-ключ.
- **Task-Scheduler конфиги** (все с ExecutionTimeLimit=PT30M + watchdog): `legacy-daily` (03:00, 14d), `legacy-weekly-iis` (Sun 04:00, 180d), `legacy-monthly` (1-е 05:00, 365d). После `install_schedule` бэкенд валидирует XML задачи (`_validate_scheduled_task`) чтобы поймать quote-escape регрессии вроде 2026-04-22.
- **Telegram-алерты**: на каждый прогон → ✅ / ⚠️ / ❌ (с ручным суммированием MB, не `Measure-Object` — тот не видит хэш-ключи в PS 5.1). Если backup.ps1 не дошёл до отчёта (Task Scheduler kill) → **watchdog.ps1** шлёт отдельный `[X] Backup hung / killed by timeout`. Молчание = тревога. Токен и chat_id в `.env`.
- **Byte-level progress**: `backup.ps1` атомарно пишет `progress.json` (per-alias тик + 5 с heartbeat-job), `BackupService.fetch_live_progress` поллит по SSH, `/servers/{id}/backups/{cfg}/runs/{h}/progress` отдаёт phase/percent/age, `run.html` рисует вторую полоску с цветом по фазе (running/stalled/finished). Threshold stall настраивается per-config (`stall_threshold_seconds`, default 120).
- **Видимость в UI**: фоновый поллер (default 15 мин) читает `last_report.json` по SSH и создаёт `BackupHistory` → scheduled-run'ы видны на Dashboard + `/servers/{id}` + `/servers/{id}/backups`. Кнопки `⟲ sync` тянут мгновенно.
- **Автозапуск на маке**: LaunchAgent `ru.gefest.serverpanel` (RunAtLoad + KeepAlive), логи в `~/Library/Logs/serverpanel.*`. После `git pull` обязательно `uv run alembic upgrade head` (в доке прописано).
- **UI CRUD**: StorageConfig, ProviderConfig (+ Re-discover), BackupConfig (visual builder), backup list со статус-колонкой, карточки-сводки на dashboard и странице сервера.
- **Emergency backup самой serverpanel**: кнопка **⬇ Скачать архив** на `/settings` (роут `POST /settings/self-backup`) — стримит tar.gz с `.env` + `data/serverpanel.db` + `~/.ssh/serverpanel-seed/` как download, браузер показывает нативный «Сохранить как…», сохраняешь в iCloud `Desktop/gefest/Сервер/`. Восстановление на голом маке — двойной клик по [`scripts/restore.command`](scripts/restore.command) (GUI-выбор архива → [`scripts/bootstrap-mac.sh`](scripts/bootstrap-mac.sh): CLT + brew + python@3.12 + clone + venv + распаковка + LaunchAgent). Репо публичный → без GitHub-авторизации. **Проверено 2026-04-22.**
- **Reliability-фиксы 2026-04-23** (проверены 3 прогонами подряд на hetzner-windows, 9/9 success каждый, rotation сработала): date_folder = live `Get-Date` вместо frozen `$plan.date_folder`, icacls `/grant:r` по SID (не по `$env:USERNAME` — под SYSTEM он `"<HOST>$"`), ssh `rm -rf` target перед `scp -r` (иначе `target/basename/` дубликат), rotation-basename fix (sftp `ls -1` возвращает полные пути — старый regex молча не матчил → rotation никогда не работала с дня 1), scp stderr в report.json. См. `docs/HISTORY.md` 2026-04-23.
- **Тесты**: 42 passed, ruff чистый.

**Что осталось** (не срочное):
- Первый реальный прогон RESTORE_TEST (план Q2 2026).
- Recovery-flow `both` — код есть, на реальном сервере не прогонялся.
- S3-provider — stub.
