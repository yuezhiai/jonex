#!/usr/bin/env python3
"""检查并准备 Wiki 的服务器运行环境。

在已拉取新版 Wiki 代码的平台目录中运行：

    make build && make up
    make wiki-check

默认只读检查。--repair 显式修复可识别的运行配置、命名空间、Redis 地址及已核对的
平台数据库迁移。数据库迁移前会自动创建目标数据库的逻辑备份。
未知迁移、自定义配置冲突和缺失的外部服务配置会停止并提示，不会猜测。未知迁移
经管理员审查后可使用 --reviewed-new-migrations 执行，仍会先备份。脚本不会启动
或重启服务；Wiki 和 Anything 的待执行升级会在备份后由新容器启动时执行。备份不
包含其他数据库、用户角色和对象存储；--check 仅检查，不修改配置、宿主机或数据库。
"""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, unquote, urlsplit


DEFAULTS = {
    "JONEX_WIKI_API_PARSE_CONCURRENCY": "15",
    "JONEX_WIKI_API_COMPILE_CONCURRENCY": "15",
    "JONEX_WIKI_COMPILE_CONCURRENCY": "15",
    "JONEX_WIKI_BLOCKING_POOL_SIZE": "15",
    "JONEX_WIKI_KEEP_BUNDLES": "1",
    "TASK_CENTER_SCHEDULER_BATCH": "15",
}
WRITER_LIMITS = {
    "JONEX_WIKI_AGENT_COMPILE_BATCH_MAX_CALLS": (1, 500, True),
    "JONEX_WIKI_AGENT_COMPILE_BATCH_TIMEOUT_SECONDS": (0, 3600, False),
    "JONEX_WIKI_AGENT_COMPILE_BATCH_IDLE_SECONDS": (0, 3600, False),
    "JONEX_WIKI_AGENT_COMPILE_BATCH_MAX_INPUT_BYTES": (65536, 4194304, True),
    "JONEX_WIKI_AGENT_COMPILE_TURN_MAX_OUTPUT_TOKENS": (1024, 65536, True),
}

# Only these reviewed, byte-identical platform migrations can be applied by
# this helper. Future migrations must be reviewed and added in a later release.
KNOWN_MIGRATIONS = {
    "20261113105521_extract_tool_calls_backfill.sql": "8c524a0e9d8973a279bfe8efb78779d3b5c35aa3f5daa3541fd7e19e0b05b7e5",
    "20261114190000_landing_agent_runs.sql": "0222cfd15374b67a9b49139ed26a62c4b36b89d99c66f6bfbc574abda4c315dd",
    "20261114190100_landing_agent_run_params.sql": "13f216a186ce594b04f8c4784884009b257ad4fcfc90a70b1d1ec109330a801e",
    "20261114210000_metric_proposals.sql": "cac12641fb14f16e98ca982976f95e1e98068f9a5d81f62fff9fb54deff93c5e",
    "20261115100000_join_proposals.sql": "51935a60b42542a4fa35e5c624a8f601e57737b0bdd8ffe03fc29249d48593cf",
    "20261117110000_task_step_concurrency_15.sql": "985c4b6a2b9f3bc8eb71b295bac1681f9c68d8cf1cea224518f02dc713099ae5",
    "20261117160000_kb_schema_and_task_cleanup.sql": "0b1d6f71a25b8f91bd31344ffb9df515eb2a1340f1a77001dc3f4310f952821f",
}
KEY_RE = re.compile(r"^[A-Za-z0-9._~+/-]+$")
PLACEHOLDERS = {"", "sk-live-xxx", "change-me", "change-me-in-production"}
MANAGED_SYSCTL = Path("/etc/sysctl.d/90-jonex-wiki.conf")
MANAGED_CONTENT = "# Managed by wiki-runtime-ensure.py\nuser.max_user_namespaces = 64\n"
BUNDLED_PROFILE = "wiki-seccomp-x86_64.json"
# The reviewed candidate is derived from the test-server profile, with the
# legacy socketcall multiplexor blocked and socket() limited to address
# families below AF_ALG (38), which also excludes AF_VSOCK (40). Update only
# with review.
BUNDLED_PROFILE_SHA256 = "453f14ed59c1ebb1ffa154614ace09d71090879a87b19b7712a461f03a3af3e5"
# Hosts whose kernel has this switch (Ubuntu 23.10 and later) move unconfined
# processes that create a user namespace into a profile without capabilities.
# The Wiki container then needs a named profile that allows userns.
APPARMOR_ENABLED = Path("/sys/module/apparmor/parameters/enabled")
APPARMOR_USERNS_SWITCH = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
APPARMOR_PROFILE_NAME = "jonex-wiki-sandbox"
APPARMOR_PROFILE_SOURCE = Path(__file__).resolve().parent / "apparmor" / APPARMOR_PROFILE_NAME
MANAGED_APPARMOR_PROFILE = Path("/etc/apparmor.d") / APPARMOR_PROFILE_NAME


class Problem(Exception):
    pass


def run(args, *, cwd=None, capture=True, stdout=None):
    try:
        result = subprocess.run(
            [str(x) for x in args], cwd=cwd, universal_newlines=True,
            stdout=stdout if stdout is not None else (subprocess.PIPE if capture else None),
            stderr=subprocess.PIPE if capture else None, check=False,
        )
    except OSError as exc:
        raise Problem(f"无法运行 {args[0]}：{exc}") from exc
    if result.returncode:
        # Do not print command arguments or subprocess output: they may contain
        # connection details or credentials.
        raise Problem(f"{args[0]} 执行失败（退出码 {result.returncode}）；请检查权限、服务和日志。")
    return result.stdout or ""


def read_env(path):
    if not path.is_file() or path.is_symlink():
        raise Problem(f"缺少普通配置文件：{path}")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            key, value = match.groups()
            if key in values:
                raise Problem(f"{path} 中 {key} 重复，请先人工处理。")
            values[key] = value.strip()
    return values


def env_value(value):
    """Read ordinary dotenv values without expanding or printing secrets."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value.split(" #", 1)[0].strip()


def write_private(path, text):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise Problem(f"拒绝修改非普通文件：{path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wiki-ensure-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def update_env(path, changes):
    """Update exact KEY=value lines; preserve unrelated comments and values."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if path.exists():
        read_env(path)  # refuse duplicates and symlinks
    lines, found = [], set()
    for line in existing.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        key = match.group(1) if match else None
        if key in changes:
            lines.append(f"{key}={changes[key]}")
            found.add(key)
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in changes.items() if key not in found)
    write_private(path, "\n".join(lines) + "\n")


