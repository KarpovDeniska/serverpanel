# Операционная инструкция — serverpanel

Что здесь: как поставить с нуля, как восстановиться после сбоя мака, как настроить/починить Telegram-алерты, как добавить новый источник в бэкап. Не архитектура (это в `CLAUDE.md`) и не восстановление 1С-БД (это в `RESTORE_TEST.md`).

---

## 1. Setup с нуля (новый мак / после сброса)

Предполагается что на целевом Windows-сервере уже что-то работает (либо это рабочий сервер с 1С, либо ты его только что развернул вручную).

### 1.0-auto. Всё одним скриптом (рекомендуемый путь)

В репе лежит [scripts/bootstrap-mac.sh](../scripts/bootstrap-mac.sh) — ставит CLT + brew + python@3.12, клонирует репо, создаёт venv, опционально распаковывает твой tar-архив, генерирует `.env` (если его нет), прописывает LaunchAgent, проверяет `/health`.

С восстановлением из архива:
```bash
curl -fsSL https://raw.githubusercontent.com/KarpovDeniska/serverpanel/main/scripts/bootstrap-mac.sh -o /tmp/bootstrap-mac.sh
SERVERPANEL_BACKUP_TAR=~/Downloads/serverpanel-backup-20260422.tar.gz bash /tmp/bootstrap-mac.sh
```

С нуля (без архива — ключи и конфиги придётся завести руками после):
```bash
curl -fsSL https://raw.githubusercontent.com/KarpovDeniska/serverpanel/main/scripts/bootstrap-mac.sh | bash
```

Если предпочитаешь делать шагами — читай §1.0…§1.8 ниже. Скрипт делает ровно те же действия.

### 1.0. Системные зависимости (голый мак)

На чистой macOS нет ни `git`, ни `python3.12`, ни компиляторов для `cryptography`/`bcrypt`. Поставить всё за один проход:

```bash
# 1. Xcode Command Line Tools — git, clang, headers (нужны для pip install cryptography/bcrypt)
xcode-select --install   # откроет GUI-диалог, нажать Install, подождать ~5 мин

# 2. Homebrew (если ещё нет)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# после установки следовать инструкции на экране: добавить brew в PATH
# (Apple Silicon: eval "$(/opt/homebrew/bin/brew shellenv)")

# 3. Python 3.12 (pydantic v2 требует ≥3.11, проект таргетит 3.12)
brew install python@3.12

# 4. Проверка
python3.12 --version   # Python 3.12.x
git --version
```

Дополнительно понадобится:
- **SSH-ключи** для hetzner-windows и Storage Box. Если восстановление из §4-tar — они внутри архива (`~/.ssh/serverpanel-seed/`). Если с нуля — нужны приватные ключи от `gefest@hetzner-windows` и `u571198@your-storagebox.de`.
- Доступ к Telegram (аккаунт, чтобы прочитать сообщения от бота).

### 1.1. Клонировать репо и поставить зависимости

```bash
mkdir -p ~/projects && cd ~/projects
git clone https://github.com/KarpovDeniska/serverpanel.git
cd serverpanel
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

`pip install -e .` потянет все Python-зависимости из `pyproject.toml` (FastAPI, uvicorn, SQLAlchemy, paramiko, cryptography, bcrypt, itsdangerous, pydantic, jinja2, httpx, alembic). Занимает ~1–2 мин на быстром инете.

### 1.2. Сгенерировать `.env`

```bash
cat > .env <<EOF
DEBUG=false
SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
DATABASE_URL=sqlite+aiosqlite:///./data/serverpanel.db
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
```

⚠ **`ENCRYPTION_KEY` потеряешь = все SSH-ключи в БД станут мусором.** Сразу после генерации положи `.env` куда-то ещё (iCloud / 1Password / зашифрованный архив, см. §4).

### 1.3. Создать User + Server + StorageConfig одной командой

```bash
serverpanel seed \
  --admin-email you@example.com --admin-password 'yourpass' \
  --server-ip 1.2.3.4 \
  --server-ssh-key ~/.ssh/hetzner_ed25519 \
  --sb-host u571198.your-storagebox.de --sb-user u571198 \
  --sb-ssh-key ~/.ssh/storagebox_ed25519
