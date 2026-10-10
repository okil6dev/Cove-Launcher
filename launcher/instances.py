"""
Instance management for Cove Launcher.

Each instance is a fully isolated Minecraft directory under
``%APPDATA%/Cove/instances/<name>/`` with its own ``versions/``,
``saves/``, ``mods/``, ``config/`` and ``options.txt``.

Disk optimisation: asset files are identical across versions, so a new
instance's ``assets`` folder is created as an NTFS *directory junction*
pointing at one shared store -- the same trick PrismLauncher-style launchers
use. Writes go through the junction into the shared store transparently.
If junction creation fails (permissions, non-NTFS), the instance silently
falls back to its own real directory.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict, field
from datetime import datetime

from .constants import (
    DEFAULT_RAM_GB,
    INSTANCE_INDEX_PATH,
    INSTANCES_DIR,
    JAVA_MODE_GRAALVM,
    SHARED_ASSETS_DIR,
)

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")


class InstanceError(Exception):
    pass


@dataclass
class Instance:
    name: str
    mc_version: str
    loader_id: str = "vanilla"           # vanilla | fabric | neoforge
    loader_build: str | None = None      # None = latest at install time
    java_mode: str = "auto"              # kept for backward compat (ignored by UI)
    java_path: str | None = None         # explicit java.exe path; overrides Mojang runtime
    ram_gb: int = DEFAULT_RAM_GB
    # Optional PNG (copied into the instance dir) shown instead of the
    # vanilla title-screen panorama; applied via a generated resource pack.
    # Mode selects how the image is mapped: "static" fills the view with a
    # centre-square crop on every frame, "panorama" slices a wide 360°
    # strip across the 10 frames so the menu keeps slowly rotating.
    custom_bg_png: str | None = None
    custom_bg_mode: str = "static"       # static | panorama
    # Extra JVM args (e.g. ["-Dfoo=bar"]) appended after Xmx/Xms.
    extra_jvm_args: list[str] = field(default_factory=list)
    created: str = field(default_factory=lambda: datetime.now().isoformat())
    last_played: str | None = None
    # Accumulated seconds actually spent in-game (updated when a game exits).
    playtime_seconds: int = 0
    # Version id actually launched (differs for modded/modpack installs).
    launch_version_id: str | None = None
    # Optional custom tile icon (image file path). Falls back to the cube.
    icon_path: str | None = None
    # Optional wide banner image (file path) shown on the home page when
    # this instance is selected. Falls back to a neutral placeholder.
    banner_path: str | None = None
    # Absolute paths of external folders whose jars are exposed into this
    # instance's mods folder (see launcher/external_mods.py). Empty by
    # default; the field is a list so several shared mod folders can be
    # attached to one instance.
    external_mod_dirs: list[str] = field(default_factory=list)

    @property
    def dir(self) -> str:
        return os.path.join(INSTANCES_DIR, self.name)

    @property
    def mods_dir(self) -> str:
        return os.path.join(self.dir, "mods")

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict) -> "Instance":
        known = {f for f in Instance.__dataclass_fields__}  # noqa: C416
        return Instance(**{k: v for k, v in raw.items() if k in known})


def validate_name(name: str) -> None:
    if not _NAME_PATTERN.match(name.strip()):
        raise InstanceError(
            "Instance name must be 1-64 characters: letters, digits, "
            "spaces, dots, dashes or underscores."
        )


def make_assets_junction(instance_dir: str) -> bool:
    """Junction-link <instance>/assets -> shared store. True on success."""
    link_path = os.path.join(instance_dir, "assets")
    os.makedirs(SHARED_ASSETS_DIR, exist_ok=True)
    if os.path.exists(link_path):
        return os.path.realpath(link_path) == os.path.realpath(SHARED_ASSETS_DIR)
    try:
        result = subprocess.run(  # noqa: S603,S607 - fixed args
            ["cmd", "/c", "mklink", "/J", link_path, SHARED_ASSETS_DIR],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0 and os.path.isdir(link_path)
    except Exception:
        return False


class InstanceRegistry:
    """Loads/saves instances.json and manages instance directories."""

    def __init__(self, index_path: str = INSTANCE_INDEX_PATH) -> None:
        self._index_path = index_path
        self._instances: list[Instance] = []
        self.load()

    # -- persistence ---------------------------------------------------------
    def load(self) -> None:
        try:
            with open(self._index_path, "r", encoding="utf-8") as fh:
                raw_list = json.load(fh)
            self._instances = [Instance.from_dict(raw) for raw in raw_list]
        except FileNotFoundError:
            self._instances = []
        except (ValueError, TypeError):
            self._instances = []

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._index_path), exist_ok=True)
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([inst.to_dict() for inst in self._instances], fh, indent=2)
        os.replace(tmp, self._index_path)

    # -- queries ----------------------------------------------------------------
    def list(self) -> list[Instance]:
        return list(self._instances)

    def get(self, name: str) -> Instance | None:
        for inst in self._instances:
            if inst.name == name:
                return inst
        return None

    # -- mutations ---------------------------------------------------------------
    def create(
        self,
        name: str,
        mc_version: str,
        loader_id: str = "vanilla",
        loader_build: str | None = None,
        ram_gb: int = DEFAULT_RAM_GB,
    ) -> Instance:
        validate_name(name)
        if self.get(name) is not None:
            raise InstanceError(f"An instance named '{name}' already exists.")

        instance = Instance(
            name=name,
            mc_version=mc_version,
            loader_id=loader_id,
            loader_build=loader_build or None,
            ram_gb=int(ram_gb),
        )
        os.makedirs(instance.dir, exist_ok=True)
        os.makedirs(instance.mods_dir, exist_ok=True)
        # Pre-create the junction BEFORE installation so the installer writes
        # assets straight into the shared store.
        make_assets_junction(instance.dir)

        self._instances.append(instance)
        self.save()
        return instance

    def register_existing(
        self,
        name: str,
        launch_version_id: str,
        mc_version: str,
    ) -> Instance:
        """Add an instance entry for an imported modpack directory."""
        validate_name(name)
        if self.get(name) is not None:
            raise InstanceError(f"An instance named '{name}' already exists.")
        instance = Instance(
            name=name,
            mc_version=mc_version,
            loader_id="vanilla",  # actual loader tracked via launch_version_id
            launch_version_id=launch_version_id,
        )
        os.makedirs(instance.dir, exist_ok=True)
        os.makedirs(instance.mods_dir, exist_ok=True)
        self._instances.append(instance)
        self.save()
        return instance

    def update(self, instance: Instance) -> None:
        for i, existing in enumerate(self._instances):
            if existing.name == instance.name:
                self._instances[i] = instance
                self.save()
                return
        raise KeyError(instance.name)

    def delete(self, name: str, also_remove_files: bool = True) -> None:
        instance = self.get(name)
        if instance is None:
            return
        self._instances = [i for i in self._instances if i.name != name]
        self.save()
        if also_remove_files and os.path.isdir(INSTANCES_DIR):
            target = instance.dir
            # Safety: only ever delete inside our instances root.
            if os.path.realpath(target).startswith(os.path.realpath(INSTANCES_DIR)):
                shutil.rmtree(target, ignore_errors=True)

    def mark_played(self, name: str) -> None:
        instance = self.get(name)
        if instance:
            instance.last_played = datetime.now().isoformat()
            self.save()

    def add_playtime(self, name: str, seconds: int) -> None:
        """Accumulate ``seconds`` of in-game time onto the instance total."""
        if seconds <= 0:
            return
        instance = self.get(name)
        if instance:
            instance.playtime_seconds = int(
                getattr(instance, "playtime_seconds", 0) or 0
            ) + int(seconds)
            self.save()


def resolve_java(instance: "Instance", system_java: str | None) -> tuple[str | None, str | None]:
    """
    Resolve the Java executables for an instance.

    Returns ``(override, fallback)``:
      * ``override`` -- passed as ``executablePath`` (forces this exact JVM)
      * ``fallback`` -- passed as ``defaultExecutablePath`` (used only when
        the version json has no managed runtime)

    Precedence (highest first):

    1. ``instance.java_path`` -- whatever was browsed/typed into the instance
       editor. This is THE user-facing selector; it must win over everything,
       including a stale legacy ``java_mode == "graalvm"`` flag.
    2. Legacy ``java_mode == "graalvm"`` -- auto-detects GraalVM CE; only
       honoured when no explicit path is set. When GraalVM cannot be found we
       fall back to *auto* (Mojang bundled runtime) rather than forcing the
       system java, so modern versions still get their matching JRE.
    3. ``java_mode == "path"`` -- force the system/PATH java.
    4. Otherwise auto -- ``None`` lets Mojang's version-matched runtime win.
    """
    # 1. Explicit per-instance selection (Browse / Detect / typed path).
    if instance.java_path and os.path.isfile(instance.java_path):
        return instance.java_path, system_java
    mode = instance.java_mode
    # 2. Legacy GraalVM mode still honoured for existing instances that never
    #    set an explicit path.
    if mode == JAVA_MODE_GRAALVM:
        from .graalvm import detect_graalvm
        result = detect_graalvm()
        if result:
            graalvm_exe, _ = result
            return graalvm_exe, system_java
        return None, system_java
    # PATH mode: force the system java.
    if mode == "path":
        return system_java, system_java
    # Auto: let Mojang's bundled runtime handle it.
    return None, system_java


def validate_java_executable(path: str) -> tuple[bool, str]:
    """Run ``<path> -version`` and report success + parsed version string."""
    if not os.path.isfile(path):
        return False, "File does not exist"
    try:
        result = subprocess.run(  # noqa: S603 - user-selected path
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        output = (result.stderr or "") + (result.stdout or "")
        first_line = output.strip().splitlines()[0] if output.strip() else ""
        if "version" in output.lower():
            return True, first_line[:120]
        return False, first_line[:120] or f"Exit code {result.returncode}"
    except Exception as exc:
        return False, f"Failed to execute: {exc}"


def build_jvm_arguments(instance: "Instance", ram_mb: int) -> list[str]:
    """
    Assemble the JVM memory + custom arguments for a launch.

    ``ram_mb`` is the resolved heap (already multiplied into MB). Custom
    arguments from the instance editor are appended after the memory flags.

    When GraalVM mode is selected, GraalVM-optimised JVM flags are prepended
    (G1GC tuning, JVMCI compiler unlock, larger code cache) for better FPS.
    """
    args = [
        f"-Xmx{ram_mb}M",
        f"-Xms{ram_mb}M",
    ]
    # Flags only when the *effective* JVM for this launch is genuinely a
    # GraalVM — mirrored from resolve_java()'s precedence so the selection UI
    # and the flag injection can never disagree:
    #   1. explicit java_path (Browse/Detect/typed) wins,
    #   2. else legacy graalvm mode with a detected installation.
    # Injecting -XX:+UseJVMCICompiler into plain HotSpot makes it abort with
    # exit code 1 ("Cannot use JVMCI compiler").
    is_graal = False
    if instance.java_path and os.path.isfile(instance.java_path):
        from .graalvm import is_graalvm_java

        is_graal = is_graalvm_java(instance.java_path)
    elif instance.java_mode == JAVA_MODE_GRAALVM:
        from .graalvm import detect_graalvm

        is_graal = detect_graalvm() is not None
    if is_graal:
        from .graalvm import graalvm_jvm_flags

        args += graalvm_jvm_flags()
    args += list(instance.extra_jvm_args or [])
    return args


def list_runtimes(instance_dir: str) -> list[str]:
    """
    Return human-friendly markers about what is installed for an instance.

    Used by the Installation tab of the instance editor.
    """
    parts: list[str] = []
    versions_path = os.path.join(instance_dir, "versions")
    runtimes_path = os.path.join(instance_dir, "runtime")
    mods_path = os.path.join(instance_dir, "mods")

    if os.path.isdir(versions_path):
        try:
            versions = [d for d in os.listdir(versions_path)
                        if os.path.isdir(os.path.join(versions_path, d))]
        except OSError:
            versions = []
        if versions:
            parts.append(f"{len(versions)} version(s) installed")
        else:
            parts.append("no versions installed")
    if os.path.isdir(runtimes_path):
        try:
            runtimes = [d for d in os.listdir(runtimes_path)
                        if os.path.isdir(os.path.join(runtimes_path, d))]
        except OSError:
            runtimes = []
        parts.append(f"{len(runtimes)} Java runtime(s)")
    if os.path.isdir(mods_path):
        try:
            mod_count = len(
                [n for n in os.listdir(mods_path)
                 if n.lower().endswith(".jar")]
            )
        except OSError:
            mod_count = 0
        parts.append(f"{mod_count} mod jar(s)")

    return parts or ["no installation yet"]


def dir_size_bytes(path: str) -> int:
    """Total size (bytes) of a directory tree. Mirrors disk usage."""
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except OSError:
        return 0
    return total



# __CHUNK2__
