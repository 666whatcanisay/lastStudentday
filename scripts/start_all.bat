@echo off
rem ============================================================
rem  doc-ocr-system launcher (double-click)
rem
rem  Keep this batch file ASCII-only: cmd.exe may use a legacy code page.
rem  The actual launcher and Chinese messages live in start_all.ps1.
rem ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1"
echo.
pause
