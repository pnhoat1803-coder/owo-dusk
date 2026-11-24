# This file is part of owo-dusk.
#
# Copyright (c) 2024-present EchoQuill
#
# Portions of this file are based on code by EchoQuill, licensed under the
# GNU General Public License v3.0 (GPL-3.0).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""tokens.txt: one account per line, `<token> <channel id>`: exactly two fields separated by spaces.

bot_runner.run_bots unpacks every pair as (token, channel_id), so a line with any other number of fields
would crash the start-up. Lines that are blank or start with # are comments.
"""

from pathlib import Path

NO_ACCOUNTS_MESSAGE = (
    "Chưa có tài khoản nào trong tokens.txt.\n"
    "Mở file tokens.txt và thêm mỗi dòng một tài khoản theo dạng:\n"
    "<token> <id kênh>\n"
    "Xem hướng dẫn chi tiết trong README.md."
)


def read_accounts(path="tokens.txt"):
    """The accounts in the file as [token, channel_id] pairs.

    A missing file is created empty and gives no accounts. A line with the wrong number of fields is skipped with
    a message that gives its number only, never its text, because a token line must not reach the console or a log.
    """
    path = Path(path)
    if not path.exists():
        path.write_text("", encoding="utf-8")
        return []
    accounts = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 2:
            print(f"Dòng {number} trong tokens.txt sai định dạng, bỏ qua.")
            continue
        accounts.append(fields)
    return accounts
