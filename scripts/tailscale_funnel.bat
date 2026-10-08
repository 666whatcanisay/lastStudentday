@echo off
REM ASCII only on purpose: .bat files here cannot be executed/verified in this
REM environment, and non-ASCII .bat gets garbled by the console codepage.
REM All real logic lives in tailscale_funnel.ps1 (which IS verifiable).
REM
REM   tailscale_funnel.bat up      - publish http://127.0.0.1:8000 as public HTTPS
REM   tailscale_funnel.bat status  - show login protection + funnel mapping
REM   tailscale_funnel.bat off     - remove the public mapping
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tailscale_funnel.ps1" %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" echo [exit code %RC%]
endlocal
exit /b %RC%
