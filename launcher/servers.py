"""
Server management for Cove Launcher.

Each server is a folder under ``%APPDATA%/Cove/servers/<name>/`` (right beside
the instances) holding the server jar the user imports, plus ``mods/`` and
``plugins/`` subfolders and the generated ``eula.txt`` / ``server.properties``.

The launcher never downloads server jars -- the user imports their own -- and
then sniffs it (:func:`detect_server_jar`): the jar's manifest, embedded
``version.json`` and filename are matched against known server software to
report the Minecraft version and whether the jar supports mods, plugins, or
**both** (hybrid "dual loader" servers like Mohist/Arclight/Cardboard).

This module also owns the per-server file management the server *editor*
needs: a comment-preserving ``server.properties`` reader/writer, world-folder
detection (``level.dat`` markers with a safe fallback listing), and guarded
mods/plugins file listing + deletion. Everything stays Qt-free so it can be
unit tested without an app instance.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, asdict, field
from datetime import datetime

from .constants import (
    DEFAULT_RAM_GB,
    SERVER_INDEX_PATH,
    SERVERS_DIR,
    SERVER_TYPES,
)

# Same rules as instance names so both folder trees stay predictable.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")

# "1.21", "1.20.4" plus the year-based scheme the launcher documents (26.1).
# The trailing lookahead is (?!\d) rather than (?![\d.]) so a version at the
# end of a filename ("paper-1.21.1.jar") still matches.
_MC_VERSION_RE = re.compile(
    r"(?<![\d.])1\.\d{1,2}(?:\.\d{1,2})*(?!\d)"
    r"|(?<![\d.])\d{2}\.\d{1,2}(?:\.\d{1,2})*(?!\d)"
)

# Hybrid markers FIRST: they embed the base software name too
# ("mohist-1.20.1.jar" would otherwise classify as plain Forge).
_HYBRID_MARKERS = (
    "mohist", "arclight", "cardboard", "magma", "banner",
    "granite", "sunlight", "limelight",
)


class ServerError(Exception):
    pass


def validate_name(name: str) -> None:
    if not _NAME_PATTERN.match(name.strip()):
        raise ServerError(
            "Server name must be 1-64 characters: letters, digits, "
            "spaces, dots, dashes or underscores."
        )


def _type_flags(server_type: str) -> tuple[bool, bool]:
    """(supports_mods, supports_plugins) for a SERVER_TYPES key."""
    entry = SERVER_TYPES.get(server_type)
    if entry is None:
        return False, False
    return entry[1], entry[2]


# Dual-loader servers execute Bukkit-family plugins, but they are not tagged
# with a dedicated Modrinth loader category, so they search under "spigot".
_DUAL_TYPES = frozenset({
    "mohist", "magma", "arclight", "cardboard", "banner",
    "granite", "sunlight", "limelight",
})
_PROXY_TYPES = frozenset({"velocity", "bungeecord", "waterfall"})
# Modrinth *mod* loader category per server type, for dual-loader and plain
# modded servers. Hybrids wrap a specific mod loader:
#   Magma/Banner = Forge, Mohist/Arclight = Forge-or-Fabric era-dependent
#   (Forge is the safe search facet), Cardboard/Granite/Sunlight/Limelight
#   = Fabric-family, plain Fabric/Forge/NeoForge servers map to themselves.
_MOD_LOADER_FOR = {
    "magma": "forge",
    "banner": "forge",
    "mohist": "forge",
    "arclight": "forge",
    "cardboard": "fabric",
    "granite": "fabric",
    "sunlight": "fabric",
    "limelight": "fabric",
    "fabric": "fabric",
    "forge": "forge",
    "neoforge": "neoforge",
}


def mod_loader_for(server_type: str) -> str | None:
    """Modrinth *mod* loader category for this server, or None.

    ``None`` means the server cannot run mods at all (vanilla, proxies,
    pure Bukkit-family) -- callers must refuse the mod search/install.
    """
    if not _type_flags(server_type)[0]:
        return None
    return _MOD_LOADER_FOR.get(server_type, "forge")


def plugin_loader_for(server_type: str) -> str | None:
    """Modrinth loader category for this server's plugins, or None.

    ``None`` means the server cannot run plugins at all (vanilla, unknown,
    pure modloaders) -- callers must refuse the plugin search/install.
    """
    if not _type_flags(server_type)[1]:
        return None
    if server_type in _DUAL_TYPES:
        return "spigot"          # hybrids accept Bukkit/Spigot plugins
    if server_type in _PROXY_TYPES:
        return server_type
    if server_type in ("paper", "purpur", "pufferfish", "folia", "spigot"):
        return server_type
    return "spigot"              # unknown-but-plugin-capable: broad category


# ---------------------------------------------------------------------------
# Jar detection
# ---------------------------------------------------------------------------
def _manifest_value(manifest_text: str, key: str) -> str:
    """Read one key from a JAR manifest, honouring 72-byte continuation lines."""
    # Continuation lines start with a single space; join them first.
    unfolded = manifest_text.replace("\r\n", "\n").replace("\n ", "")
    for line in unfolded.splitlines():
        if line.startswith(f"{key}:"):
            return line[len(key) + 1:].strip()
    return ""


def _json_id(version_json: str) -> str:
    match = re.search(r'"id"\s*:\s*"([^"]+)"', version_json)
    return match.group(1) if match else ""


def _classify(hay: str, main_class: str) -> str:
    """Map a lowercase blob of jar clues to a SERVER_TYPES key."""
    for marker in _HYBRID_MARKERS:
        if marker in hay:
            return marker
    # Fork families before their generic parents.
    if "pufferfish" in hay:
        return "pufferfish"
    if "purpur" in hay:
        return "purpur"
    if "folia" in hay:
        return "folia"
    if "paper" in hay:
        return "paper"
    if "spigot" in hay or "craftbukkit" in hay:
        return "spigot"
    if "neoforge" in hay:
        return "neoforge"
    if "fabric" in hay:
        return "fabric"
    if "forge" in hay:
        return "forge"
    if "velocity" in hay:
        return "velocity"
    if "waterfall" in hay:
        return "waterfall"
    if "bungeecord" in hay:
        return "bungeecord"
    if "net.minecraft.server.main" in main_class.lower():
        return "vanilla"
    return "unknown"


@dataclass
class JarInfo:
    """What the launcher figured out from an imported server jar."""

    filename: str
    mc_version: str = ""
    server_type: str = "unknown"

    @property
    def display_name(self) -> str:
        return SERVER_TYPES.get(self.server_type, ("Unknown", False, False))[0]

    @property
    def supports_mods(self) -> bool:
        return _type_flags(self.server_type)[0]

    @property
    def supports_plugins(self) -> bool:
        return _type_flags(self.server_type)[1]

    @property
    def is_dual(self) -> bool:
        return self.supports_mods and self.supports_plugins


def detect_server_jar(path: str) -> JarInfo:
    """Best-effort identification of a server jar: software + MC version.

    Reads ``META-INF/MANIFEST.MF``, the embedded ``version.json``,
    ``paperclip.properties`` and ``fabric-server-launch.properties`` when
    present, then falls back to filename heuristics. Unrecognised jars report
    ``server_type="unknown"`` (no mods/plugins claims) rather than guessing.
    """
    filename = os.path.basename(path)
    title = version_field = main_class = inner_id = extra = ""

    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "META-INF/MANIFEST.MF" in names:
                raw = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
                title = _manifest_value(raw, "Implementation-Title")
                version_field = _manifest_value(raw, "Implementation-Version")
                main_class = _manifest_value(raw, "Main-Class")
            if "version.json" in names:
                inner_id = _json_id(
                    zf.read("version.json").decode("utf-8", "replace"))
            if "paperclip.properties" in names:
                extra += " " + zf.read("paperclip.properties").decode(
                    "utf-8", "replace")
            if "fabric-server-launch.properties" in names:
                extra += " fabric-server-launch"
    except (zipfile.BadZipFile, OSError, RuntimeError):
        pass  # corrupt/partial jar: filename heuristics still run below

    hay = " ".join(
        p for p in (filename, title, version_field, inner_id, extra) if p
    ).lower()

    server_type = _classify(hay, main_class)
    mc_version = ""
    for blob in (inner_id, version_field, filename, extra):
        match = _MC_VERSION_RE.search(blob or "")
        if match:
            mc_version = match.group(0)
            break

    return JarInfo(filename=filename, mc_version=mc_version,
                   server_type=server_type)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
@dataclass
class Server:
    name: str
    jar: str = ""                       # jar filename inside the server folder
    mc_version: str = ""
    server_type: str = "unknown"
    ram_gb: int = DEFAULT_RAM_GB
    java_path: str = ""                 # custom java exe; "" = auto-detect
    extra_jvm_args: list = field(default_factory=list)
    eula_accepted: bool = False
    created: str = field(default_factory=lambda: datetime.now().isoformat())
    last_started: str | None = None
    # Root is stamped by the registry (lets tests point at a temp dir);
    # never serialized into servers.json.
    root: str = SERVERS_DIR

    @property
    def dir(self) -> str:
        return os.path.join(self.root, self.name)

    @property
    def mods_dir(self) -> str:
        return os.path.join(self.dir, "mods")

    @property
    def plugins_dir(self) -> str:
        return os.path.join(self.dir, "plugins")

    @property
    def jar_path(self) -> str:
        return os.path.join(self.dir, self.jar) if self.jar else ""

    @property
    def supports_mods(self) -> bool:
        return _type_flags(self.server_type)[0]

    @property
    def supports_plugins(self) -> bool:
        return _type_flags(self.server_type)[1]

    @property
    def is_dual(self) -> bool:
        return self.supports_mods and self.supports_plugins

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("root", None)
        return data

    @staticmethod
    def from_dict(raw: dict) -> "Server":
        known = set(Server.__dataclass_fields__)
        return Server(**{k: v for k, v in raw.items() if k in known})


def _safe_join(root: str, name: str) -> str:
    """Join *name* under *root* and refuse anything that escapes it."""
    target = os.path.realpath(os.path.join(root, name))
    if not target.startswith(os.path.realpath(root) + os.sep):
        raise ServerError(f"Refusing path outside the servers folder: {name}")
    return target


class ServerRegistry:
    """Loads/saves servers.json and manages server directories."""

    def __init__(self, index_path: str = SERVER_INDEX_PATH,
                 root: str = SERVERS_DIR) -> None:
        self._index_path = index_path
        self._root = root
        self._servers: list[Server] = []
        self.load()

    # -- persistence ---------------------------------------------------------
    def load(self) -> None:
        try:
            with open(self._index_path, "r", encoding="utf-8") as fh:
                raw_list = json.load(fh)
            self._servers = [Server.from_dict(raw) for raw in raw_list]
        except (FileNotFoundError, ValueError, TypeError):
            self._servers = []
        for server in self._servers:
            server.root = self._root

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._index_path), exist_ok=True)
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([s.to_dict() for s in self._servers], fh, indent=2)
        os.replace(tmp, self._index_path)

    # -- queries -------------------------------------------------------------
    def list(self) -> list[Server]:
        return list(self._servers)

    def get(self, name: str) -> Server | None:
        for server in self._servers:
            if server.name == name:
                return server
        return None

    # -- mutations -----------------------------------------------------------
    def create(self, name: str, ram_gb: int = DEFAULT_RAM_GB) -> Server:
        validate_name(name)
        if self.get(name) is not None:
            raise ServerError(f"A server named '{name}' already exists.")
        server = Server(name=name.strip(), ram_gb=int(ram_gb),
                        root=self._root)
        os.makedirs(server.dir, exist_ok=True)
        os.makedirs(server.mods_dir, exist_ok=True)
        os.makedirs(server.plugins_dir, exist_ok=True)
        self._servers.append(server)
        self.save()
        return server

    def import_jar(self, server: Server, src_path: str) -> JarInfo:
        """Copy *src_path* into the server folder, detect it, persist."""
        if not os.path.isfile(src_path):
            raise ServerError(f"Jar not found: {src_path}")
        os.makedirs(server.dir, exist_ok=True)
        dest_name = os.path.basename(src_path)
        dest = os.path.join(server.dir, dest_name)
        if os.path.realpath(src_path) != os.path.realpath(dest):
            shutil.copy2(src_path, dest)
        info = detect_server_jar(dest)
        server.jar = dest_name
        server.mc_version = info.mc_version
        server.server_type = info.server_type
        # Keep the content folders present so users can drop files in by hand
        # even for server types whose flags say otherwise.
        os.makedirs(server.mods_dir, exist_ok=True)
        os.makedirs(server.plugins_dir, exist_ok=True)
        self.save()
        return info

    def update(self, server: Server) -> None:
        for i, existing in enumerate(self._servers):
            if existing.name == server.name:
                server.root = self._root
                self._servers[i] = server
                self.save()
                return
        raise KeyError(server.name)

    def rename(self, old_name: str, new_name: str) -> Server:
        server = self.get(old_name)
        if server is None:
            raise ServerError(f"No server named '{old_name}'.")
        validate_name(new_name)
        new_name = new_name.strip()
        if new_name != old_name and self.get(new_name) is not None:
            raise ServerError(f"A server named '{new_name}' already exists.")
        src = server.dir
        dst = _safe_join(self._root, new_name)
        if os.path.isdir(dst):
            raise ServerError(f"Folder '{new_name}' already exists.")
        if os.path.isdir(src):
            os.rename(src, dst)
        server.name = new_name
        self.save()
        return server

    def delete(self, name: str, also_remove_files: bool = True) -> None:
        server = self.get(name)
        if server is None:
            return
        self._servers = [s for s in self._servers if s.name != name]
        self.save()
        if also_remove_files and os.path.isdir(self._root):
            target = _safe_join(self._root, name)
            # Safety: only ever delete inside our servers root.
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)

    def mark_started(self, name: str) -> None:
        server = self.get(name)
        if server:
            server.last_started = datetime.now().isoformat()
            self.save()


# ---------------------------------------------------------------------------
# server.properties (comment-preserving)
# ---------------------------------------------------------------------------
@dataclass
class PropertyLine:
    """One editable key from server.properties (comments stay in the file).

    Modded servers add dozens of unknown keys, so the editor shows *every*
    ``key=value`` line it finds and writes them all back in place -- only
    the value of an edited key changes. Comment/blank lines are preserved
    verbatim and are never editable.
    """

    key: str
    value: str = ""


def read_properties(server: Server) -> tuple[list[PropertyLine], list[str]]:
    """Read ``server.properties``.

    Returns ``(entries, raw_lines)`` where *raw_lines* is the verbatim file
    content (including comments/blank lines) that :func:`write_properties`
    needs to preserve formatting. When the file does not exist yet (the
    server never ran) both lists are empty -- callers show a "run once to
    generate" hint instead of an error.
    """
    path = os.path.join(server.dir, "server.properties")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw_lines = fh.read().splitlines()
    except OSError:
        return [], []
    entries: list[PropertyLine] = []
    seen: set[str] = set()
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        entries.append(PropertyLine(key=key, value=value.strip()))
    return entries, raw_lines


def write_properties(server: Server, raw_lines: list[str],
                     values: dict[str, str]) -> None:
    """Write edited values back, preserving comments/order/blank lines.

    Only the value portion of ``key=...`` lines whose key appears in
    *values* is replaced; everything else (comments, spacing around ``=``,
    unknown keys) is written back untouched. New keys not present in the
    original file are appended.
    """
    remaining = dict(values)
    out: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key, sep, _old = line.partition("=")
        if key.strip() in remaining:
            out.append(f"{key}{sep}{remaining.pop(key.strip())}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    os.makedirs(server.dir, exist_ok=True)
    tmp = os.path.join(server.dir, "server.properties.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, os.path.join(server.dir, "server.properties"))


# ---------------------------------------------------------------------------
# Worlds
# ---------------------------------------------------------------------------
@dataclass
class WorldInfo:
    """One world folder inside the server directory."""

    name: str
    path: str
    # Size in bytes (best effort; 0 when the folder vanished mid-scan).
    size_bytes: int = 0
    # ISO timestamp of the newest mtime found, "" when unknown.
    last_modified: str = ""
    # True when level.dat/region markers were found; False means "looks like
    # a folder, listed as a fallback" (e.g. a fresh/empty world dir).
    confirmed: bool = True


_WORLD_CONFIRM_FILES = frozenset({"level.dat", "level.dat_old"})
_WORLD_CONFIRM_DIRS = frozenset({"region", "entities", "poi", "DIM1", "DIM-1"})
# Top-level folders that are launcher/server machinery, never worlds.
_NON_WORLD_DIRS = frozenset({
    "mods", "plugins", "cache", "logs", "libraries", "versions",
    "config", "defaultconfigs", "kubejs", "scripts", "backups",
})


def _dir_size_and_newest(path: str) -> tuple[int, str]:
    total = 0
    newest = 0.0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            total += st.st_size
            if st.st_mtime > newest:
                newest = st.st_mtime
    stamp = (datetime.fromtimestamp(newest).isoformat(timespec="seconds")
             if newest else "")
    return total, stamp


def list_worlds(server: Server) -> list[WorldInfo]:
    """List world folders inside the server directory.

    A folder counts as a *confirmed* world when it holds ``level.dat`` /
    ``level.dat_old`` or a ``region/`` (etc.) subfolder. Folders without
    markers are still listed as unconfirmed fallbacks so a fresh world dir is
    visible instead of silently hidden.
    """
    worlds: list[WorldInfo] = []
    if not os.path.isdir(server.dir):
        return worlds
    try:
        names = sorted(os.listdir(server.dir))
    except OSError:
        return worlds
    for name in names:
        if name in _NON_WORLD_DIRS or name.startswith("."):
            continue
        full = os.path.join(server.dir, name)
        if not os.path.isdir(full) or os.path.islink(full):
            continue
        try:
            children = set(os.listdir(full))
        except OSError:
            continue
        confirmed = bool(children & _WORLD_CONFIRM_FILES
                         or children & _WORLD_CONFIRM_DIRS)
        if not confirmed:
            # Only surface plausible world dirs as fallback: skip anything
            # that looks like a file dump (jars, json, txt, properties...).
            if any(n.lower().endswith((".jar", ".json", ".txt", ".properties",
                                       ".yml", ".yaml", ".log", ".db"))
                   for n in children):
                continue
            if not children:
                continue
        size, stamp = _dir_size_and_newest(full)
        worlds.append(WorldInfo(name=name, path=full, size_bytes=size,
                                last_modified=stamp, confirmed=confirmed))
    return worlds


def rename_world(server: Server, old_name: str, new_name: str) -> WorldInfo:
    """Rename a world folder. Raises :class:`ServerError` on bad input."""
    validate_name(new_name)
    new_name = new_name.strip()
    src = _safe_join(server.dir, old_name)
    dst = _safe_join(server.dir, new_name)
    if not os.path.isdir(src):
        raise ServerError(f"World '{old_name}' no longer exists.")
    if os.path.exists(dst):
        raise ServerError(f"A file or folder named '{new_name}' already exists.")
    os.rename(src, dst)
    size, stamp = _dir_size_and_newest(dst)
    return WorldInfo(name=new_name, path=dst, size_bytes=size,
                     last_modified=stamp, confirmed=True)


# ---------------------------------------------------------------------------
# Server files (mods/ + plugins/)
# ---------------------------------------------------------------------------
def list_server_files(server: Server, subdir: str) -> list[str]:
    """Sorted ``.jar`` filenames in ``mods/`` or ``plugins/`` (empty ok).

    *subdir* must be exactly ``"mods"`` or ``"plugins"`` -- anything else
    raises so callers can never reach outside the server folder.
    """
    if subdir not in ("mods", "plugins"):
        raise ServerError(f"Refusing unknown server folder: {subdir!r}")
    folder = os.path.join(server.dir, subdir)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(n for n in names
                  if n.lower().endswith(".jar")
                  and os.path.isfile(os.path.join(folder, n)))


def delete_server_file(server: Server, subdir: str, filename: str) -> None:
    """Delete one jar from ``mods/``/``plugins/``. Guards against escapes."""
    if subdir not in ("mods", "plugins"):
        raise ServerError(f"Refusing unknown server folder: {subdir!r}")
    base = os.path.basename(filename)
    if not base or base in (".", "..") or not base.lower().endswith(".jar"):
        raise ServerError(f"Refusing suspicious filename: {filename!r}")
    folder = os.path.realpath(os.path.join(server.dir, subdir))
    target = os.path.realpath(os.path.join(folder, base))
    if target != os.path.join(folder, base):
        raise ServerError(f"Refusing path outside the server folder: {filename!r}")
    try:
        os.remove(target)
    except FileNotFoundError:
        raise ServerError(f"'{base}' is already gone.")
    except OSError as exc:
        raise ServerError(str(exc))




