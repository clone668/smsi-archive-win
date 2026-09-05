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
)

rem An importable dependency is not necessarily the pinned one.  Full
rem verification recomputes the collector's Parquet fingerprints, so the pinned
rem versions are part of that contract - stamping requirements.txt lets a
rem changed pin reach an existing installation exactly once, without making the
rem network a condition of every start.
set "STAMP=.venv\.smsi-requirements.sha256"
set "REQUIRED="
for /f "usebackq delims=" %%H in (`".venv\Scripts\python.exe" -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())"`) do set "REQUIRED=%%H"
set "INSTALLED="
if exist "%STAMP%" set /p INSTALLED=<"%STAMP%"

set "NEED_INSTALL="
".venv\Scripts\python.exe" -c "import PySide6,paramiko,pyarrow,lucide; from zoneinfo import ZoneInfo; ZoneInfo('UTC')" >nul 2>&1
if errorlevel 1 set "NEED_INSTALL=1"
if not defined REQUIRED set "NEED_INSTALL=1"
if not "%REQUIRED%"=="%INSTALLED%" set "NEED_INSTALL=1"

if defined NEED_INSTALL (
  echo Installing client dependencies. This may take several minutes...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :install_failed
  rem Redirection first: the hash ends in a hex digit, which cmd would read as
  rem a stream handle if it stood immediately before the ">".
  > "%STAMP%" echo %REQUIRED%
)
goto :launch

:install_failed
rem Keeping the archive running matters more than a fresh dependency set: if the
rem client can still start, say what is stale and start it.
".venv\Scripts\python.exe" -c "import PySide6,paramiko,pyarrow,lucide; from zoneinfo import ZoneInfo; ZoneInfo('UTC')" >nul 2>&1
if errorlevel 1 goto :failed
echo.
echo WARNING: dependency install failed, starting with the versions already installed.
echo Sync and transport checks are unaffected. Run this file again with a working
echo network so full verification uses the same versions as the collector.
echo.

:launch
start "" ".venv\Scripts\pythonw.exe" "%CD%\main.py"
exit /b 0

:failed
echo.
echo SMSI archive sync client could not be started.
echo Check Python 3.10+ and the network connection, then run this file again.
pause
exit /b 1
