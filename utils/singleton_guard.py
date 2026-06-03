"""Boot-time singleton guard.

Refuses to start a second OpenAlgo instance, which would put two concurrent
writers on the SQLite databases and corrupt them (``bad parameter or other API
misuse`` / DB-locked errors). Two independent checks:

  1. TCP port 5000 already bound — another web server is already up.
  2. Another live process holds ``db/openalgo.db`` open (best-effort, via
     psutil; skipped gracefully if psutil is unavailable).

On a hit we print a clear message to stderr and ``sys.exit(1)``. This runs
before any Flask init, so it deliberately uses ``print`` rather than the
logging stack (which may not be configured yet).
"""

import os
import socket
import sys

# Repo root is the parent of this utils/ directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.path.abspath(os.path.join(_REPO_ROOT, "db", "openalgo.db"))
_PORT = 5000


def check_singleton_or_abort() -> None:
    """Abort the process if another OpenAlgo instance is already running."""
    # 1. Port-bind probe. If the bind raises, something already owns :5000.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", _PORT))
    except OSError:
        print(
            f"OpenAlgo already running on :{_PORT} — refusing to boot to prevent "
            "concurrent-writer DB corruption. Stop the other instance first.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    finally:
        probe.close()

    # 2. Open-handle probe on db/openalgo.db. Best-effort: needs psutil and
    #    per-process file access, both of which can be unavailable.
    try:
        import psutil
    except ImportError:
        return

    my_pid = os.getpid()
    for proc in psutil.process_iter(["pid"]):
        pid = proc.info.get("pid")
        if pid == my_pid:
            continue
        try:
            for handle in proc.open_files():
                if os.path.abspath(handle.path) == _DB_PATH:
                    print(
                        f"OpenAlgo already running (pid {pid}) holding "
                        "db/openalgo.db — refusing to boot to prevent "
                        "concurrent-writer DB corruption. Stop the other "
                        "instance first.",
                        file=sys.stderr,
                        flush=True,
                    )
                    sys.exit(1)
        except (psutil.Error, OSError):
            # AccessDenied / NoSuchProcess / gone — skip and keep scanning.
            continue