def ensure_writer_limits(root, check):
    """Prefer the larger supported writing budget and retain security overrides."""
    deploy = root / "deploy"
    runtime = deploy / ".wiki-runtime"
    overlay = runtime / "compile-limits.yml"
    if overlay.is_symlink():
        raise Problem(f"拒绝使用符号链接配置：{overlay}")
    effective = (compose_config(root)["services"].get("jonex-wiki", {}).get("environment") or {})
    template = read_env(deploy / ".env.jonex_wiki.example")
    configured = [
        read_env(file) for file in (deploy / ".env", deploy / ".env.jonex_wiki",
                                    runtime / "settings.env") if file.exists()
    ]
    limits = {}
    for key, (minimum, maximum, whole) in WRITER_LIMITS.items():
        values = [env_value(str(source[key])) for source in (template, *configured, effective, os.environ)
                  if source.get(key) not in (None, "")]
        try:
            numbers = [Decimal(value) for value in values if value]
            if not numbers or any(not value.is_finite() or value < minimum or value > maximum
                                  or (whole and value != value.to_integral_value())
                                  or (not whole and value == 0) for value in numbers):
                raise InvalidOperation()
        except InvalidOperation as exc:
            raise Problem(f"写作参数 {key} 超出有效范围，请检查配置。") from exc
        chosen = max(numbers)
        limits[key] = str(int(chosen)) if chosen == chosen.to_integral_value() else str(chosen)
    content = ("# Generated by wiki-runtime-ensure.py.\n"
               "services:\n  jonex-wiki:\n    environment:\n"
               + "".join(f"      {key}: {json.dumps(value)}\n" for key, value in limits.items()))
    if overlay.is_file() and overlay.read_text(encoding="utf-8") == content:
        return
    if check:
        raise Problem("Wiki 写作额度尚未同步；请运行 make up 准备运行配置。")
    write_private(overlay, content)
    print("✓ 已同步 Wiki 写作额度，保留更高的有效设置。")


def validate_profile(path):
    if not path or not path.is_file():
        raise Problem("缺少已审查的 seccomp 文件：使用 --seccomp /实际路径/seccomp.json。")
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Problem("seccomp 文件无法读取或不是合法 JSON。") from exc
    if not isinstance(profile, dict) or profile.get("defaultAction") not in {
        "SCMP_ACT_ERRNO", "SCMP_ACT_KILL", "SCMP_ACT_KILL_PROCESS"
    } or not isinstance(profile.get("syscalls"), list):
        raise Problem("seccomp 文件不是有效的默认拒绝策略。")
    for rule in profile["syscalls"]:
        if (not isinstance(rule, dict) or not isinstance(rule.get("names"), list)
                or not all(isinstance(name, str) and name for name in rule["names"])
                or not isinstance(rule.get("action"), str)
                or not isinstance(rule.get("args", []), list)
                or not all(isinstance(arg, dict) for arg in rule.get("args", []))):
            raise Problem("seccomp 文件包含格式错误的系统调用规则。")
    machine = os.uname().machine.lower()
    expected = {"x86_64": "SCMP_ARCH_X86_64", "aarch64": "SCMP_ARCH_AARCH64"}.get(machine)
    arches = profile.get("architectures")
    arch_map = profile.get("archMap")
    if expected and isinstance(arches, list) and expected not in arches:
        raise Problem(f"seccomp 文件不包含当前架构 {machine}。")
    if expected and isinstance(arch_map, list) and arch_map and not any(
        isinstance(item, dict) and item.get("architecture") == expected for item in arch_map
    ):
        raise Problem(f"seccomp 文件的 archMap 不包含当前架构 {machine}。")
    return path.resolve()


def find_profile(root, explicit):
    runtime = root / "deploy/.wiki-runtime"
    if (runtime / "compose.yml").exists():
        print(f"复用已有 Wiki 运行策略：{runtime / 'seccomp.json'}")
        return validate_profile(runtime / "seccomp.json")
    if explicit:
        print(f"使用指定的 Wiki 运行策略：{explicit}")
        return validate_profile(Path(explicit).expanduser())
    # Existing releases may have recorded their original Compose file location.
    try:
        labels = run(["docker", "inspect", "--format",
                      '{{index .Config.Labels "com.docker.compose.project.config_files"}}',
                      "jonex-wiki"])
        previous = Path(labels.strip().split(",")[-1]).parent / "wiki-seccomp.json"
        if previous.is_file():
            print(f"复用已有 Wiki 部署策略：{previous}")
            return validate_profile(previous)
    except Problem:
        pass
    bundled = root / "deploy" / BUNDLED_PROFILE
    machine = os.uname().machine.lower()
    if machine in {"x86_64", "amd64"} and bundled.is_file() and not bundled.is_symlink():
        if hashlib.sha256(bundled.read_bytes()).hexdigest() != BUNDLED_PROFILE_SHA256:
            raise Problem("项目内 Wiki seccomp 策略的校验和不符；请核对文件，不能自动使用。")
        print("使用项目内的 x86_64 Wiki seccomp 策略；请确认服务器安全策略允许。")
        return validate_profile(bundled)
    if machine not in {"x86_64", "amd64"}:
        raise Problem(f"项目内没有适用于 {machine} 的 Wiki seccomp 策略；请提供已审查的 --seccomp 文件。")
    return validate_profile(None)


def report_profile_risks(path):
    """Highlight known legacy network-policy gaps without replacing custom policy."""
    profile = json.loads(path.read_text(encoding="utf-8"))
    allowed = [rule for rule in profile.get("syscalls", [])
               if isinstance(rule, dict) and rule.get("action") == "SCMP_ACT_ALLOW"]
    if any("socketcall" in rule.get("names", []) for rule in allowed):
        print("警告：已有 seccomp 策略允许旧式 socketcall；请管理员审查网络隔离后再使用。")
    socket_rules = [rule for rule in allowed if "socket" in rule.get("names", [])]
    if any(socket_rule_allows(rule, family) for rule in socket_rules for family in (38, 40)):
        print("警告：已有 seccomp 策略未有效限制 AF_ALG 或 AF_VSOCK；请管理员审查。")


