import threading
import time
import win32gui
import win32process
import psutil

class ScreenOracle:
    def __init__(self, poll_interval: float = 1.5):
        self._context = ""
        self._lock = threading.Lock()
        self._running = True
        self._poll_interval = poll_interval
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="screen-oracle")
        self._thread.start()

    def _poll_loop(self):
        while self._running:
            ctx = ""
            try:
                hwnd = win32gui.GetForegroundWindow()
                if hwnd:
                    title = win32gui.GetWindowText(hwnd)
                    try:
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        if pid:
                            proc_name = psutil.Process(pid).name()
                            ctx = f"Active window: '{title}' ({proc_name})"
                        else:
                            ctx = f"Active window: '{title}'"
                    except Exception:
                        ctx = f"Active window: '{title}'"
            except Exception:
                pass

            with self._lock:
                self._context = ctx

            time.sleep(self._poll_interval)

    @property
    def context(self) -> str:
        with self._lock:
            return self._context

    def stop(self):
        self._running = False
