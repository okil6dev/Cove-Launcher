#!/usr/bin/env python3
"""Build script for packaging the launcher with PyInstaller.

Usage examples:
  python build.py                # one-file, no-console build named CoveLauncher
  python build.py --name MyApp   # change exe name
  python build.py --onedir       # build a directory instead of one-file
  python build.py --add-data "data;data" --add-data "assets;assets"

By default this script will include the `launcher` package as data so QSS
and other resources under that package are available at runtime inside the
frozen app.
"""
import argparse
import os
import subprocess
import sys
import shutil

PYINSTALLER_MODULE = "pyinstaller"


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
        return
    except Exception:
        print("PyInstaller not found, installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"]) 


def is_windows():
    return os.name == "nt"


def normalize_add_data(entries):
    # PyInstaller CLI uses ';' on Windows and ':' on POSIX
    sep = ";" if is_windows() else ":"
    normalized = []
    for e in entries:
        if sep in e:
            normalized.append(e)
        else:
            # if user passed a single path, map it to itself
            normalized.append(f"{e}{sep}{e}")
    return normalized


def build(args):
    ensure_pyinstaller()

    cmd = [sys.executable, "-m", "PyInstaller"]
    if args.noconsole:
        cmd.append("--noconsole")
    if args.onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")
    cmd += ["--name", args.name]
    if args.clean:
        cmd.append("--clean")

    # Ensure launcher package resources are included
    add_data = list(args.add_data)
    # default add-data: include the entire launcher package
    default_launcher = "launcher"
    if default_launcher not in [os.path.normpath(x.split(";" if is_windows() else ":")[0]) for x in add_data]:
        add_data.insert(0, f"{default_launcher}{(';' if is_windows() else ':')}{default_launcher}")
    # The window/taskbar icon is loaded at runtime from <base>/assets/, which
    # in a frozen app is PyInstaller's _MEIPASS - so the assets folder MUST be
    # bundled or the compiled exe silently falls back to the generic icon.
    if "assets" not in [os.path.normpath(x.split(";" if is_windows() else ":")[0]) for x in add_data]:
        add_data.insert(0, f"assets{(';' if is_windows() else ':')}assets")

    for ad in normalize_add_data(add_data):
        cmd += ["--add-data", ad]

    # Give the .exe file itself the Cove icon (Explorer/taskbar/pin icon).
    exe_icon = os.path.join("assets", "cove_icon.ico")
    if os.path.isfile(exe_icon):
        cmd += ["--icon", exe_icon]

    # Entry point
    cmd.append(args.entry)

    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd)
    print("Build completed. See the 'dist' folder.")


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the launcher with PyInstaller")
    p.add_argument("--name", default="CoveLauncher", help="Name of the output executable")
    p.add_argument("--onefile", dest="onefile", action="store_true", help="Produce a single-file executable (default)")
    p.add_argument("--onedir", dest="onefile", action="store_false", help="Produce a directory instead of a single file")
    p.set_defaults(onefile=True)
    p.add_argument("--noconsole", dest="noconsole", action="store_true", default=True, help="Hide console window for GUI app (default)")
    p.add_argument("--console", dest="noconsole", action="store_false", help="Show console window")
    p.add_argument("--add-data", action="append", default=[], help='Extra files or folders to bundle (format: src;dest on Windows or src:dest on POSIX)')
    p.add_argument("--entry", default="main.py", help="Python entry point file (default: main.py)")
    p.add_argument("--clean", action="store_true", help="Clean PyInstaller cache and build artifacts")
    args = p.parse_args(argv)

    try:
        build(args)
    except subprocess.CalledProcessError as exc:
        print("Build failed:", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
