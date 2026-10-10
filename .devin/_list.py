import pathlib

base = pathlib.Path(r'C:\Users\minec\AppData\Apps\MinecraftLauncher')
# List what's actually in the directory
for item in sorted(base.iterdir()):
    print(repr(str(item)))