```

Под `--server-ssh-key` и `--sb-ssh-key` указываешь пути **к существующим private-key файлам на маке** — содержимое зашифруется и ляжет в БД.

### 1.4. Создать стандартные 3 бэкап-конфига

```bash
serverpanel seed-legacy-backups
```

Появятся `legacy-daily` (03:00, rotation 14), `legacy-weekly-iis` (вс 04:00, rotation 180), `legacy-monthly` (1-го числа 05:00, rotation 365).

### 1.5. Настроить Telegram-алерты

См. §3. Результат — `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` в `.env`.

### 1.6. Экспортировать ключи из БД на диск

Дублируем SSH-ключи вне шифрованной БД, чтобы не было single-point-of-failure:

```bash
serverpanel export-keys
```

Кладёт в `~/.ssh/serverpanel-seed/<имя>_id_ed25519` с chmod 0600. Теперь ключ живёт в двух независимых местах (БД + plain).

### 1.7. Запустить uvicorn

Первый раз — руками:

```bash
uvicorn serverpanel.main:app --host 0.0.0.0 --port 5000 --reload
```

Открыть `http://localhost:5000`, залогиниться, проверить что видны 3 бэкап-конфига. Прожать **Install schedule** на каждом — создаст Task Scheduler задачи на целевом сервере.

Потом (для постоянной работы) — лучше поставить LaunchAgent, см. §7.

### 1.8. Первый прогон

У каждого конфига — **Run now**. Убедиться что пришёл ✅ в Telegram и status=success. Если partial/failed — смотреть лог прогона в UI.

---

## 2. Ежедневная жизнь

### Что ожидать

- Каждое утро в ~03:15 — ✅ в Telegram от `legacy-daily`.
- Каждое воскресенье в ~04:30 — ✅ от `legacy-weekly-iis`.
- Каждое 1-е число месяца в ~05:15 — ✅ от `legacy-monthly`.
- Через ~15 мин после прогона — запись появится в UI (Dashboard, страница сервера, список бэкапов). Фоновый поллер сам подтягивает отчёты с сервера.

**Если ✅ не пришло — проверять сразу**, не откладывать. Тишина = сигнал, что что-то не пинговалось. Сценарии:
- Задача в Task Scheduler отключена / удалена → в RDP: `taskschd.msc` → Task Scheduler Library → найти `serverpanel-backup-*`.
- `backup.ps1` падает до отправки в TG → в UI открыть `legacy-daily` → History → последний run → читать `details.log`.
- Сервер упал целиком → в RDP зайти, разбираться.
- Telegram API лежал → обычно к следующему прогону всё само починится.

### Синхронизация UI с реальностью

Task Scheduler на сервере работает независимо от serverpanel — он не создаёт `BackupHistory` строки напрямую. За видимость в UI отвечают:

- **Фоновый поллер** в `uvicorn` lifespan. По умолчанию раз в 15 минут (`SERVERPANEL_BACKUP_SYNC_INTERVAL_SECONDS=900`) ходит SSH-ом, читает `C:\ProgramData\serverpanel\configs\<id>\last_report.json` и дедупит по `run_id` → создаёт строки истории со статусом/размером/временем. `0` в env — выключить поллер.
- **Кнопки `⟲ sync`** — на Dashboard (per-server strip), на `/servers/{id}/backups`, на карточке «Бэкапы» в `/servers/{id}`. Тянет прямо сейчас без ожидания таймера.

Проверить что поллер живой: `tail -f ~/Library/Logs/serverpanel.log | grep -i "backup sync"`.

### Прогон вручную

UI → `legacy-daily` → **Run now**. Триггерит бэкап «здесь и сейчас» через SSH с мака. В `plan.json` креды подставляются на лету. Алерт в TG придёт тем же каналом.

### Поменять source paths / rotation / schedule

UI → конфиг → **Edit** → сохранить. **Внимание**: на уже **Install'енную** задачу это НЕ повлияет — в Task Scheduler сидит замороженный `plan.json`. Чтобы изменения подхватились в scheduled-run — нажать **Install schedule** ещё раз (он перезапишет frozen plan).

