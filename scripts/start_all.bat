@echo off
rem ============================================================
rem  doc-ocr-system 一键启动（双击运行）
rem
rem  真正的逻辑在同目录的 start_all.ps1。
rem  这个 .bat 刻意只用 ASCII —— batch 文件里的中文受代码页影响，
rem  很容易变成乱码；把中文放在 .ps1（UTF-8 BOM）里更可靠。
rem ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1"
echo.
pause
