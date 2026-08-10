@echo off
setlocal enabledelayedexpansion

rem  skill-sync launcher for Windows.
rem
rem  Finds a Python that actually runs, installs one for this user if there is none, then
rem  runs a skill-sync script with it. Spawning `python menu.py` directly is what used to
rem  fail: on a clean Windows `python` resolves to the Microsoft Store stub, which opens
rem  the Store instead of running anything, and the console closed with no explanation.
rem
rem      launch.cmd                    opens the interactive menu
rem      launch.cmd sync.py status     runs that script with those arguments

set "HERE=%~dp0"

set "STATE=%SKILL_SYNC_HOME%"
if not defined STATE set "STATE=%USERPROFILE%\.claude\skill-sync"
set "MANAGED=%STATE%\python\python.exe"

set "PYEXE="
set "PYPRE="

call :find_python
if defined PYEXE goto run

echo(
echo   Python was not found on this computer, and skill-sync is written in Python.
echo   Installing it for your user only - no administrator rights are needed.
echo(

where winget >nul 2>&1
if not errorlevel 1 (
    echo   ^> winget install --id Python.Python.3.12 --scope user
    winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
    call :find_python
    if defined PYEXE goto installed
)

if exist "%HERE%get_python.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%get_python.ps1" -Dest "%STATE%\python"
    call :try "%MANAGED%"
    if defined PYEXE goto installed
)

echo(
echo   skill-sync could not install Python automatically.
echo   Install it yourself from https://www.python.org/downloads/ ^(tick "Add python.exe
echo   to PATH"^), then run this again.
echo(
pause
exit /b 1

:installed
echo(
echo   Python is ready: %PYEXE% %PYPRE%
echo(

:run
set "SCRIPT=%~1"
if not defined SCRIPT (
    "%PYEXE%" %PYPRE% "%HERE%menu.py"
    exit /b %errorlevel%
)
shift
set "REST="
:collect
if "%~1"=="" goto launch
set "REST=%REST% %1"
shift
goto collect

:launch
"%PYEXE%" %PYPRE% "%HERE%%SCRIPT%"%REST%
exit /b %errorlevel%


rem ------------------------------------------------------------------ helpers

:find_python
call :try "%MANAGED%"
if defined PYEXE goto :eof

rem The py launcher is the one entry point that never resolves to the Store stub.
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PYEXE=py"
    set "PYPRE=-3"
    goto :eof
)

for /f "delims=" %%P in ('where python 2^>nul') do call :try_path "%%P"
if defined PYEXE goto :eof
for /f "delims=" %%P in ('where python3 2^>nul') do call :try_path "%%P"
if defined PYEXE goto :eof

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :try "%%D\python.exe"
if defined PYEXE goto :eof
for /d %%D in ("%ProgramFiles%\Python3*") do call :try "%%D\python.exe"
goto :eof

:try_path
rem Skip the Microsoft Store alias: running it pops the Store window and installs nothing.
echo "%~1"| find /i "\WindowsApps\" >nul
if not errorlevel 1 goto :eof
call :try "%~1"
goto :eof

:try
if defined PYEXE goto :eof
if "%~1"=="" goto :eof
"%~1" -c "import sys;sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if errorlevel 1 goto :eof
set "PYEXE=%~1"
set "PYPRE="
goto :eof
