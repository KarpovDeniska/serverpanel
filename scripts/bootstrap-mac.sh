#!/usr/bin/env bash
# bootstrap-mac.sh — от голого macOS до работающего serverpanel.
#
# Поддерживает два режима:
#   1. С архивом восстановления (SERVERPANEL_BACKUP_TAR=/путь/к/backup.tar.gz)
#      — распаковывает .env + data/serverpanel.db + ~/.ssh/serverpanel-seed/
#   2. Без архива — ставит всё с нуля, дальше нужно руками seed + seed-legacy-backups.
#
# Запуск:
#   curl -fsSL https://raw.githubusercontent.com/KarpovDeniska/serverpanel/main/scripts/bootstrap-mac.sh | bash
#   # или с архивом:
#   SERVERPANEL_BACKUP_TAR=~/Downloads/serverpanel-backup-20260422.tar.gz bash bootstrap-mac.sh

set -euo pipefail

REPO_URL="https://github.com/KarpovDeniska/serverpanel.git"
PROJECTS_DIR="${HOME}/projects"
PROJECT_DIR="${PROJECTS_DIR}/serverpanel"
PY_VER="3.12"

info()  { printf "\033[1;34m[*]\033[0m %s\n" "$*"; }
ok()    { printf "\033[1;32m[✓]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[!]\033[0m %s\n" "$*"; }
die()   { printf "\033[1;31m[✗]\033[0m %s\n" "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "Скрипт только для macOS"

# --- 1. Xcode Command Line Tools ---
if xcode-select -p >/dev/null 2>&1; then
  ok "Xcode Command Line Tools уже установлены"
else
  info "Устанавливаю Xcode Command Line Tools (откроется GUI-диалог, нажми Install)"
  xcode-select --install || true
  until xcode-select -p >/dev/null 2>&1; do
    sleep 10
    info "Жду завершения установки CLT..."
  done
  ok "Xcode Command Line Tools установлены"
fi

# --- 2. Homebrew ---
if command -v brew >/dev/null 2>&1; then
  ok "Homebrew уже установлен ($(brew --prefix))"
else
  info "Устанавливаю Homebrew"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
    ZSHRC="${HOME}/.zprofile"
    if ! grep -q 'brew shellenv' "${ZSHRC}" 2>/dev/null; then
      echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "${ZSHRC}"
    fi
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  ok "Homebrew установлен"
fi

# --- 3. Python 3.12 ---
if command -v "python${PY_VER}" >/dev/null 2>&1; then
  ok "python${PY_VER} уже установлен ($(python${PY_VER} --version))"
else
  info "Ставлю python@${PY_VER} через brew"
  brew install "python@${PY_VER}"
  ok "python${PY_VER} установлен"
fi

# --- 4. Git ---
command -v git >/dev/null 2>&1 || die "git не найден (должен был приехать с CLT)"
ok "git найден ($(git --version))"

# --- 5. Клонировать / подтянуть репо ---
mkdir -p "${PROJECTS_DIR}"
if [[ -d "${PROJECT_DIR}/.git" ]]; then
  info "Репо уже склонирован — git pull"
  git -C "${PROJECT_DIR}" pull --ff-only origin main
else
  info "Клонирую ${REPO_URL}"
  git clone "${REPO_URL}" "${PROJECT_DIR}"
fi
ok "Репо: ${PROJECT_DIR}"

# --- 6. venv + pip install ---
cd "${PROJECT_DIR}"
if [[ ! -d .venv ]]; then
  info "Создаю .venv"
  "python${PY_VER}" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
info "Обновляю pip и ставлю зависимости (-e .)"
pip install --quiet --upgrade pip
pip install --quiet -e .
ok "Python-зависимости установлены"

# --- 7. Восстановление из tar-архива (опционально) ---
if [[ -n "${SERVERPANEL_BACKUP_TAR:-}" ]]; then
  [[ -f "${SERVERPANEL_BACKUP_TAR}" ]] || die "Архив не найден: ${SERVERPANEL_BACKUP_TAR}"
  info "Распаковываю ${SERVERPANEL_BACKUP_TAR} в ${HOME}"
  tar xzf "${SERVERPANEL_BACKUP_TAR}" -C "${HOME}"
  if [[ -d "${HOME}/.ssh/serverpanel-seed" ]]; then
    chmod 700 "${HOME}/.ssh/serverpanel-seed"
    find "${HOME}/.ssh/serverpanel-seed" -type f -exec chmod 600 {} \;
  fi
  [[ -f "${PROJECT_DIR}/.env" ]] || warn ".env в архиве не найден — сгенерируй вручную (docs/OPERATIONS.md §1.2)"
  [[ -f "${PROJECT_DIR}/data/serverpanel.db" ]] || warn "data/serverpanel.db в архиве не найден"
  ok "Архив распакован"
else
  mkdir -p data
  if [[ ! -f .env ]]; then
    info "Генерирую .env с новыми SECRET_KEY/ENCRYPTION_KEY"
    SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
    ENC_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    cat > .env <<EOF
DEBUG=false
SECRET_KEY=${SECRET_KEY}
ENCRYPTION_KEY=${ENC_KEY}
DATABASE_URL=sqlite+aiosqlite:///./data/serverpanel.db
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
    warn ".env создан со свежими ключами — положи копию в iCloud/1Password СРАЗУ"
    warn "Дальше: serverpanel seed ... + seed-legacy-backups (см. docs/OPERATIONS.md §1.3)"
  else
    ok ".env уже есть — оставляю как есть"
  fi
fi

# --- 8. LaunchAgent для автозапуска ---
AGENT_PLIST="${HOME}/Library/LaunchAgents/ru.gefest.serverpanel.plist"
mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/Library/Logs"
VENV_UVICORN="${PROJECT_DIR}/.venv/bin/uvicorn"

info "Пишу LaunchAgent ${AGENT_PLIST}"
cat > "${AGENT_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ru.gefest.serverpanel</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_UVICORN}</string>
        <string>serverpanel.main:app</string>
        <string>--host</string><string>127.0.0.1</string>
        <string>--port</string><string>5000</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>${HOME}/Library/Logs/serverpanel.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/Library/Logs/serverpanel.err.log</string>
</dict>
</plist>
EOF

launchctl unload "${AGENT_PLIST}" >/dev/null 2>&1 || true
launchctl load -w "${AGENT_PLIST}"
ok "LaunchAgent загружен"

# --- 9. Проверка ---
sleep 2
if curl -fsS http://127.0.0.1:5000/health >/dev/null 2>&1; then
  ok "ServerPanel работает: http://127.0.0.1:5000"
else
  warn "Панель пока не отвечает на /health — смотри ~/Library/Logs/serverpanel.err.log"
fi

echo
ok "Готово. Что дальше:"
echo "  - открыть http://127.0.0.1:5000 и залогиниться"
if [[ -z "${SERVERPANEL_BACKUP_TAR:-}" ]]; then
  echo "  - serverpanel seed ... (см. docs/OPERATIONS.md §1.3)"
  echo "  - serverpanel seed-legacy-backups"
  echo "  - настроить Telegram (§3) и Install schedule на каждом конфиге"
fi
echo "  - логи:  tail -f ~/Library/Logs/serverpanel.log"
echo "  - stop:  launchctl unload ${AGENT_PLIST}"
