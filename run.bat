@echo off
rem Start FBD Studio. Works from any directory; extra options are passed through:
rem   run.bat                         opens http://127.0.0.1:8765/ in your browser
rem   run.bat --port 9000 --no-browser
rem   run.bat --workspace D:\plc-projects
setlocal EnableExtensions
set "DIR=%~dp0"
set "VPY=%DIR%.venv\Scripts\python.exe"

"%VPY%" -c "import fbdstudio, fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo FBD Studio is not set up yet, or the setup is incomplete. Running setup first...
  call "%DIR%setup.bat" --no-pause
  if errorlevel 1 (
    echo Setup failed - see the messages above.
    pause
    exit /b 1
  )
)

"%VPY%" -m fbdstudio %*
if errorlevel 1 pause
