"""
Dialog freeze flight recorder -- catch the intermittent modal-dialog freeze.

Why this exists
---------------
The app freezes intermittently after using dialogs, and previous targeted
fixes did not eliminate it.  An intermittent, usage-dependent freeze cannot
be reproduced by short unit tests, so instead this module instruments every
dialog in the *running app* and records exactly what happened:

* every ``QDialog`` construction, ``exec()`` start and end (with duration
  and result code), and every ``done()/accept()/reject()/close()`` with the
  caller's file:line -- so a "dialog never closed" freeze shows whether
  done() was ever requested, and from where;
* a GUI-thread event spy counts Show/Hide/Close/paint/mouse/key/focus
  events per open dialog, so a report distinguishes "clicks land but
  nothing happens" from "no events at all" (dead input grab / stuck loop);
* a monitor thread watches the open-dialog records (plain Python state
  only -- it never touches Qt objects off the GUI thread) and writes a
  CRITICAL dump when it sees an impossible state:
    - done(code) was called but exec() has not returned within ~2.5 s
      (the nested event loop failed to unwind -- the classic freeze);
    - the dialog was destroyed, or hid itself without done(), while
      exec() still runs;
  The dump contains the full flight recorder, the GUI heartbeat age (tells
  a stuck nested loop apart from a fully blocked GUI thread) and a
  faulthandler dump of every thread;
* a hook into ``launcher.hang_watchdog`` appends the recent dialog events
  to any hard-hang report, so even a fully-blocked GUI thread leaves the
  dialog trail behind.

Where to look
-------------
``%APPDATA%/Cove/dialog-debug.log`` -- the flight recorder (rotated to
``.old`` once at startup when oversized).  Critical dumps are mirrored to
``%APPDATA%/Cove/hang-report.txt`` and to stderr.

Disable with ``COVE_DIALOG_DEBUG=0``.  Thresholds are the constants below.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback
from collections import deque

LOG_PATH = os.path.join(
    os.environ.get("APPDATA", os.getcwd()), "Cove", "dialog-debug.log"
)
HANG_REPORT_PATH = os.path.join(
    os.environ.get("APPDATA", os.getcwd()), "Cove", "hang-report.txt"
)

LOG_MAX_BYTES = 1_500_000          # rotate once at install
_RING_SIZE = 400                   # in-memory flight recorder length
_STUCK_DONE_SECONDS = 2.5          # done() called, exec() must not linger
_STUCK_HIDDEN_SECONDS = 2.5        # hidden without done() while exec runs
_LONG_OPEN_SECONDS = 60.0          # visible dialog open very long -> info
_LONG_OPEN_REPEAT = 30.0           # ... re-logged at this interval
_MONITOR_TICK = 1.0

_state = {
    "installed": False,
    "main_ident": None,
    "seq": 0,
    "active": [],        # exec records currently inside exec() (newest last)
    "started": time.monotonic(),
}
_lock = threading.Lock()
_ring: deque[str] = deque(maxlen=_RING_SIZE)

# QEvent.Type -> short name; filled in by install().  Kept small on purpose.
_EVENT_NAMES: dict[int, str] = {}


def _fmt_rel(t: float) -> str:
    return f"+{t - _state['started']:.3f}s"


def _write(text: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def _log(tag: str, message: str) -> None:
    """Append one flight-recorder line (thread-safe, never raises)."""
    try:
        line = f"{time.strftime('%H:%M:%S')} {_fmt_rel(time.monotonic())} [{tag}] {message}"
        with _lock:
            _ring.append(line)
        _write(line + "\n")
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        pass


def _caller(max_frames: int = 3) -> str:
    """Compact 'file:line:func' for the frames that initiated this call."""
    try:
        frames = traceback.extract_stack()[:-2]  # drop this + immediate wrapper
        if len(frames) > max_frames:
            frames = frames[-max_frames:]
        return " > ".join(
            f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in frames
        )
    except Exception:  # noqa: BLE001
        return "<caller unavailable>"


def _parent_chain(dlg) -> str:
    parts: list[str] = []
    try:
        p = dlg.parent()
        depth = 0
        while p is not None and depth < 6:
            parts.append(type(p).__name__)
            p = p.parent()
            depth += 1
    except RuntimeError:
        pass
    return "->".join(parts) if parts else "<none>"


def _find_rec(dlg) -> dict | None:
    """Innermost active exec record for *dlg*, if any."""
    with _lock:
        for rec in reversed(_state["active"]):
            if rec.get("obj") is dlg:
                return rec
    return None


def _fmt_counts(rec: dict) -> str:
    parts = [f"{k}={v}" for k, v in rec["ev"].items() if v]
    return " ".join(parts) if parts else "<no events recorded>"


def _gui_snapshot(rec: dict, dlg) -> None:
    """Record GUI-side state into *rec*.  MUST run on the GUI thread only."""
    try:
        rec["visible"] = bool(dlg.isVisible())
        bits = [f"visible={dlg.isVisible()}", f"enabled={dlg.isEnabled()}"]
        try:
            eff = dlg.graphicsEffect()
            if eff is not None:
                bits.append(f"effect={type(eff).__name__}")
        except Exception:  # noqa: BLE001
            pass
        try:
            from PyQt6.QtCore import QAbstractAnimation

            running = [
                f"{type(a).__name__}(obj={a.objectName() or '-'})"
                for a in dlg.findChildren(QAbstractAnimation)
                if a.state() == QAbstractAnimation.State.Running
            ]
            if running:
                bits.append("running_animations=" + ",".join(running))
        except Exception:  # noqa: BLE001
            pass
        rec["desc"] = " ".join(bits)
    except RuntimeError:
        pass  # C++ object already gone
    except Exception:  # noqa: BLE001
        pass


def _heartbeat_desc() -> str:
    try:
        from . import hang_watchdog as hw

        age = hw.seconds_since_beat()
        if age < 3.0:
            state = "fresh (event loop running)"
        else:
            state = f"STALE for {age:.0f}s (GUI event loop fully blocked)"
        return f"{state}; last beat {age:.1f}s ago"
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({exc!r})"


def _critical(reason: str, rec: dict | None) -> None:
    """Dump flight recorder + all thread stacks for a stuck dialog."""
    rule = "=" * 72
    lines = [
        "",
        rule,
        f"DIALOG STUCK: {reason}",
        f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"GUI heartbeat: {_heartbeat_desc()}",
    ]
    if rec is not None:
        lines += [
            (
                f"record: exec#{rec['seq']} {rec['cls']} "
                f"parent={rec['parent']} opened {_fmt_rel(rec['opened'])} "
                f"depth={rec['depth']} thread={rec['thread']}"
            ),
            f"state: {rec.get('desc', '<no snapshot>')} visible_flag={rec['visible']}",
            (
                f"markers: done_at={_fmt_rel(rec['done_at']) if rec['done_at'] else None} "
                f"code={rec.get('done_code')} "
                f"hidden_at={_fmt_rel(rec['hidden_at']) if rec['hidden_at'] else None} "
                f"destroyed={rec['destroyed_at'] is not None} "
                f"minimized={rec.get('minimized', False)}"
            ),
            f"done requested by: {rec.get('done_caller', '<never>')}",
            f"event counts: {_fmt_counts(rec)} "
            f"last_event={rec['last_ev'] or '-'} at {_fmt_rel(rec['last_ev_t']) if rec['last_ev_t'] else '-'}",
        ]
    with _lock:
        tail = list(_ring)
    lines += [
        rule,
        f"FLIGHT RECORDER (last {len(tail)} events, newest last):",
        *(f"  {line}" for line in tail),
        rule,
        "ALL THREADS (faulthandler):",
    ]
    text = "\n".join(lines) + "\n"

    _write(text)
    try:
        os.makedirs(os.path.dirname(HANG_REPORT_PATH), exist_ok=True)
        with open(HANG_REPORT_PATH, "a", encoding="utf-8") as fh:
            fh.write(text)
            faulthandler.dump_traceback(file=fh, all_threads=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.stderr.write(text)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    except Exception:  # noqa: BLE001
        pass


# --- GUI-thread event spy ----------------------------------------------------
class _DialogEventSpy:
    """Application-level event filter counting events per open dialog.

    Installed only while at least one modal exec() is running, so the cost
    is bounded to dialog lifetimes.  All callbacks run on the GUI thread;
    they only mutate plain-Python record fields read by the monitor thread.
    """

    def eventFilter(self, obj, ev) -> bool:
        try:
            with _lock:
                actives = list(_state["active"])
            if not actives:
                return False
            name = _EVENT_NAMES.get(int(ev.type()))
            if name is None:
                return False
            now = time.monotonic()
            for rec in actives:
                dlg = rec.get("obj")
                if dlg is None:
                    continue
                try:
                    if obj is not dlg and not dlg.isAncestorOf(obj):
                        continue
                except RuntimeError:
                    continue
                if name == "Paint":
                    rec["ev"]["Paint"] = rec["ev"].get("Paint", 0) + 1
                    continue
                rec["ev"][name] = rec["ev"].get(name, 0) + 1
                rec["last_ev"] = name
                rec["last_ev_t"] = now
                if name == "Show":
                    rec["hidden_at"] = None
                    rec["minimized"] = False
                    rec["visible"] = True
                elif name == "Hide" and obj is dlg and rec["done_at"] is None:
                    try:
                        minimized = bool(dlg.isMinimized())
                    except RuntimeError:
                        minimized = False
                    rec["minimized"] = minimized
                    if not minimized:
                        rec["hidden_at"] = now
        except Exception:  # noqa: BLE001
            pass
        return False


_spy = _DialogEventSpy()
_spy_obj = None  # QObject wrapper, created in install()


def _ensure_app_filter(attach: bool) -> None:
    """Attach/detach the spy as an application-level event filter."""
    try:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return
        if attach:
            if _spy_obj is not None:
                app.installEventFilter(_spy_obj)
        else:
            if _spy_obj is not None:
                app.removeEventFilter(_spy_obj)
    except Exception:  # noqa: BLE001
        pass


# --- monitor thread ----------------------------------------------------------
def _monitor_tick() -> None:
    now = time.monotonic()
    with _lock:
        actives = list(_state["active"])
    for rec in actives:
        if rec.get("reported"):
            continue
        age = now - rec["opened"]
        if rec["done_at"] is not None and now - rec["done_at"] > _STUCK_DONE_SECONDS:
            rec["reported"] = True
            _critical(
                f"done({rec['done_code']}) was called "
                f"{now - rec['done_at']:.1f}s ago but exec() never returned "
                f"(nested event loop stuck)",
                rec,
            )
        elif (
            rec["destroyed_at"] is not None
            and now - rec["destroyed_at"] > _STUCK_DONE_SECONDS
        ):
            rec["reported"] = True
            _critical(
                f"dialog was destroyed {now - rec['destroyed_at']:.1f}s ago "
                f"while exec() is still running",
                rec,
            )
        elif (
            rec["hidden_at"] is not None
            and rec["done_at"] is None
            and now - rec["hidden_at"] > _STUCK_HIDDEN_SECONDS
        ):
            rec["reported"] = True
            _critical(
                f"dialog hid itself {now - rec['hidden_at']:.1f}s ago without "
                f"calling done() while exec() is still running",
                rec,
            )
        elif (
            rec["visible"]
            and age > _LONG_OPEN_SECONDS
            and now - rec.get("long_logged", rec["opened"]) > _LONG_OPEN_REPEAT
        ):
            rec["long_logged"] = now
            _log(
                f"EXEC#{rec['seq']}",
                f"LONG-OPEN {rec['cls']} open {age:.0f}s; "
                f"events: {_fmt_counts(rec)}; last={rec['last_ev'] or '-'}; "
                f"state: {rec.get('desc', '<no snapshot>')}",
            )


def _monitor() -> None:
    while True:
        time.sleep(_MONITOR_TICK)
        try:
            _monitor_tick()
        except Exception:  # noqa: BLE001
            pass


# --- QDialog wrappers (all installed together by install()) ------------------
_ORIG: dict = {}


def _new_rec(dlg) -> dict:
    with _lock:
        _state["seq"] += 1
        seq = _state["seq"]
        depth = len(_state["active"])
    return {
        "seq": seq,
        "obj": dlg,  # strong ref; dropped when exec() returns
        "cls": type(dlg).__name__,
        "parent": _parent_chain(dlg),
        "opened": time.monotonic(),
        "depth": depth,
        "thread": threading.current_thread().name,
        "done_at": None,
        "done_code": None,
        "done_caller": None,
        "hidden_at": None,
        "destroyed_at": None,
        "minimized": False,
        "visible": False,
        "ev": {},
        "last_ev": "",
        "last_ev_t": 0.0,
        "desc": "<no snapshot>",
        "reported": False,
    }


def _finish_rec(rec: dict) -> None:
    with _lock:
        try:
            _state["active"].remove(rec)
        except ValueError:
            pass
        still_active = bool(_state["active"])
    rec["obj"] = None
    if not still_active:
        _ensure_app_filter(False)


def _wrap_init(orig):
    def _init(self, *args, **kwargs):
        result = orig(self, *args, **kwargs)
        try:
            _log(
                "CREATE",
                f"{type(self).__name__} parent={_parent_chain(self)} "
                f"thread={threading.current_thread().name}",
            )
        except Exception:  # noqa: BLE001
            pass
        return result

    return _init


def _wrap_exec(orig):
    def _exec(self):
        if threading.get_ident() != _state["main_ident"]:
            _critical(
                f"exec() called on NON-GUI thread {threading.current_thread().name!r} "
                f"for {type(self).__name__} -- modal dialogs must run on the GUI "
                f"thread; caller: {_caller()}",
                None,
            )
        existing = _find_rec(self)
        if existing is not None:
            _critical(
                f"re-entrant exec() on {type(self).__name__} "
                f"(exec#{existing['seq']} is already running for it)",
                existing,
            )
        rec = _new_rec(self)
        with _lock:
            _state["active"].append(rec)
        _ensure_app_filter(True)

        def _on_destroyed(*_args, _rec=rec):
            _rec["destroyed_at"] = time.monotonic()

        try:
            self.destroyed.connect(_on_destroyed)
        except Exception:  # noqa: BLE001
            pass

        _log(
            f"EXEC#{rec['seq']}",
            f"OPEN {rec['cls']} parent={rec['parent']} depth={rec['depth']} "
            f"thread={rec['thread']} caller={_caller()}",
        )
        try:
            from PyQt6.QtCore import QTimer

            QTimer.singleShot(0, lambda _r=rec, _d=self: _gui_snapshot(_r, _d))
        except Exception:  # noqa: BLE001
            pass

        try:
            result = orig(self)
        except BaseException as exc:  # noqa: BLE001
            _log(
                f"EXEC#{rec['seq']}",
                f"EXCEPTION from {rec['cls']}.exec(): {type(exc).__name__}: {exc}",
            )
            _finish_rec(rec)
            raise
        _log(
            f"EXEC#{rec['seq']}",
            f"CLOSE {rec['cls']} result={result} "
            f"elapsed={time.monotonic() - rec['opened']:.2f}s "
            f"done_seen={rec['done_at'] is not None} "
            f"events: {_fmt_counts(rec)}",
        )
        _finish_rec(rec)
        return result

    return _exec



    parts: list[str] = []
    try:
        p = dlg.parent()
        depth = 0
        while p is not None and depth < 6:
            parts.append(type(p).__name__)
            p = p.parent()
            depth += 1
    except RuntimeError:
        pass
    return "->".join(parts) if parts else "<none>"



def _wrap_done(orig):
    def _done(self, code):
        rec = _find_rec(self)
        caller = _caller()
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = -1
        if rec is not None:
            # Mark BEFORE delegating: if the real done() itself hangs, the
            # monitor still sees "done requested, exec() never returned".
            rec["done_at"] = time.monotonic()
            rec["done_code"] = code_int
            rec["done_caller"] = caller
            _gui_snapshot(rec, self)
            _log(
                f"EXEC#{rec['seq']}",
                f"done({code_int}) requested by {caller} "
                f"on thread={threading.current_thread().name!r} "
                f"(exec open {time.monotonic() - rec['opened']:.2f}s)",
            )
        else:
            _log(
                "DIALOG",
                f"{type(self).__name__}.done({code_int}) OUTSIDE exec() "
                f"by {caller} thread={threading.current_thread().name!r}",
            )
        return orig(self, code)

    return _done


def _wrap_simple(orig, verb: str, done_code: int | None = None):
    def _fn(self):
        rec = _find_rec(self)
        where = f"exec#{rec['seq']}" if rec is not None else "outside exec"
        caller = _caller()
        # accept()/reject() are C++ methods that call done() internally in
        # C++ -- that virtual call never reaches the Python done() patch, so
        # mark the record here or the "done but exec stuck" detector would
        # not see this path.
        if rec is not None and done_code is not None:
            rec["done_at"] = time.monotonic()
            rec["done_code"] = done_code
            rec["done_caller"] = caller
        _log(
            "DIALOG",
            f"{type(self).__name__}.{verb}() by {caller} ({where})",
        )
        return orig(self)

    return _fn


def _hang_hook(reason: str) -> str:
    """hang_watchdog callback: recent dialog events for hard-hang reports."""
    with _lock:
        tail = list(_ring)[-60:]
    if not tail:
        return "RECENT DIALOG EVENTS: <none recorded>\n"
    return (
        f"RECENT DIALOG EVENTS (last {len(tail)}, newest last):\n"
        + "\n".join(f"  {line}" for line in tail)
        + "\n"
    )


def install_dialog_debug() -> bool:
    """Instrument every QDialog in the process.  Idempotent; never raises.

    Returns True when the instrumentation was installed by this call.
    """
    if os.environ.get("COVE_DIALOG_DEBUG", "1").strip().lower() in ("0", "false", "off"):
        return False
    if _state["installed"]:
        return False

    from PyQt6.QtCore import QEvent, QObject
    from PyQt6.QtWidgets import QDialog

    _EVENT_NAMES.update(
        {
            int(QEvent.Type.Show): "Show",
            int(QEvent.Type.Hide): "Hide",
            int(QEvent.Type.Close): "Close",
            int(QEvent.Type.Paint): "Paint",
            int(QEvent.Type.MouseButtonPress): "MousePress",
            int(QEvent.Type.MouseButtonRelease): "MouseRelease",
            int(QEvent.Type.KeyPress): "KeyPress",
            int(QEvent.Type.FocusIn): "FocusIn",
            int(QEvent.Type.Move): "Move",
            int(QEvent.Type.Resize): "Resize",
            int(QEvent.Type.WindowActivate): "WinActivate",
        }
    )

    global _spy_obj
    _spy_obj = QObject()  # thin wrapper so Qt accepts the filter
    _spy_obj.eventFilter = _spy.eventFilter  # type: ignore[method-assign]

    _ORIG.update(
        init=QDialog.__init__,
        exec=QDialog.exec,
        done=QDialog.done,
        accept=QDialog.accept,
        reject=QDialog.reject,
        close=QDialog.close,
    )
    QDialog.__init__ = _wrap_init(_ORIG["init"])          # type: ignore[assignment]
    QDialog.exec = _wrap_exec(_ORIG["exec"])              # type: ignore[assignment]
    QDialog.done = _wrap_done(_ORIG["done"])              # type: ignore[assignment]
    QDialog.accept = _wrap_simple(_ORIG["accept"], "accept", done_code=1)  # type: ignore[assignment]
    QDialog.reject = _wrap_simple(_ORIG["reject"], "reject", done_code=0)  # type: ignore[assignment]
    QDialog.close = _wrap_simple(_ORIG["close"], "close")      # type: ignore[assignment]

    try:
        _write(
            f"\n{'=' * 72}\n"
            f"dialog-debug installed {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"pid={os.getpid()} log={LOG_PATH}\n{'=' * 72}\n"
        )
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".old")
    except OSError:
        pass

    _state["main_ident"] = threading.get_ident()
    _state["installed"] = True

    try:
        from . import hang_watchdog as _hw

        _hw.add_dump_hook(_hang_hook)
    except Exception:  # noqa: BLE001
        pass

    threading.Thread(
        target=_monitor, daemon=True, name="dialog-debug-monitor"
    ).start()
    _log("SYSTEM", "dialog-debug installed; monitoring modal dialogs")
    return True
