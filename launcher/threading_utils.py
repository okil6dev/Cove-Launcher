"""
Thread lifecycle helpers -- stop QThread pile-up freezing the app.

Root cause of the "locks up after time using it" freeze
--------------------------------------------------------
Several pages create a QThread per action (mod search, description fetch,
AI turn, install...) and store it as e.g. ``self._search_thread``. Starting
a new action simply *replaces* the handle:

    self._search_thread = ModSearchThread(...)   # old object orphaned
    self._search_thread.start()

The orphaned thread keeps running: it holds a socket in a blocking
``requests.get(..., timeout=20)``, and when it finishes its ``ready`` signal
is still queued onto the GUI thread. After a browsing session dozens of
stale threads are alive at once, each fighting for the GIL, sockets and the
event loop -- the whole app goes unresponsive. ``QThread: Destroyed while
thread is still running`` aborts are the same bug from the other side
(handle dropped while the C++ thread lives on).

Fix pattern (used by every page after this module landed)
---------------------------------------------------------
1. :class:`CancellableThread` -- every worker thread subclasses this and
   checks ``self.cancelled`` / ``self.generation`` between network calls,
   abandoning stale work instead of emitting into a dead UI.
2. :func:`retire` -- quit + bounded-wait + deleteLater the previous thread
   occupying a slot BEFORE a new one is stored there. ``wait()`` is called
   from the GUI thread with a short timeout; workers only ever block in
   interruptible network I/O, so this cannot deadlock the UI.
3. Generation counters -- each slot carries a monotonically increasing int;
   slots ignore ``ready``/``failed`` emissions whose generation does not
   match the current one, so a late stale result can never clobber the UI.

Qt-free (operates on duck-typed objects with quit()/wait()/isRunning()/
deleteLater()), so it unit-tests without an app instance.
"""

from __future__ import annotations


__all__ = ["CancellableThread", "retire", "is_current"]


class CancellableThread:
    """Mixin contract for worker threads (documents the expected API).

    Real workers subclass ``QThread`` AND use this protocol::

        class ModSearchThread(QThread):
            def __init__(self, *args, generation=0, **kwargs):
                super().__init__(...)
                self.generation = generation
                self._cancelled = False

            def cancel(self):
                self._cancelled = True

            @property
            def cancelled(self):
                return self._cancelled

    The helpers below only rely on duck typing, so plain test doubles work.
    """

    generation: int = 0

    def cancel(self) -> None:  # pragma: no cover - interface doc
        raise NotImplementedError

    @property
    def cancelled(self) -> bool:  # pragma: no cover - interface doc
        raise NotImplementedError


def retire(old, timeout_ms: int = 2000) -> None:
    """Stop *old* thread if it is still running, then schedule deletion.

    Safe to call with None or an already-finished thread (no-op). Never
    raises -- cleanup must not break the action that follows it.
    """
    if old is None:
        return
    try:
        cancel = getattr(old, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
        try:
            running = old.isRunning()
        except Exception:
            running = False
        if running:
            try:
                quit_fn = getattr(old, "quit", None)
                if callable(quit_fn):
                    quit_fn()
            except Exception:
                pass
            try:
                old.wait(timeout_ms)
            except Exception:
                pass
        # Only schedule deletion once the C++ thread has really exited --
        # deleteLater() on a live QThread is the "Destroyed while thread is
        # still running" abort. If the wait timed out the worker is stuck in
        # uninterruptible I/O; dropping our reference is no worse than the
        # old orphan behaviour, and far safer than force-deleting.
        try:
            if old.isRunning():
                return
        except Exception:
            return
        try:
            old.deleteLater()
        except Exception:
            pass
    except Exception:
        pass


def is_current(thread, generation: int) -> bool:
    """True when *thread*'s generation still matches the slot's counter."""
    try:
        return int(getattr(thread, "generation", generation)) == int(generation)
    except (TypeError, ValueError):
        return True
