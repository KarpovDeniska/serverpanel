"""i18n translation lookup."""

from serverpanel.domain import i18n


def test_unknown_key_returns_key_as_is():
    assert i18n.t("nonexistent.key") == "nonexistent.key"


def test_russian_default():
    assert i18n.t("install.done") == "Завершено"


def test_english_translation_table_complete():
    # Sanity: every Russian key has an English counterpart so `t()` with
    # LANGUAGE=en never silently falls back to Russian.
    missing = [k for k, v in i18n._TRANSLATIONS.items() if "en" not in v]
    assert not missing, f"missing English translation for: {missing}"


def test_formatting_kwargs():
    # `install.image_line` → "Образ: {name}"
    out = i18n.t("install.image_line", name="Ubuntu 24.04")
    assert out == "Образ: Ubuntu 24.04"
