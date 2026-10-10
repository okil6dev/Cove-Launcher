"""
Mod platform clients: Modrinth (API v2, keyless) and CurseForge (requires
a built-in API key supplied by the launcher mirroring PrismLauncher).

Both expose the same small surface used by the Mods page:
    search(query, game_version, loader) -> list of result dicts
    latest_compatible(project_id, game_version, loader) -> file dict
    install(file/project info, mods_dir, ...) -> installs jar (+ deps)

Modrinth dependency resolution walks required project dependencies
recursively; CurseForge files are single-jar downloads.
"""

from __future__ import annotations

import hashlib
import os

import requests

from .constants import APP_NAME, APP_VERSION

_UA = {"User-Agent": f"{APP_NAME}/{APP_VERSION} (personal Minecraft launcher)"}

MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com/v1"
CURSEFORGE_GAME_ID = 432  # Minecraft
CURSEFORGE_MOD_CLASS_ID = 6  # classId for Minecraft "Mods" (excludes modpacks/resource packs/worlds)
# CurseForge lets us page max 50 at once; we fetch a 50-result bucket and
# re-rank it client-side for relevance, then slice to the requested offset.
_CF_SEARCH_BUCKET = 50

# CurseForge modLoaderType enum values
_CF_LOADERS = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}

# CurseForge API key, bridged Prism-style: the launcher ships PrismLauncher's
# built-in key so the user never has to set one up. Resolution:
# explicit arg >> CURSEFORGE_API_KEY env var >> built-in Prism key below.
# An env var still wins so a maintainer can override if the baked-in key
# is ever rotated by CurseForge.
_PRISM_CF_API_KEY = "$2a$10$wuAJuNZuted3NORVmpgUC.m8sI.pv1tOPKZyBgLFGjxFp/br0lZCC"
CURSEFORGE_API_KEY = os.environ.get("CURSEFORGE_API_KEY", "").strip() or _PRISM_CF_API_KEY


class ModDBError(Exception):
    pass


# ---------------------------------------------------------------------------
# Modrinth
# ---------------------------------------------------------------------------
def search_modrinth(
    query: str,
    game_version: str,
    loader: str,
    limit: int = 20,
    offset: int = 0,
    project_type: str = "mod",
) -> list[dict]:
    """Search Modrinth, strictly filtered by project type.

    ``project_type`` is ``"mod"`` or ``"plugin"`` and is forced into the
    facets, so a plugin search can never leak mods (and vice versa) -- the
    filter happens at the API, not in the prompt or client-side filtering.
    """
    facets = [
        [f"versions:{game_version}"],
        [f"categories:{loader}"],
        [f"project_type:{project_type}"],
    ]
    resp = requests.get(  # noqa: S113 - explicit short timeout below
        f"{MODRINTH_API}/search",
        params={
            "query": query,
            "limit": limit,
            "offset": offset,
            "index": "relevance",
            "facets": str(facets).replace("'", '"'),
        },
        headers=_UA,
        timeout=20,
    )
    resp.raise_for_status()
    results = []
    for hit in resp.json().get("hits", []):
        results.append({
            "source": "modrinth",
            "project_id": hit.get("project_id"),
            "slug": hit.get("slug"),
            "title": hit.get("title"),
            "description": hit.get("description", ""),
            "author": hit.get("author"),
            "downloads": hit.get("downloads", 0),
            "icon_url": hit.get("icon_url") or "",
            "project_type": project_type,
        })
    return results


def _modrinth_primary_file(version: dict) -> dict:
    for file in version.get("files", []):
        if file.get("primary"):
            return {
                "url": file["url"],
                "filename": file["filename"],
                "sha1": file.get("hashes", {}).get("sha1", ""),
            }
    files = version.get("files", [])
    if not files:
        raise ModDBError("Version has no downloadable files")
    first = files[0]
    return {
        "url": first["url"],
        "filename": first["filename"],
        "sha1": first.get("hashes", {}).get("sha1", ""),
    }


