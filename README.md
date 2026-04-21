# ServerPanel

Веб-панель для управления выделенными серверами (dedicated): инвентаризация, переустановка ОС, резервное копирование и восстановление. Мультипровайдерная архитектура — сейчас поддерживается Hetzner Robot и Hetzner Storage Box (SFTP), добавление нового провайдера сводится к реализации одного `Protocol`-интерфейса.

## Возможности

- Автодискавери серверов через API провайдера (Hetzner Robot).
- Переустановка ОС через rescue + `installimage` (12-фазная оркестрация).
- Резервное копирование Windows-сервера через Task Scheduler: VSS → robocopy → zip → локальная копия + Storage Box, ротация.
- Восстановление Windows из бэкапа в трёх сценариях: только `C:\`, только `D:\`, оба диска (с reinstall).
- Шифрование SSH-ключей, паролей и API-токенов в БД (Fernet).
- Мульти-пользовательский доступ, первый зарегистрированный аккаунт становится `admin`.
- SSR UI (Jinja2 + HTMX) + WebSocket-прогресс для долгих операций.

## Установка

```bash
cd serverpanel
pip install -e .
# для разработки:
pip install -e .[dev]
# опционально:
pip install -e .[oauth,postgres]
```

Создайте `.env` в корне проекта:

```ini
# 1. Секрет для сессионных cookies
SECRET_KEY=<сгенерируйте: python -c "import secrets; print(secrets.token_urlsafe(32))">

# 2. Ключ для шифрования креденшелов (Fernet, base64 32 байта)
ENCRYPTION_KEY=<сгенерируйте: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">

# 3. БД: по умолчанию SQLite в data/serverpanel.db.
# Для PostgreSQL: DATABASE_URL=postgresql+asyncpg://user:pass@host/db
# DATABASE_URL=sqlite+aiosqlite:///./data/serverpanel.db

# 4. debug=True включает /api/docs и uvicorn --reload
# DEBUG=true
```

> `ENCRYPTION_KEY` менять нельзя после того, как в БД появились зашифрованные записи — расшифровать их потом будет нечем. Делайте бэкап ключа вместе с бэкапом БД.

## Запуск

```bash
serverpanel          # старт веб-сервера на http://0.0.0.0:5000
# либо:
uvicorn serverpanel.main:app --host 0.0.0.0 --port 5000
```

Миграции Alembic запускаются автоматически при старте (`init_db`).

Откройте `http://localhost:5000/register`, создайте первого пользователя — он автоматически получит роль `admin`.

## Workflow

### 1. Подключить сервер

1. Войдите в Hetzner Robot → «Настройки» → Webservice и Apps → создайте **webservice-логин** (начинается с `#ws+`). Обычный логин для Robot Webservice не работает.
2. В ServerPanel: `Серверы → Добавить сервер`.
   - `Тип провайдера`: `hetzner_dedicated`.
   - `Имя`: любое (метка для UI).
   - `Логин / пароль`: webservice-креды из шага 1.
3. ServerPanel обратится к Robot API и импортирует все серверы аккаунта. Они появятся в `Серверы`.
4. Откройте карточку сервера — увидите IP, OS, capabilities (что умеет провайдер), панель статуса (порты TCP, rescue-режим, трафик).

### 2. Настроить бэкап (Windows)

Предпосылки: на сервере уже стоит Windows, есть RDP, есть SSH (OpenSSH server) c доступом по ключу для админ-учётки. В БД — запись `Server` с указанными `ssh_username` и `ssh_private_key_encrypted` (сейчас SSH-ключи для Windows заводятся импортёром / админом в БД напрямую; UI для этого ещё не сделан).

1. **Создать StorageConfig для сервера.** Сейчас это делается либо CLI-импортом legacy-конфига (см. ниже), либо руками в БД — форма для создания Storage Box на сервер-детейле пока в TODO. Поддерживаются типы `hetzner_storagebox`, `sftp`, `s3` (реализован пока только `hetzner_storagebox`).
2. `Серверы → <сервер> → Бэкапы → Новый бэкап`.
   - `Имя`: любое, например `daily-system`.
   - `Sources` (JSON-массив): что копировать.
     ```json
     [
       {"type": "local", "path": "C:/Users"},
       {"type": "local", "path": "C:/inetpub/wwwroot"}
     ]
     ```
   - `Destinations` (JSON-массив): куда класть.
     ```json
     [
       {"type": "local", "path": "D:/backups"},
       {"type": "storage", "storage_config_id": 1, "remote_path": "/backups/daily"}
     ]
     ```
   - `Schedule`: когда запускать. Поддерживаются два формата:
     - `HH:MM` — ежедневно в это время (например `03:00`).
     - `weekly:DAY@HH:MM` — раз в неделю, `DAY` из `Mon/Tue/Wed/Thu/Fri/Sat/Sun` (например `weekly:Sun@04:00`).
     - Пусто = только ручной запуск. Реализация: [backup_service.py](src/serverpanel/application/services/backup_service.py).
   - `Rotation days`: сколько дневных папок хранить (по умолчанию 14).
