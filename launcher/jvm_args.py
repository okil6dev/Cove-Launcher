"""Custom JVM arguments: what the user typed, and the flags the launcher adds.

Two jobs, because they are the same problem seen twice.

*Splitting* -- the box under "Custom JVM arguments" holds a command line, and
every argument in it has to reach the JVM as the user wrote it. ``shlex`` is
the obvious tool and the wrong one on Windows: in its default POSIX mode a
backslash is an escape character, so ``-Djava.library.path=C:\\Users\\me``
silently becomes ``C:Usersme``, quoting is stripped even when the JVM's own
flag parser needs it, and an unbalanced quote or a trailing backslash raises
``ValueError`` -- out of a Qt slot, which aborts the app instead of saving.
Everything here therefore follows Windows' rules: a backslash is a character,
quotes only group a value that contains spaces, and nothing is ever an error.

*ZGC* -- which flags actually turn on the Z Garbage Collector depends on the
JDK that will run the game. ZGC is a product feature from JDK 15 (11-14 need
``-XX:+UnlockExperimentalVMOptions``); generational ZGC is opt-in through
``-XX:+ZGenerational`` on 21-23, the default from 24 (where the flag is
obsolete and merely warns), and does not exist before 21. See :func:`zgc_flags`.
"""

from __future__ import annotations

import json
import os
import re

#: Quote characters that group a value containing spaces. Both are accepted
#: because the launcher's boxes invite pasting from documentation, where
#: either may appear; neither survives into the argument itself.
_QUOTES = ('"', "'")

#: ``JAVA_VERSION="21.0.4"`` / ``JAVA_VERSION="1.8.0_402"`` from a JDK release
#: file. The 1.x form (every JDK before 9) means feature version *minor*.
_RELEASE_RE = re.compile(r'JAVA_VERSION="?(\d+)(?:\.(\d+))?')

#: ``-XX:+UseZGC`` and friends -- the flags that pick a garbage collector.
_COLLECTOR_RE = re.compile(
    r"^-XX:\+Use(Serial|Parallel|G1|Z|Shenandoah|Epsilon)GC$", re.I
)

#: A version-named folder, e.g. ``jdks/graalvm-java21/bin/java.exe`` or
#: ``jdk-17.0.2``. Only a last resort when there is no release file.
_PATH_VERSION_RE = re.compile(
    r"(?:java|jdk|jre|openjdk|temurin|zulu)[-_]?(\d{1,2})(?!\d)", re.I
)


# ── what the user typed ──────────────────────────────────────────────────────

def split(raw: str) -> list[str]:
    """Split a typed argument line into the list the JVM should receive.

    Whitespace separates arguments; ``"`` or ``'`` groups a value that
    contains whitespace and is removed, exactly as a shell would, so the JVM
    receives one argv element -- ``-Dname=my launcher`` written as
    ``"-Dname=my launcher"`` arrives as the single argument ``-Dname=my
    launcher``. A backslash is never an escape, so Windows paths keep their
    separators. An unclosed quote is not an error: the group simply runs to
    the end of the line, which is the only reading that cannot lose text.
    """
    args: list[str] = []
    token: list[str] = []
    quote: str | None = None
    started = False
    for char in raw or "":
        if quote is not None:
            if char == quote:
                quote = None
            else:
                token.append(char)
            continue
        if char in _QUOTES:
            quote = char
            started = True
        elif char.isspace():
            if started:
                args.append("".join(token))
                token = []
                started = False
        else:
            started = True
            token.append(char)
    if started:
        args.append("".join(token))
    return args


def requested_collector(args) -> str | None:
    """The garbage collector a list of arguments selects, lower case, or ``None``.

    ``-XX:+UseZGC`` is ``"z"``, ``-XX:+UseG1GC`` is ``"g1"``. The launcher adds
    its own tuning flags for one collector, and naming two of them is not a
    harmless duplicate: HotSpot refuses to start with "Multiple garbage
    collectors selected".
    """
    for arg in args or []:
        match = _COLLECTOR_RE.match(str(arg).strip())
        if match:
            return match.group(1).lower()
    return None


