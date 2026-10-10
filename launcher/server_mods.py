"""Server mods sync -- push instance mods/ jars into a server mods/ folder.

Mirrors external_mods (Add Folder method) but inverted: instance -> server.
Files are exposed as symlink -> hardlink -> copy. Only manifest entries
(<server>/server-mods.json) are ever removed/overwritten. Qt-free.
"""
from __future__ import annotations
import json
import os
import shutil
from dataclasses import dataclass

MANIFEST_NAME = "server-mods.json"
MANIFEST_VERSION = 1
MOD_SUFFIXES = (".jar", ".jar.disabled")
SYNC_SUFFIX = " (synced)"

__all__ = ["Entry", "ServerModsError", "SyncResult", "linked_instance",
    "link", "unlink", "sync", "status", "list_jars", "load_manifest",
    "save_manifest", "manifest_path", "managed_entries", "remove_entry",
    "method_summary"]


class ServerModsError(Exception):
    pass


@dataclass
class Entry:
    filename: str
    source: str
    method: str
    def describe(self) -> str:
        return {"symlink": "linked", "hardlink": "hardlink",
                "copy": "copied"}.get(self.method, self.method)


@dataclass
class SyncResult:
    linked: int = 0
    copied: int = 0
    refreshed: int = 0
    removed: int = 0
    skipped: int = 0
    errors: list[str] | None = None
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
        return ", ".join(bits) if bits else "already in sync"
def manifest_path(server) -> str:
    return os.path.join(server.dir, MANIFEST_NAME)


