"""
Java download worker for Cove Launcher.

Downloads and extracts a JDK distribution into the launcher's own managed
directory so the user never has to touch a system installer.

Supported flavors
-----------------
* temurin   — Eclipse Temurin (Adoptium OpenJDK, HotSpot JVM).
              API: https://api.adoptium.net/v3/assets/latest/{ver}/hotspot
* graalvm   — GraalVM Community Edition.
              API: GitHub releases for graalvm/graalvm-ce-builds
* microsoft — Microsoft Build of OpenJDK.
              Links: https://aka.ms/download-jdk/… (visualstudio CDN)

Install location
----------------
Each JDK lands in  <APP_DATA_DIR>/jdks/<flavor>-java<version>/
e.g.  %APPDATA%/Cove/jdks/temurin-java21/bin/java.exe
"""

from __future__ import annotations

import io
import json
import os
import urllib.request
import zipfile
from typing import Callable

from PyQt6.QtCore import QThread, pyqtSignal

from .constants import APP_DATA_DIR

# Root folder for all launcher-managed JDKs.
JDKS_DIR = os.path.join(APP_DATA_DIR, "jdks")

# ── API helpers ────────────────────────────────────────────────────────────────

_ADOPTIUM_URL = (
    "https://api.adoptium.net/v3/assets/latest/{version}/hotspot"
    "?os=windows&arch=x64&image_type=jdk"
)

_GRAALVM_RELEASES_URL = (
    "https://api.github.com/repos/graalvm/graalvm-ce-builds/releases"
)

_MICROSOFT_AKA_MS_TEMPLATE = (
    "https://aka.ms/download-jdk/microsoft-jdk-{ver}-windows-x64.zip"
)

_UA = "Cove-Launcher/1.0 (https://github.com/placeholder)"


def _fetch_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def resolve_temurin_url(java_version: int) -> tuple[str, str]:
    """
    Return ``(download_url, filename)`` for the latest Temurin Windows x64
    JDK zip matching *java_version*.
    """
    url = _ADOPTIUM_URL.format(version=java_version)
    data = _fetch_json(url)
    for entry in data:
        binary = entry.get("binary", {})
        pkg = binary.get("package", {})
        dl_url = pkg.get("link", "")
        name = pkg.get("name", "")
        if dl_url and name.endswith(".zip"):
            return dl_url, name
    raise RuntimeError(
        f"Adoptium API returned no Windows x64 zip for Java {java_version}. "
        "Check https://adoptium.net for availability."
    )


def resolve_graalvm_url(java_version: int) -> tuple[str, str]:
    """
    Return ``(download_url, filename)`` for the latest GraalVM Community
    Edition Windows x64 zip matching *java_version*.

    GraalVM CE assets follow the naming convention:
        graalvm-community-jdk-<ver>_windows-x64_bin.zip
    """
    releases = _fetch_json(_GRAALVM_RELEASES_URL)
    target_prefix = f"graalvm-community-jdk-{java_version}"
    for release in releases:
        if release.get("prerelease") or release.get("draft"):
            continue
        for asset in release.get("assets", []):
            name: str = asset.get("name", "")
            if (
                name.startswith(target_prefix)
                and "windows-x64" in name
                and name.endswith(".zip")
            ):
                return asset["browser_download_url"], name
    raise RuntimeError(
        f"No GraalVM CE Windows x64 release found for Java {java_version}. "
        "Check https://github.com/graalvm/graalvm-ce-builds/releases."
    )


def resolve_microsoft_url(java_version: int) -> tuple[str, str]:
    """
    Return ``(download_url, filename)`` for the latest Microsoft Build of
    OpenJDK Windows x64 zip matching *java_version*.

    Uses Microsoft's official ``aka.ms`` permanent links, which always
    redirect to the newest GA build of that version line on the
    ``download.visualstudio.microsoft.com`` CDN. (We previously scraped
    api.github.com/repos/microsoft/openjdk/releases, but that listing has
    been reduced to stale entries carrying no Windows x64 assets at all,
    which made every resolution fail.)
    """
    url = _MICROSOFT_AKA_MS_TEMPLATE.format(ver=java_version)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Range": "bytes=0-0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status in (200, 206):
                return url, f"microsoft-jdk-{java_version}-windows-x64.zip"
    except Exception as exc:  # noqa: BLE001 - reported below
        raise RuntimeError(
            f"No Microsoft OpenJDK build found for Java {java_version} "
            f"(aka.ms link unavailable: {exc}). "
            "Check https://learn.microsoft.com/java/openjdk/download."
        ) from exc
    raise RuntimeError(
        f"No Microsoft OpenJDK build found for Java {java_version}. "
        "Check https://learn.microsoft.com/java/openjdk/download."
    )