### Добавить новый источник в бэкап

1. UI → `legacy-daily` → **Edit** → кнопка **+ источник**.
2. Заполнить:
   - **alias** — короткое имя (без пробелов, латиница). Под этим именем файл/папка ляжет в архив на SB.
   - **type** — `dir` (папка через robocopy), `file` (один файл), `vss_dir` (папка из VSS-снимка, для живой 1С-БД).
   - **path** — абсолютный путь на целевом сервере.
   - **compress** — `none` или `zip`.
3. **Сохранить** → **Install schedule** (чтобы новый источник попал в frozen plan).

### Новый сервер

- Повторить `serverpanel seed ...` с другими `--server-name` и `--storage-name`.
- Или через UI вручную. Потом `seed-legacy-backups --server-name <новый>`.

---

## 3. Telegram-алерты

### Первичная настройка

1. В Telegram написать `@BotFather` → `/newbot` → имя бота → получить **токен** вида `123456:ABC-DEF…`.
2. Открыть созданного бота, прислать ему `/start` (чтобы он мог тебе писать).
3. Узнать свой chat_id:
   ```
   https://api.telegram.org/bot<ТОКЕН>/getUpdates
   ```
   В JSON найти `"chat":{"id":237917104,...}` — это **chat_id**.
4. Добавить в `~/projects/serverpanel/.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_CHAT_ID=237917104
   ```
5. Перезапустить uvicorn (Ctrl+C + запуск заново).
6. В UI → `legacy-daily` → **Install schedule** (чтобы frozen plan подхватил новые креды). Повторить для `legacy-weekly-iis` и `legacy-monthly`.

### Проверка

Run now → должно прийти ✅. Если нет — проверь `.env` (опечатки в токене / chat_id), что uvicorn перезапущен, и что Install schedule был повторён.

### Ротация / смена бота

Старый бот не отзывается / токен утёк → у `@BotFather`: `/revoke` на старый, `/newbot` на новый, поменять `TELEGRAM_BOT_TOKEN` в `.env`, перезапустить uvicorn, **Install schedule** ещё раз на всех конфигах.

### Текущая политика уведомлений

- **Каждый прогон** отправляет сообщение (success / partial / failed). Не молчим даже на success — отсутствие сообщения и есть сигнал тревоги.
- На partial/failed сообщение содержит per-destination ошибки.

---

## 4. Резервная копия самого serverpanel

Что нужно защитить от потери мака / диска:
1. `~/projects/serverpanel/.env` — `ENCRYPTION_KEY`, без него БД не расшифровать.
2. `~/projects/serverpanel/data/serverpanel.db` — юзеры, серверы, storage configs, бэкап-планы, история, зашифрованные ключи.
3. `~/.ssh/serverpanel-seed/` — plain SSH-ключи (страховка на случай потери пары 1+2).

### Создать бэкап

```bash
cd ~
tar czf ~/Desktop/serverpanel-backup-$(date +%Y%m%d).tar.gz \
  .ssh/serverpanel-seed \
  projects/serverpanel/.env \
  projects/serverpanel/data/serverpanel.db
```

Получившийся `.tar.gz` положить в iCloud / Google Drive / внешний диск / 1Password. **Не** хранить единственную копию только на маке.

### Восстановить на новом маке

**Если мак голый** — сначала §1.0 (xcode-select, brew, python@3.12), потом:

```bash
# 1. Клонировать репо
mkdir -p ~/projects && cd ~/projects
git clone https://github.com/KarpovDeniska/serverpanel.git
cd serverpanel
python3.12 -m venv .venv && source .venv/bin/activate && pip install --upgrade pip && pip install -e .

# 2. Достать архив из облака, распаковать
tar xzf ~/Downloads/serverpanel-backup-YYYYMMDD.tar.gz -C ~

# 3. Запустить
uvicorn serverpanel.main:app --host 0.0.0.0 --port 5000 --reload
```

В UI должны быть все три бэкап-конфига и история. Task Scheduler задачи на hetzner-windows переустанавливать не надо — они живут на сервере независимо, frozen plan уже на месте.