def load_manifest(server) -> dict:
    try:
        with open(manifest_path(server), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        items = raw.get("entries", []) if isinstance(raw, dict) else []
        out = {}
        for item in items:
            try:
                filename = os.path.basename(str(item["filename"]))
                source = str(item["source"])
                method = str(item["method"])
            except (KeyError, TypeError, ValueError):
                continue
            if (not filename or filename in (".", "..")
                    or "/" in filename or "\\" in filename):
                continue
            if method not in ("symlink", "hardlink", "copy"):
                continue
            if not os.path.isabs(source):
                continue
            out[filename] = Entry(filename=filename, source=source,
                                  method=method)
        return out
    except (OSError, ValueError):
        return {}


def _linked_name(server) -> str:
    try:
        with open(manifest_path(server), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            return str(raw.get("instance") or "")
    except (OSError, ValueError):
        pass
    return ""


def save_manifest(server, entries: dict, instance_name=None) -> None:
    if instance_name is None:
        instance_name = _linked_name(server)
    payload = {"version": MANIFEST_VERSION, "instance": instance_name,
               "entries": [{"filename": e.filename, "source": e.source,
                            "method": e.method} for e in entries.values()]}
    try:
        os.makedirs(server.dir, exist_ok=True)
    except OSError:
        return
    tmp = manifest_path(server) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, manifest_path(server))
    except OSError:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def linked_instance(server):
    name = _linked_name(server)
    return name or None


def managed_entries(server) -> dict:
    return load_manifest(server)


def method_summary(server) -> str:
    from collections import Counter
    counts = Counter()
    for entry in load_manifest(server).values():
        counts[entry.describe()] += 1
    if not counts:
        return ""
    order = ("linked", "hardlink", "copied")
    bits = [f"{counts[k]} {k}" for k in order if counts.get(k)]
    bits += [f"{v} {k}" for k, v in counts.items() if k not in order]
    return " • ".join(bits)


def list_jars(folder: str) -> list:
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(n for n in names if n.lower().endswith(MOD_SUFFIXES)
                  and os.path.isfile(os.path.join(folder, n)))


def _with_suffix(name: str, suffix: str) -> str:
    lower = name.lower()
    if lower.endswith(".jar.disabled"):
        return f"{name[:-len('.jar.disabled')]}{suffix}.jar.disabled"
    if lower.endswith(".jar"):
        return f"{name[:-len('.jar')]}{suffix}.jar"
    return f"{name}{suffix}"


def _unique_name(mods_dir: str, name: str, taken: set) -> str:
    def _free(candidate: str) -> bool:
        return (candidate not in taken
                and not os.path.lexists(os.path.join(mods_dir, candidate)))
    if _free(name):
        return name
    candidate = _with_suffix(name, SYNC_SUFFIX)
    counter = 2
    while not _free(candidate):
        if counter > 999:
            return ""
        candidate = _with_suffix(name, f" ({counter}){SYNC_SUFFIX}")
        counter += 1
    return candidate


def _expose(source: str, target: str) -> str:
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
    if not os.path.lexists(target):
        return True
    if entry.method in ("symlink", "hardlink"):
        return not os.path.exists(target)
    try:
        src_stat = os.stat(source)
        dst_stat = os.stat(target)
    except OSError:
        return True
    return (src_stat.st_size != dst_stat.st_size
            or int(src_stat.st_mtime) != int(dst_stat.st_mtime))


def _unlink(path: str) -> None:
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


def _instance_jar_sources(instance) -> list:
    mods_dir = instance.mods_dir
    out = []
    for jar_name in list_jars(mods_dir):
        candidate = os.path.join(mods_dir, jar_name)
        source = candidate
        try:
            from . import external_mods as _em
            ext = _em.source_for(instance, jar_name)
            if ext is not None and os.path.isfile(ext.source):
                source = ext.source
        except Exception:
            pass
        # Exclude client-only mods (e.g. Sodium) so they never land on the
        # server. This is the same "environment" signal the Modrinth/
        # CurseForge install path uses to skip mods a server can't run,
        # sourced here from the jar's own fabric.mod.json so it works on
        # already-installed mods with no network round trip.
        try:
            from . import moddb as _moddb
            if _moddb.is_client_only_jar(source):
                continue
        except Exception:
            pass
        out.append((jar_name, source))
    return out
def sync(server, instance):
    result = SyncResult()
    mods_dir = server.mods_dir
    try:
        os.makedirs(mods_dir, exist_ok=True)
    except OSError as exc:
        result.errors.append(f"Cannot create server mods folder: {exc}")
        return result
    entries = load_manifest(server)
    sources = _instance_jar_sources(instance)
    wanted = {src for _, src in sources}
    keep = {}
    for name, entry in entries.items():
        if entry.source in wanted and os.path.isfile(entry.source):
            keep[name] = entry
            continue
        try:
            _unlink(os.path.join(mods_dir, name))
            result.removed += 1
        except OSError as exc:
            keep[name] = entry
            result.errors.append(f"Could not remove {name}: {exc}")
    taken = set()
    for jar_name, source in sources:
        existing_name = next(
            (n for n, e in keep.items() if e.source == source), None)
        name = existing_name or _unique_name(mods_dir, jar_name, taken)
        if not name:
            result.skipped += 1
            continue
        taken.add(name)
        existing = keep.get(name)
        target = os.path.join(mods_dir, name)
        if existing is not None and existing.source == source:
            if not _needs_refresh(existing, target, source):
                continue
            try:
                _unlink(target)
            except OSError as exc:
                result.errors.append(f"Could not replace {name}: {exc}")
                continue
        elif existing is None and os.path.lexists(target):
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
    save_manifest(server, keep, instance_name=instance.name)
    return result


def link(server, instance):
    if not os.path.isdir(instance.mods_dir) or not list_jars(instance.mods_dir):
        raise ServerModsError(
            f"Instance '{instance.name}' has no mods to sync yet.")
    try:
        supports = bool(server.supports_mods)
    except AttributeError:
        supports = True
    if not supports:
        raise ServerModsError(
            f"Server '{server.name}' does not support mods "
            f"({getattr(server, 'server_type', 'unknown')}).")
    return sync(server, instance)


def unlink(server):
    result = SyncResult()
    entries = load_manifest(server)
    mods_dir = server.mods_dir
    keep = {}
    for name, entry in entries.items():
        try:
            _unlink(os.path.join(mods_dir, name))
            result.removed += 1
        except OSError as exc:
            keep[name] = entry
            result.errors.append(f"Could not remove {name}: {exc}")
    save_manifest(server, keep, instance_name="")
    if not keep and not _linked_name(server):
        try:
            if os.path.isfile(manifest_path(server)):
                os.remove(manifest_path(server))
        except OSError:
            pass
    return result


def remove_entry(server, filename: str) -> bool:
    name = os.path.basename(filename)
    entries = load_manifest(server)
    entry = entries.pop(name, None)
    if entry is None:
        return False
    _unlink(os.path.join(server.mods_dir, name))
    save_manifest(server, entries)
    return True


def status(server, instance=None) -> str:
    linked = linked_instance(server)
    if not linked:
        return "Not linked."
    entries = load_manifest(server)
    detail = method_summary(server)
    base = f"{len(entries)} mod(s) from '{linked}'"
    return f"{base} -- {detail}" if detail else base

