@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
echo [0/1] 正在启动职位筛选台，浏览器会自动打开...
"%PY%" cli.py serve
pause