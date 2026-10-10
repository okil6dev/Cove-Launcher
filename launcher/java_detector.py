"""
Java installation detector for Cove Launcher.

Mimics the behaviour of PrismLauncher's Java detection:
  1. Registry scan  (HKLM + HKCU, JDK + JRE keys, OpenJDK variants)
  2. Common install path scan  (Program Files, AdoptOpenJDK, Eclipse, GraalVM, etc.)
  3. PATH scan  (whichever ``java.exe`` the shell would pick)
  4. Launcher-managed JDKs  (inside APP_DATA_DIR/jdks/)

Every candidate is run with ``-version`` to confirm it works and to read
the real version string.  Results are deduplicated by resolved path.
"""

from __future__ import annotations

import os
import subprocess
import shutil
from pathlib import Path
from typing import NamedTuple

from .java_downloader import JDKS_DIR


class JavaInstall(NamedTuple):
    """A validated Java installation."""
    path: str       # absolute path to java.exe
    version: str    # e.g. "21.0.3" or full first version line
    vendor: str     # e.g. "Eclipse Temurin", "GraalVM CE", "Microsoft", ...
    source: str     # "registry" | "common_path" | "PATH" | "launcher"


# ── vendor name heuristics ─────────────────────────────────────────────────────

def _guess_vendor(path: str, version_output: str) -> str:
    p = path.lower()
    v = version_output.lower()
    if "graalvm" in p or "graalvm" in v:
        return "GraalVM CE"
    if "temurin" in p or "adoptium" in p or "eclipse" in p:
        return "Eclipse Temurin"
    if "microsoft" in p or "microsoft" in v:
        return "Microsoft OpenJDK"
    if "semeru" in p or "semeru" in v:
        return "IBM Semeru"
    if "zulu" in p or "zulu" in v:
        return "Azul Zulu"
    if "corretto" in p or "corretto" in v:
        return "Amazon Corretto"
    if "liberica" in p or "liberica" in v:
        return "BellSoft Liberica"
    if "dragonwell" in p or "dragonwell" in v:
        return "Alibaba Dragonwell"
    if "openjdk" in v:
        return "OpenJDK"
    if "jdk" in p or "java" in p:
        return "JDK"
    return "Unknown"


# ── probe a single java.exe ────────────────────────────────────────────────────

