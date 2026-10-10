import pathlib

# Use forward slashes to avoid any backslash escaping issues
p = pathlib.Path('C:/Users/minec/AppData/Apps/MinecraftLauncher/launch/pages/accounts_page.py')
print("Exists:", p.exists())
print("Resolved:", p.resolve())
