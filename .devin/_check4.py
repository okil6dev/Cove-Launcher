import pathlib

# CORRECT path - the directory is "launcher" not "launch"
p = pathlib.Path('C:/Users/minec/AppData/Apps/MinecraftLauncher/launcher/pages/accounts_page.py')
print("Exists:", p.exists())
if p.exists():
    print("Size:", p.stat().st_size)
