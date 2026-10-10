"""
Shared constants for Cove Launcher.

All tunable values live here so behaviour can be adjusted without touching
logic code. Note on Minecraft's *year based* versioning scheme (e.g. ``26.2``):
the launcher never parses version IDs for ordering -- it relies purely on the
``releaseTime`` field reported by Mojang's manifest.

This module also owns the on-disk layout of the launcher's own data
(instances, vault, shared asset store). Directories are created lazily by
the modules that use them.
"""

from datetime import datetime
import os
import sys
from pathlib import Path

# --- Application metadata -------------------------------------------------
APP_NAME: str = "Cove Launcher"
APP_VERSION: str = "1.1.0"


def asset_path(name: str) -> str:
    """Absolute path to a bundled asset (works in dev and frozen exe)."""
    base = getattr(sys, "_MEIPASS", None) or str(
        Path(__file__).resolve().parent.parent
    )
    path = os.path.join(base, "assets", name)
    if not os.path.isfile(path) and getattr(sys, "frozen", False):
        # Fallback: assets shipped next to the .exe (onedir builds / manual copies).
        path = os.path.join(os.path.dirname(sys.executable), "assets", name)
    return path


ICON_FILE: str = "cove_icon.ico"
ICON_PNG: str = "cove_icon.png"

# Name reported to Mojang/Microsoft telemetry via the launch options.
LAUNCHER_ID_FOR_JVM: str = "Cove"

# --- Supported Minecraft version window -----------------------------------
MIN_SUPPORTED_RELEASE: str = "1.21.1"
FALLBACK_MIN_RELEASE_TIME: datetime = datetime(2024, 8, 8)

# --- Memory allocation ----------------------------------------------------
MIN_RAM_GB: int = 2
DEFAULT_RAM_GB: int = 4
BYTES_PER_GIB: int = 1024**3

# --- Mod loaders ----------------------------------------------------------
MODLOADER_VANILLA: tuple[str, str] = ("vanilla", "Vanilla")
MODLOADER_FABRIC: tuple[str, str] = ("fabric", "Fabric")
MODLOADER_NEOFORGE: tuple[str, str] = ("neoforge", "NeoForge")
MODLOADERS: tuple[tuple[str, str], ...] = (
    MODLOADER_VANILLA,
    MODLOADER_FABRIC,
    MODLOADER_NEOFORGE,
)
LATEST_LOADER_BUILD_SENTINEL: str = "__latest__"

# --- Launcher data locations ---------------------------------------------
APP_DATA_DIR: str = os.path.join(
    os.getenv("APPDATA") or os.path.join(str(Path.home()), "AppData", "Roaming"),
    "Cove",
)
INSTANCES_DIR: str = os.path.join(APP_DATA_DIR, "instances")
INSTANCE_INDEX_PATH: str = os.path.join(APP_DATA_DIR, "instances.json")
# Server folders live right beside the instances: one folder per named server,
# holding the imported jar plus its mods/ and plugins/ subfolders.
SERVERS_DIR: str = os.path.join(APP_DATA_DIR, "servers")
SERVER_INDEX_PATH: str = os.path.join(APP_DATA_DIR, "servers.json")
VAULT_PATH: str = os.path.join(APP_DATA_DIR, "vault.dat")
SHARED_ASSETS_DIR: str = os.path.join(APP_DATA_DIR, "shared-assets")

# --- Skin editor -----------------------------------------------------------
# Local skin/cape library managed by the Skins tab. Every uploaded skin/cape
# is copied into these folders so the library survives restarts; an index
# (active skin, active cape, model) is kept in the JSON manifest.
SKINS_DIR: str = os.path.join(APP_DATA_DIR, "skins")
CAPES_DIR: str = os.path.join(SKINS_DIR, "capes")
SKINS_MANIFEST_PATH: str = os.path.join(SKINS_DIR, "manifest.json")

# Base hosts for changing a *server-side* skin/cape on the active account.
#   Microsoft -> api.minecraftservices.com (official Mojang services API)
#   Ely.by    -> skinsystem.ely.by
MINECRAFT_SERVICES_URL: str = "https://api.minecraftservices.com"
SKINSYSTEM_URL: str = "https://skinsystem.ely.by"

# Modrinth project slug for CustomSkinLoader, used to auto-install the mod
# into an instance's mods/ folder so locally-saved skins/capes render in-game
# (needed for offline accounts and as a fallback for MS/Ely.by servers that
# don't honour server-side textures).
CUSTOM_SKIN_LOADER_SLUG: str = "customskinloader"

# Bundled files shipped with the launcher itself (e.g. packaged mods for the
# per-instance toggles in the editor). Works in dev (repo layout) and frozen
# (PyInstaller _MEIPASS) builds.
INTERNAL_FILES_DIR: str = os.path.join(
    getattr(sys, "_MEIPASS", None) or str(Path(__file__).resolve().parent.parent),
    "launcher",
    "internalfiles",
)

# --- Microsoft login ------------------------------------------------------
MS_LOGIN_PORT: int = 8765
MS_REDIRECT_URI: str = f"http://localhost:{MS_LOGIN_PORT}"
MS_LOGIN_TIMEOUT_SECONDS: int = 300

# Built-in fallback OAuth client id, borrowed from the open-source
# PrismLauncher project (GPLv3). Their public app registration accepts any
# personal Microsoft account and allows http://localhost redirects on any
# port, so our MS_REDIRECT_URI works with it out of the box. A user's own
# client id entered in Settings always takes precedence over this default.
DEFAULT_AZURE_CLIENT_ID: str = "c36a9fb6-4f2a-41ff-90bd-ae7cc92031eb"

