import pathlib

p = pathlib.Path(r'C:\Users\minec\AppData\Apps\MinecraftLauncher\launch\pages\accounts_page.py')
print("Exists:", p.exists())
print("Contents length:", len(p.read_text(encoding='utf-8')))
