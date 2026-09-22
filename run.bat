@echo off
rem Starts the GUI from source, without building an .exe.
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    echo Python not found in PATH. Install it from python.org,
    echo then run:  python -m pip install -r requirements.txt
    pause
    exit /b 1
)
start "" pythonw main.py %*
