# Build EXE with PyInstaller (PowerShell)
# Usage: Open PowerShell in this folder and run: .\build_exe.ps1
param(
    [switch]$OneFile = $true,
    [string]$Name = "CoveLauncher"
)
python -m pip install --upgrade pip
python -m pip install pyinstaller
if ($OneFile) {
    pyinstaller --noconsole --onefile --name $Name --icon "assets\cove_icon.ico" --add-data "assets;assets" --clean main.py
} else {
    pyinstaller --noconsole --onedir --name $Name --icon "assets\cove_icon.ico" --add-data "assets;assets" --clean main.py
}
Write-Host "Build finished. See the 'dist' folder for $Name.exe" -ForegroundColor Green
