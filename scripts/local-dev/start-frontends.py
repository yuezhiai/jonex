#!/usr/bin/env python3
"""前端 dev server 启动器（脱离会话，父进程退出后仍存活）。

用法：.venv/bin/python scripts/local-dev/start-frontends.py [start|stop|status]
"""
import os
import pathlib
import shutil
import signal
import subprocess
import sys

# Windows 上 pnpm 是 .cmd 垫片，`Popen(["pnpm", ...])` 会 FileNotFoundError
# （报「系统找不到指定的文件」，看不出是缺扩展名）——POSIX 上 which 也照样返回它。
PNPM = shutil.which("pnpm") or "pnpm"

# 仓库根 = 本文件的上两层（scripts/local-dev/ → 仓库根）
WS = pathlib.Path(__file__).resolve().parents[2]
LOGS = WS / ".tmp-setup" / "logs"
PIDS = WS / ".tmp-setup" / "pids"
APPS = {
    "dev-gateway": 8080,
    "shell": 5173,
    "core-business": 5175,
    "platform-management": 5177,
    "ecosystem-management": 5176,
}


def start():
    # 首次运行这两个目录还不存在，open(.../"ab") 与 write_text 会先炸在这儿
    # （launch.py 里有同样的两行，这里原先漏了）。
    LOGS.mkdir(parents=True, exist_ok=True)
    PIDS.mkdir(parents=True, exist_ok=True)
    for app, port in APPS.items():
        pid_file = PIDS / f"fe-{app}.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), 0)
                print(f"fe-{app}: 已在跑 pid={pid_file.read_text().strip()}")
                continue
            except (OSError, ValueError):
                pid_file.unlink(missing_ok=True)
        log = open(LOGS / f"fe-{app}.log", "ab", buffering=0)
        proc = subprocess.Popen(
            [PNPM, "-C", "frontends", "run", "dev:gateway" if app == "dev-gateway" else f"dev:{app}"],
            cwd=WS, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file.write_text(str(proc.pid))
        print(f"fe-{app}: pid={proc.pid} http://localhost:{port}")


def stop():
    for app in APPS:
        pid_file = PIDS / f"fe-{app}.pid"
        if not pid_file.exists():
            continue
        try:
            os.killpg(os.getpgid(int(pid_file.read_text().strip())), signal.SIGTERM)
            print(f"fe-{app}: 已停止")
        except (OSError, ValueError) as exc:
            print(f"fe-{app}: {exc}")
        pid_file.unlink(missing_ok=True)


def status(_picked=None):
    for app, port in APPS.items():
        pid_file = PIDS / f"fe-{app}.pid"
        state = "未启动"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), 0)
                state = f"运行中 pid={pid_file.read_text().strip()}"
            except (OSError, ValueError):
                state = "已退出"
        print(f"fe-{app:22} {state:20} http://localhost:{port}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    {"start": start, "stop": stop, "status": lambda: status()}[cmd]()
