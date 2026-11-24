from website.website import dashboard_password_notice
from utils import lan


def test_no_password_says_only_this_computer_can_open_it():
    notice = dashboard_password_notice("")
    assert notice == "Dashboard chưa đặt mật khẩu: chỉ mở được trên máy này."
    assert dashboard_password_notice(None) == notice


def test_a_short_password_gets_the_length_hint_with_the_real_minimum():
    assert dashboard_password_notice("x" * (lan.MIN_PASSWORD - 1)) == (
        f"Mật khẩu dashboard quá ngắn (nên từ {lan.MIN_PASSWORD} ký tự trở lên)."
    )


def test_a_password_of_the_minimum_length_gets_no_notice():
    assert dashboard_password_notice("x" * lan.MIN_PASSWORD) is None
    assert dashboard_password_notice("a much longer dashboard password") is None
