@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 goto try_py
where python >nul 2>nul
if not errorlevel 1 goto try_python

echo.
echo [!] 这台机器上没有检测到 Python，所以装不了。
echo     1. 打开 https://www.python.org/downloads/ 下载 3.10 或更高版本
echo     2. 安装时务必勾选 "Add python.exe to PATH"
echo     3. 装完再双击本文件
echo     中文说明见 README.md 的『安装』一节。
pause
exit /b 1

:try_py
py -3 install.py
set "RC=%errorlevel%"
goto finish

:try_python
python install.py
set "RC=%errorlevel%"

:finish
exit /b %RC%