def _arg_allows(arg, value):
    expected = arg.get("value")
    return {
        "SCMP_CMP_NE": lambda: value != expected,
        "SCMP_CMP_LT": lambda: value < expected,
        "SCMP_CMP_LE": lambda: value <= expected,
        "SCMP_CMP_EQ": lambda: value == expected,
        "SCMP_CMP_GE": lambda: value >= expected,
        "SCMP_CMP_GT": lambda: value > expected,
        "SCMP_CMP_MASKED_EQ": lambda: (value & expected) == arg.get("valueTwo", 0),
    }.get(arg.get("op"), lambda: True)()


def socket_rule_allows(rule, family):
    """Whether runc would let this allow-rule pass socket(family, ...)."""
    args = [arg for arg in (rule.get("args") or []) if isinstance(arg, dict)]
    indexes = [arg.get("index") for arg in args]
    if len(indexes) != len(set(indexes)):
        # runc turns every condition into its own rule when an index repeats,
        # so the conditions are OR-ed instead of AND-ed.
        return any(arg.get("index") != 0 or _arg_allows(arg, family) for arg in args)
    return all(arg.get("index") != 0 or _arg_allows(arg, family) for arg in args)


def apparmor_mode():
    """The AppArmor option the Wiki container needs on this host, or None."""
    try:
        if APPARMOR_ENABLED.read_text(encoding="utf-8").strip() != "Y":
            return None
    except OSError:
        return None
    return APPARMOR_PROFILE_NAME if APPARMOR_USERNS_SWITCH.exists() else "unconfined"


def ensure_apparmor_profile(check):
    """Install the reviewed userns profile on hosts that restrict unconfined userns."""
    if apparmor_mode() != APPARMOR_PROFILE_NAME:
        return
    expected = APPARMOR_PROFILE_SOURCE.read_text(encoding="utf-8")
    target = MANAGED_APPARMOR_PROFILE
    if target.is_symlink():
        raise Problem(f"{target} 是符号链接；不自动修改。")
    if target.exists():
        if read_host_policy(target) != expected:
            raise Problem(f"{target} 与项目内 deploy/apparmor/{APPARMOR_PROFILE_NAME} 不一致；为保护原配置，不自动覆盖。")
        if not check:
            privileged(["apparmor_parser", "-r", str(target)])
        print(f"✓ AppArmor 策略 {APPARMOR_PROFILE_NAME} 已安装")
        return
    if check:
        raise Problem(f"宿主机限制非特权用户命名空间，Wiki 容器需要 AppArmor 策略 {APPARMOR_PROFILE_NAME}；请管理员运行 make wiki-runtime-setup。")
    print(f"修复：安装 AppArmor 策略 {APPARMOR_PROFILE_NAME}；需要管理员权限。")
    # cp -n avoids overwriting an administrator's concurrently created file.
    privileged(["cp", "-n", str(APPARMOR_PROFILE_SOURCE), str(target)])
    if read_host_policy(target) != expected:
        raise Problem("AppArmor 策略文件被并发修改；未加载。")
    privileged(["chmod", "0644", str(target)])
    privileged(["apparmor_parser", "-r", str(target)])
    print(f"✓ 已安装并加载 AppArmor 策略 {APPARMOR_PROFILE_NAME}；开机时由 AppArmor 自动加载。")


def validate_existing_runtime(root, profile):
    runtime = root / "deploy/.wiki-runtime"
    if runtime.is_symlink():
        raise Problem("Wiki 本机配置目录是符号链接；请管理员检查。")
    compose = runtime / "compose.yml"
    if compose.exists():
        if compose.is_symlink():
            raise Problem("Wiki Compose 配置为符号链接；请管理员检查。")
        content = compose.read_text(encoding="utf-8")
        required = (f"seccomp={profile}", "no-new-privileges:true", "cap_drop:",
                    "JONEX_WIKI_API_KEY", "JONEX_WIKI_URL")
        if "seccomp=unconfined" in content or any(token not in content for token in required):
            raise Problem("已有 Wiki Compose 配置与预期不一致；不覆盖自定义安全配置。")
    elif any((runtime / name).exists() for name in ("seccomp.json", "gateway.env", "settings.env")):
        raise Problem("发现未完成的 Wiki 本机配置；为保护已有文件，不自动覆盖。")
    for name in ("settings.env", "gateway.env"):
        if (runtime / name).exists():
            read_env(runtime / name)
    values = read_env(root / "deploy/.env.jonex_wiki")
    key = values.get("JONEX_WIKI_API_KEY", "")
    if key not in PLACEHOLDERS and not KEY_RE.fullmatch(key):
        raise Problem("Wiki 密钥格式不符合初始化脚本要求；保留原值，请管理员检查。")


def privileged(args):
    if os.geteuid() == 0:
        run(args, capture=False)
    else:
        if not shutil.which("sudo"):
            raise Problem("需要管理员权限调整命名空间；请安装 sudo 或由管理员运行。")
        run(["sudo", *args], capture=False)


def read_host_policy(path):
    try:
        return path.read_text(encoding="utf-8")
    except PermissionError:
        if os.geteuid() == 0 or not shutil.which("sudo"):
            raise Problem("无权限读取已有的宿主机命名空间配置。")
        return run(["sudo", "cat", str(path)])


