"""Double-click launcher for the dashboard: starts `python -m shorts dashboard` in the background
(no console window) unless it is already running, then opens it in the browser.
Output goes to launcher/dashboard.log. Stop it from Task Manager (pythonw.exe) or by logging off."""
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

PORT = 5000
URL = f"http://127.0.0.1:{PORT}"
REPO = Path(__file__).resolve().parent.parent
LOG = Path(__file__).resolve().parent / "dashboard.log"


def running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def start() -> None:
    pythonw = REPO / ".venv" / "Scripts" / "pythonw.exe"
    exe = str(pythonw if pythonw.exists() else sys.executable)
    out = open(LOG, "a", encoding="utf-8")
    out.write(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    out.flush()
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen([exe, "-u", "-m", "shorts", "dashboard", "--port", str(PORT)], cwd=REPO,
                     stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags)


def main() -> None:
    if not running():
        start()
        for _ in range(60):  # up to ~30 s for the first start
            if running():
                break
            time.sleep(0.5)
    if "--no-browser" not in sys.argv:
        webbrowser.open(URL)


if __name__ == "__main__":
    main()
