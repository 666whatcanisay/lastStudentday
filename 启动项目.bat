@echo off
rem Double-click this file from the project folder to start the local app.
rem %~dp0 resolves to this file's folder, so moving the whole project works.
set "START_SCRIPT=%~dp0scripts\start_all.bat"
if not exist "%START_SCRIPT%" (
    echo Start script not found. Keep this file in the project root.
    pause
    exit /b 1
)
call "%START_SCRIPT%"
