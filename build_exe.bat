@echo off
rem Builds dist\ImageSplitter.exe -- one portable file, no installer.
rem Run it from a Windows machine that has Python installed.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found in PATH. Install it from python.org and tick
    echo "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip setuptools wheel || goto :fail
python -m pip install -r requirements.txt || goto :fail
python -m pip install pyinstaller || goto :fail

python -m PyInstaller --noconfirm --clean splitter.spec || goto :fail

echo.
echo Done: dist\ImageSplitter.exe
echo Copy that single file anywhere; Python is not needed on the target machine.
pause
exit /b 0

:fail
echo.
echo Build failed, see the messages above.
pause
exit /b 1
