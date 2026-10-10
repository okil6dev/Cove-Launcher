@echo off
REM Build a single-file, no-console EXE using PyInstaller
REM Usage: run this from the project root (double-click or run in CMD)
python -m pip install --upgrade pip
python -m pip install pyinstaller
pyinstaller --noconsole --onefile --name CoveLauncher main.py
echo.
echo Build finished. See the `dist` folder for CoveLauncher.exe
pause
