from utils.accounts_file import NO_ACCOUNTS_MESSAGE, read_accounts

FAKE_LINE = "FAKE.TOKEN.VALUE 123456789012345678"
FAKE_PAIR = ["FAKE.TOKEN.VALUE", "123456789012345678"]


def test_missing_file_is_created_empty_and_gives_no_accounts(tmp_path, capsys):
    path = tmp_path / "tokens.txt"

    assert read_accounts(path) == []
    assert path.read_text(encoding="utf-8") == ""
    assert capsys.readouterr().out == ""


def test_empty_file_gives_no_accounts(tmp_path):
    path = tmp_path / "tokens.txt"
    path.write_text("", encoding="utf-8")

    assert read_accounts(path) == []


def test_comments_and_blank_lines_only_give_no_accounts(tmp_path):
    path = tmp_path / "tokens.txt"
    path.write_text("# first comment\n\n   \n   # indented comment\n", encoding="utf-8")

    assert read_accounts(path) == []


def test_one_valid_line_gives_one_pair_and_prints_nothing(tmp_path, capsys):
    path = tmp_path / "tokens.txt"
    path.write_text(FAKE_LINE + "\n", encoding="utf-8")

    assert read_accounts(path) == [FAKE_PAIR]
    assert capsys.readouterr().out == ""


def test_a_bad_line_is_skipped_and_its_text_is_never_printed(tmp_path, capsys):
    path = tmp_path / "tokens.txt"
    path.write_text(FAKE_LINE + "\nFAKE.ONLY.ONEFIELD extra fields here\n", encoding="utf-8")

    assert read_accounts(path) == [FAKE_PAIR]
    out = capsys.readouterr().out
    assert "Dòng 2 trong tokens.txt sai định dạng, bỏ qua." in out
    assert "FAKE.ONLY" not in out
    assert "extra" not in out


def test_the_message_for_no_accounts_names_the_format_and_the_readme():
    assert "Chưa có tài khoản nào trong tokens.txt." in NO_ACCOUNTS_MESSAGE
    assert "<token> <id kênh>" in NO_ACCOUNTS_MESSAGE
    assert "README.md" in NO_ACCOUNTS_MESSAGE
