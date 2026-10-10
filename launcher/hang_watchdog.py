"""
Hang diagnostics: dump every thread's stack when the GUI thread stops.

Why this exists
---------------
A frozen GUI means the main thread is blocked inside something that never
returns. Guessing at candidates has been unreliable, so instead we measure:
a plain Python thread watches a heartbeat that a Qt timer on the GUI thread
refreshes. If the heartbeat goes stale, the main thread is not running the
event loop, and we dump the stack of *every* thread.

The dump names the exact function the GUI thread is stuck in -- whether that
is ours or a library's -- which is the only reliable way to find a freeze.

Usage
-----
    from launcher.hang_watchdog import start_hang_watchdog
    start_hang_watchdog()

Writes to stderr (so it is visible in the console running ``python main.py``)
and appends to ``%APPDATA%/Cove/hang-report.txt``.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time

DEFAULT_STALE_SECONDS = 8.0
HEARTBEAT_PATH = os.path.join(
    os.environ.get("APPDATA", os.getcwd()), "Cove", "gui-heartbeat.txt"
)
REPORT_PATH = os.path.join(
    os.environ.get("APPDATA", os.getcwd()), "Cove", "hang-report.txt"
)

_state = {"last": time.monotonic(), "reported": 0.0, "enabled": False}
_lock = threading.Lock()

# Callbacks (fn(reason: str) -> str) whose returned text is appended to every
# hang report.  launcher.dialog_debug uses this to attach the dialog flight
# recorder to hard-hang reports.  Never let a broken hook kill the watchdog.
_dump_hooks: list = []


def add_dump_hook(fn) -> None:
    """Register fn(reason) -> str; its text is appended to hang reports."""
    try:
        _dump_hooks.append(fn)
    except Exception:  # noqa: BLE001
        pass


def seconds_since_beat() -> float:
    """Seconds since the GUI thread last refreshed the heartbeat."""
    with _lock:
        return time.monotonic() - _state["last"]


def beat() -> None:
    """Called from the GUI thread (by a QTimer) to prove the loop is alive."""
    with _lock:
        _state["last"] = time.monotonic()


def _write_heartbeat_file() -> None:
    """Mirror the heartbeat to disk so a hard crash leaves evidence behind."""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f}\n")
    except OSError:
        pass


def _report(reason: str) -> None:
    """Dump all thread stacks to stderr and to the report file."""
    rule = "=" * 72
    banner = (
        "\n"
        + rule
        + f"\nGUI HANG DETECTED ({reason})\n"
        + f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        + rule
        + "\n"
    )
    sys.stderr.write(banner)
    sys.stderr.flush()

    # Extra context from registered hooks (e.g. the dialog flight recorder).
    extras = []
    for hook in list(_dump_hooks):
        try:
            text = hook(reason)
        except Exception:  # noqa: BLE001 - hooks must never break the report
            continue
        if text:
            extras.append(str(text))
    extra_text = "".join(extras)

    try:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "a", encoding="utf-8") as fh:
            fh.write(banner)
            if extra_text:
                fh.write(extra_text)
            # all_threads=True is the whole point: we need the GUI thread.
            faulthandler.dump_traceback(file=fh, all_threads=True)
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        pass

    try:
        if extra_text:
            sys.stderr.write(extra_text)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    except Exception:  # noqa: BLE001
        pass


def _watch(stale_seconds: float) -> None:
    while True:
        time.sleep(1.0)
        with _lock:
            idle = time.monotonic() - _state["last"]
            already = time.monotonic() - _state["reported"]
        if idle < stale_seconds:
            continue
        # Re-arm only after a long quiet period so one hang logs once.
        if already < 60:
            continue
        with _lock:
            _state["reported"] = time.monotonic()
        _report(f"GUI thread unresponsive for {idle:.0f}s")


def start_hang_watchdog(stale_seconds: float = DEFAULT_STALE_SECONDS) -> bool:
    """Start the watcher. Returns True if it was started by this call."""
    with _lock:
        if _state["enabled"]:
            return False
        _state["enabled"] = True
        _state["last"] = time.monotonic()

    try:
        faulthandler.enable()
    except Exception:  # noqa: BLE001
        pass

    threading.Thread(target=_watch, args=(stale_seconds,),
                     daemon=True, name="gui-hang-watchdog").start()
    return True
