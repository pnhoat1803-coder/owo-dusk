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

import os
import sys
from pathlib import Path

# The dashboard may be started from any folder (python -m website.server): the project folder is the working
# folder, and this must come before the package imports below
PROJECT_DIR = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_DIR)
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from .website import start_console_log_writer, web_start, website_append  # noqa: E402

# This shuts Ruff warnings.
__all__ = ["web_start", "website_append", "start_console_log_writer"]
