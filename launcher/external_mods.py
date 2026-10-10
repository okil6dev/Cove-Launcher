"""
External mods folders -- expose a folder of .jar files as if it were part of
an instance's own ``mods`` folder.

Why this module exists
----------------------
Fabric/NeoForge only scan ``*.jar`` files sitting *directly* inside ``mods/``;
they never recurse into subdirectories. So a folder cannot simply be attached
the way the shared ``assets`` store is (that trick relies on an NTFS directory
junction, and loaders would still ignore everything below ``mods/``). Each
external jar therefore has to be exposed as its own entry inside ``mods/``.

Three ways to do that, tried in order by :func:`_expose`:

  1. ``symlink`` -- always live; needs Admin/Developer Mode on Windows.
  2. ``hardlink`` -- live as well (same inode) and needs no privileges, but
     only works when source and mods folder share a volume.
  3. ``copy``    -- works everywhere, but is a snapshot until the next sync.

:func:`sync` runs before every launch (and after every editor save), so a
copied file picks up modifications made to its source. The chosen method is
recorded per entry and surfaced in the UI, so "my edit did not show up" is
never a mystery.

Safety
------
Every file this module creates is recorded in a manifest
(``<instance>/external-mods.json``). Only manifest entries are ever removed or
overwritten -- a mod the user installed themselves is never touched, and source
files are never deleted.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass

MANIFEST_NAME = "external-mods.json"
MANIFEST_VERSION = 1

# Extensions treated as mods, mirroring moddb.list_installed_mods() so the
# Installed Mods tab and the browse page agree on what counts as a mod.
MOD_SUFFIXES = (".jar", ".jar.disabled")

# Appended to the jar name when an external file would otherwise overwrite one
# of the user's own mods. Jar identity comes from the embedded
# fabric.mod.json / mods.toml, never from the filename, so renaming is safe.
EXTERNAL_SUFFIX = " (ext)"

__all__ = [
    "Entry", "ExternalModsError", "SyncResult",
    "add_folder", "clear", "folder_methods", "list_jars",
    "load_manifest", "managed_entries", "normalise_folders",
    "remove_entry", "remove_folder", "save_manifest", "source_for", "sync",
]


class ExternalModsError(Exception):
    """Raised for user-facing validation problems (bad folder, etc.)."""


@dataclass
class Entry:
    """One exposed file inside the instance's mods folder."""

    filename: str      # name inside mods_dir
    source: str        # absolute path of the originating file
    method: str        # "symlink" | "hardlink" | "copy"

    def describe(self) -> str:
        return {
            "symlink": "linked",
            "hardlink": "hardlink",
            "copy": "copied",
        }.get(self.method, self.method)


@dataclass
class SyncResult:
    """Outcome of a :func:`sync` run, used for logging / toasts."""

    linked: int = 0
    copied: int = 0
    refreshed: int = 0   # copies re-made because the source changed
    removed: int = 0
    skipped: int = 0     # files we refused to touch (real mods in the way)
    errors: list[str] = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def total(self) -> int:
        return self.linked + self.copied + self.refreshed

    def summary(self) -> str:
        bits = []
        if self.linked:
            bits.append(f"{self.linked} linked")
        if self.copied:
            bits.append(f"{self.copied} copied")
        if self.refreshed:
            bits.append(f"{self.refreshed} refreshed")
        if self.removed:
            bits.append(f"{self.removed} removed")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        if self.errors:
            bits.append(f"{len(self.errors)} error(s)")
        return ", ".join(bits) or "nothing to do"


# --------------------------------------------------------------- manifest --
def manifest_path(instance) -> str:
    return os.path.join(instance.dir, MANIFEST_NAME)


def load_manifest(instance) -> dict[str, Entry]:
    """Read the manifest, tolerating a missing/corrupt file.

    Returns a mapping of ``filename -> Entry`` for files this module manages.
    """
    try:
        with open(manifest_path(instance), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}

    if not isinstance(raw, dict):
        return {}

    entries: dict[str, Entry] = {}
    for item in raw.get("entries") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("filename") or "").strip()
        source = str(item.get("source") or "").strip()
        method = str(item.get("method") or "").strip()
        # Reject hand-edited entries: unknown methods, and any name that would
        # escape the mods folder (basename both ways).
        if not name or os.path.basename(name) != name:
            continue
        if not source or method not in ("symlink", "hardlink", "copy"):
            continue
        entries[name] = Entry(filename=name, source=source, method=method)
    return entries


