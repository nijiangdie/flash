@echo off
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

rem 登录凭证默认存在项目外：C:\Users\你的用户名\AppData\Local\job-stats\auth\boss_storage.json
rem 和工程文件夹分开，所以这个文件夹随时可以压缩发人，不会夹带登录态。
rem 命令运行时会把它实际用的地址打印出来，扫之前确认一眼就行。
rem
rem 想换到别的目录：去掉下面这行开头的 rem，改成你要的路径。
rem 更建议用 setx 一次设成用户变量（cmd 里跑：setx JOBSTATS_AUTH_DIR "D:\secrets\jobstats-auth"），
rem 因为采集和 serve 也要读同一个位置；只在某个 bat 里 set，登录和采集会指向两个目录。
rem 别设到网盘/同步目录或网络盘上，那是登录凭证，同步一份等于多台机器能登你的号。
rem set "JOBSTATS_AUTH_DIR=D:\secrets\jobstats-auth"

echo 马上弹出 Edge 登录窗口：请在里面扫码或用短信登录。判据一凑齐就自动存盘，
echo 不用回这个黑窗口按回车（最多等 5 分钟，扫得慢也不算失败）。
echo 登录成功后那个 Edge 窗口请保持开着（采集要从它身上现取登录态）；
echo 关掉了也不要紧，采集会自己把窗口弹回来，只是要等十几秒。
echo.
"%PY%" cli.py login boss --edge
echo.
echo 上面出现"登录成功"并带着凭证存放地址就成了。
echo 窗口根本没弹出来：多半是 .venv 没装，先双击 install.bat；或者还有老的 Edge 窗口占着，全关掉再试。
pause