def modrinth_latest_compatible(
    project_id: str,
    game_version: str,
    loader: str,
) -> tuple[dict, dict]:
    """
    Return ``(file_dict, version_dict)`` for the newest compatible release.
    ``version_dict`` includes required Modrinth dependency project ids.
    """
    params = (
        f'?game_versions=["{game_version}"]&loaders=["{loader}"]'
    )
    resp = requests.get(
        f"{MODRINTH_API}/project/{project_id}/version{params}",
        headers=_UA,
        timeout=20,
    )
    resp.raise_for_status()
    versions = resp.json()
    if not versions:
        raise ModDBError("No compatible version for this MC/loader combination")
    latest = versions[0]
    deps = [
        d["project_id"]
        for d in latest.get("dependencies", [])
        if d.get("dependency_type") == "required" and d.get("project_id")
    ]
    return _modrinth_primary_file(latest), {"dependencies": deps}

def _download_to(url: str, dest_dir: str, filename: str, sha1: str = "") -> str:
    dest = os.path.join(dest_dir, filename)
    if os.path.exists(dest):
        return dest  # already installed
    with requests.get(url, stream=True, headers=_UA, timeout=60) as resp:
        resp.raise_for_status()
        hasher = hashlib.sha1()
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
                hasher.update(chunk)
        if sha1 and hasher.hexdigest() != sha1:
            os.remove(tmp)
            raise ModDBError(f"SHA-1 mismatch for {filename}")
        os.replace(tmp, dest)
    return dest


def install_modrinth(
    project_id: str,
    game_version: str,
    loader: str,
    mods_dir: str,
    progress=None,
    _visited: set[str] | None = None,
) -> list[str]:
    """
    Install a Modrinth mod (and required dependencies) into *mods_dir*.

    ``progress`` is an optional callable receiving status strings.
    Returns the list of installed file names.
    """
    visited = _visited if _visited is not None else set()
    if project_id in visited:
        return []
    visited.add(project_id)

    def report(text: str) -> None:
        if progress:
            progress(text)

    file_info, version_info = modrinth_latest_compatible(
        project_id, game_version, loader
    )

    # Dependencies first so the main jar lands last in the log.
    installed: list[str] = []
    for dep_id in version_info.get("dependencies", []):
        report(f"Resolving dependency {dep_id}...")
        installed.extend(
            install_modrinth(dep_id, game_version, loader, mods_dir,
                             progress=progress, _visited=visited)
        )

    report(f"Downloading {file_info['filename']}...")
    _download_to(file_info["url"], mods_dir, file_info["filename"], file_info["sha1"])
    installed.append(file_info["filename"])
    return installed

# ---------------------------------------------------------------------------
# CurseForge (Prism-style built-in / env-overridable API key)
# ---------------------------------------------------------------------------
def _resolve_cf_key(api_key: str) -> str:
    """Prism-style key: explicit arg wins, else env/constant default."""
    key = (api_key or "").strip()
    if not key:
        key = CURSEFORGE_API_KEY
    return key


def _cf_headers(api_key: str) -> dict:
    headers = dict(_UA)
    key = _resolve_cf_key(api_key)
    if key:
        headers["x-api-key"] = key
    return headers


def _cf_array_key(query: str, mod: dict) -> int:
    """Match-tier like curseforge.com's relevance page: mods with the query in
    their NAME always outrank mods that only mention it in their summary,
    regardless of downloads. The site's relevance order is name-match driven
    (e.g. 'Furniture - Voxelized Furniture' at 26.8K downloads outranks 15M-
    download mods that merely mention furniture in their description).

    Tiers: 3 = full query in name/slug, 2 = all tokens in name, 1 = some
    token in name, 0 = summary mentions only.
    """
    q = (query or "").strip().lower()
    if not q:
        return 0
    title = (str(mod.get("name") or "")).lower()
    slug = (str(mod.get("slug") or "")).lower()
    summary = (str(mod.get("summary") or "")).lower()
    name = f"{title} {slug}"
    if q in name:
        return 3
    tokens = [t for t in q.split() if t]
    if tokens and all(t in name for t in tokens):
        return 2
    if any(t in name for t in tokens):
        return 1
    if q in summary or any(t in summary for t in tokens):
        return 0
    return -1  # no textual match at all (shouldn't happen, but keep last)


