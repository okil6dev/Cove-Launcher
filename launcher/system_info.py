"""
Platform introspection helpers.

Contains only standard-library code so the dependency pre-flight check in
``main.py`` can rely on it before third-party packages are imported.
"""

from __future__ import annotations

import ctypes
import platform
import shutil
import subprocess
import sys

from .constants import BYTES_PER_GIB

_FALLBACK_TOTAL_RAM_BYTES: int = 8 * BYTES_PER_GIB


def get_total_ram_bytes() -> int:
    """
    Return the total physical memory of the machine in bytes.

    Tries, in order:
      * Windows: ``GlobalMemoryStatusEx`` via ctypes.
      * Linux:   ``/proc/meminfo``.
      * macOS:   ``sysctl hw.memsize``.
    Falls back to a conservative 8 GiB if detection fails.
    """
    system = platform.system()

    if system == "Windows":
        try:
            kernel32 = ctypes.windll.kernel32

            class MEMORYSTATUSEX(ctypes.Structure):
                """See Win32 MEMORYSTATUSEX documentation."""

                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except Exception:
            pass

    elif system == "Linux":
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        # Value is expressed in kB.
                        kb = int(line.split()[1])
                        return kb * 1024
        except Exception:
            pass

    elif system == "Darwin":
        try:
            raw = subprocess.check_output(["sysctl", "-n", "hw.memsize"], timeout=5)
            return int(raw.strip())
        except Exception:
            pass

    return _FALLBACK_TOTAL_RAM_BYTES


def get_total_ram_gb() -> int:
    """Return total physical memory rounded down to whole GiB (min 1)."""
    return max(1, get_total_ram_bytes() // BYTES_PER_GIB)


def find_java_executable() -> str | None:
    """
    Locate a ``java`` executable on PATH.

    Returns the absolute path, or ``None`` when no runtime is available.
    The launcher surfaces a descriptive QMessageBox in that case instead of
    letting a cryptic subprocess error escape later.
    """
    return shutil.which("java")


def is_windows() -> bool:
    """Convenience predicate used for process-tree termination logic."""
    return sys.platform.startswith("win")
