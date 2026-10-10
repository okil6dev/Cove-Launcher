import pathlib, re

p = pathlib.Path(r'C:\Users\minec\AppData\Apps\MinecraftLauncher\launch\pages\accounts_page.py')
print("Path exists:", p.exists())
print("Absolute:", p.resolve())
