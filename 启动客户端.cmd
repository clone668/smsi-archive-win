@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON=py -3"
where py >nul 2>&1
if errorlevel 1 set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

if not exist ".venv\Scripts\python.exe" (
  echo Creating Python virtual environment...
  %PYTHON% -m venv .venv
  if errorlevel 1 goto :failed
  set "PYTHON=.venv\Scripts\python.exe"
)

".venv\Scripts\python.exe" -c "import PySide6,paramiko,pyarrow,lucide; from zoneinfo import ZoneInfo; ZoneInfo('UTC')" >nul 2>&1
if errorlevel 1 (
  echo Installing client dependencies. This may take several minutes...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  if errorlevel 1 goto :failed
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :failed
)

start "" ".venv\Scripts\pythonw.exe" "%CD%\main.py"
exit /b 0

:failed
echo.
echo SMSI archive sync client could not be started.
echo Check Python 3.10+ and the network connection, then run this file again.
pause
exit /b 1