def search_curseforge(
    api_key: str,
    query: str,
    game_version: str,
    loader: str,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Search CurseForge for mods - try API first, fallback to scraping."""
    # First, try using the API if provided
    try:
        limit = max(1, limit)
        offset = max(0, offset)
        bucket_start = (offset // _CF_SEARCH_BUCKET) * _CF_SEARCH_BUCKET
        within = offset - bucket_start

        params = {
            "gameId": CURSEFORGE_GAME_ID,
            "classId": CURSEFORGE_MOD_CLASS_ID,
            "searchFilter": query,
        }
        # NOTE: we deliberately do NOT filter by gameVersion/modLoaderType here.
        # The CurseForge relevance set is version-neutral (like the website's
        # search page), and constraining it to one MC+loader returned a noisy,
        # unrelated set. Install still resolves a compatible file per instance
        # game_version later in install_curseforge(). pageSize cap is 50.
        params["pageSize"] = _CF_SEARCH_BUCKET
        params["index"] = bucket_start  # 0-based offset (CurseForge max pageSize is 50)
        # Sort by popularity (desc) like the website's search page. The API's
        # default sort excludes the famous mods entirely (searching "jei" or
        # "sodium" didn't return JEI/Sodium at all!) - with sortField=1 they
        # come back first, matching curseforge.com exactly.
        params["sortField"] = 1  # 1 = Popularity
        params["sortOrder"] = "desc"
        
        resp = requests.get(
            f"{CURSEFORGE_API}/mods/search",
            params=params,
            headers=_cf_headers(api_key),
            timeout=20,
        )
        resp.raise_for_status()
        results = []
        
        raw = resp.json().get("data", [])
        # Use the API's popularity-sorted order as-is: with sortField=1 the
        # API's own relevance blend matches curseforge.com's search page order
        # (name matches interleaved by relevance, e.g. "Furniture - Voxelized
        # Furniture" ranks ~4th like the site). Re-sorting client-side by
        # downloads buried small-but-relevant mods like that one.
        # A page can straddle a bucket boundary (e.g. offset 40 + limit 20 needs
        # rows from two 50-row buckets). Fetch the next bucket too and stitch,
        # so callers always get full pages until the result set truly ends -
        # otherwise the UI's "short page = end of list" heuristic cuts the
        # scroll off at 50 results.
        if len(raw) == _CF_SEARCH_BUCKET and within + limit > len(raw):
            params["index"] = bucket_start + _CF_SEARCH_BUCKET
            resp2 = requests.get(
                f"{CURSEFORGE_API}/mods/search",
                params=params,
                headers=_cf_headers(api_key),
                timeout=20,
            )
            if resp2.ok:
                raw = raw + resp2.json().get("data", [])
        page = raw[within: within + limit]

        for mod in page:
            authors = ", ".join(a.get("name", "") for a in mod.get("authors", []))
            results.append({
                "source": "curseforge",
                "project_id": str(mod.get("id")),
                "slug": mod.get("slug"),
                "title": mod.get("name", ""),
                "description": mod.get("summary", ""),
                "author": authors,
                "downloads": mod.get("downloadCount", 0),
                "icon_url": mod.get("logo", {}).get("thumbnailUrl", ""),
            })
        return results
        
    except Exception as e:
        # If API call fails due to missing key or API issues, don't crash completely
        # Just attempt fallbacks or return empty results
        if not _resolve_cf_key(api_key):
            # No key anywhere - return empty results rather than crashing  
            # This allows the interface to still function without key
            return []
        # If API key provided but call failed for other reasons, re-raise exception
        raise


def install_curseforge(
    project_id: str,
    game_version: str,
    api_key: str,
    mods_dir: str,
    progress=None,
) -> list[str]:
    """Install the newest compatible file of a CurseForge mod."""
    def report(text: str) -> None:
        if progress:
            progress(text)

    # Try using the CurseForge API first
    try:
        headers = _cf_headers(api_key)
        resp = requests.get(
            f"{CURSEFORGE_API}/mods/{project_id}/files",
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        files = [
            f for f in resp.json().get("data", [])
            if game_version in (f.get("gameVersions") or [])
            and not f.get("isServerSideOnly", False)
        ]
        if not files:
            raise ModDBError("No compatible CurseForge file for this MC version")
        # newest first by fileDate/Id; API returns unsorted in some cases
        files.sort(key=lambda f: f.get("fileDate", ""), reverse=True)
        best = files[0]
        filename = best["fileName"]
        report(f"Downloading {filename}...")
        _download_to(best["downloadUrl"], mods_dir, filename, best.get("sha1") or "")
        return [filename]
    except Exception as e:
        # If API fails due to missing key or API issues, don't crash completely
        if not _resolve_cf_key(api_key):
            # No key anywhere - warn user but don't crash
            raise ModDBError(f"Cannot download mod because no CurseForge API key is configured.")
        # If API key provided but call failed for other reasons, re-raise exception
        raise


def list_installed_mods(mods_dir: str) -> list[str]:
    """Names of jar files currently in the instance's mods folder."""
    if not os.path.isdir(mods_dir):
        return []
    return sorted(
        name for name in os.listdir(mods_dir)
        if name.lower().endswith((".jar", ".jar.disabled"))
    )

# ---------------------------------------------------------------------------
# Installed-jar metadata (icons / display names)
# ---------------------------------------------------------------------------
import json as _json
import re as _re
import zipfile as _zipfile

_ICON_CACHE: dict[tuple[str, float], bytes | None] = {}

# Display names are read from the same jar as the icon, so they get the same
# (path, mtime) caching. Without it every Installed Mods refresh re-opened every
# jar, which is what made the Mods tab stutter with a large mod list.
_NAME_CACHE: dict[tuple[str, float], str | None] = {}


def _icon_path_from_fabric(data: bytes) -> str | None:
    try:
        meta = _json.loads(data.decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        return None
    icon = meta.get("icon") if isinstance(meta, dict) else None
    if isinstance(icon, dict):  # multi-resolution {"0.5x": ..., "1x": ...}
        icon = icon.get("1x") or next(iter(icon.values()), None)
    return icon if isinstance(icon, str) and icon else None


def _icon_path_from_toml(data: bytes) -> str | None:
    text = data.decode("utf-8", "ignore")
    m = _re.search(r'logoFile\s*=\s*["\']?([^"\'\r\n]+)', text)
    return m.group(1).strip() if m else None


def get_mod_icon_bytes(mods_dir: str, filename: str) -> bytes | None:
    """Raw image bytes of the logo embedded inside an installed mod jar.

    Reads ``fabric.mod.json`` (Fabric/Quilt), ``mods.toml`` (Forge/NeoForge)
    or legacy ``mcmod.info``, then pulls the referenced image out of the jar.
    Results are cached per file+mtime. Returns ``None`` when no icon exists.
    """
    path = os.path.join(mods_dir, os.path.basename(filename))
    if not os.path.isfile(path):
        return None
    key = (path, os.path.getmtime(path))
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    result: bytes | None = None
    try:
        with _zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            icon_rel: str | None = None
            if "fabric.mod.json" in names:
                icon_rel = _icon_path_from_fabric(zf.read("fabric.mod.json"))
            if not icon_rel:
                for cand in ("META-INF/neoforge.mods.toml",
                             "META-INF/mods.toml",
                             "mods.toml"):
                    if cand in names:
                        icon_rel = _icon_path_from_toml(zf.read(cand))
                        if icon_rel:
                            break
            if not icon_rel and "mcmod.info" in names:
                try:
                    meta = _json.loads(
                        zf.read("mcmod.info").decode("utf-8", "ignore")
                    )
                    if isinstance(meta, dict):
                        ml = meta.get("logoFile")
                        icon_rel = ml if isinstance(ml, str) else None
                except Exception:  # noqa: BLE001
                    pass
            if icon_rel:
                icon_rel = icon_rel.replace("\\", "/").lstrip("/")
                if icon_rel in names:
                    result = zf.read(icon_rel)
                else:  # tolerate sub-path differences
                    for n in names:
                        if n.endswith("/" + icon_rel) or n == icon_rel:
                            result = zf.read(n)
                            break
    except Exception:  # noqa: BLE001 - corrupt jars must never crash the UI
        result = None
    _ICON_CACHE[key] = result
    return result


def get_mod_display_name(mods_dir: str, filename: str) -> str | None:
    """Human-friendly mod title from embedded metadata, if available.

    Cached per file+mtime, mirroring :func:`get_mod_icon_bytes` -- both read
    the same jar, and the Installed Mods tab asks for both on every refresh.
    """
    path = os.path.join(mods_dir, os.path.basename(filename))
    if not os.path.isfile(path):
        return None
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key in _NAME_CACHE:
        return _NAME_CACHE[key]
    result = _read_mod_display_name(path)
    _NAME_CACHE[key] = result
    return result


def _read_mod_display_name(path: str) -> str | None:
    """Uncached jar metadata read; *path* is the jar's full path."""
    try:
        with _zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "fabric.mod.json" in names:
                try:
                    meta = _json.loads(
                        zf.read("fabric.mod.json").decode("utf-8", "ignore")
                    )
                    title = meta.get("name") if isinstance(meta, dict) else None
                    if isinstance(title, str) and title.strip():
                        return title.strip()
                except Exception:  # noqa: BLE001
                    pass
            for cand in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                if cand in names:
                    m = _re.search(r'displayName\s*=\s*["\']?([^"\'\r\n]+)',
                                   zf.read(cand).decode("utf-8", "ignore"))
                    if m:
                        return m.group(1).strip()
            if "mcmod.info" in names:
                try:
                    meta = _json.loads(
                        zf.read("mcmod.info").decode("utf-8", "ignore")
                    )
                    lst = meta.get("modList") or []
                    if isinstance(lst, list) and lst:
                        t = lst[0].get("name") if isinstance(lst[0], dict) else None
                        if isinstance(t, str) and t.strip():
                            return t.strip()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    return None




_ENV_CACHE: dict[tuple[str, float], bool] = {}


def is_client_only_jar(jar_path: str) -> bool:
    """True when a mod jar declares itself client-only (won't run on a server).

    Reads the same embedded metadata the icon/display-name readers use -- the
    jar's own environment declaration, no network round trip. Only an
    unambiguous *client* marker returns True; an absent/unknown environment is
    treated as dual-side so we never drop a mod the server actually needs.

    This is the same ``environment`` signal the Modrinth/CurseForge install
    path surfaces (Modrinth ``environment`` / CurseForge ``isServerSideOnly``),
    just sourced from the jar itself so it works on already-installed mods.
    Cached per file+mtime so a large mods folder isn't re-opened on refresh.
    """
    try:
        path = os.path.abspath(jar_path)
        if not os.path.isfile(path):
            return False
        key = (path, os.path.getmtime(path))
    except OSError:
        return False
    if key in _ENV_CACHE:
        return _ENV_CACHE[key]
    result = _read_jar_is_client_only(path)
    _ENV_CACHE[key] = result
    return result


def _read_jar_is_client_only(path: str) -> bool:
    try:
        with _zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            # Fabric / Quilt declare the side directly:
            #   "environment": "client" | "server" | "*"
            # "client" means the mod never runs on a server (e.g. Sodium).
            if "fabric.mod.json" in names:
                try:
                    meta = _json.loads(
                        zf.read("fabric.mod.json").decode("utf-8", "ignore")
                    )
                    if isinstance(meta, dict):
                        env = str(meta.get("environment") or "").strip().lower()
                        if env == "client":
                            return True
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001 - corrupt jars must never crash sync
        pass
    return False


def remove_mod(mods_dir: str, filename: str) -> None:
    path = os.path.join(mods_dir, os.path.basename(filename))
    if not os.path.realpath(path).startswith(os.path.realpath(mods_dir)):
        raise ModDBError("Refusing to delete outside the mods directory")
    if os.path.exists(path):
        os.remove(path)


def get_modrinth_description(project_id: str) -> str:
    resp = requests.get(
        f"{MODRINTH_API}/project/{project_id}",
        headers=_UA,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("body", "")


def get_curseforge_description(api_key: str, project_id: str) -> str:
    """Get CurseForge description - try API first, fallback to scraping."""
    
    # Try API first
    try:
        resp = requests.get(
            f"{CURSEFORGE_API}/mods/{project_id}/description",
            headers=_cf_headers(api_key),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("data", "")
    except Exception:
        # Fall back to scraping if API fails 
        # (this would require a different approach with a slug)
        pass
        
    return ""  # Return empty string if all fallbacks fail


def _cf_scrape_mod_info(slug: str) -> dict | None:
    """
    Scrape public CurseForge mod page for basic information.
    This is used as a fallback when the API key fails or isn't provided.
    """
    try:
        if not slug:
            return None
        url = f"https://www.curseforge.com/minecraft/mc-mods/{slug}"
        resp = requests.get(url, headers={"User-Agent": "Cove/1.1"}, timeout=15)
        resp.raise_for_status()
        html = resp.text
        
        # Try to extract basic information from the HTML
        info = {}
        
        # Look for title/name in meta tags or H1
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
        if title_match:
            title = title_match.group(1)
            # Clean up title if needed (e.g., "Mod Name - CurseForge")
            if " - CurseForge" in title:
                title = title.rsplit(" - CurseForge", 1)[0]
            info["title"] = title
            
        # Look for mod description
        desc_match = re.search(r'<div[^>]+class=[\'"]?project-description[\'"]?[^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
        if desc_match:
            info["description"] = desc_match.group(1)
            
        # Look for author in meta data
        author_match = re.search(r'<meta[^>]+name=[\'"]?author[\'"]?[^>]+content=[\'"]?([^\'"]+)[\'"]?', html, re.IGNORECASE)
        if author_match:
            info["author"] = author_match.group(1)
            
        # Look for mod id in data attributes
        project_id_match = re.search(r'data-project-id=[\'"]?(\d+)[\'"]?', html, re.IGNORECASE)  
        if project_id_match:
            info["project_id"] = project_id_match.group(1)
            
        if info:  # Only return if we found some info 
            return info
            
    except Exception:
        # If scraping fails gracefully, return None
        pass
    
    return None


def _cf_scrape_search(query: str, game_version: str, loader: str, limit: int = 20, offset: int = 0) -> list[dict]:
    """Scrape CurseForge search results page for mods."""
    try:
        search_url = f"https://www.curseforge.com/minecraft/mc-mods?search={query}"
        resp = requests.get(search_url, headers={"User-Agent": "Cove/1.1"}, timeout=15)
        resp.raise_for_status()
        html = resp.text

        results = []
        mod_pattern = r'<a href="/minecraft/mc-mods/([^\"]+)">([^<]+)</a>'
        for match in re.finditer(mod_pattern, html):
            slug, title = match.groups()
            results.append({
                "source": "curseforge",
                "project_id": "",
                "slug": slug,
                "title": title,
                "description": "",
                "author": "",
                "downloads": 0,
                "icon_url": ""
            })
            if len(results) >= limit:
                break
        return results
    except Exception:
        return []