def save_manifest(instance, entries: dict[str, Entry]) -> None:
    """Atomically persist the manifest (best effort -- never raises)."""
    payload = {
        "version": MANIFEST_VERSION,
        "entries": [
            {
                "filename": e.filename,
                "source": e.source,
                "method": e.method,
            }
            for e in sorted(entries.values(), key=lambda e: e.filename)
        ],
    }
    path = manifest_path(instance)
    try:
        os.makedirs(instance.dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        # A failed manifest write only costs us bookkeeping; the exposed files
        # are still valid, so never let this break a save or a launch.
        pass


# ---------------------------------------------------------------- helpers --
def normalise_folders(instance) -> list[str]:
    """Registered folders, normalised, de-duplicated, order preserved.

    Blank entries are dropped *before* abspath() (which would otherwise turn an
    empty string into the current working directory), and de-duplication is
    case-insensitive on Windows where the filesystem is.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in getattr(instance, "external_mod_dirs", None) or []:
        try:
            text = str(raw).strip()
        except (TypeError, ValueError):
            continue
        if not text:
            continue
        path = os.path.normpath(os.path.abspath(text))
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def list_jars(folder: str) -> list[str]:
    """Top-level ``.jar`` files in *folder* (no recursion, like the loaders)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(
        n for n in names
        if n.lower().endswith(MOD_SUFFIXES)
        and os.path.isfile(os.path.join(folder, n))
    )


def _with_suffix(name: str, suffix: str) -> str:
    """Insert *suffix* before the jar extension (``a.jar`` -> ``a (ext).jar``).

    ``.jar.disabled`` carries two extensions and both must be preserved, so a
    renamed copy stays disabled: ``a.jar.disabled`` -> ``a (ext).jar.disabled``.
    """
    lower = name.lower()
    if lower.endswith(".jar.disabled"):
        return f"{name[:-len('.jar.disabled')]}{suffix}.jar.disabled"
    if lower.endswith(".jar"):
        return f"{name[:-len('.jar')]}{suffix}.jar"
    return f"{name}{suffix}"


def _unique_name(mods_dir: str, name: str, taken: set[str]) -> str:
    """Pick a name that does not clobber an existing file.

    *taken* holds names claimed by this sync run; files already on disk that we
    do not manage are never overwritten.
    """
    def _free(candidate: str) -> bool:
        return (candidate not in taken
                and not os.path.lexists(os.path.join(mods_dir, candidate)))

    if _free(name):
        return name
    candidate = _with_suffix(name, EXTERNAL_SUFFIX)
    counter = 2
    while not _free(candidate):
        if counter > 999:  # pathological pile-up; give up gracefully
            return ""
        candidate = _with_suffix(name, f" ({counter}){EXTERNAL_SUFFIX}")
        counter += 1
    return candidate


def _expose(source: str, target: str) -> str:
    """Expose *source* at *target*; returns the method used.

    Order matters: a symlink is the most transparent, a hardlink needs no
    special privileges, and copying is the universal fallback. Raises OSError
    when even the copy fails (locked file, disk full, ...).
    """
    try:
        os.symlink(source, target)
        return "symlink"
    except (OSError, NotImplementedError, AttributeError):
        pass
    try:
        os.link(source, target)
        return "hardlink"
    except (OSError, NotImplementedError, AttributeError):
        pass
    shutil.copy2(source, target)
    return "copy"


def _needs_refresh(entry: Entry, target: str, source: str) -> bool:
    """True when an already-exposed file must be re-created.

    Symlinks/hardlinks resolve through the filesystem, so a healthy entry never
    needs work. Copies are compared against the source's size+mtime, which is
    what makes a modified file show up on the next launch.
    """
    if not os.path.lexists(target):
        return True
    if entry.method in ("symlink", "hardlink"):
        # A dangling link (source deleted or moved) is not usable.
        return not os.path.exists(target)
    try:
        src_stat = os.stat(source)
        dst_stat = os.stat(target)
    except OSError:
        return True
    return (src_stat.st_size != dst_stat.st_size
            or int(src_stat.st_mtime) != int(dst_stat.st_mtime))


def _unlink(path: str) -> None:
    """Remove an exposed file.

    Directory symlinks must be removed with ``rmdir`` on Windows; a missing
    file is fine. Raises OSError when the file is locked (e.g. game running).
    """
    try:
        if os.path.islink(path):
            try:
                os.rmdir(path)
                return
            except OSError:
                pass
        os.remove(path)
    except FileNotFoundError:
        pass


# ------------------------------------------------------------- public API --
def managed_entries(instance) -> dict[str, Entry]:
    """Entries this module currently manages (used by the Mods tab)."""
    return load_manifest(instance)


def source_for(instance, filename: str) -> Entry | None:
    """Return the :class:`Entry` for *filename* if it is externally managed."""
    return load_manifest(instance).get(os.path.basename(filename))


def folder_methods(instance) -> dict[str, set[str]]:
    """Map each registered folder to the link methods used for its jars.

    Drives the "linked" / "hardlink" / "copied" hint in the Mods tab.
    """
    by_folder: dict[str, set[str]] = {
        folder: set() for folder in normalise_folders(instance)
    }
    for entry in load_manifest(instance).values():
        by_folder.setdefault(os.path.dirname(entry.source), set()).add(
            entry.method
        )
    return by_folder


def add_folder(instance, path: str) -> str:
    """Register *path* as an external mods folder for *instance*.

    Rejects the instance's own mods folder (sync would otherwise recurse into
    its own output) and syncs immediately so the list reflects reality.
    """
    folder = os.path.normpath(os.path.abspath(str(path or "").strip()))
    if not folder or not os.path.isdir(folder):
        raise ExternalModsError("That folder does not exist.")

    mods_dir = os.path.normcase(os.path.abspath(instance.mods_dir))
    if os.path.normcase(folder) == mods_dir:
        raise ExternalModsError(
            "This is already the instance's own mods folder."
        )

    folders = normalise_folders(instance)
    if folder not in folders:
        folders.append(folder)
        instance.external_mod_dirs = folders
    sync(instance)
    return folder


def remove_folder(instance, path: str) -> SyncResult:
    """Unregister *path* and unlink everything it contributed.

    Source files are never touched -- only entries this module created.
    """
    folder = os.path.normpath(os.path.abspath(str(path or "").strip()))
    instance.external_mod_dirs = [
        f for f in normalise_folders(instance) if f != folder
    ]
    return sync(instance)


def clear(instance) -> SyncResult:
    """Unregister every external folder for this instance."""
    instance.external_mod_dirs = []
    return sync(instance)


def remove_entry(instance, filename: str) -> bool:
    """Unlink a single externally managed mod. True when something was removed.

    The file in the *source* folder is deliberately left alone.
    """
    name = os.path.basename(filename)
    entries = load_manifest(instance)
    entry = entries.pop(name, None)
    if entry is None:
        return False
    _unlink(os.path.join(instance.mods_dir, name))
    save_manifest(instance, entries)
    return True


def sync(instance) -> SyncResult:
    """Make ``mods_dir`` reflect every registered external folder.

    Idempotent and safe to call before every launch:

      * files whose source disappeared are unlinked,
      * entries belonging to a folder that is no longer registered are removed,
      * healthy symlinks/hardlinks are left untouched (no work per launch),
      * copies are refreshed when the source's size or mtime changed.

    Never raises -- problems are collected in ``SyncResult.errors`` so a broken
    folder can never stop the game from starting.
    """
    result = SyncResult()
    if instance is None:
        # LaunchThread only syncs when the caller supplied an instance.
        return result
    mods_dir = instance.mods_dir
    try:
        os.makedirs(mods_dir, exist_ok=True)
    except OSError as exc:
        result.errors.append(f"Cannot create mods folder: {exc}")
        return result

    entries = load_manifest(instance)
    folders = normalise_folders(instance)

    # 1. Forget entries whose source file is gone or whose folder was removed.
    keep: dict[str, Entry] = {}
    for name, entry in entries.items():
        if os.path.isfile(entry.source) and os.path.dirname(
                entry.source) in folders:
            keep[name] = entry
            continue
        try:
            _unlink(os.path.join(mods_dir, name))
            result.removed += 1
        except OSError as exc:
            # Locked (game running) -- keep it in the manifest so the next sync
            # retries instead of forgetting about the file.
            keep[name] = entry
            result.errors.append(f"Could not remove {name}: {exc}")

    # 2. Expose everything the still-registered folders contain.
    taken: set[str] = set()
    for folder in folders:
        if not os.path.isdir(folder):
            result.errors.append(f"Folder unavailable: {folder}")
            continue
        for jar_name in list_jars(folder):
            source = os.path.join(folder, jar_name)

            # Reuse the name we already recorded for this exact source file.
            # Without this, _unique_name() would see our own link sitting on
            # disk and rename it to "... (ext).jar" on every single sync.
            existing_name = next(
                (n for n, e in keep.items() if e.source == source), None
            )
            name = existing_name or _unique_name(mods_dir, jar_name, taken)
            if not name:
                result.skipped += 1
                continue
            taken.add(name)

            existing = keep.get(name)
            target = os.path.join(mods_dir, name)

            if existing is not None and existing.source == source:
                # Already ours: only touch it when it is broken or stale.
                if not _needs_refresh(existing, target, source):
                    continue
                try:
                    _unlink(target)
                except OSError as exc:
                    result.errors.append(f"Could not replace {name}: {exc}")
                    continue
            elif existing is None and os.path.lexists(target):
                # Something we do not manage owns this name. The renamer above
                # already tried to dodge it; give up rather than clobber.
                result.skipped += 1
                continue

            try:
                method = _expose(source, target)
            except OSError as exc:
                result.errors.append(f"Could not link {jar_name}: {exc}")
                continue
            keep[name] = Entry(filename=name, source=source, method=method)
            if method == "copy":
                result.copied += 1
            else:
                result.linked += 1

    save_manifest(instance, keep)
    return result

