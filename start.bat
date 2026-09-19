@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [0/1] 正在启动职位筛选台，浏览器会自动打开...
python cli.py serve
pause