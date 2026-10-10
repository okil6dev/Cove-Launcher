"""
Run every test suite and summarise, one line each.

Usage: python run_tests.py

Each suite runs as its own subprocess: several of them monkeypatch module
globals (e.g. NewInstanceDialog.exec), and running them in-process would let
one suite leak into the next.
"""

from __future__ import annotations

import os
import subprocess
import sys

SUITES = (
    "test_ai_client",
    "test_ai_gui",
    "test_ai_pipeline",
    "test_external_mods",
    "test_server_mods",
    "test_freeze_fixes",
    "test_dialog_freeze",
    "test_hang_watchdog",
    "test_threading_utils",
    "test_window_sizes",
)


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    failed: list[str] = []
    for name in SUITES:
        script = os.path.join(here, name + ".py")
        if not os.path.exists(script):
            continue
        proc = subprocess.run([sys.executable, "-u", script], cwd=here,
                              capture_output=True, text=True, errors="replace")
        lines = [ln for ln in proc.stdout.splitlines()
                 if "passed" in ln or ln.startswith("FAIL")]
        summary = lines[-1] if lines else "(no summary)"
        ok = proc.returncode == 0
        if not ok:
            failed.append(name)
        print(f"{'OK  ' if ok else 'FAIL'} {name:<20} {summary}")
        if not ok:
            for ln in lines:
                if ln.startswith("FAIL"):
                    print(f"        {ln}")
    print()
    print("ALL SUITES PASSED" if not failed
          else f"FAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())