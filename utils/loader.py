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

import json
import tomllib

import utils.config_models as config_models

with open("config/global_settings.json", "r", encoding="utf-8") as config_file:
    global_settings_dict = config_models.configs.FetchGlobalSettings(
        json.load(config_file)
    )

with open("config/webhookContent.json", "r", encoding="utf-8") as config_file:
    webhook_data_dict = json.load(config_file)


with open("config/captcha.toml", "rb") as f:
    captcha_settings_dict = tomllib.load(f)

with open("config/danger.toml", "rb") as f:
    danger_settings_dict = tomllib.load(f)

# C0: which accounts may run autoquest: Discord user ids or their last 4 digits, "*" for all. Missing key = none
_auto_quest_accounts = danger_settings_dict.get("auto_quest_accounts", [])
danger_settings_dict["auto_quest_accounts"] = (
    [str(entry).strip() for entry in _auto_quest_accounts]
    if isinstance(_auto_quest_accounts, list)
    else []
)

with open("config/misc.json", "r", encoding="utf-8") as f:
    misc_dict = json.load(f)