# ── Extraction helper ──────────────────────────────────────────────────────────

def extract_jdk_zip(
    zip_path: str,
    dest_dir: str,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    """
    Extract a JDK zip (which always contains a single top-level folder) into
    *dest_dir*, collapsing that top-level folder so the result is:
        dest_dir/bin/java.exe  (rather than dest_dir/<jdk-21.0.3>/bin/java.exe)

    Returns the path to the extracted ``java.exe``.
    """
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.namelist()
        total = len(members)
        # Detect the single top-level prefix folder (e.g. "jdk-21.0.3+9/")
        prefix = members[0].split("/")[0] + "/" if members else ""
        for i, member in enumerate(members):
            if progress:
                progress(i, total)
            # Strip the top-level folder name.
            rel = member[len(prefix):] if member.startswith(prefix) else member
            if not rel:
                continue
            target = os.path.join(dest_dir, rel)
            if member.endswith("/"):
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
        if progress:
            progress(total, total)

    java_exe = os.path.join(dest_dir, "bin", "java.exe")
    if not os.path.isfile(java_exe):
        # Some zips use just "java" (no extension) on non-Windows builds.
        java_exe = os.path.join(dest_dir, "bin", "java")
    return java_exe


# ── Qt worker thread ───────────────────────────────────────────────────────────

class JdkDownloadThread(QThread):
    """
    Downloads and extracts a JDK distribution off the GUI thread.

    Signals
    -------
    status(str)          human-readable step label
    progress(int, int)   (bytes_done, total_bytes) during download;
                         (files_done, total_files) during extraction
    succeeded(str)       path to the installed java.exe
    failed(str)          error message
    """

    status = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    _FLAVORS = ("temurin", "graalvm", "microsoft")

    def __init__(
        self,
        flavor: str,           # "temurin" | "graalvm"
        java_version: int,     # 17 | 21 | ...
        parent=None,
    ) -> None:
        super().__init__(parent)
        assert flavor in self._FLAVORS, f"Unknown flavor: {flavor!r}"
        self._flavor = flavor
        self._java_version = java_version

    def run(self) -> None:  # noqa: D102
        try:
            self._run()
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _run(self) -> None:
        flavor = self._flavor
        ver = self._java_version

        # 1. Resolve download URL from the API.
        self.status.emit(f"Resolving {flavor} Java {ver} download URL…")
        if flavor == "temurin":
            url, filename = resolve_temurin_url(ver)
        elif flavor == "graalvm":
            url, filename = resolve_graalvm_url(ver)
        else:  # microsoft
            url, filename = resolve_microsoft_url(ver)

        # Install location.
        install_dir = os.path.join(JDKS_DIR, f"{flavor}-java{ver}")
        java_exe = os.path.join(install_dir, "bin", "java.exe")

        # If already installed, skip download.
        if os.path.isfile(java_exe):
            self.status.emit("Already installed — skipping download.")
            self.succeeded.emit(java_exe)
            return

        # 2. Download to a temp zip.
        zip_path = os.path.join(JDKS_DIR, filename)
        os.makedirs(JDKS_DIR, exist_ok=True)

        self.status.emit(f"Downloading {filename}…")
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            chunk = 1024 * 256  # 256 KB
            with open(zip_path, "wb") as fh:
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    fh.write(data)
                    done += len(data)
                    self.progress.emit(done, total or done)

        # 3. Extract.
        self.status.emit("Extracting…")
        java_exe = extract_jdk_zip(
            zip_path,
            install_dir,
            progress=lambda done, total: self.progress.emit(done, total),
        )

        # 4. Clean up the zip.
        try:
            os.remove(zip_path)
        except OSError:
            pass

        if not os.path.isfile(java_exe):
            raise RuntimeError(
                f"Extraction finished but java.exe not found at {java_exe!r}."
            )

        self.status.emit(f"Installed to {install_dir}")
        # Reset the cached GraalVM probe so the freshly installed JDK is
        # visible to detect_graalvm()/instance launches without a restart.
        from .graalvm import detect_graalvm

        detect_graalvm.cache_clear()
        self.succeeded.emit(java_exe)
