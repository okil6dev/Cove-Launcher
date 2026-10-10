"""
GraalVM detection utilities for Cove Launcher.

GraalVM Community Edition ships a drop-in compatible JVM whose Graal JIT
compiler produces better-optimised machine code than the standard HotSpot C2
JIT, which can meaningfully improve Minecraft's frame-time consistency.

This module auto-detects GraalVM on the user's machine and exposes helpers
used by the launcher's Java resolution and JVM argument assembly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache

from .constants import APP_DATA_DIR

# Root folder the launcher's own "Download Java ↓" wizard installs JDKs into
# (<flavor>-java<ver>/bin/java.exe layout). Mirrors java_downloader.JDKS_DIR;
# computed here from APP_DATA_DIR to keep this module's import graph tiny.
_LAUNCHER_JDKS_DIR: str = os.path.join(APP_DATA_DIR, "jdks")

# Common Windows installation roots for GraalVM Community / Oracle GraalVM.
_GRAALVM_SEARCH_ROOTS: list[str] = [
    # Launchers's own downloaded JDKs — checked FIRST because that is where the
    # built-in downloader puts them (this bit us: GraalVM installed by the
    # launcher itself was invisible to the older Program Files-only scan).
    _LAUNCHER_JDKS_DIR,
    os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "GraalVM"),
    os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Java"),
    os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "GraalVM"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "GraalVM"),
    os.path.join(os.environ.get("USERPROFILE", ""), ".jdks"),
    # SDKMAN / scoop / mise style installs
    os.path.join(os.environ.get("USERPROFILE", ""), ".sdkman", "candidates", "java"),
    os.path.join(os.environ.get("USERPROFILE", ""), "scoop", "apps"),
]

_GRAALVM_MARKERS = ("graalvm", "graal")  # substrings to match in java -version output


def _release_file_says_graalvm(java_path: str) -> bool | None:
    """
    Identify a JVM by reading its ``<jdk-home>/release`` metadata file.

    Returns True/False when a readable release file gave a definitive answer,
    or ``None`` when there is no release file (caller falls back to probing
    the binary). Every GraalVM build (CE and Oracle) writes identifying keys
    such as ``IMPLEMENTOR="GraalVM Community"`` or ``GRAALVM_VERSION="..."``,
    while other vendors (Temurin, Microsoft, JetBrains) never contain the
    substring "graa" anywhere in the file.
    """
    # java_path looks like <jdk-home>\bin\java.exe → jdk home is two up.
    jdk_home = os.path.dirname(os.path.dirname(os.path.abspath(java_path)))
    try:
        with open(os.path.join(jdk_home, "release"), encoding="utf-8", errors="replace") as fh:
            text = fh.read().lower()
    except OSError:
        return None
    if not text:
        return None
    return "graa" in text


def _is_graalvm_binary(java_path: str) -> bool:
    """Return True if the given `java` binary is a GraalVM JVM."""
    release_verdict = _release_file_says_graalvm(java_path)
    if release_verdict is not None:
        return release_verdict
    try:
        result = subprocess.run(
            [java_path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        version_text = (result.stderr + result.stdout).lower()
        return any(marker in version_text for marker in _GRAALVM_MARKERS)
    except Exception:
        return False


def _candidate_java_paths(root: str) -> list[str]:
    """
    Collect ``<...>/bin/java.exe`` candidates below *root*, preferring
    graalvm-named locations.

    Covers installs nested up to three directory levels deep, which handles
    both the launcher's flat layout (``jdks\\graalvm-java21\\bin\\java.exe``)
    and raw zip extractions that add their own wrapper folder
    (``root\\distro-name\\graalvm-community-openjdk-21…\\bin\\java.exe``).
    """
    candidates: list[str] = []
    direct = os.path.join(root, "bin", "java.exe")
    if os.path.isfile(direct):
        candidates.append(direct)
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return candidates
    for entry in entries:
        lvl1 = os.path.join(root, entry)
        exe = os.path.join(lvl1, "bin", "java.exe")
        if os.path.isfile(exe):
            candidates.append(exe)
        try:
            sub_entries = sorted(os.listdir(lvl1))
        except OSError:
            continue
        for sub in sub_entries:
            deep_exe = os.path.join(lvl1, sub, "bin", "java.exe")
            if os.path.isfile(deep_exe):
                candidates.append(deep_exe)
    # Prefer candidates whose path mentions graal (stable ordering regardless
    # of alphabetical quirks in mixed JDK folders).
    candidates.sort(key=lambda p: "graal" not in p.lower())
    return candidates


def _find_in_search_roots() -> str | None:
    """
    Walk well-known GraalVM installation roots for a valid `java.exe`,
    checking every found binary with :func:`_is_graalvm_binary`.
    """
    for root in _GRAALVM_SEARCH_ROOTS:
        if not root or not os.path.isdir(root):
            continue
        for candidate in _candidate_java_paths(root):
            if _is_graalvm_binary(candidate):
                return candidate
    return None


@lru_cache(maxsize=1)
def detect_graalvm() -> tuple[str, str] | None:
    """
    Auto-detect the best available GraalVM installation.

    Search order:
      1. The ``GRAALVM_HOME`` environment variable (explicit user override).
      2. The launcher's own JDK download folder (``%APPDATA%\\Cove\\jdks``),
         where the built-in "Download Java ↓" wizard installs GraalVM.
      3. ``java`` on the system PATH — if it identifies as GraalVM, use it.
      4. Well-known Windows installation directories.

    Returns ``(java_exe_path, version_string)`` or ``None`` when not found.
    The result is cached for the lifetime of the process.
    """
    # 1. Explicit GRAALVM_HOME override.
    graalvm_home = os.environ.get("GRAALVM_HOME", "")
    if graalvm_home:
        java_exe = os.path.join(graalvm_home, "bin", "java.exe")
        if not os.path.isfile(java_exe):
            java_exe = os.path.join(graalvm_home, "bin", "java")
        if os.path.isfile(java_exe) and _is_graalvm_binary(java_exe):
            version = _version_string(java_exe)
            return java_exe, version

    # 2. System PATH `java`.
    system_java = shutil.which("java")
    if system_java and _is_graalvm_binary(system_java):
        return system_java, _version_string(system_java)

    # 3. Scan well-known directories.
    found = _find_in_search_roots()
    if found:
        return found, _version_string(found)

    return None


def _version_string(java_path: str) -> str:
    """Return the first line of `java -version` output, truncated."""
    try:
        result = subprocess.run(
            [java_path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = (result.stderr + result.stdout).strip()
        return output.splitlines()[0][:120] if output else "unknown"
    except Exception:
        return "unknown"


def is_graalvm_java(java_path: str) -> bool:
    """
    Public helper: does *java_path* point at a GraalVM JVM?

    Used by JVM-flag assembly so Graal-only flags are never handed to a
    plain HotSpot binary (which would abort the launch).
    """
    return bool(java_path) and os.path.isfile(java_path) and _is_graalvm_binary(java_path)


def graalvm_jvm_flags() -> list[str]:
    """
    Return a list of extra JVM flags that improve performance when running on
    GraalVM Community Edition.

    These flags are safe no-ops on standard HotSpot and ignored by GraalVM
    when not applicable, so they can be passed unconditionally.
    """
    return [
        # Enable the Graal JIT compiler explicitly (default on GraalVM but
        # harmless on HotSpot where it is simply unrecognised and ignored).
        "-XX:+UnlockExperimentalVMOptions",
        "-XX:+UseJVMCICompiler",
        # Larger code cache helps long-running sessions like Minecraft.
        "-XX:ReservedCodeCacheSize=512m",
        # G1GC is a good default; Graal works well with it.
        "-XX:+UseG1GC",
        "-XX:MaxGCPauseMillis=50",
        "-XX:G1HeapRegionSize=16m",
        "-XX:G1NewSizePercent=20",
        "-XX:G1ReservePercent=20",
        "-XX:G1MixedGCCountTarget=4",
        # Reduce JIT compilation overhead on background threads.
        "-XX:+ParallelRefProcEnabled",
        # Faster class loading.
        "-XX:+TieredCompilation",
    ]
