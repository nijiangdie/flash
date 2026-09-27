#!/usr/bin/env python3
"""一键安装：建项目内独立环境、装依赖、下载浏览器内核、跑离线自检。

给不懂命令行的人用的——双击 install.bat 就会走到这里。
安装逻辑放 Python 而不是批处理里，是因为批处理对中文和 UTF-8 的处理太不可靠。
"""
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
VENV_PY = PROJECT / ".venv" / "Scripts" / "python.exe"
MIN_PY = (3, 10)


def step(n: int, text: str):
    print(f"[{n}/4] {text}", flush=True)


def run(*cmd: str) -> bool:
    print("      $", " ".join(cmd[:4]) if len(cmd) <= 4 else " ".join(cmd), flush=True)
    return subprocess.run(list(cmd), cwd=str(PROJECT)).returncode == 0


def fail(why: str, hint: str = "") -> int:
    print("\n" + "=" * 60)
    print(f"安装没有完成：{why}")
    if hint:
        print(hint)
    print("上面最后几行就是原因；网络断了最常见，重新双击 install.bat 再来一次即可。")
    print("=" * 60)
    return 1


def check_python() -> int | None:
    if sys.version_info < MIN_PY:
        print(f"这台机器上的 Python 是 {sys.version.split()[0]}，太旧了。")
        print("这个项目需要 3.10 以上（代码用了较新的语法，旧版本导入就会报错）。")
        print("请到 https://www.python.org/downloads/ 下载新版，")
        print("安装时勾选 “Add python.exe to PATH”，装完重新双击 install.bat。")
        return 1
    print(f"检测到 Python {sys.version.split()[0]}，版本没问题。")
    print("依赖只有一个 playwright；数据库用 Python 自带的 SQLite，不用另装。\n")
    return None


def make_venv() -> bool:
    if VENV_PY.exists():
        step(1, "已有独立环境 .venv，跳过创建")
        return True
    step(1, "在项目里创建独立运行环境 .venv（不动系统里的 Python）")
    return run(sys.executable, "-m", "venv", ".venv")


def install_deps() -> bool:
    step(2, "安装依赖 playwright（从 pip 镜像下载，几十秒）")
    run(str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip")
    return run(str(VENV_PY), "-m", "pip", "install", "-r", "requirements.txt")


def install_browser() -> bool:
    step(3, "准备采集用的浏览器内核（约 130MB，装过会自动跳过，可能要几分钟）")
    print("      只有前程无忧那家用得到它；智通直聘不用浏览器，Boss 用你自己电脑上的 Edge。")
    return run(str(VENV_PY), "-m", "playwright", "install", "chromium")


def self_check() -> bool:
    step(4, "自检：离线跑一遍单元测试，确认代码和环境都正常（不联网、不采集）")
    return run(str(VENV_PY), "-m", "unittest", "discover", "-s", "tests")


def main() -> int:
    print("=" * 60)
    print("职位统计工具 一键安装")
    print("换电脑或第一次拿到这个文件夹时双击本文件，装完以后不用再碰。")
    print("=" * 60 + "\n")
    rc = check_python()
    if rc:
        return rc
    if not make_venv():
        return fail("没能创建独立环境 .venv")
    if not install_deps():
        return fail("依赖没装上", "多半是网络问题：换个网络或稍后再双击一次。")
    if not install_browser():
        return fail("浏览器内核没下载下来",
                    "这不影响智通直聘和 Boss直聘；稍后重试即可，或先看 README 的说明。")
    if not self_check():
        return fail("自检没通过", "把上面这段窗口截图发回来，就能看出是哪一步的问题。")

    print("\n" + "=" * 60)
    print("安装完成。以后每天只用这一件事：双击 start.bat")
    print("它会自动打开 http://127.0.0.1:8765，采集和筛选都在这个网页里点。")
    print("\n采 Boss直聘 多一步：第一次点『开始采集』会弹出 Edge 登录窗口，")
    print("扫码登录后回到网页再点一次『开始采集』。另两家不用登录。")
    print("\n提醒：数据只存在本项目 data\\jobs.db，不上传；每天采集有 200 条上限。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print("\n已取消。")
        code = 1
    try:
        input("\n按回车键关闭这个窗口... ")
    except EOFError:
        pass
    sys.exit(code)
