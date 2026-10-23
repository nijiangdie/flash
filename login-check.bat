@echo off
rem 给外部调度（影刀 RPA / 任务计划程序）用的登录态巡检入口。
rem 输出定成 UTF-8 落进文件，读的一方就不必猜代码页；中文判据只有 browser.login_state_report 打得出来。
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%~dp0data\logs" mkdir "%~dp0data\logs"
set "PYTHONIOENCODING=utf-8"
"%PY%" cli.py status boss > "%~dp0data\logs\login-check.txt" 2>&1
exit /b 0
