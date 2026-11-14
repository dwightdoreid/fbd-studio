@echo off
rem FBD Studio - one-time setup (Windows)
rem
rem   setup.bat                 create/repair .venv and install dependencies
rem   setup.bat --platformio    also install PlatformIO (needed for Build/Upload to real boards)
rem   setup.bat --recreate      delete the existing .venv and start fresh
rem   setup.bat --no-dev        skip test tools (pytest, httpx)
rem
rem Safe to run again at any time: it only does what is missing.
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV=.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "WITH_PIO=0"
set "RECREATE=0"
set "EXTRAS=dev"
set "NOPAUSE=0"

rem ---------------------------------------------------------------- options
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--platformio" (set "WITH_PIO=1" & shift & goto parse)
if /i "%~1"=="--recreate" (set "RECREATE=1" & shift & goto parse)
if /i "%~1"=="--no-dev" (set "EXTRAS=" & shift & goto parse)
if /i "%~1"=="--no-pause" (set "NOPAUSE=1" & shift & goto parse)
if /i "%~1"=="--help" goto help
if /i "%~1"=="-h" goto help
echo Unknown option: %~1  (try --help)
set "RC=2" & goto finish
:help
echo Usage: setup.bat [--platformio] [--recreate] [--no-dev]
echo   --platformio   also install PlatformIO (Build/Upload to real boards)
echo   --recreate     delete the existing .venv and start fresh
echo   --no-dev       skip test tools (pytest, httpx)
set "RC=0" & goto finish
:parsed

rem ---------------------------------------------------------------- 1. Python
echo ==^> Looking for Python 3.10+
set "PY="
rem "py -3" is the official Python launcher; "python" may be the Microsoft Store stub, which fails this check.
for %%C in ("py -3" "python" "python3") do (
  if not defined PY (
    %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=%%~C"
  )
)
if not defined PY (
  echo     [X] Python 3.10 or newer not found.
  echo         Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
  echo         or run:  winget install -e --id Python.Python.3.13
  echo         Then run this script again.
  set "RC=1" & goto finish
)
for /f "delims=" %%V in ('%PY% --version 2^>^&1') do set "PYVER=%%V"
echo     [OK] %PYVER% ^(%PY%^)

rem ---------------------------------------------------------------- 2. virtual environment
echo ==^> Virtual environment ^(%VENV%^)
if "%RECREATE%"=="1" if exist "%VENV%" (
  rmdir /s /q "%VENV%"
  echo     [OK] removed old %VENV%
)
if exist "%VPY%" (
  "%VPY%" -c "import sys" >nul 2>&1 && goto venv_ok
  echo     [!] existing %VENV% is broken, recreating it
  rmdir /s /q "%VENV%"
)
%PY% -m venv "%VENV%"
if errorlevel 1 (
  echo     [X] Could not create the virtual environment.
  set "RC=1" & goto finish
)
echo     [OK] created
goto venv_done
:venv_ok
echo     [OK] already exists
:venv_done

rem ---------------------------------------------------------------- 3. dependencies
echo ==^> Installing FBD Studio and its dependencies
call :pip --upgrade pip
if errorlevel 1 (set "RC=1" & goto finish)
if defined EXTRAS (
  call :pip -e ".[%EXTRAS%]"
) else (
  call :pip -e .
)
if errorlevel 1 (set "RC=1" & goto finish)
"%VPY%" -c "import fastapi, uvicorn, pydantic, yaml, serial, fbdstudio" >nul 2>&1
if errorlevel 1 (
  echo     [X] dependency check failed
  set "RC=1" & goto finish
)
echo     [OK] installed

if "%WITH_PIO%"=="1" (
  echo ==^> Installing PlatformIO
  call :pip "platformio>=6.1"
  if errorlevel 1 (set "RC=1" & goto finish)
  echo     [OK] PlatformIO installed. The first build for a board downloads its toolchain, several hundred MB.
)

rem ---------------------------------------------------------------- 4. optional tools
echo ==^> Checking optional tools
set "CXX="
where g++ >nul 2>&1 && set "CXX=g++"
if not defined CXX (where clang++ >nul 2>&1 && set "CXX=clang++")
if defined CXX (
  echo     [OK] C++ compiler found: %CXX% - the simulator will work
) else (
  echo     [!] No g++ or clang++ found: the simulator needs one.
  echo         Easiest: install MSYS2 from https://www.msys2.org, then in the "MSYS2 UCRT64" shell run
  echo           pacman -S mingw-w64-ucrt-x86_64-gcc
  echo         and add C:\msys64\ucrt64\bin to your PATH.
)

set "PIO="
if exist "%VENV%\Scripts\pio.exe" set "PIO=1"
if exist "%USERPROFILE%\.platformio\penv\Scripts\pio.exe" set "PIO=1"
where pio >nul 2>&1 && set "PIO=1"
if defined PIO (
  echo     [OK] PlatformIO found - Build/Upload will work
) else (
  echo     [!] PlatformIO not installed: Build/Upload need it. Run "setup.bat --platformio" when you want it.
)
echo         Board not showing up as a COM port? Install its USB driver ^(CP210x or CH340, depending on the board^).

echo.
echo Setup complete. Start FBD Studio with:  run.bat
set "RC=0"
goto finish

rem ---------------------------------------------------------------- helpers
:pip
rem Run pip quietly; show its full output only if it fails.
set "PIPLOG=%TEMP%\fbd-pip-%RANDOM%.log"
"%VPY%" -m pip install --disable-pip-version-check %* > "%PIPLOG%" 2>&1
if errorlevel 1 (
  type "%PIPLOG%"
  del "%PIPLOG%" >nul 2>&1
  echo     [X] pip install failed - see the output above
  exit /b 1
)
del "%PIPLOG%" >nul 2>&1
exit /b 0

:finish
rem Keep the window open when the script was started by double-clicking it.
if "%NOPAUSE%"=="0" (
  echo %cmdcmdline% | find /i "/c" >nul && pause
)
exit /b %RC%