def join(args) -> str:
    """Render *args* back into the editor box -- the inverse of :func:`split`.

    An argument that contains whitespace is quoted, so save -> reopen ->
    save is byte-for-byte stable instead of quietly splitting one argument
    into two on the second visit.
    """
    parts = []
    for arg in args or []:
        text = str(arg)
        if any(char.isspace() for char in text):
            parts.append(f'"{text}"')
        else:
            parts.append(text)
    return " ".join(parts)


# ── the JDK that will run the game ───────────────────────────────────────────

def java_major(java_path: str | None) -> int | None:
    """The feature version of the JDK behind *java_path* (``21``), or ``None``.

    Read from the JDK's own ``release`` file, which is where the JDK records
    it; no process is spawned, so this is cheap and safe to call while a game
    is starting. A version-named folder is used as a fallback, because older
    or repackaged runtimes occasionally ship without a release file.
    """
    if not java_path:
        return None
    home = os.path.dirname(os.path.dirname(os.path.abspath(java_path)))
    try:
        with open(os.path.join(home, "release"), encoding="utf-8",
                  errors="replace") as handle:
            text = handle.read()
    except OSError:
        text = ""
    match = _RELEASE_RE.search(text)
    if match:
        major, minor = int(match.group(1)), match.group(2)
        # 1.8.0_402 is Java 8; 21.0.4 is Java 21.
        if major == 1 and minor is not None:
            return int(minor)
        return major
    return _major_from_name(home)


def _major_from_name(path: str) -> int | None:
    """Feature version guessed from a version-named folder, else ``None``."""
    match = _PATH_VERSION_RE.search(os.path.basename(os.path.normpath(path)))
    if match is None:
        match = _PATH_VERSION_RE.search(path)
    if match is None:
        return None
    value = int(match.group(1))
    return value if 5 <= value <= 99 else None


def major_from_version_json(minecraft_dir: str | None,
                            version_id: str | None) -> int | None:
    """``javaVersion.majorVersion`` of an installed version json, else ``None``.

    This is the runtime Mojang will hand to the game, so it is the answer for
    an instance that does not pin its own ``java_path``.
    """
    if not minecraft_dir or not version_id:
        return None
    path = os.path.join(
        minecraft_dir, "versions", str(version_id), f"{version_id}.json"
    )
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
        major = int(data["javaVersion"]["majorVersion"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return major or None


def launch_java_major(instance, override_java: str | None = None) -> int | None:
    """Feature version of the JDK that will launch *instance*, or ``None``.

    *override_java* is the JVM the launch profile forces, so it wins; then the
    instance's own ``java_path``; then the version json's declared runtime,
    which is what Mojang installs when nothing is pinned. Reading the version
    json needs the instance's ``dir``, ``launch_version_id`` and
    ``mc_version``, so this takes the instance itself rather than a path.
    """
    if override_java:
        major = java_major(override_java)
        if major is not None:
            return major
    own = java_major(getattr(instance, "java_path", None))
    if own is not None:
        return own
    version_id = (getattr(instance, "launch_version_id", None)
                  or getattr(instance, "mc_version", None))
    return major_from_version_json(getattr(instance, "dir", None), version_id)


# ── ZGC ──────────────────────────────────────────────────────────────────────

def zgc_flags(java_major: int | None) -> list[str]:
    """The flags that enable ZGC on a JDK of feature version *java_major*.

    ZGC does not exist before JDK 11, so such a runtime gets nothing at all
    rather than flags it would refuse to start with; 11-14 hide ZGC behind
    ``-XX:+UnlockExperimentalVMOptions``. ``-XX:+ZGenerational`` is added for
    21-23, where it selects the generational collector; from 24 generational
    ZGC is the only mode and the flag is obsolete (it still parses, but warns
    and will eventually stop parsing), so it is left out. An unknown version is
    treated as modern -- 21 -- because that is what the launcher installs.
    """
    if java_major is not None and java_major < 11:
        return []
    flags = ["-XX:+UseZGC"]
    if java_major is not None and java_major < 15:
        flags.insert(0, "-XX:+UnlockExperimentalVMOptions")
    if java_major is None or 21 <= java_major <= 23:
        flags.append("-XX:+ZGenerational")
    return flags