def ensure_namespace(check):
    if sys.platform != "linux":
        raise Problem("此脚本的宿主机沙箱准备仅适用于 Linux。")
    value = run(["sysctl", "-n", "user.max_user_namespaces"]).strip()
    if not value.isdigit():
        raise Problem("无法读取用户命名空间上限。")
    if int(value) >= 64:
        print(f"✓ 用户命名空间上限：{value}（持久性应由管理员核对）")
        return
    if check:
        raise Problem(f"用户命名空间上限为 {value}，需要至少 64。")
    if MANAGED_SYSCTL.is_symlink():
        raise Problem(f"{MANAGED_SYSCTL} 是符号链接；不自动修改。")
    if MANAGED_SYSCTL.exists() and read_host_policy(MANAGED_SYSCTL) != MANAGED_CONTENT:
        raise Problem(f"{MANAGED_SYSCTL} 已有其他内容；为保护原配置，不自动覆盖。")
    print(f"修复：用户命名空间上限 {value} → 64；需要管理员权限。")
    # Install a dedicated file; never overwrite a pre-existing unmanaged one.
    fd, tmp = tempfile.mkstemp(prefix="wiki-sysctl-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(MANAGED_CONTENT)
        if not MANAGED_SYSCTL.exists():
            # cp -n avoids overwriting an administrator's concurrently created file.
            privileged(["cp", "-n", tmp, str(MANAGED_SYSCTL)])
            if read_host_policy(MANAGED_SYSCTL) != MANAGED_CONTENT:
                raise Problem("命名空间配置被并发修改；未应用。")
            privileged(["chmod", "0644", str(MANAGED_SYSCTL)])
        privileged(["sysctl", "-p", str(MANAGED_SYSCTL)])
    finally:
        os.unlink(tmp)
    after = run(["sysctl", "-n", "user.max_user_namespaces"]).strip()
    if not after.isdigit() or int(after) < 64:
        raise Problem("命名空间配置写入后未达到 64；请管理员检查宿主机策略。")
    print("✓ 用户命名空间配置已应用；请管理员确认重启后的配置顺序。")


def ensure_runtime(root, profile, check):
    deploy = root / "deploy"
    wiki_env = deploy / ".env.jonex_wiki"
    runtime = deploy / ".wiki-runtime"
    if runtime.is_symlink():
        raise Problem("Wiki 本机配置目录是符号链接；请管理员检查。")
    validate_existing_runtime(root, profile)
    values = read_env(wiki_env)
    key = values.get("JONEX_WIKI_API_KEY", "")
    if key in PLACEHOLDERS:
        if check:
            raise Problem("Wiki 内部密钥缺失或仍是模板占位值。")
        key = secrets.token_hex(32)
        update_env(wiki_env, {"JONEX_WIKI_API_KEY": key})
        print("✓ 已生成 Wiki 内部密钥（未显示密钥）。")
    elif not KEY_RE.fullmatch(key):
        raise Problem("Wiki 密钥格式不符合初始化脚本要求；保留原值，请管理员检查。")

    compose = runtime / "compose.yml"
    if not compose.exists():
        leftovers = [p for p in (runtime / "seccomp.json", runtime / "gateway.env",
                                  runtime / "settings.env") if p.exists()]
        if leftovers:
            raise Problem("发现未完成的 Wiki 本机配置；为保护已有文件，不自动覆盖。")
        if check:
            raise Problem("Wiki 本机运行配置尚未创建。")
        env = dict(os.environ, WIKI_SECCOMP_PROFILE=str(profile), JONEX_PLATFORM_DIR=str(root))
        result = subprocess.run(["bash", str(deploy / "wiki-runtime-setup.sh")],
                                cwd=root, env=env, check=False)
        if result.returncode:
            raise Problem("Wiki 运行配置初始化失败，请查看上方输出。")
    else:
        validate_existing_runtime(root, profile)

    settings_path = runtime / "settings.env"
    settings = read_env(settings_path) if settings_path.exists() else {}
    missing = {k: v for k, v in DEFAULTS.items() if not settings.get(k)}
    if missing:
        if check:
            raise Problem("Wiki 运行参数缺失：" + ", ".join(missing))
        update_env(settings_path, missing)
        settings.update(missing)
        print("✓ 已补齐缺失的 Wiki 运行参数。")
    for name in ("JONEX_WIKI_API_PARSE_CONCURRENCY", "JONEX_WIKI_API_COMPILE_CONCURRENCY",
                 "JONEX_WIKI_COMPILE_CONCURRENCY"):
        if settings[name] != "15":
            print(f"提示：{name} 使用自定义值，未覆盖。")
    gateway_path = runtime / "gateway.env"
    gateway = read_env(gateway_path) if gateway_path.exists() else {}
    if gateway.get("JONEX_WIKI_API_KEY") != key:
        if check:
            raise Problem("Wiki 与网关的内部密钥不一致。")
        update_env(gateway_path, {"JONEX_WIKI_API_KEY": key})
        print("✓ 已同步 Wiki 与网关的内部密钥。")
    print("✓ Wiki 本机配置已准备；沙箱实际效果需在启动后检查。")


def check_services(root):
    deploy = root / "deploy"
    main = read_env(deploy / ".env")
    wiki = read_env(deploy / ".env.jonex_wiki")
    llm_host = env_value(wiki.get("LLM_BINDING_HOST", ""))
    llm_key = env_value(wiki.get("LLM_BINDING_API_KEY", ""))
    if not llm_host or not llm_key:
        raise Problem("Wiki 模型网关地址或内部令牌未配置。")
    try:
        gateway_host = urlsplit(llm_host).hostname
    except ValueError as exc:
        raise Problem("Wiki 模型网关地址格式无效。") from exc
    if gateway_host == "llm-gateway":
        allowed = [x.strip() for x in env_value(main.get("LLMGW_INTERNAL_TOKENS", "")).split(",") if x.strip()]
        if allowed and not llm_key.startswith("${") and llm_key not in allowed:
            raise Problem("Wiki 模型网关内部令牌不在网关白名单中。")
        upstream_key = env_value(main.get("LLMGW_UPSTREAM_LLM_API_KEY", ""))
        if upstream_key == "your_upstream_llm_api_key_here":
            raise Problem("模型网关的上游密钥仍是模板占位值。")
    if not env_value(wiki.get("DATABASE_URL", "")):
        same_host = env_value(wiki.get("DB_HOST", "postgres")) == env_value(main.get("DB_HOST", "postgres"))
        same_user = env_value(wiki.get("DB_USERNAME", "jonex")) == env_value(main.get("DB_USERNAME", "jonex"))
        if same_host and same_user:
            main_pw = env_value(main.get("DB_PASSWORD", "change-me"))
            wiki_pw = env_value(wiki.get("DB_PASSWORD", "change-me"))
            if wiki_pw and not wiki_pw.startswith("${") and main_pw != wiki_pw:
                raise Problem("平台与 Wiki 的数据库口令不一致；请管理员核对。")
    if not env_value(wiki.get("LLM_MODEL", "")) and not env_value(wiki.get("DEFAULT_LLM_MODEL", "")):
        print("提示：Wiki 环境未指定默认模型；请确认知识库级模型配置。")
    print("✓ Wiki 的基础配置项存在；真实连通性尚未验证。")


def compose_config(root):
    """Resolve the same Compose files used by make up, without printing secrets."""
    deploy = root / "deploy"
    runtime = deploy / ".wiki-runtime"
    env = dict(os.environ)
    for file in (deploy / ".env", runtime / "settings.env", runtime / "gateway.env"):
        env.update({k: env_value(v) for k, v in read_env(file).items()})
    args = ["docker", "compose", "-f", "docker-compose.yml"]
    if sys.platform == "darwin":
        args += ["-f", "docker-compose.mac.yml"]
    args += ["-f", "docker-compose.override.yml", "-f", ".wiki-runtime/compose.yml"]
    if (runtime / "compile-limits.yml").is_file():
        args += ["-f", ".wiki-runtime/compile-limits.yml"]
    args += ["config", "--format", "json"]
    try:
        result = subprocess.run(args, cwd=deploy, env=env, universal_newlines=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            raise Problem("无法解析平台 Compose 配置；请检查 Docker Compose 和缺失的环境文件。")
        config = json.loads(result.stdout)
        if not isinstance(config, dict) or not isinstance(config.get("services"), dict):
            raise ValueError("services missing")
        return config
    except (OSError, ValueError) as exc:
        raise Problem("无法读取 Docker Compose 合并配置；请检查 Compose 版本。") from exc


def redis_mismatches(config):
    services = config["services"]
    redis = services.get("redis", {}).get("environment", {}) or {}
    password = str(redis.get("REDIS_PASSWORD") or "")
    if not password:
        return "", []
    mismatches = []
    for name, config_item in services.items():
        env = config_item.get("environment", {}) or {}
        value = env.get("REDIS_URL") if isinstance(env, dict) else None
        if not value:
            continue
        try:
            parsed = urlsplit(str(value))
        except ValueError as exc:
            raise Problem(f"{name} 的 Redis 地址格式无效；请管理员检查。") from exc
        if parsed.hostname != "redis":
            continue  # A custom external Redis is not ours to rewrite.
        if unquote(parsed.password or "") != password:
            if str(value) != "redis://redis:6379/0":
                raise Problem(f"{name} 的 Redis 地址为自定义配置且认证不匹配；不自动覆盖。")
            mismatches.append(name)
    return password, sorted(mismatches)


def redis_overlay(content, services):
    """Add mapping-style service overrides without replacing custom entries."""
    lines = content.splitlines()
    if lines.count("services:") != 1 or any("\t" in line for line in lines):
        raise Problem("已有 Compose 覆盖文件不是可安全修复的标准格式。")
    start = lines.index("services:")
    for name in services:
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i] and not lines[i][0].isspace() and not lines[i].startswith("#")), len(lines))
        pattern = re.compile(r"^  " + re.escape(name) + r":\s*(?:#.*)?$")
        hits = [i for i in range(start + 1, end) if pattern.match(lines[i])]
        if len(hits) > 1:
            raise Problem(f"Compose 中 {name} 配置重复；不自动修改。")
        if not hits:
            lines[end:end] = [f"  {name}:", "    environment:", '      REDIS_URL: "${REDIS_URL:?}"']
            continue
        service_start = hits[0]
        service_end = next((i for i in range(service_start + 1, end)
                            if re.match(r"^  [A-Za-z0-9_.-]+:\s*(?:#.*)?$", lines[i])), end)
        environments = [i for i in range(service_start + 1, service_end)
                        if re.match(r"^    environment:\s*(?:#.*)?$", lines[i])]
        if len(environments) != 1:
            raise Problem(f"{name} 的 Compose 环境配置不是标准映射格式；不自动修改。")
        pos = environments[0]
        env_end = next((i for i in range(pos + 1, service_end)
                        if re.match(r"^    [^ #]", lines[i])), service_end)
        if any(re.match(r"^\s*-\s", line) for line in lines[pos + 1:env_end]):
            raise Problem(f"{name} 使用列表式环境配置；不自动修改。")
        if any(re.match(r"^      REDIS_URL\s*:", line) for line in lines[pos + 1:env_end]):
            raise Problem(f"{name} 已有自定义 Redis 覆盖；不自动覆盖。")
        lines.insert(pos + 1, '      REDIS_URL: "${REDIS_URL:?}"')
    return "\n".join(lines) + "\n"


