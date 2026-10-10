"""
One-off cleanup: remove the junk instances that automated test runs leaked
into the real %APPDATA%/Cove/instances.json.

The registry binds INSTANCE_INDEX_PATH as a default argument at import time,
so reassigning launcher.constants.INSTANCE_INDEX_PATH after import has no
effect -- some test windows therefore wrote to the real index.

Safety: only removes instances whose name is in the explicit list AND that
have no mods, no loader and were created during the test run. A backup of
instances.json is written first.
"""
from __future__ import annotations

import json
import os
import shutil
import time

JUNK = {"Recovered", "BackToWork"}

INDEX = os.path.join(os.environ["APPDATA"], "Cove", "instances.json")
INSTANCES_DIR = os.path.join(os.environ["APPDATA"], "Cove", "instances")


def main() -> None:
    if not os.path.isfile(INDEX):
        print("No instances.json found.")
        return

    with open(INDEX, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    keep, drop = [], []
    for inst in data:
        name = str(inst.get("name") or "")
        if name not in JUNK:
            keep.append(inst)
            continue
        # Belt and braces: only drop empty vanilla instances.
        mods_dir = os.path.join(INSTANCES_DIR, name, "mods")
        mods = (os.listdir(mods_dir)
                if os.path.isdir(mods_dir) else [])
        if mods or inst.get("loader_id") not in (None, "vanilla"):
            print(f"SKIPPING {name}: has mods or a loader -- not test junk.")
            keep.append(inst)
        else:
            drop.append(name)

    if not drop:
        print("Nothing to clean up.")
        return

    backup = INDEX + f".bak-{int(time.time())}"
    shutil.copy2(INDEX, backup)
    print(f"Backup written to {backup}")

    with open(INDEX, "w", encoding="utf-8") as fh:
        json.dump(keep, fh, indent=2)

    for name in drop:
        folder = os.path.join(INSTANCES_DIR, name)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
            print(f"Removed instance folder: {name}")

    print(f"\ninstances.json: {len(data)} -> {len(keep)}")
    print("Remaining:", [i.get("name") for i in keep])


if __name__ == "__main__":
    main()
