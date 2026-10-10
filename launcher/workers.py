"""
Background workers for Cove Launcher.

Every long-running operation (network fetches, downloads, installation,
game process supervision) runs inside a dedicated :class:`PyQt6.QtCore.QThread`
so the GUI thread never blocks. Workers communicate exclusively through
``pyqtSignal`` emissions; Qt automatically queues those onto the GUI thread.

Library compatibility
---------------------
``minecraft-launcher-lib`` 8.0 replaced the legacy per-loader modules
(``fabric``, ``neoforge``, ``forge``) with a unified
:mod:`~minecraft_launcher_lib.mod_loader` package. This module prefers the
modern API and transparently falls back to the deprecated legacy modules when
running against 7.x releases.
"""

from __future__ import annotations

import os
import subprocess
import threading
import uuid as uuidlib
import warnings

from PyQt6.QtCore import QThread, pyqtSignal

from minecraft_launcher_lib import command as mll_command
from minecraft_launcher_lib import install as mll_install
from minecraft_launcher_lib import utils as mll_utils

try:  # minecraft-launcher-lib >= 8.0
    from minecraft_launcher_lib import mod_loader as _mod_loader_module

    _HAS_UNIFIED_MOD_LOADER = hasattr(_mod_loader_module, "get_mod_loader")
except ImportError:  # pragma: no cover - exercised only on library 7.x
    _mod_loader_module = None
    _HAS_UNIFIED_MOD_LOADER = False

# NeoForge's own maven version listing; used as a fallback when the library's
# metadata cannot resolve a Minecraft version (see LoaderVersionsThread).
_NEOFORGE_MAVEN_URL: str = (
    "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
)

from .accounts import AuthError, offline_uuid
from .constants import (
    APP_VERSION,
    ELY_AUTHLIB_INJECTOR_ALIAS,
    FALLBACK_MIN_RELEASE_TIME,
    LAUNCHER_ID_FOR_JVM,
    MIN_SUPPORTED_RELEASE,
)
from .elyby import ensure_authlib_injector


class _InstallationCancelled(Exception):
    """Raised internally from install callbacks when the user aborts."""


# ---------------------------------------------------------------------------
# Version data fetching
# ---------------------------------------------------------------------------
class VersionListThread(QThread):
    """
    Fetches the list of stable Minecraft releases offered by Mojang.

    The result contains every stable release newer than (and including)
    :data:`launcher.constants.MIN_SUPPORTED_RELEASE`, ordered newest first.
    Because ordering relies on ``releaseTime`` rather than version IDs, the
    year-based scheme (e.g. ``26.2``) works without special-casing.
    """

    ready = pyqtSignal(list)     # list[str]: version ids, newest first
    failed = pyqtSignal(str)

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            releases = [
                info for info in mll_utils.get_version_list()
                if info["type"] == "release"
            ]
            # Mojang's manifest is usually already sorted, but never trust it:
            # sort explicitly by release time so future schemes stay correct.
            releases.sort(key=lambda info: info["releaseTime"], reverse=True)

            ids: list[str] = []
            found_cutoff = False
            for info in releases:
                ids.append(info["id"])
                if info["id"] == MIN_SUPPORTED_RELEASE:
                    found_cutoff = True
                    break

            if not found_cutoff:
                # Fallback: cut off by release date instead of exact ID.
                ids = [
                    info["id"] for info in releases
                    if info["releaseTime"].replace(tzinfo=None)
                    >= FALLBACK_MIN_RELEASE_TIME
                ]

            if not ids:
                raise RuntimeError("Mojang returned no usable releases")

            self.ready.emit(ids)
        except Exception as exc:  # network errors, manifest changes, ...
            self.failed.emit(f"Could not fetch version list: {exc!r}")