def ensure_redis(root, check):
    config = compose_config(root)
    password, mismatches = redis_mismatches(config)
    if not mismatches:
        print("✓ Compose 中使用本地 Redis 的地址与认证配置一致。")
        return
    names = ", ".join(mismatches)
    if check:
        raise Problem(f"以下服务未携带 Redis 口令：{names}")
    runtime = root / "deploy/.wiki-runtime"
    settings_path, compose_path = runtime / "settings.env", runtime / "compose.yml"
    settings = read_env(settings_path)
    desired = "redis://:" + quote(password, safe="") + "@redis:6379/0"
    old = settings.get("REDIS_URL")
    if old and env_value(old) != desired:
        raise Problem("Wiki 运行配置已有自定义 REDIS_URL；不自动覆盖。")
    previous_settings = settings_path.read_text(encoding="utf-8")
    previous_compose = compose_path.read_text(encoding="utf-8")
    repaired = redis_overlay(previous_compose, mismatches)
    try:
        update_env(settings_path, {"REDIS_URL": desired})
        write_private(compose_path, repaired)
        _, remaining = redis_mismatches(compose_config(root))
        if remaining:
            raise Problem("Redis 覆盖配置校验未通过。")
    except Exception:
        write_private(settings_path, previous_settings)
        write_private(compose_path, previous_compose)
        raise
    print(f"✓ 已修复 {len(mismatches)} 个服务的本地 Redis 认证配置；重新启动后生效。")


