Packaging the launcher as a single Windows EXE using PyInstaller

1) Install PyInstaller (script automates this):

   - Windows CMD: run `build_exe.bat`
   - PowerShell: run `.uild_exe.ps1`

2) Common flags used by the scripts:
   - `--noconsole` : hides the terminal window for GUI apps
   - `--onefile`   : produce a single EXE containing all dependencies
   - `--name <Name>`: override the output executable name

3) Notes and troubleshooting
   - PyQt6 and PyQt6 plugins may require extra hooks; if the built EXE crashes, try using
     `pyinstaller --noconsole --onefile main.py` first, then inspect `dist/` and `build/` logs.
   - If your UI uses external data files (icons, QSS, images), add them with `--add-data`:
     `--add-data "launcher;launcher"` (semicolon separator on Windows)
   - For complex cases create a `.spec` file and include `datas`, `binaries`, and `hiddenimports`.

4) Reproducible build tips
   - Use a clean virtual environment and pin versions in `requirements.txt`.
   - Test the EXE on a clean Windows PC to ensure no missing DLLs.

5) Example manual command

```powershell
python -m pip install pyinstaller
pyinstaller --noconsole --onefile --name CoveLauncher main.py --add-data "launcher;launcher"
```

If you'd like, I can generate a `launcher.spec` tuned for this project and add `--add-data` entries automatically. Reply with "make spec" and I'll create it and explain next steps.