# ---------------------------------------------------------------------------
# Loader metadata fetching
# ---------------------------------------------------------------------------
class LoaderVersionsThread(QThread):
    """
    Fetches loader metadata (supported builds + availability) for one
    ``(modloader, minecraft_version)`` pair on demand.

    Emitted payload:
        supported -- whether the selected vanilla version has a build
        builds    -- loader build identifiers, newest first
        latest    -- convenience copy of ``builds[0]`` when available
    """

    ready = pyqtSignal(bool, list, str)   # supported, builds, latest
    failed = pyqtSignal(str)

    def __init__(self, modloader_id: str, mc_version: str, parent=None) -> None:
        super().__init__(parent)
        self._modloader_id = modloader_id
        self._mc_version = mc_version

    def _fetch_unified(self) -> tuple[bool, list[str]]:
        """Implementation for minecraft-launcher-lib >= 8.0."""
        assert _mod_loader_module is not None
        loader = _mod_loader_module.get_mod_loader(self._modloader_id)
        supported = loader.is_minecraft_version_supported(self._mc_version)
        if not supported:
            # minecraft-launcher-lib's NeoForge metadata predates Mojang's
            # year-based version ids ("26.1", "26.1.2", ...): its internal
            # lookup blindly prepends "1." so those versions report as
            # unsupported even though builds exist. Query NeoForge's maven
            # directly before giving up (installing works either way, since
            # the installer just downloads the build we name).
            if self._modloader_id == "neoforge":
                builds = self._fetch_neoforge_maven()
                if builds:
                    return True, builds
            return False, []
        builds = list(loader.get_loader_versions(self._mc_version, True))
        if not builds and self._modloader_id == "neoforge":
            builds = self._fetch_neoforge_maven()
        return True, builds

    def _fetch_neoforge_maven(self) -> list[str]:
        """Query NeoForge's maven directly for builds of this MC version.

        NeoForge build ids embed the Minecraft version they target:
          * legacy MC ``1.21.1``     -> ``21.1.<build>``
          * year-based MC ``26.1.2`` -> ``26.1.2.<build>``
          * year-based MC ``26.2``   -> ``26.2.0.<build>`` (implicit .0)
        A prefix match on the (de-"1."-ed) MC version covers all three.
        Only stable builds (no alpha/beta/snapshot tags) are returned,
        newest first.
        """
        mc = self._mc_version
        if mc.startswith("1."):
            prefix = f"{mc[2:]}."
        elif mc.count(".") == 1:
            prefix = f"{mc}.0."
        else:
            prefix = f"{mc}."
        try:
            import requests

            resp = requests.get(_NEOFORGE_MAVEN_URL, timeout=15)
            resp.raise_for_status()
            versions = resp.json().get("versions", [])
        except Exception:
            return []
        builds = [
            v for v in versions
            if v.startswith(prefix)
            and "alpha" not in v and "beta" not in v and "+" not in v
        ]
        builds.reverse()  # maven lists oldest -> newest; we want newest first
        return builds

    def _fetch_legacy(self) -> tuple[bool, list[str]]:
        """Fallback implementation for minecraft-launcher-lib 7.x."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

            if self._modloader_id == "fabric":
                from minecraft_launcher_lib import fabric as legacy_fabric

                supported = legacy_fabric.is_minecraft_version_supported(
                    self._mc_version
                )
                builds = [
                    b["version"] for b in legacy_fabric.get_all_loader_versions()
                ]
                return bool(supported), builds

            if self._modloader_id == "neoforge":
                from minecraft_launcher_lib import neoforge as legacy_neoforge

                versions = list(legacy_neoforge.get_neoforge_versions())
                return self._mc_version in versions, versions

        raise ValueError(f"Unsupported modloader id {self._modloader_id!r}")

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            if _HAS_UNIFIED_MOD_LOADER:
                supported, builds = self._fetch_unified()
            else:
                supported, builds = self._fetch_legacy()

            latest = builds[0] if builds else ""
            self.ready.emit(supported, builds, latest)
        except Exception as exc:
            self.failed.emit(
                f"Could not fetch {self._modloader_id} builds "
                f"for {self._mc_version}: {exc!r}"
            )


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------
class InstallerThread(QThread):
    """
    Downloads / repairs a Minecraft installation (optionally with a modloader).

    Vanilla installs use :func:`install_minecraft_version`; modded installs
    delegate to the library's modloader engine, which also installs the
    matching vanilla base first. The library's callback dict is bridged into
    Qt signals so the progress bar updates live.
    """

    status = pyqtSignal(str)          # human readable step description
    progress_max = pyqtSignal(int)    # new upper bound of the progress bar
    progress_value = pyqtSignal(int)  # new current value
    succeeded = pyqtSignal(str)       # installed/launchable version id
    failed = pyqtSignal(str)

    def __init__(
        self,
        mc_version: str,
        minecraft_directory: str,
        modloader_id: str = "vanilla",
        loader_version: str | None = None,
        java_executable: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._mc_version = mc_version
        self._mc_dir = minecraft_directory
        self._modloader_id = modloader_id
        self._loader_version = loader_version or None  # sentinel -> latest
        self._java = java_executable
        self._cancelled = False

    # -- public control ----------------------------------------------------
    def cancel(self) -> None:
        """Request cancellation; takes effect at the next callback tick."""
        self._cancelled = True

    # -- internal helpers --------------------------------------------------
    def _make_callback(self) -> dict:
        """Bridge minecraft-launcher-lib's callback dict into Qt signals."""

        def guard() -> None:
            if self._cancelled:
                raise _InstallationCancelled()

        def set_status(text: str) -> None:
            # Guard runs inside every status update, giving frequent and
            # responsive cancellation checkpoints during large downloads.
            guard()
            self.status.emit(str(text))

        def set_progress(value: int) -> None:
            self.progress_value.emit(int(value))

        def set_max(value: int) -> None:
            self.progress_max.emit(max(1, int(value)))

        return {
            "setStatus": set_status,
            "setProgress": set_progress,
            "setMax": set_max,
        }

    def _install_vanilla(self) -> str:
        mll_install.install_minecraft_version(
            self._mc_version, self._mc_dir, callback=self._make_callback()
        )
        return self._mc_version

    def _install_modded_unified(self) -> str:
        """Install path for minecraft-launcher-lib >= 8.0."""
        assert _mod_loader_module is not None
        loader = _mod_loader_module.get_mod_loader(self._modloader_id)
        return str(
            loader.install(
                self._mc_version,
                self._mc_dir,
                callback=self._make_callback(),
                java=self._java or "java",
                loader_version=self._loader_version,
            )
        )

    def _install_modded_legacy(self) -> str:
        """Install path for minecraft-launcher-lib 7.x."""
        callback = self._make_callback()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

            if self._modloader_id == "fabric":
                from minecraft_launcher_lib import fabric as legacy_fabric

                legacy_fabric.install_fabric(
                    self._mc_version,
                    self._mc_dir,
                    loader_version=self._loader_version,
                    callback=callback,
                    java=self._java,
                )
                lv = (
                    self._loader_version
                    or legacy_fabric.get_latest_loader_version()
                )
                return f"fabric-loader-{lv}-{self._mc_version}"

            if self._modloader_id == "neoforge":
                from minecraft_launcher_lib import neoforge as legacy_neoforge

                legacy_neoforge.install_neoforge(
                    self._mc_version,
                    self._mc_dir,
                    callback=callback,
                    java=self._java,
                    forge_version=self._loader_version,
                )
                return f"neoforge-{self._mc_version}"

        raise ValueError(f"Unsupported modloader id {self._modloader_id!r}")

    # -- Qt entry point ------------------------------------------------------
    def run(self) -> None:  # noqa: D102
        try:
            if self._cancelled:
                raise _InstallationCancelled()

            if self._modloader_id == "vanilla":
                installed_id = self._install_vanilla()
            elif _HAS_UNIFIED_MOD_LOADER:
                installed_id = self._install_modded_unified()
            else:
                installed_id = self._install_modded_legacy()

            self.succeeded.emit(installed_id)
        except _InstallationCancelled:
            self.failed.emit("Installation cancelled.")
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Game execution
# ---------------------------------------------------------------------------
class LaunchThread(QThread):
    """
    Builds the JVM command line, spawns the game process asynchronously and
    pipes its stdout/stderr back into the GUI log view.

    Reading both pipes happens on daemon helper threads so interleaved output
    cannot deadlock, while this QThread merely waits for process exit.
    """

    game_output = pyqtSignal(str)    # single decoded line from the game
    game_started = pyqtSignal(list)  # full command line that was spawned
    game_exited = pyqtSignal(int)    # process exit code
    status = pyqtSignal(str)         # progress text (e.g. auth steps)
    failed = pyqtSignal(str)

    def __init__(
        self,
        version_id: str,
        minecraft_directory: str,
        ram_gb: int,
        java_executable: str | None = None,
        extra_jvm_args: list[str] | None = None,
        username: str = "Player",
        account_store=None,   # AccountStore, required for MSA accounts
        account=None,         # Account; when None -> offline mode
        instance=None,        # Instance; used for the pre-launch mods sync
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._version_id = version_id
        self._mc_dir = minecraft_directory
        self._instance = instance
        self._username = username.strip() or "Player"
        self._ram_mb = max(512, int(ram_gb) * 1024)
        self._java = java_executable
        # Set post-construction (before start()) to force a *specific* JVM,
        # e.g. the detected GraalVM or an explicitly browsed java.exe.
        self._java_override: str | None = None
        self._extra_jvm_args = list(extra_jvm_args or [])
        self._account_store = account_store
        self._account = account
        self._process: subprocess.Popen | None = None

    # -- public control ----------------------------------------------------
    def stop(self) -> None:
        """
        Terminate the game process.

        On Windows ``terminate()`` only kills the direct child, which is fine
        here because the JVM itself spawns no intermediate wrapper processes.
        """
        proc = self._process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass

    # -- internals -----------------------------------------------------------
    @staticmethod
    def _offline_uuid(username: str) -> str:
        """Deprecated shim -- see :func:`launcher.accounts.offline_uuid`."""
        return offline_uuid(username)

    def _resolve_identity(self) -> tuple[str, str, str]:
        """
        Return ``(username, uuid, access_token)`` for the configured account.

        MSA and Ely.by accounts get a fresh access token here (network call
        happens on this worker thread, never on the GUI thread). Tokens are
        never logged.
        """
        if self._account is not None and self._account.type in ("msa", "ely"):
            if self._account_store is None:
                raise AuthError("Internal error: account store missing")
            name, uuid_str, token = self._account_store.ensure_access_token(
                self._account
            )
            kind = "Microsoft" if self._account.type == "msa" else "Ely.by"
            self.status.emit(f"Authenticated as {name} ({kind}).")
            return name or "Player", uuid_str, token
        # Offline / developer mode.
        return self._username, offline_uuid(self._username), "0"

    def _build_options(
        self, username: str, user_uuid: str, access_token: str
    ) -> dict:
        """Assemble the options dict consumed by ``get_minecraft_command``."""
        jvm_args = [
            f"-Xmx{self._ram_mb}M",
            f"-Xms{self._ram_mb}M",
            *self._extra_jvm_args,
        ]
        if self._account is not None and self._account.type == "ely":
            # Ely.by sessions only work when the game's authlib talks to
            # ely.by's session servers; authlib-injector does that at runtime
            # without touching the version's own authlib library. The jar is
            # cached in the launcher data dir on first use.
            try:
                jar = ensure_authlib_injector(self.status.emit)
            except Exception as exc:
                raise AuthError(
                    "Ely.by authlib-injector is required but could not be "
                    f"prepared: {exc}"
                ) from exc
            jvm_args.insert(
                0, f"-javaagent:{jar.replace(os.sep, '/')}={ELY_AUTHLIB_INJECTOR_ALIAS}"
            )
        options: dict = {
            # Offline / developer authentication: any name + derived UUID.
            "username": username,
            "uuid": user_uuid,
            "token": access_token,
            "jvmArguments": jvm_args,
            "launcherName": LAUNCHER_ID_FOR_JVM,
            "launcherVersion": APP_VERSION,
            "enableLoggingConfig": True,
        }
        if self._java:
            # This is only a *fallback* (defaultExecutablePath): it is used
            # solely when the version json declares no javaVersion. For
            # versions WITH a managed runtime this key is ignored entirely.
            options["defaultExecutablePath"] = self._java
        if self._java_override:
            # Hard override: minecraft-launcher-lib honours executablePath
            # BEFORE its managed-runtime lookup, so this forces the exact JVM
            # regardless of what javaVersion declares (GraalVM / custom path).
            options["executablePath"] = self._java_override
        return options


    def _pump_pipe(self, pipe) -> None:
        """Reader loop executed on a daemon thread."""
        try:
            for raw_line in iter(pipe.readline, ""):
                line = raw_line.rstrip("\r\n")
                if line:
                    self.game_output.emit(line)
        except Exception:
            pass  # pipe closed during shutdown - harmless
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def _sync_external_mods(self) -> None:
        """Refresh external mods folders into ``mods/`` before launching.

        Runs here (a worker thread) rather than on the GUI thread: with the
        symlink/hardlink fallback unavailable, ``sync`` copies every jar, which
        is far too slow to do while the UI is trying to paint.

        It must complete before :meth:`run` builds the command line, because
        the loaders read ``mods/`` as the game starts.
        """
        try:
            from . import external_mods

            result = external_mods.sync(self._instance)
        except Exception as exc:  # noqa: BLE001 - mods must not block play
            self.status.emit(f"External mods sync skipped: {exc}")
            return
        if result.total() or result.removed:
            self.status.emit(f"External mods: {result.summary()}.")
        for err in result.errors[:3]:
            self.status.emit(f"External mods: {err}")

    def run(self) -> None:  # noqa: D102
        try:
            self._sync_external_mods()
            username, user_uuid, access_token = self._resolve_identity()
            options = self._build_options(username, user_uuid, access_token)
            command_line = mll_command.get_minecraft_command(
                self._version_id, self._mc_dir, options
            )
        except AuthError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:
            self.failed.emit(f"Failed to build launch command: {exc!r}")
            return

        self.game_started.emit(command_line)

        try:
            self._process = subprocess.Popen(  # noqa: S603 - fixed arg list
                command_line,
                cwd=self._mc_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # line buffered for live streaming
            )
        except FileNotFoundError as exc:
            self.failed.emit(f"Java runtime not found: {exc}")
            return
        except OSError as exc:
            self.failed.emit(f"Could not start the game process: {exc}")
            return

        readers = [
            threading.Thread(target=self._pump_pipe, args=(pipe,), daemon=True)
            for pipe in (self._process.stdout, self._process.stderr)
        ]
        for reader in readers:
            reader.start()

        exit_code = self._process.wait()
        for reader in readers:
            reader.join(timeout=2)

        self.game_exited.emit(exit_code if exit_code is not None else -1)
