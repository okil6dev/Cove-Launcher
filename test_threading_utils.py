"""
Regression tests for launcher.threading_utils -- the retire()/cancel protocol
that stops QThread pile-up from freezing the GUI.

Run: python -u test_threading_utils.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QThread  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

from launcher.threading_utils import retire, is_current  # noqa: E402


class _FakeThread:
    """Duck-typed double: records cancel/quit/wait/deleteLater calls."""

    def __init__(self, running: bool = True) -> None:
        self._running = running
        self.cancelled = False
        self.quit_called = 0
        self.wait_calls: list[int] = []
        self.deleted = False
        self.generation = 0

    def cancel(self) -> None:
        self.cancelled = True

    def isRunning(self) -> bool:
        return self._running

    def quit(self) -> None:
        self.quit_called += 1

    def wait(self, timeout_ms: int = 0) -> bool:
        self.wait_calls.append(timeout_ms)
        self._running = False      # a well-behaved worker exits after quit
        return True

    def deleteLater(self) -> None:
        self.deleted = True


class _StubbornThread(QThread):
    """Ignores quit(); only cancel() unblocks it -- proves the wait path."""

    def __init__(self) -> None:
        super().__init__()
        self._cancelled = False
        self._done = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        deadline = time.time() + 15          # far longer than retire's timeout
        while time.time() < deadline and not self._cancelled:
            time.sleep(0.01)
        self._done = True


def test_retire_none_is_noop():
    """retire(None) must never raise -- cleanup can't break the next action."""
    retire(None)


def test_retire_stops_running_thread_and_deletes_it():
    fake = _FakeThread(running=True)
    retire(fake)
    assert fake.cancelled, "retire did not cancel the worker"
    assert fake.quit_called == 1, "retire did not ask the thread to quit"
    assert fake.wait_calls, "retire did not wait for the thread to exit"
    assert fake.deleted, "retire did not deleteLater the finished thread"


def test_retire_skips_finished_thread_but_still_deletes():
    fake = _FakeThread(running=False)
    retire(fake)
    assert not fake.wait_calls, "retire waited on an already-finished thread"
    assert fake.deleted, "retire did not deleteLater a finished thread"


def test_retire_never_deletes_a_live_thread():
    """deleteLater() on a running QThread is the 'Destroyed while thread is
    still running' abort -- retire must refuse to schedule it."""
    fake = _FakeThread(running=True)
    fake.wait = lambda timeout_ms=0: False   # wait times out: still running
    retire(fake, timeout_ms=10)
    assert fake.cancelled, "cancel not requested"
    assert not fake.deleted, \
        "retire deleteLater'd a thread that never exited (abort risk)"


def test_retire_survives_a_broken_worker():
    class _Exploding:
        def cancel(self):
            raise RuntimeError("boom")

        def isRunning(self):
            raise RuntimeError("boom")

        def deleteLater(self):
            raise RuntimeError("boom")

    retire(_Exploding())      # must not raise


def test_retire_actually_reaps_a_real_qthread():
    """End-to-end with a real QThread that only stops when cancelled."""
    t = _StubbornThread()
    t.start()
    time.sleep(0.05)
    assert t.isRunning()
    t0 = time.perf_counter()
    retire(t, timeout_ms=1000)
    assert not t.isRunning(), "retire() did not stop a cancellable QThread"
    assert time.perf_counter() - t0 < 5, "retire() blocked far too long"


def test_is_current_matches_generation():
    class _T:
        generation = 3

    assert is_current(_T(), 3)
    assert not is_current(_T(), 4)
    # A thread without a generation attribute defaults to "current".
    assert is_current(object(), 7)


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
