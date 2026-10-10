"""
Verifies hang_watchdog detects a genuinely blocked GUI thread.

Run: python -u test_hang_watchdog.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

from launcher import hang_watchdog as hw  # noqa: E402


def test_beat_refreshes_the_heartbeat():
    hw.start_hang_watchdog(stale_seconds=30)
    hw.beat()
    first = hw._state["last"]
    time.sleep(0.2)
    hw.beat()
    assert hw._state["last"] > first, "beat() did not update the heartbeat"


def test_watchdog_reports_a_real_hang():
    """Freeze the GUI thread for longer than the threshold and prove it logs."""
    report = hw.REPORT_PATH
    try:
        os.remove(report)
    except OSError:
        pass

    hw.start_hang_watchdog(stale_seconds=2.0)
    hw._state["reported"] = 0.0          # re-arm

    # Stop beating, then block the GUI thread so the loop really is stuck.
    def blocker():
        hw._state["last"] = time.monotonic() - 60
        time.sleep(4)

    QTimer.singleShot(200, blocker)

    deadline = time.time() + 12
    found = False
    while time.time() < deadline:
        app = QApplication.instance()
        app.processEvents()
        time.sleep(0.05)
        if os.path.isfile(report) and os.path.getsize(report) > 200:
            found = True
            break

    assert found, "watchdog never wrote a hang report"
    with open(report, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    assert "GUI HANG DETECTED" in text
    assert "Thread" in text or "Current thread" in text
    print("  report captured:\n   " +
          "\n   ".join(text.splitlines()[:6]))


def test_no_false_positive_while_healthy():
    """Beating regularly must never produce a report."""
    report = hw.REPORT_PATH
    before = os.path.getsize(report) if os.path.isfile(report) else 0
    hw.start_hang_watchdog(stale_seconds=5.0)

    end = time.time() + 3
    while time.time() < end:
        hw.beat()
        QApplication.instance().processEvents()
        time.sleep(0.05)

    after = os.path.getsize(report) if os.path.isfile(report) else 0
    assert after == before, "reported a hang while the GUI was healthy"


def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
