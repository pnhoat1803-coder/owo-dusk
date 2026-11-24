"""D1 hint for the console command fields (read only, changed by hand in config/global_settings.json), in Vietnamese
and English, and the password wording that says 8 characters (LAN and the warning use the same minimum)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
I18N = (ROOT / "website" / "static" / "v2" / "i18n.js").read_text(encoding="utf-8")
HTML = (ROOT / "website" / "templates" / "v2.html").read_text(encoding="utf-8")
APP = (ROOT / "website" / "static" / "v2" / "app.js").read_text(encoding="utf-8")

VI_HINT = "Chỉ sửa được trong file config/global_settings.json"
EN_HINT = "Can only be changed in config/global_settings.json"


def test_the_vietnamese_hint_is_in_the_vi_table():
    vi = I18N[I18N.index("var VI = {"):I18N.index("var DICTS")]
    assert '"cfg.locked.hint": "%s"' % VI_HINT in vi


def test_the_english_hint_is_in_the_en_table():
    en = I18N[I18N.index("var EN = {"):I18N.index("var VI = {")]
    assert '"cfg.locked.hint": "%s"' % EN_HINT in en


def test_each_console_field_is_on_the_page_with_the_hint():
    for field in ("cfg-lock-cmd-captcha", "cfg-lock-cmd-ban", "cfg-lock-run-captcha",
                  "cfg-lock-run-ban", "cfg-lock-tesseract"):
        assert 'id="%s"' % field in HTML
    assert HTML.count('data-i18n="cfg.locked.hint"') >= 5


def test_the_page_fills_the_console_fields_from_the_saved_config():
    assert "function renderLockedForm(" in APP
    assert "renderLockedForm((res.config && res.config.console)" in APP


def test_the_password_wording_says_eight_characters():
    assert "password of 8+ characters" in I18N
    assert "cần mật khẩu từ 8 ký tự" in I18N
    assert "password of 6+" not in I18N