def validate_effective_runtime(root, profile):
    """Check merged Compose values, not merely strings in the overlay file."""
    services = compose_config(root)["services"]
    wiki, gateway = services.get("jonex-wiki", {}), services.get("gateway", {})
    installed_profile = (root / "deploy/.wiki-runtime/seccomp.json").resolve()
    if not installed_profile.is_file() or hashlib.sha256(installed_profile.read_bytes()).digest() != hashlib.sha256(profile.read_bytes()).digest():
        raise Problem("保存的 seccomp 文件与已检查的策略不一致；请重建本机运行配置：mv deploy/.wiki-runtime deploy/.wiki-runtime.bak && make wiki-runtime-setup")
    security = wiki.get("security_opt", []) or []
    seccomp = [str(value).split("=", 1)[1] for value in security if str(value).startswith("seccomp=")]
    if len(seccomp) != 1 or Path(seccomp[0]).resolve() != installed_profile:
        raise Problem("合并后的 Wiki 容器没有使用预期的 seccomp 文件。")
    nnp = [str(value).replace("=", ":") for value in security
           if str(value).startswith("no-new-privileges")]
    if nnp != ["no-new-privileges:true"]:
        raise Problem("合并后的 Wiki 容器未开启 no-new-privileges。")
    if "ALL" not in {str(value).upper() for value in wiki.get("cap_drop", []) or []}:
        raise Problem("合并后的 Wiki 容器未丢弃预期的能力。")
    mode = apparmor_mode()
    apparmor = [str(value).replace(":", "=", 1) for value in security if str(value).startswith("apparmor")]
    rebuild = "请重建本机运行配置：mv deploy/.wiki-runtime deploy/.wiki-runtime.bak && make wiki-runtime-setup"
    if mode == APPARMOR_PROFILE_NAME and apparmor != [f"apparmor={APPARMOR_PROFILE_NAME}"]:
        raise Problem(f"宿主机限制非特权用户命名空间，Wiki 容器需要 apparmor={APPARMOR_PROFILE_NAME}；{rebuild}")
    if mode == "unconfined" and not apparmor:
        raise Problem(f"宿主启用了 AppArmor，Wiki 容器需要 apparmor=unconfined 才能让沙箱创建挂载命名空间；{rebuild}")
    added = set()
    for value in wiki.get("cap_add", []) or []:
        name = str(value).upper()
        added.add(name[4:] if name.startswith("CAP_") else name)
    if added - {"SETFCAP"}:
        raise Problem("Wiki 容器加回了 SETFCAP 以外的能力；请管理员检查。")
    if "SETFCAP" not in added:
        raise Problem("Wiki 容器以 root 运行，沙箱映射 uid 0 需要 cap_add: SETFCAP；请重建本机运行配置：mv deploy/.wiki-runtime deploy/.wiki-runtime.bak && make wiki-runtime-setup")
    if wiki.get("privileged") or wiki.get("pid") == "host":
        raise Problem("Wiki 容器启用了特权或宿主机 PID；请管理员检查。")
    mounts = wiki.get("volumes", []) or []
    bwrap = [item for item in mounts if isinstance(item, dict) and item.get("target") == "/usr/bin/bwrap"]
    if len(bwrap) != 1 or bwrap[0].get("source") != "/dev/null" or bwrap[0].get("read_only") is not True:
        raise Problem("合并后的 Wiki 容器缺少沙箱兼容挂载。")
    wiki_key = (wiki.get("environment", {}) or {}).get("JONEX_WIKI_API_KEY")
    gateway_key = (gateway.get("environment", {}) or {}).get("JONEX_WIKI_API_KEY")
    if not wiki_key or wiki_key != gateway_key:
        raise Problem("合并后的 Wiki 与网关内部密钥不一致。")
    print("✓ 已核对合并后的 Wiki 容器沙箱与网关配置；仍需启动后预检。")


def db_command(container, user, database, query):
    return run(["docker", "exec", container, "psql", "-X", "-v", "ON_ERROR_STOP=1",
                "-U", user, "-d", database, "-At", "-c", query])


def pending_migrations(root, container, user, database, allow_reviewed_new=False):
    folder = root / "deploy/postgres/update"
    files = {p.name: p for p in folder.glob("*.sql") if p.is_file()}
    exists = db_command(container, user, database,
                        "SELECT COALESCE(to_regclass('public.schema_migrations')::text, '');").strip()
    applied = set(db_command(container, user, database,
                             "SELECT filename FROM public.schema_migrations;").splitlines()) if exists else set()
    pending = sorted(set(files) - applied)
    for name in pending:
        digest = hashlib.sha256(files[name].read_bytes()).hexdigest()
        if KNOWN_MIGRATIONS.get(name) != digest and not allow_reviewed_new:
            raise Problem(f"待执行迁移 {name} 不在已审查清单中或内容发生变化；请管理员处理。")
    return pending


def backup_database(root, container, user, database):
    directory = root / "deploy/.wiki-runtime/backups"
    if directory.is_symlink():
        raise Problem("备份目录是符号链接；请管理员检查。")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    size = db_command(container, user, database, "SELECT pg_database_size(current_database());").strip()
    if not size.isdigit():
        raise Problem("无法估算数据库大小；未执行迁移。")
    # Leave room for concurrent activity and a conservative uncompressed-size estimate.
    required = max(1024**3, int(size) * 3 // 2 + 256 * 1024**2)
    if shutil.disk_usage(directory).free < required:
        raise Problem("备份目录剩余空间不足；未执行迁移，请先由管理员处理。")
    fd, tmp = tempfile.mkstemp(prefix=".wiki-db-", dir=str(directory))
    safe_database = re.sub(r"[^A-Za-z0-9_.-]", "_", database)[:64] or "database"
    name = safe_database + datetime.now(timezone.utc).strftime("-%Y%m%dT%H%M%SZ-") + secrets.token_hex(4) + ".dump"
    destination = directory / name
    try:
        with os.fdopen(fd, "wb") as stream:
            result = subprocess.run(["docker", "exec", container, "pg_dump", "-U", user,
                                     "-d", database, "-Fc", "--no-owner"],
                                    stdout=stream, stderr=subprocess.PIPE, check=False)
            stream.flush()
            os.fsync(stream.fileno())
        if result.returncode or os.path.getsize(tmp) == 0:
            raise Problem("数据库备份失败；未执行迁移。")
        with open(tmp, "rb") as stream:
            verify = subprocess.run(["docker", "exec", "-i", container, "pg_restore", "--list"],
                                    stdin=stream, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, check=False)
        if verify.returncode:
            raise Problem("数据库备份目录校验失败；未执行迁移。")
        os.replace(tmp, destination)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return destination


def ensure_database(root, check, allow_reviewed_new=False, backed_up=None):
    backed_up = backed_up if backed_up is not None else set()
    container = os.environ.get("JONEX_PG_CONTAINER", "jonex-postgres")
    user = os.environ.get("JONEX_PG_USER", "jonex")
    database = os.environ.get("JONEX_PG_DB", "jonex")
    print(f"数据库目标：容器 {container}，数据库 {database}，用户 {user}")
    pending = pending_migrations(root, container, user, database, allow_reviewed_new)
    if not pending:
        print("✓ 平台数据库没有待执行的已知迁移。")
        return backed_up
    print("待执行的平台迁移：" + ", ".join(pending))
    if check:
        raise Problem("平台数据库有待执行迁移。")
    folder = root / "deploy/postgres/update"
    fingerprints = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in pending}
    if database not in backed_up:
        backup = backup_database(root, container, user, database)
        backed_up.add(database)
        print(f"✓ 已备份平台数据库：{backup}")
    else:
        print("✓ 平台数据库已在本次运行中备份。")
    # Recheck after the potentially long backup; never run changed or unknown SQL.
    if pending_migrations(root, container, user, database, allow_reviewed_new) != pending:
        raise Problem("备份期间迁移状态发生变化；未自动执行迁移。")
    for name in pending:
        remaining = pending_migrations(root, container, user, database, allow_reviewed_new)
        if name not in remaining:
            raise Problem("迁移状态被其他操作改变；已停止自动执行。")
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != fingerprints[name]:
            raise Problem(f"迁移 {name} 的内容在备份后发生变化；未执行该迁移。")
        run(["bash", root / "deploy/postgres/update/apply.sh", "--only", name, "--yes"],
            cwd=root, capture=False)
    if pending_migrations(root, container, user, database, allow_reviewed_new):
        raise Problem("执行后仍有待处理的平台迁移。")
    print("✓ 已执行并登记已知平台数据库迁移。")
    return backed_up


