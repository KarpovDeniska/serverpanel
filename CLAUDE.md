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
- **Integrity-check после scp (2026-04-23)**: backup.ps1 после каждого scp снимает remote-size через `sftp ls -l` и сравнивает с локальным. Mismatch → item=failed (Telegram-alert автоматом). В item в report.json добавлены `remote_size` и `integrity` (`verified`/`skipped_dir`). Закрывает «scp exit 0 на усечённом upload» (обрыв/ENOSPC/timeout). SHA256 — отдельной итерацией.
- **Тесты**: 45 passed, ruff чистый.

**Что осталось** (не срочное):
- Recovery-flow `both` — код есть, на реальном сервере не прогонялся.
- S3-provider — stub.

**Проверено 2026-04-23**: Первый прогон RESTORE_TEST end-to-end — UNF.zip с SB → скачан на hetzner-windows → распакован → `1Cv8.1CD` 2.24 GB → зарегистрирован в 1cestart как `UNF-RESTORE-TEST-2026-04-23` → база открылась, spot-check ОК. См. журнал в [docs/RESTORE_TEST.md](docs/RESTORE_TEST.md). Одновременно подтверждён integrity-check на живом пайплайне: `remote_size` совпал с `size_bytes` байт-в-байт.

**TODO — Low (UI)**: Длительность scheduled-run'ов показывается отрицательной (напр. `-6927s`). Причина: [backup_service.py:725](src/serverpanel/application/services/backup_service.py#L725) парсит `run_id` как naive server-local (CEST/UTC+2), [:732](src/serverpanel/application/services/backup_service.py#L732) парсит `run_at` как aware UTC, но БД-колонка `DateTime` без `timezone=True` стрипает tz → оба naive, но в разных зонах → `completed_at - started_at` = минус смещение. Фикс: в [backup.ps1:20](src/serverpanel/application/static/scripts/backup.ps1#L20) писать `runId` в UTC — `(Get-Date).ToUniversalTime().ToString(...)`. Тогда оба naive UTC, разница корректна. Backward compat OK: старые entries уже в БД, sync не дублирует. Один коммит.

**TODO — Critical**: Restore-pipeline не реализован. Сейчас восстановление с Storage Box — это ручные scp + Expand-Archive по [docs/RESTORE_TEST.md](docs/RESTORE_TEST.md) §0-7. Для «одна команда — новый сервер готов» нужен симметричный backup.ps1 → **restore.ps1** (скачать с SB, проверить size/hash, распаковать по путям из source'ов исходного BackupConfig) + **RestoreService** в Python (формирует plan, заливает restore.ps1 + plan, мониторит, пишет в `restore_history`) + **CLI/UI** `serverpanel restore-from-backup --server X --date Y` / кнопка «Restore» в UI рядом с backup-run. Тогда сценарий disaster recovery: (1) `serverpanel recover <server>` (Robot reinstall + cloud-init с нашим ключом — уже есть) → (2) `serverpanel restore-from-backup <server> --date <Y>` → ноль ручных scp, ноль ACL, ноль known_hosts-плясок. Панель сама выкатывает SB-ключ во временный файл на сервере (как backup делает) и удаляет в `finally`. Приоритет: сразу после валидации текущего прогона RESTORE_TEST. Объём: 1-2 дня.