def _probe(path: str, source: str) -> JavaInstall | None:
    """Return a JavaInstall if *path* is a valid java executable, else None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        result = subprocess.run(
            [path, "-version"],
            capture_output=True, text=True, timeout=8,
        )
        output = (result.stderr or "") + (result.stdout or "")
        if "version" not in output.lower():
            return None
        first_line = output.strip().splitlines()[0] if output.strip() else ""
        # Extract just the version number from e.g. 'openjdk version "21.0.3" 2024-04-16'
        import re
        m = re.search(r'"([^"]+)"', first_line)
        version = m.group(1) if m else first_line[:80]
        vendor = _guess_vendor(path, output)
        return JavaInstall(path=os.path.normpath(path),
                           version=version, vendor=vendor, source=source)
    except Exception:
        return None


# ── 1. registry scan (Windows only) ───────────────────────────────────────────

def _scan_registry() -> list[str]:
    """Return candidate java.exe paths from the Windows registry."""
    candidates: list[str] = []
    if os.name != "nt":
        return candidates
    try:
        import winreg
        registry_keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\JDK"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\Java Development Kit"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\JRE"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\Java Runtime Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Eclipse Adoptium\JDK"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Eclipse Foundation\JDK"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\JDK"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GraalVM"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\JavaSoft\JDK"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\JavaSoft\Java Development Kit"),
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\JavaSoft\JDK"),
        ]
        for hive, key_path in registry_keys:
            try:
                with winreg.OpenKey(hive, key_path) as root:
                    i = 0
                    while True:
                        try:
                            sub_name = winreg.EnumKey(root, i)
                            i += 1
                            with winreg.OpenKey(root, sub_name) as sub:
                                try:
                                    home, _ = winreg.QueryValueEx(sub, "JavaHome")
                                    exe = os.path.join(str(home), "bin", "java.exe")
                                    candidates.append(exe)
                                except FileNotFoundError:
                                    pass
                        except OSError:
                            break
            except OSError:
                continue
    except ImportError:
        pass
    return candidates


# ── 2. common path scan ────────────────────────────────────────────────────────

def _scan_common_paths() -> list[str]:
    """Walk well-known Windows JDK directories for java.exe."""
    candidates: list[str] = []
    if os.name != "nt":
        return candidates

    pf = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    vendor_dirs = [
        "Java", "Eclipse Adoptium", "Eclipse Foundation",
        "AdoptOpenJDK", "Temurin", "GraalVM",
        "Microsoft", "Zulu", "BellSoft", "Amazon Corretto",
        "Semeru", "Liberica", "Dragonwell",
    ]

    for base in pf:
        if not base:
            continue
        for vendor in vendor_dirs:
            vendor_path = os.path.join(base, vendor)
            if not os.path.isdir(vendor_path):
                continue
            for entry in os.scandir(vendor_path):
                if entry.is_dir():
                    exe = os.path.join(entry.path, "bin", "java.exe")
                    candidates.append(exe)

    # Also try C:\Program Files\Java\jdk-* and similar flat structures
    for base in (r"C:\Program Files\Java", r"C:\Program Files (x86)\Java"):
        if os.path.isdir(base):
            for entry in os.scandir(base):
                if entry.is_dir():
                    candidates.append(os.path.join(entry.path, "bin", "java.exe"))

    return candidates


# ── 3. PATH scan ───────────────────────────────────────────────────────────────

def _scan_path() -> list[str]:
    found = shutil.which("java")
    return [found] if found else []


# ── 4. launcher-managed JDKs ──────────────────────────────────────────────────

def _scan_launcher() -> list[str]:
    candidates: list[str] = []
    if not os.path.isdir(JDKS_DIR):
        return candidates
    for entry in os.scandir(JDKS_DIR):
        if entry.is_dir():
            exe = os.path.join(entry.path, "bin", "java.exe")
            candidates.append(exe)
    return candidates


# ── public API ────────────────────────────────────────────────────────────────

def detect_java_installations(
    progress_cb=None,      # optional callable(done: int, total: int)
) -> list[JavaInstall]:
    """
    Scan the system for Java installations and return a deduplicated,
    validated list sorted newest-version-first.

    Parameters
    ----------
    progress_cb
        Optional callable invoked as ``progress_cb(done, total)`` during
        the (potentially slow) probe phase.
    """
    # Gather all raw candidates (may include duplicates / non-existent paths)
    raw: list[tuple[str, str]] = []  # (path, source)

    launcher_paths = _scan_launcher()
    for p in launcher_paths:
        raw.append((p, "launcher"))

    path_paths = _scan_path()
    for p in path_paths:
        raw.append((p, "PATH"))

    registry_paths = _scan_registry()
    for p in registry_paths:
        raw.append((p, "registry"))

    common_paths = _scan_common_paths()
    for p in common_paths:
        raw.append((p, "common_path"))

    # Deduplicate by normalised path
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for path, source in raw:
        norm = os.path.normpath(path).lower()
        if norm not in seen:
            seen.add(norm)
            unique.append((path, source))

    # Probe each candidate
    results: list[JavaInstall] = []
    total = len(unique)
    for i, (path, source) in enumerate(unique):
        if progress_cb:
            progress_cb(i, total)
        install = _probe(path, source)
        if install:
            results.append(install)
    if progress_cb:
        progress_cb(total, total)

    # Sort: launcher-managed first, then by major version descending
    def _sort_key(j: JavaInstall) -> tuple[int, int]:
        source_prio = {"launcher": 0, "PATH": 1, "registry": 2, "common_path": 3}
        prio = source_prio.get(j.source, 9)
        try:
            major = int(j.version.split(".")[0])
        except Exception:
            major = 0
        return (prio, -major)

    results.sort(key=_sort_key)
    return results