# --- Java selection modes --------------------------------------------------
JAVA_MODE_AUTO: str = "auto"        # Mojang bundled runtime (recommended)
JAVA_MODE_GRAALVM: str = "graalvm"  # GraalVM CE — faster JIT for better FPS
JAVA_MODE_PATH: str = "path"        # system 'java' from PATH
JAVA_MODE_CUSTOM: str = "custom"    # explicit executable path per instance
JAVA_MODES: tuple[tuple[str, str], ...] = (
    (JAVA_MODE_AUTO, "Auto (Mojang runtime)"),
    (JAVA_MODE_GRAALVM, "GraalVM (best FPS) ⚡"),
    (JAVA_MODE_PATH, "System PATH"),
)

# --- Mod sources -----------------------------------------------------------
SOURCE_MODRINTH: str = "modrinth"
SOURCE_CURSEFORGE: str = "curseforge"
# Not a real mod site: selects the AI assistant view on the Mods page.
SOURCE_AI: str = "ai"
# Selects the server-plugin browser on the Mods page (Modrinth
# project_type=plugin). Distinct from SOURCE_AI so the rail keeps both tabs.
SOURCE_PLUGINS: str = "plugins"

# Strict project-type filter for searches (browser + AI). The two never mix:
# a "mod" query must never return a plugin, and vice versa.
PROJECT_TYPE_MOD: str = "mod"
PROJECT_TYPE_PLUGIN: str = "plugin"

# --- Server software -------------------------------------------------------
# server_type -> (display name, supports_mods, supports_plugins).
# "Dual" servers run mods AND plugins in the same folder:
#   Mohist/Cardboard = Forge/Fabric + plugins, Arclight = Forge/Fabric +
#   plugins, Magma/Banner = Forge + plugins, Granite/Sunlight = Fabric-ish
#   hybrids. Detection lives in launcher/servers.py.
SERVER_TYPES: dict[str, tuple[str, bool, bool]] = {
    # Vanilla runs neither mods nor plugins (plugin support needs the
    # Bukkit family; mod support needs Fabric/Forge).
    "vanilla":    ("Vanilla", False, False),
    "spigot":     ("Spigot", False, True),
    "paper":      ("Paper", False, True),
    "purpur":     ("Purpur", False, True),
    "pufferfish": ("Pufferfish", False, True),
    "folia":      ("Folia", False, True),
    "velocity":   ("Velocity (proxy)", False, False),
    "bungeecord": ("BungeeCord (proxy)", False, False),
    "waterfall":  ("Waterfall (proxy)", False, False),
    "fabric":     ("Fabric", True, False),
    "forge":      ("Forge", True, False),
    "neoforge":   ("NeoForge", True, False),
    # -- dual loaders (mods + plugins) --
    "mohist":     ("Mohist (dual)", True, True),
    "magma":      ("Magma (dual)", True, True),
    "arclight":   ("Arclight (dual)", True, True),
    "cardboard":  ("Cardboard (dual)", True, True),
    "banner":     ("Banner (dual)", True, True),
    "granite":    ("Granite (dual)", True, True),
    "sunlight":   ("Sunlight (dual)", True, True),
    "limelight":  ("Limelight (dual)", True, True),
    "unknown":    ("Unknown", False, False),
}

# Modrinth loader categories for plugin searches (project_type=plugin).
PLUGIN_LOADERS: tuple[tuple[str, str], ...] = (
    ("paper", "Paper"),
    ("spigot", "Spigot"),
    ("bukkit", "Bukkit"),
    ("purpur", "Purpur"),
    ("folia", "Folia"),
    ("velocity", "Velocity"),
    ("bungeecord", "BungeeCord"),
    ("waterfall", "Waterfall"),
)

# --- Ely.by login (OAuth2 device-code flow) --------------------------------
# Ely.by runs a Mojang-style authentication service for third-party accounts.
# We use their OAuth2 *device code* flow (https://docs.ely.by/en/api/auth/oauth2):
# the user opens the verification URI in any browser and types a short code,
# which avoids embedding a registered callback URL in a desktop app.
#
# The device-code endpoints only accept client ids known to the ely.by OAuth
# server. ``ely`` is the official id from ely.by's own documentation (their
# public demo client) and is accepted out of the box; launchers that register
# their own application can override it in Settings -> Ely.by client ID.
ELY_CLIENT_ID: str = "ely"
ELY_SCOPES: str = "account_info offline_access minecraft_server_session"
# With these scopes the *OAuth access token doubles as the Minecraft session
# token* (minecraft_server_session), and account_info yields the player uuid.
ELY_DEVICE_CODE_URL: str = "https://account.ely.by/api/oauth2/v1/devicecode"
ELY_TOKEN_URL: str = "https://account.ely.by/api/oauth2/v1/token"
ELY_ACCOUNT_INFO_URL: str = "https://account.ely.by/api/account/v1/info"
ELY_DEVICE_GRANT: str = "urn:ietf:params:oauth:grant-type:device_code"
ELY_LOGIN_TIMEOUT_SECONDS: int = 600  # server-set device_code expiry ceiling

# authlib-injector patches the vanilla authlib so the game talks to Ely.by's
# session servers instead of Mojang's. Without it an ely.by account cannot
# join online-mode servers or download skin textures.

ELY_AUTHLIB_INJECTOR_ALIAS: str = "ely.by"
ELY_AUTHLIB_INJECTOR_FILENAME: str = "authlib-injector.jar"
# The JAR asset name used to be a flat "authlib-injector.jar" but newer
# releases version it (e.g. "authlib-injector-1.2.8.jar"), so a static
# /latest/download/ URL 404s.  Instead we resolve the real asset URL from
# the GitHub API at runtime (see elyby.ensure_authlib_injector).
ELY_AUTHLIB_INJECTOR_URL: str = "https://api.github.com/repos/yushijinhun/authlib-injector/releases/latest"