Для постоянной работы — поставить LaunchAgent (§7). От голого мака до работающей панели с автозапуском — ~15 минут при наличии tar-архива.

### Восстановить без `.env` (потерян `ENCRYPTION_KEY`)

БД с зашифрованными ключами стала нечитаемой — но у тебя есть `~/.ssh/serverpanel-seed/`. Вариант:

1. Сгенерировать новый `ENCRYPTION_KEY`, собрать свежий `.env`.
2. Залить свежую БД через `serverpanel seed` (см. §1.3), подставив пути к ключам из seed-папки.
3. `serverpanel seed-legacy-backups` чтобы вернуть 3 конфига.
4. **Install schedule** на всех трёх (frozen plan на сервере обновится свежими кредами).

История запусков пропадёт — это нестрашно, сами бэкапы на SB сохранены.

---

## 5. Восстановление всего Windows-сервера

Если сервер упал железом и Hetzner дал новое железо — **без Robot webservice creds serverpanel recovery не работает**. Выбор:

- **Руками**: зайти через Hetzner Robot в веб-интерфейсе, переинсталлировать Windows, поднять 1С-платформу, восстановить данные из SB по чеклисту в `RESTORE_TEST.md`.
- **Через serverpanel**: в Hetzner Robot создать webservice login (`Administration → Webservice settings → новый логин типа #ws+karpov`), добавить в UI serverpanel (`/servers/X` → провайдер-конфиг), потом `/servers/X/recovery`.

Пока креды не заведены — эта функция в UI красиво светит «not configured».

---

## 7. Автозапуск на маке через LaunchAgent

Чтобы uvicorn крутился всегда (стартовал при логине, перезапускался при падении, не требовал открытого терминала).

### Установка

```bash
mkdir -p ~/Library/LaunchAgents ~/Library/Logs
cat > ~/Library/LaunchAgents/ru.gefest.serverpanel.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ru.gefest.serverpanel</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/deniskarpov/projects/serverpanel/.venv/bin/uvicorn</string>
        <string>serverpanel.main:app</string>
        <string>--host</string><string>127.0.0.1</string>
        <string>--port</string><string>5000</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/deniskarpov/projects/serverpanel</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>/Users/deniskarpov/Library/Logs/serverpanel.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/deniskarpov/Library/Logs/serverpanel.err.log</string>
</dict>
</plist>
EOF

# перед этим — Ctrl+C на уже запущенном uvicorn, иначе порт 5000 занят
launchctl load -w ~/Library/LaunchAgents/ru.gefest.serverpanel.plist
```

Проверить: `launchctl list | grep serverpanel` (должен быть PID), `curl http://127.0.0.1:5000/health`.

### Обновление после `git pull`

```bash
cd ~/projects/serverpanel
git pull origin main
launchctl kickstart -k gui/$(id -u)/ru.gefest.serverpanel
```

### Логи

```bash
tail -f ~/Library/Logs/serverpanel.log       # stdout
tail -f ~/Library/Logs/serverpanel.err.log   # stderr (тут будут traceback-и)
```

### Выключить / включить

```bash
launchctl unload ~/Library/LaunchAgents/ru.gefest.serverpanel.plist   # остановить и убрать из автозапуска
launchctl load -w ~/Library/LaunchAgents/ru.gefest.serverpanel.plist  # снова включить
```

### Режим разработки (--reload)

Выгрузить агент (`launchctl unload ...`) → запустить `uvicorn ... --reload` в терминале вручную → после работы `launchctl load -w ...`.

## 6. Список CLI-команд serverpanel

```
serverpanel                      — запустить uvicorn (равно `serverpanel serve`)
serverpanel serve                — то же самое явно
serverpanel seed ...             — создать user/provider/server/storage из аргументов
serverpanel seed-legacy-backups  — создать 3 стандартных backup-конфига (legacy-daily/weekly/monthly)
serverpanel import-hetzner-recovery <yaml> — импортировать старый hetzner-recovery config.yaml
serverpanel export-keys          — выгрузить SSH-ключи из БД на диск (~/.ssh/serverpanel-seed/)
```

`--help` на каждой — покажет все аргументы.
