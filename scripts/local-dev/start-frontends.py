#!/usr/bin/env python3
"""前端 dev server 启动器（脱离会话，父进程退出后仍存活）。

用法：.venv/bin/python scripts/local-dev/start-frontends.py [start|stop|status]
"""
import os
import pathlib
import shutil
import signal
import socket
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
    # dev-gateway 的值只是 index.js 的兜底默认值；真实端口以它的 .env 为准（见 _port_of）。
    "dev-gateway": 8080,
    "shell": 5173,
    "core-business": 5175,
    "platform-management": 5177,
    "ecosystem-management": 5176,
}


def _gateway_port() -> int:
    """dev-gateway 的真实监听端口 = 它的 .env 里的 GATEWAY_PORT。

    本机 .env 配的是 8081（8080 被 Windows HTTP.sys 的排除端口段占着），
    所以脚本里那个 8080 是陈旧的兜底值，拿它做端口检查会查到没人用的端口。
    """
    env_file = WS / "frontends" / "dev-gateway" / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GATEWAY_PORT="):
                return int(line.split("=", 1)[1].strip().strip('"\''))
    except (OSError, ValueError):
        pass
    return APPS["dev-gateway"]


def _port_of(app: str) -> int:
    return _gateway_port() if app == "dev-gateway" else APPS[app]


def _alive(pid: int) -> bool:
    """pid 是否还活着。

    **不能用 `os.kill(pid, 0)`**：那是 POSIX 的探活语义，Windows 上对任何 pid 都抛
    `OSError [WinError 87] 参数错误`。原实现把它当「进程已死」，于是每次 start 都会
    删掉 pid 文件并**重复拉起一份**——两条 pnpm 链抢同一端口，抢输的抛 EADDRINUSE
    崩溃、`node --watch` 又把它拉起来，页面就一直整页重载（2026-10-15 实测）。
    """
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _port_in_use(port: int) -> bool:
    """端口上是否已有进程在监听。

    用 socket 连一下，不依赖 netstat / lsof 的平台差异。IPv4 与 IPv6 都要探：
    本仓 gateway 绑的是 `::`（`EADDRINUSE ... :::8081` 就是这个形态），
    只探 127.0.0.1 会在某些机器上漏判。
    """
    for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def start():
    # 首次运行这两个目录还不存在，open(.../"ab") 与 write_text 会先炸在这儿
    # （launch.py 里有同样的两行，这里原先漏了）。
    LOGS.mkdir(parents=True, exist_ok=True)
    PIDS.mkdir(parents=True, exist_ok=True)
    blocked: list[tuple[str, int]] = []
    for app, port in APPS.items():
        real_port = _port_of(app)
        pid_file = PIDS / f"fe-{app}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
            except ValueError:
                pid = None
            if pid is not None and _alive(pid):
                print(f"fe-{app}: 已在跑 pid={pid}")
                continue
            pid_file.unlink(missing_ok=True)
        # 端口闸：pid 文件里没人（或已退出），但端口已被占用 = 有「不是本脚本启动的」
        # 实例在跑。不拦住就会两条链抢同一端口，表现为页面一直自动重载。
        if _port_in_use(real_port):
            blocked.append((app, real_port))
            print(f"fe-{app}: 端口 {real_port} 已被占用（不是本脚本启动的实例）——跳过")
            continue
        log = open(LOGS / f"fe-{app}.log", "ab", buffering=0)
        proc = subprocess.Popen(
            [PNPM, "-C", "frontends", "run", "dev:gateway" if app == "dev-gateway" else f"dev:{app}"],
            cwd=WS, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file.write_text(str(proc.pid))
        print(f"fe-{app}: pid={proc.pid} http://localhost:{real_port}")
    if blocked:
        print("\n以下端口被「不是本脚本启动的」进程占用，已跳过启动：")
        for app, port in blocked:
            print(f"    fe-{app}  :{port}")
        print("先查清是谁占着，再启动——否则两条链会互相抢端口、反复重启，页面会一直自动重载。")
        print("  Windows:      netstat -ano | findstr LISTENING | findstr :%d" % blocked[0][1])
        print("  macOS/Linux:  lsof -nP -iTCP:%d -sTCP:LISTEN" % blocked[0][1])
        sys.exit(1)


def _kill_tree(pid: int) -> None:
    """结束 pid 及其子进程树。

    这些进程是用 `start_new_session=True` 起的，POSIX 下 pid 即进程组组长，`killpg` 一次
    收干净；**Windows 没有 `os.getpgid` / `os.killpg`**（直接调用会抛 AttributeError，
    而 stop 原先只捕 OSError/ValueError —— 于是整条 stop 在 Windows 上是崩的），改用
    `taskkill /T` 收进程树。
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        os.killpg(os.getpgid(pid), signal.SIGTERM)


def stop():
    for app in APPS:
        port = _port_of(app)
        pid_file = PIDS / f"fe-{app}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
            except ValueError:
                pid = None
            if pid is not None and _alive(pid):
                try:
                    _kill_tree(pid)
                    print(f"fe-{app}: 已停止 pid={pid}")
                except OSError as exc:
                    print(f"fe-{app}: 停止失败 {exc}")
            else:
                print(f"fe-{app}: pid 文件是陈迹（进程已不在），已清理")
            pid_file.unlink(missing_ok=True)
        # 收完了端口还听着 = 有 pid 文件之外的残留实例（历史上 start 重复拉起留下的）。
        # 这里只报不杀：没有可靠的跨平台「端口 → PID」解析，硬猜着杀风险更大。
        if _port_in_use(port):
            print(f"fe-{app}: 注意——端口 {port} 仍被占用，有 pid 文件之外的残留实例。")
            print(f"            查：Windows `netstat -ano | findstr :{port}`；"
                  f"macOS/Linux `lsof -nP -iTCP:{port} -sTCP:LISTEN`")


def status(_picked=None):
    for app, port in APPS.items():
        port = _port_of(app)
        pid_file = PIDS / f"fe-{app}.pid"
        state = "未启动"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
            except ValueError:
                pid = None
            state = f"运行中 pid={pid}" if pid is not None and _alive(pid) else "已退出"
        print(f"fe-{app:22} {state:20} http://localhost:{port}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    {"start": start, "stop": stop, "status": lambda: status()}[cmd]()
