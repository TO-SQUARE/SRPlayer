@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 (
    echo Failed to open the project folder.
    pause
    exit /b 1
)

py -3 --version >nul 2>&1
if errorlevel 1 (
    echo Python 3 was not found. Install Python and enable the py launcher.
    pause
    exit /b 1
)

echo Installing project requirements...
py -3 -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install project requirements.
    pause
    exit /b 1
)

echo Installing PyInstaller...
py -3 -m pip install pyinstaller
if errorlevel 1 (
    echo Failed to install PyInstaller.
    pause
    exit /b 1
)

echo Building SRPlayer.exe...
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name SRPlayer --add-data "Data;Data" --icon SR.ico SR.py
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Build completed: dist\SRPlayer.exe
pause