def migration_chain(folder):
    """Read a linear Alembic history without importing application code."""
    parents = {}
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            raise Problem(f"Alembic 迁移文件语法无效：{path.name}") from exc
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = [item.id for item in node.targets if isinstance(item, ast.Name)]
                value_node = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names, value_node = [node.target.id], node.value
            else:
                continue
            for name in names:
                if name in {"revision", "down_revision"}:
                    try:
                        values[name] = ast.literal_eval(value_node)
                    except (ValueError, TypeError):
                        raise Problem(f"无法识别迁移版本：{path.name}")
        revision, parent = values.get("revision"), values.get("down_revision")
        if not isinstance(revision, str) or not (parent is None or isinstance(parent, str)) or revision in parents:
            raise Problem(f"Alembic 迁移不是可自动检查的单线历史：{path.name}")
        parents[revision] = parent
    if not parents:
        raise Problem(f"找不到 Alembic 迁移文件：{folder}")
    heads = set(parents) - {p for p in parents.values() if p is not None}
    if len(heads) != 1 or any(p is not None and p not in parents for p in parents.values()):
        raise Problem(f"Alembic 迁移存在分支或缺失：{folder}")
    head = next(iter(heads))
    cursor, seen = head, set()
    while cursor is not None:
        if cursor in seen:
            raise Problem(f"Alembic 迁移存在循环：{folder}")
        seen.add(cursor)
        cursor = parents[cursor]
    if seen != set(parents):
        raise Problem(f"Alembic 迁移存在未连接的版本：{folder}")
    return parents, head


def engine_databases(root):
    wiki = read_env(root / "deploy/.env.jonex_wiki")
    wiki_url = env_value(wiki.get("DATABASE_URL", ""))
    if wiki_url:
        try:
            parsed = urlsplit(wiki_url)
        except ValueError as exc:
            raise Problem("Wiki 数据库地址格式无效。") from exc
        wiki_host, wiki_db = parsed.hostname, unquote(parsed.path.lstrip("/"))
        if not parsed.scheme.startswith("postgres"):
            raise Problem("Wiki 使用非 PostgreSQL 数据库；请管理员核对其 Alembic 版本和备份。")
    else:
        wiki_host = env_value(wiki.get("DB_HOST", "postgres"))
        wiki_db = env_value(wiki.get("DB_NAME", "jonex"))
    if wiki_host != "postgres":
        raise Problem("Wiki 使用非本机 Compose PostgreSQL；请管理员核对数据库版本和备份。")
    if not wiki_db or "${" in wiki_db:
        raise Problem("数据库名未配置或包含未解析变量；请管理员核对。")
    schema = env_value(wiki.get("DB_SCHEMA", "jonex_wiki"))
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise Problem("Wiki 数据库 schema 名称无法安全检查。")
    return [("Wiki", wiki_db, schema, root / "Reference/JonexWiki/src/jonex_wiki/storage/migrations/versions")]


def inspect_engine_migrations(root):
    container = os.environ.get("JONEX_PG_CONTAINER", "jonex-postgres")
    user = os.environ.get("JONEX_PG_USER", "jonex")
    plans = []
    for label, database, schema, folder in engine_databases(root):
        parents, head = migration_chain(folder)
        qualified = f'"{schema}"."alembic_version"'
        exists = db_command(container, user, database,
                            f"SELECT COALESCE(to_regclass('{qualified}')::text, '');").strip()
        rows = db_command(container, user, database,
                          f'SELECT version_num FROM {qualified} ORDER BY version_num;').splitlines() if exists else []
        if len(rows) > 1 or (rows and rows[0] not in parents):
            raise Problem(f"{label} 数据库迁移版本无法与当前源码对应；请管理员检查。")
        current = rows[0] if rows else None
        pending, cursor, visited = [], head, set()
        while cursor != current:
            if cursor is None or cursor in visited:
                raise Problem(f"{label} 数据库迁移历史不连续；请管理员检查。")
            visited.add(cursor)
            pending.append(cursor)
            cursor = parents[cursor]
        pending.reverse()
        if pending:
            print(f"{label} 数据库 {database}：当前 {current or '未初始化'}，启动时升级到 {head}。")
            print("待执行版本：" + ", ".join(pending))
        else:
            print(f"✓ {label} 数据库 {database} 已是源码迁移版本 {head}。")
        plans.append((label, database, pending))
    return plans


def prepare_engine_backups(root, plans, backed_up):
    container = os.environ.get("JONEX_PG_CONTAINER", "jonex-postgres")
    user = os.environ.get("JONEX_PG_USER", "jonex")
    for label, database, pending in plans:
        if pending and database not in backed_up:
            backup = backup_database(root, container, user, database)
            backed_up.add(database)
            print(f"✓ {label} 数据库升级前备份：{backup}")
    if any(pending for _, _, pending in plans):
        print("提示：Wiki/Anything 数据库升级由新容器启动时执行，启动后仍需核对结果。")


def ensure_local_docker_context():
    """The host sysctl must describe the daemon that will run the container."""
    host = os.environ.get("DOCKER_HOST", "").strip()
    if os.environ.get("DOCKER_CONTEXT") or not host:
        host = run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]).strip()
    if not host.startswith("unix://"):
        raise Problem("Wiki 沙箱初始化要求在 Docker 宿主机本地执行；当前 Docker 连接不是本地 Unix 套接字。")


