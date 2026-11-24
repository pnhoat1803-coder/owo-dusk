import argparse
import ctypes
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

# Run from the project folder wherever the dashboard is started from. This must come before the project imports
PROJECT_DIR = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_DIR)
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

try:  # Vietnamese text must come out right in consoles, pipes and log files alike, and must not crash the start-up
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from werkzeug.serving import make_server

from utils import lan
from . import bot_supervisor
from .website import app, configure_dashboard


def parent_is_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # access denied means it still exists
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _website_config():
    """The website block of config/global_settings.json, read only. A missing or unreadable file gives an empty dict"""
    try:
        with open(Path("config") / "global_settings.json", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except (OSError, ValueError):
        return {}
    site = data.get("website") if isinstance(data, dict) else None
    return site if isinstance(site, dict) else {}


def run_server(port=None, host=None, refresh_interval=None, version="", parent_pid=0, check_interval=5):
    """Serves the dashboard until it is stopped. parent_pid > 0: the bot started this process and it ends with the bot.
    parent_pid 0 (the default): the dashboard runs on its own, for example under pm2, and the bot is started from it."""
    site = _website_config()
    password = site.get("password") if isinstance(site.get("password"), str) else ""
    port = int(port if port is not None else site.get("port", 1200))
    refresh = int(refresh_interval if refresh_interval is not None else site.get("refreshInterval", 15))
    wanted = host if host is not None else lan.configured_host(site)
    # One bind decision for every launch path (utils/lan.py): a LAN address without a password of MIN_PASSWORD
    # characters listens on this machine only, and the block printed below says why
    bind_host, reason = lan.effective_bind(wanted, password)
    urls = lan.lan_urls(bind_host, port)
    for line in lan.startup_lines(bind_host, port, urls, reason):
        print(line)
    configure_dashboard(
        version, password, refresh, port=port, host=bind_host, lan_urls=urls,
        password_banner=lan.shows_password_banner(reason),
    )
    server = make_server(bind_host, port, app, threaded=True)
    standalone = not parent_pid
    supervisor = bot_supervisor.get_supervisor() if standalone else None
    stopping = threading.Event()

    def watch_parent():
        while not stopping.wait(check_interval):
            if not parent_is_alive(parent_pid):
                server.shutdown()
                return

    watcher = None
    if not standalone:
        watcher = threading.Thread(target=watch_parent, name="dashboard-parent-watch", daemon=True)
        watcher.start()

    if standalone:
        def on_terminate(signal_number, frame):
            # shutdown() must run on another thread: serve_forever() is the thread that gets stopped
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, on_terminate)
        if supervisor is not None:
            supervisor.start_on_launch()
    try:
        server.serve_forever()
    finally:
        stopping.set()
        server.server_close()
        if watcher is not None:
            watcher.join(timeout=1)
        if supervisor is not None:
            supervisor.shutdown()


def main():
    parser = argparse.ArgumentParser(description="OwO-Dusk web dashboard")
    parser.add_argument("--port", type=int, default=None, help="default: website.port in config/global_settings.json")
    parser.add_argument("--host", default=None, help="default: website.host in config/global_settings.json")
    parser.add_argument("--refresh-interval", type=int, default=None, help="default: website.refreshInterval")
    parser.add_argument("--version", default="")
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=0,
        help="end when this process ends (the bot passes its pid). 0: run on its own, the default",
    )
    args = parser.parse_args()
    run_server(args.port, args.host, args.refresh_interval, args.version, args.parent_pid)


if __name__ == "__main__":
    main()
