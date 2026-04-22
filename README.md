# ServerPanel

Веб-панель для управления выделенными серверами (dedicated): инвентаризация, переустановка ОС, резервное копирование и восстановление. Мультипровайдерная — сейчас Hetzner Robot и Hetzner Storage Box (SFTP), добавление нового провайдера сводится к реализации одного `Protocol`-интерфейса.

## Возможности

- Автодискавери серверов через API провайдера (Hetzner Robot).
- Переустановка ОС через rescue + `installimage` (12-фазная оркестрация).
- Резервное копирование Windows-сервера через Task Scheduler: VSS → robocopy → zip (Zip64) → Storage Box, ротация.
- Три расписания из коробки: `daily`, `weekly`, `monthly` (см. legacy-конфиги в импортёре).
- Восстановление Windows из бэкапа в трёх сценариях: `c_drive`, `d_drive`, `both`.
- Telegram-алерты на каждый прогон бэкапа (success/partial/failed) — как heartbeat, молчание = тревога.
- Визуальный builder источников/назначений в UI.
- Live-stream stdout `backup.ps1` в UI через WebSocket.
- Шифрование SSH-ключей и API-токенов Fernet'ом + CLI экспорта ключей на диск (страховка от single-point-of-failure).
- Мульти-пользовательский доступ, первый зарегистрированный аккаунт становится `admin`.

## Установка и первый запуск

```bash
git clone https://github.com/KarpovDeniska/serverpanel.git && cd serverpanel
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .

# .env с обязательными секретами (подробно — в docs/OPERATIONS.md §1.2)
cat > .env <<EOF
SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
EOF

uvicorn serverpanel.main:app --host 0.0.0.0 --port 5000 --reload
```

Откройте `http://localhost:5000/register`, создайте первого пользователя — он автоматически получит роль `admin`.

⚠ `ENCRYPTION_KEY` — потеряете, все SSH-ключи в БД станут мусором. Сразу же бэкап `.env` в надёжное место (см. `docs/OPERATIONS.md` §4).

## Документация

| Файл | О чём |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Архитектура: слои, доменная модель, провайдерная абстракция. |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Setup с нуля, Telegram-алерты, emergency restore серверпанели, добавление источника, CLI-команды. |
| [docs/RESTORE_TEST.md](docs/RESTORE_TEST.md) | Квартальный чеклист проверки восстанавливаемости бэкапов. |

## CLI

```
serverpanel                       — запустить uvicorn
serverpanel seed ...              — создать user/provider/server/storage из аргументов
serverpanel seed-legacy-backups   — создать 3 стандартных backup-конфига (daily/weekly/monthly)
serverpanel import-hetzner-recovery <yaml> — импортировать legacy config.yaml
serverpanel export-keys           — выгрузить SSH-ключи из БД на диск (~/.ssh/serverpanel-seed)
```

Полный список аргументов — `serverpanel <cmd> --help`. Подробности использования — в [OPERATIONS.md](docs/OPERATIONS.md).

## Формат Schedule

- `HH:MM` — ежедневно в это время (`03:00`).
- `weekly:DAY@HH:MM` — раз в неделю, `DAY` из `Mon/Tue/Wed/Thu/Fri/Sat/Sun` (`weekly:Sun@04:00`).
- `monthly:D@HH:MM` — `D` в 1..31 (Windows клампит 31 к последнему дню короткого месяца) (`monthly:1@05:00`).
- Пусто — только ручной запуск.

## Тесты

```bash
pytest
```

Разбивка: `tests/test_domain`, `tests/test_infrastructure`, `tests/test_presentation`.

## Текущие ограничения

- Hetzner Robot webservice credentials — отдельная задача провайдера; без них Recovery-сценарий в UI не работает (бэкапы работают независимо).
- S3-provider — пока только stub, реализован только `hetzner_storagebox`.

## Лицензия

Внутренний проект.
