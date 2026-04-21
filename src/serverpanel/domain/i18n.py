"""Minimal i18n for progress strings emitted from long-running services.

Services call `t("key")` instead of hard-coding the Russian string. Language
is chosen via `Settings.language` (currently "ru" or "en"). Strings are tiny
— a full gettext setup would be overkill.
"""

from __future__ import annotations

from serverpanel.config import get_settings

# Format: key → {lang: translation}
_TRANSLATIONS: dict[str, dict[str, str]] = {
    # install_service
    "install.activate_rescue": {"ru": "Активация Rescue Mode", "en": "Activating rescue mode"},
    "install.hard_reset": {"ru": "Перезагрузка сервера", "en": "Hardware reset"},
    "install.wait_rescue_ssh": {"ru": "Ожидание SSH (rescue)", "en": "Waiting for rescue SSH"},
    "install.prepare_config": {"ru": "Подготовка конфигурации", "en": "Preparing installimage config"},
    "install.installing": {"ru": "Установка {name}", "en": "Installing {name}"},
    "install.inject_creds": {"ru": "Настройка доступа", "en": "Injecting credentials"},
    "install.reboot_new_os": {"ru": "Перезагрузка в новую ОС", "en": "Rebooting into new OS"},
    "install.wait_new_os": {"ru": "Ожидание загрузки новой ОС", "en": "Waiting for new OS"},
    "install.update_system": {"ru": "Обновление системы", "en": "Updating system"},
    "install.install_software": {"ru": "Установка ПО", "en": "Installing software"},
    "install.configure_firewall": {"ru": "Настройка файрвола", "en": "Configuring firewall"},
    "install.done": {"ru": "Завершено", "en": "Done"},
    "install.rescue_ready": {"ru": "Rescue mode активирован", "en": "Rescue mode activated"},
    "install.reset_sent": {"ru": "Hardware reset выполнен, ожидание загрузки...", "en": "Hardware reset sent, waiting..."},
    "install.ssh_ready": {"ru": "SSH доступен", "en": "SSH reachable"},
    "install.image_line": {"ru": "Образ: {name}", "en": "Image: {name}"},
    "install.hostname_line": {"ru": "Hostname: {name}", "en": "Hostname: {name}"},
    "install.installimage_done": {"ru": "installimage завершён", "en": "installimage finished"},
    "install.creds_done": {"ru": "SSH ключи и пароль настроены", "en": "SSH keys and password injected"},
    "install.new_os_ready": {"ru": "Новая ОС загружена", "en": "New OS booted"},
    "install.hostname_done": {"ru": "Hostname установлен", "en": "Hostname set"},
    "install.system_updated": {"ru": "Система обновлена", "en": "System updated"},
    "install.installing_pkg": {"ru": "Устанавливаю {name}...", "en": "Installing {name}..."},
    "install.pkg_done": {"ru": "{name} установлен", "en": "{name} installed"},
    "install.pkg_warn": {"ru": "Предупреждение: {name}: {err}", "en": "Warning: {name}: {err}"},
    "install.firewall_done": {"ru": "Файрвол настроен: порты {ports}", "en": "Firewall configured: ports {ports}"},
    "install.firewall_skipped": {"ru": "Файрвол не настраивался", "en": "Firewall not configured"},
    "install.final": {"ru": "Установка {name} завершена!", "en": "Install of {name} complete!"},
    "install.error": {"ru": "ОШИБКА: {msg}", "en": "ERROR: {msg}"},
    # recovery_service
    "recovery.wait_rescue_boot": {"ru": "Ожидание загрузки Rescue Linux", "en": "Waiting for rescue Linux"},
    "recovery.install_tools": {"ru": "Установка wimtools и ntfs-3g", "en": "Installing wimtools and ntfs-3g"},
    "recovery.iso_download": {"ru": "Загрузка Windows ISO с Storage Box", "en": "Downloading Windows ISO from Storage Box"},
    "recovery.partition": {"ru": "Разметка диска", "en": "Partitioning disk"},
    "recovery.wimapply": {"ru": "Развёртывание Windows (wimapply)", "en": "Applying Windows image (wimapply)"},
    "recovery.inject": {"ru": "Инъекция autounattend и скриптов", "en": "Injecting autounattend and scripts"},
    "recovery.bcd": {"ru": "Восстановление загрузчика BCD", "en": "Restoring BCD loader"},
    "recovery.disable_rescue": {"ru": "Отключение Rescue и reboot в Windows", "en": "Deactivating rescue and rebooting into Windows"},
    "recovery.wait_windows": {"ru": "Ожидание Windows", "en": "Waiting for Windows"},
    "recovery.init_d": {"ru": "Инициализация нового диска D:", "en": "Initializing new D: disk"},
    "recovery.upload_restore": {"ru": "Загрузка restore_data.ps1", "en": "Uploading restore_data.ps1"},
    "recovery.restore_data": {"ru": "Восстановление данных из Storage Box", "en": "Restoring data from Storage Box"},
    "recovery.reinstall_backup_cfg": {"ru": "Переустановка backup-конфигов", "en": "Reinstalling backup configs"},
    "recovery.done": {"ru": "Готово", "en": "Done"},
    "recovery.rescue_ready": {"ru": "Rescue mode активирован", "en": "Rescue mode activated"},
    "recovery.reset_sent": {"ru": "Hardware reset отправлен", "en": "Hardware reset sent"},
    "recovery.iso_ok": {"ru": "ISO скачан → {path}", "en": "ISO downloaded → {path}"},
    "recovery.reinstall_hint": {"ru": "Бэкапы заново зарегистрируются через BackupService.install_schedule()", "en": "Backups will be re-registered via BackupService.install_schedule()"},
    "recovery.ssh_wait": {"ru": "SSH ожидание... {sec}s", "en": "SSH waiting... {sec}s"},
}


def t(key: str, **kwargs) -> str:
    """Lookup translation for the configured language; fall back to `ru`, then key."""
    lang = (get_settings().language or "ru").lower()
    entry = _TRANSLATIONS.get(key)
    if entry is None:
        return key
    text = entry.get(lang) or entry.get("ru") or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except KeyError:
            return text
    return text
