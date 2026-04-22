#!/bin/bash
# restore.command — двойной клик на маке запускает восстановление serverpanel.
# Как пользоваться:
#   1. Положи этот файл рядом с serverpanel-backup-*.tar.gz в iCloud.
#   2. chmod +x restore.command (в Finder: ПКМ → Открыть в Терминале один раз, дальше работает двойной клик).
#   3. Двойной клик → выскочит диалог выбора архива → выбираешь .tar.gz.
# Gatekeeper на первом запуске: если «неизвестный разработчик» — ПКМ → Открыть → Открыть.

set -euo pipefail

cd "$HOME"

echo
echo "=== ServerPanel restore ==="
echo

# 1. Выбор архива через GUI-диалог macOS
TAR=$(osascript <<'EOF'
try
    set theFile to choose file with prompt "Выбери serverpanel-backup-*.tar.gz" of type {"org.gnu.gnu-zip-archive","public.tar-archive","public.data"} default location (path to home folder)
    return POSIX path of theFile
on error
    return ""
end try
EOF
)

if [[ -z "${TAR}" ]]; then
    echo "Отменено."
    read -rp "Enter чтобы закрыть..."
    exit 1
fi

echo "Архив: ${TAR}"
echo

if [[ ! -f "${TAR}" ]]; then
    echo "Файл не найден: ${TAR}"
    read -rp "Enter чтобы закрыть..."
    exit 1
fi

# 2. Клонируем репо если нет, иначе git pull
mkdir -p "${HOME}/projects"
if [[ -d "${HOME}/projects/serverpanel/.git" ]]; then
    git -C "${HOME}/projects/serverpanel" pull --ff-only origin main || true
else
    git clone https://github.com/KarpovDeniska/serverpanel.git "${HOME}/projects/serverpanel"
fi

# 3. Запускаем bootstrap с выбранным архивом
export SERVERPANEL_BACKUP_TAR="${TAR}"
bash "${HOME}/projects/serverpanel/scripts/bootstrap-mac.sh"

echo
echo "=== Готово. ServerPanel: http://127.0.0.1:5000 ==="
echo
read -rp "Enter чтобы закрыть..."
