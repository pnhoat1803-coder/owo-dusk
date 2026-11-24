import atexit
import os
import subprocess
import sys
import threading
import time

# Every manager that was started, see stop_all()
_started = []


class DashboardProcessManager:
    def __init__(self, port, host, refresh_interval, version, cwd=None):
        self.port = int(port)
        self.host = host
        self.refresh_interval = int(refresh_interval)
        self.version = version
        self.cwd = cwd or os.getcwd()
        self.process = None
        self.stop_event = threading.Event()
        self.monitor_thread = None
        self.restart_count = 0
        self.lock = threading.Lock()

    def _launch(self):
        command = [
            sys.executable,
            "-m",
            "website.server",
            "--port",
            str(self.port),
            "--host",
            self.host,
            "--refresh-interval",
            str(self.refresh_interval),
            "--version",
            str(self.version),
            "--parent-pid",
            str(os.getpid()),
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.Popen(
            command,
            cwd=self.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )

    def start(self):
        try:
            self.process = self._launch()
        except Exception as error:
            self.process = None
            print(f"Warning: dashboard process could not start ({type(error).__name__}).")
        self.monitor_thread = threading.Thread(
            target=self._monitor, name="dashboard-process-monitor", daemon=True
        )
        self.monitor_thread.start()
        atexit.register(self.stop)
        _started.append(self)
        return self

    def _monitor(self):
        while not self.stop_event.wait(0.5):
            process = self.process
            if process is not None and process.poll() is None:
                continue
            if self.restart_count >= 3:
                return
            self.restart_count += 1
            print(
                f"Warning: dashboard exited; restarting "
                f"({self.restart_count}/3)."
            )
            if self.stop_event.wait(0.5):
                return
            try:
                restarted_process = self._launch()
            except Exception as error:
                with self.lock:
                    self.process = None
                print(
                    f"Warning: dashboard restart failed "
                    f"({type(error).__name__})."
                )
                continue
            with self.lock:
                if self.stop_event.is_set():
                    process = restarted_process
                else:
                    self.process = restarted_process
                    process = None
            if process is not None:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                except OSError:
                    pass
                return

    def stop(self):
        self.stop_event.set()
        with self.lock:
            process = self.process
            self.process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            except OSError:
                pass
        if self.monitor_thread and self.monitor_thread is not threading.current_thread():
            self.monitor_thread.join(timeout=1)


def stop_all():
    """Stops every dashboard process this program started, with the stop() their atexit hook calls"""
    for manager in list(_started):
        manager.stop()
