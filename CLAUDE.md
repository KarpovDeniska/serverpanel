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
- **Task-Scheduler конфиги**: `legacy-daily` (03:00, 14d), `legacy-weekly-iis` (Sun 04:00, 180d), `legacy-monthly` (1-е 05:00, 365d).
- **Telegram-алерты**: на каждый прогон → ✅ / ⚠️ / ❌. Молчание = тревога. Токен и chat_id в `.env`.
- **Видимость в UI**: фоновый поллер (default 15 мин) читает `last_report.json` по SSH и создаёт `BackupHistory` → scheduled-run'ы видны на Dashboard + `/servers/{id}` + `/servers/{id}/backups`. Кнопки `⟲ sync` тянут мгновенно.
- **Автозапуск на маке**: LaunchAgent `ru.gefest.serverpanel` (RunAtLoad + KeepAlive), логи в `~/Library/Logs/serverpanel.*`.
- **UI CRUD**: StorageConfig, ProviderConfig (+ Re-discover), BackupConfig (visual builder), backup list со статус-колонкой, карточки-сводки на dashboard и странице сервера.
- **Emergency backup самой serverpanel**: tar-архив `.env` + `data/serverpanel.db` + `~/.ssh/serverpanel-seed/` в iCloud (`Desktop/gefest/Сервер/`). Восстановление на голом маке — двойной клик по [`scripts/restore.command`](scripts/restore.command) (GUI-выбор архива → [`scripts/bootstrap-mac.sh`](scripts/bootstrap-mac.sh): CLT + brew + python@3.12 + clone + venv + распаковка + LaunchAgent). Репо публичный → без GitHub-авторизации. **Проверено 2026-04-22.**
- **Тесты**: 27 passed, ruff чистый.

**Что осталось** (не срочное):
- Первый реальный прогон RESTORE_TEST (план Q2 2026).
- Recovery-flow `both` — код есть, на реальном сервере не прогонялся.
- S3-provider — stub.
