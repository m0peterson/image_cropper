@echo off
rem Same build, pinned to what still works on Windows 7 (Python 3.8.10).
rem Install Python 3.8.10 first: https://www.python.org/downloads/release/python-3810/
setlocal
cd /d "%~dp0"

set PY=py -3.8
%PY% --version >nul 2>nul
if errorlevel 1 set PY=python

%PY% --version
if not exist .venv38 (
    %PY% -m venv .venv38 || goto :fail
)
call .venv38\Scripts\activate.bat

python -m pip install --upgrade "pip<25" || goto :fail
python -m pip install -r requirements-win7.txt || goto :fail
python -m pip install "pyinstaller==5.13.2" || goto :fail

python -m PyInstaller --noconfirm --clean splitter.spec || goto :fail

echo.
echo Done: dist\ImageSplitter.exe (built for Windows 7 and newer)
pause
exit /b 0

:fail
echo.
echo Build failed, see the messages above.
pause
exit /b 1