3. После создания — кнопка **Install schedule**. ServerPanel по SSH зальёт `backup.ps1` на сервер и зарегистрирует задачу в Task Scheduler от `SYSTEM`.
4. **Run now** — ручной запуск. Прогресс и stdout стримятся в окне истории через WebSocket.

> `backup.ps1` пишется в UTF-8 **с BOM** — без неё PowerShell 5.1 на ru-RU локали ломает парсинг кириллицы и длинных тире. Не меняйте кодировку шаблона.

### 3. Тест восстановления

Проверка, что из бэкапа реально можно развернуться — критически важна. Это «живой» тест, затрагивает боевой сервер, выполняйте на стенде.

1. Убедитесь, что на сервере отработал хотя бы один бэкап (в истории есть `success`, папка в Storage Box существует).
2. `Серверы → <сервер> → Восстановление → Новое`. Сценарии:
   - **c_drive** — восстановить только `C:\` из последней дневной папки (data-диск не трогается).
   - **d_drive** — восстановить только `D:\` (используется редко; быстро и не требует rescue).
   - **both** — полный откат: сервер уходит в rescue, партиционируется диск, ставится Windows из ISO на Storage Box, применяется config, после первого входа выполняется `restore.ps1` + `restore_data.ps1` + `install_software.ps1`.
3. В wizard’е выберите:
   - `StorageConfig` (откуда брать бэкап).
   - `daily_folder`: обычно `latest`, но можно указать конкретную дату (например `2026-04-20`).
   - Для `both`: hostname, admin_password, product_key, путь к ISO и BCD на Storage Box (дефолты подставлены).
   - Галочки софта к доставке после установки (из `install_software.ps1`).
4. После старта — редирект на страницу прогресса с WebSocket-логом. Для `both` жизненный цикл выглядит так:
   - `boot rescue` → `partition disk` → `apply windows image` → `inject config` → `reboot` → post-install (SetupComplete.cmd) → `restore data` → `install software` → `done`.
5. Проверка успеха:
   - Сервер поднимается, RDP/SSH работают.
   - `C:\` и `D:\` содержат ожидаемые файлы.
   - Службы из `monitored_services` стартовали (если заведены).

> Для `both` сервер реально переустанавливается. Ни в коем случае не запускайте на production без rescue-пароля и внешнего доступа к провайдеру — если что-то пойдёт не так, восстанавливать придётся через KVM/BMC.

## CLI — импорт legacy-конфига `hetzner-recovery`

Если есть `config.yaml` от старого проекта `hetzner-recovery`, одной командой создаются `ProviderConfig`, `Server`, `StorageConfig` и два `BackupConfig` (`legacy-daily`, `legacy-weekly-iis`):

```bash
serverpanel import-hetzner-recovery ./config.yaml \
  --user-email me@example.com \
  --sb-private-key /path/to/sb_id_ed25519 \
  --server-private-key /path/to/server_id_ed25519
```

Идемпотентно: повторный запуск ничего не дублирует.

## Архитектура

Слои и ключевые точки входа — в [CLAUDE.md](CLAUDE.md). Кратко:

- `domain/` — чистая модель и Protocol-интерфейсы.
- `application/` — сервисы (`install_service`, `backup_service`, `recovery_service`), статические скрипты (`backup.ps1`, `recovery/*.sh`, `recovery/*.ps1`).
- `infrastructure/` — БД, провайдеры, SSH, auth, Fernet.
- `presentation/` — FastAPI-роутеры, middleware, WebSocket-менеджер, Jinja2-шаблоны.

## Тесты

```bash
pytest
```

Разбивка: `tests/test_domain`, `tests/test_infrastructure`, `tests/test_presentation`.

## Текущие ограничения

- Нет UI для создания `StorageConfig` и SSH-ключей к Windows-серверам — сейчас через CLI-импорт или БД напрямую.
- `sources` и `destinations` в форме бэкапа вводятся JSON’ом — визуальный builder в планах.
- Раннера расписания на стороне ServerPanel нет: расписание выполняется Task Scheduler’ом на самом сервере (`install_schedule`), запись в `backup_history` появляется, когда задача отчитывается обратно.
- `backup.ps1` пишет stdout в файл на сервере; WebSocket в ServerPanel отдаёт лог только после завершения запуска — live-stream в планах.

## Лицензия

Внутренний проект.
