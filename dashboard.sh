#!/bin/sh
# Runs the OwO-Dusk web dashboard on its own, without starting the bot. The bot can then be started, stopped and
# restarted from the page. The address, the port and the refresh interval come from config/global_settings.json
# (website.host, website.port, website.refreshInterval).
#
# Usage:       ./dashboard.sh                settings from the config
#              ./dashboard.sh --port 1201    another port
# First time:  chmod +x dashboard.sh
# With pm2:    pm2 start dashboard.sh --name owo-dusk-dashboard
#              pm2 save
#              pm2 startup
set -eu
cd "$(dirname "$0")"
if [ ! -f config/global_settings.json ]; then
    echo "[x] config/global_settings.json was not found. Keep dashboard.sh in the project folder."
    exit 1
fi
exec python3 -m website.server "$@"