def prepare_for_up(root, explicit, configure_host):
    """Prepare only Wiki runtime files; do not alter databases on ordinary up."""
    root = root.resolve()
    if sys.platform != "linux":
        if configure_host:
            raise Problem("请在目标 Linux Docker 服务器上配置用户命名空间。")
        if (root / "deploy/.wiki-runtime/compose.yml").is_file():
            ensure_writer_limits(root, False)
        print("提示：当前不是 Linux 宿主机，保留现有 Wiki 沙箱配置。")
        return
    ensure_local_docker_context()
    profile = find_profile(root, explicit)
    report_profile_risks(profile)
    validate_existing_runtime(root, profile)
    ensure_namespace(not configure_host)
    ensure_apparmor_profile(not configure_host)
    ensure_runtime(root, profile, False)
    ensure_writer_limits(root, False)
    validate_effective_runtime(root, profile)
    print("✓ Wiki 运行配置已检查；make up 不会自动执行平台数据库迁移。")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="平台仓库根目录，默认脚本的上一级目录")
    parser.add_argument("--seccomp", default=os.environ.get("WIKI_SECCOMP_PROFILE"),
                        help="目标机器上已审查的 seccomp JSON 文件")
    parser.add_argument("--check", action="store_true", help="只检查，不修改配置、主机或数据库")
    parser.add_argument("--repair", action="store_true", help="明确允许修复配置、宿主机并在备份后执行已审查的迁移")
    parser.add_argument("--prepare-up", action="store_true", help="启动前准备 Wiki 配置和命名空间，不执行数据库迁移")
    parser.add_argument("--configure-host", action="store_true", help="允许持久配置用户命名空间和 Wiki 的 AppArmor 策略，需管理员权限")
    parser.add_argument("--reviewed-new-migrations", action="store_true",
                        help="管理员已审查新增或变动的迁移：备份后允许执行")
    args = parser.parse_args()
    if not args.prepare_up and not args.repair:
        args.check = True
    root = args.root.resolve()
    try:
        if args.configure_host and not args.prepare_up:
            raise Problem("--configure-host 必须与 --prepare-up 一起使用。")
        if args.check and (args.prepare_up or args.configure_host):
            raise Problem("--check 不可与初始化选项同时使用。")
        if args.repair and (args.check or args.prepare_up):
            raise Problem("--repair 不可与检查或初始化选项同时使用。")
        if args.reviewed_new_migrations and not args.repair:
            raise Problem("--reviewed-new-migrations 必须与 --repair 一起使用。")
        if not args.check and os.geteuid() == 0 and os.environ.get("SUDO_USER"):
            raise Problem("请以部署用户运行脚本，不要对整个脚本使用 sudo；需要系统权限时脚本会调用 sudo。")
        if not (root / "deploy/wiki-runtime-setup.sh").is_file():
            raise Problem("该平台代码缺少 Wiki 运行入口；请先拉取包含新版 Wiki 的 dev。")
        if args.prepare_up:
            prepare_for_up(root, args.seccomp, args.configure_host)
            return 0
        if not (root / "deploy/postgres/update/apply.sh").is_file():
            raise Problem("缺少平台数据库迁移脚本。")
        if args.check:
            problems = []
            profile = None
            def inspect(label, operation):
                try:
                    return operation()
                except (Problem, OSError, UnicodeError) as exc:
                    problems.append(f"{label}：{exc}")
                    print(f"✗ {label}：{exc}")
                    return None
            inspect("服务配置", lambda: check_services(root))
            profile = inspect("沙箱规则", lambda: find_profile(root, args.seccomp))
            if profile is not None:
                print(f"✓ seccomp 文件基础检查通过：{profile}；适用性仍需管理员审查。")
                inspect("Wiki 运行配置", lambda: ensure_runtime(root, profile, True))
            inspect("命名空间", lambda: ensure_namespace(True))
            inspect("AppArmor", lambda: ensure_apparmor_profile(True))
            runtime = root / "deploy/.wiki-runtime"
            if all((runtime / name).is_file() for name in ("compose.yml", "settings.env", "gateway.env")):
                inspect("写作额度", lambda: ensure_writer_limits(root, True))
                inspect("Redis 配置", lambda: ensure_redis(root, True))
                if profile is not None:
                    inspect("容器沙箱配置", lambda: validate_effective_runtime(root, profile))
            else:
                problems.append("Redis 配置：缺少完整的 Wiki 本机覆盖配置。")
                print("✗ Redis 配置：缺少完整的 Wiki 本机覆盖配置。")
            inspect("平台数据库", lambda: ensure_database(root, True, args.reviewed_new_migrations))
            plans = inspect("引擎数据库", lambda: inspect_engine_migrations(root))
            if plans is not None and any(pending for _, _, pending in plans):
                problems.append("引擎数据库：新容器启动时有待执行升级。")
                print("✗ 引擎数据库：新容器启动时有待执行升级。")
            print(f"检查完成：{len(problems)} 项需要处理；未修改配置、宿主机或数据库。")
            print("未验证真实模型和解析连通性、图片效果、容器沙箱实测及重启后的命名空间持久性。")
            return 1 if problems else 0
        # Check external settings first, before any host/database mutation.
        ensure_local_docker_context()
        check_services(root)
        profile = find_profile(root, args.seccomp)
        print(f"✓ seccomp 文件基础检查通过：{profile}；适用性仍需管理员审查。")
        validate_existing_runtime(root, profile)
        # Validate the target DB and pending scripts before changing the host.
        pending_migrations(root, os.environ.get("JONEX_PG_CONTAINER", "jonex-postgres"),
                           os.environ.get("JONEX_PG_USER", "jonex"),
                           os.environ.get("JONEX_PG_DB", "jonex"), args.reviewed_new_migrations)
        engine_plans = inspect_engine_migrations(root)
        ensure_namespace(args.check)
        ensure_apparmor_profile(args.check)
        ensure_runtime(root, profile, args.check)
        ensure_writer_limits(root, args.check)
        ensure_redis(root, args.check)
        validate_effective_runtime(root, profile)
        backed_up = set()
        prepare_engine_backups(root, engine_plans, backed_up)
        if inspect_engine_migrations(root) != engine_plans:
            raise Problem("备份期间引擎数据库迁移状态发生变化；未自动执行平台迁移。")
        ensure_database(root, False, args.reviewed_new_migrations, backed_up)
    except (Problem, OSError, UnicodeError) as exc:
        print(f"未完成：{exc}", file=sys.stderr)
        return 1
    print("下一步：make build && make up && make wiki-check")
    print("提示：真实模型、解析、图片链路及容器启动后的升级结果未由此脚本验收。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
