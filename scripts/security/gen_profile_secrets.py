#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""为一个**全新生产环境**画像生成生产用密钥值（本地执行，直接改写画像文件）。

与另外两个脚本的分工::

    rotate_secrets.py   只认一小组硬编码键，且**只能在已有行上改值** —— 补不出缺失的
                        ``NEO4J_PASSWORD`` / ``MINIO_ROOT_*`` 行；公开值名单也窄，实测漏掉
                        ``LIGHTRAG_API_KEY`` / ``JONEX_WIKI_API_KEY`` / ``AUTH_ACCOUNTS`` /
                        ``TOKEN_SECRET`` / ``POSTGRES_PASSWORD``。
    audit_profile.py    只读体检，不改文件。
    本脚本              面向「新环境一次成型」：生成**平台自管**的全部密钥，补上模板有而
                        画像缺的行，并顺手清理只对「新环境」有意义的遗留值。

⚠️ 安全设计：只用**显式白名单**驱动生成（``PLATFORM_SECRETS``），**绝不**按「名字像密钥」
就随机化。原因是一次真实踩坑：按名字匹配会把 ``LLMGW_UPSTREAM_LLM_API_KEY``（上游 LLM 厂商
key）、``MPS_SECRET_ID/KEY``（腾讯云 MPS）、``MINERU_API_TOKEN``、``ASR_API_KEY`` 一起重签，
直接打断上游模型与云服务。这些是**外部签发**的凭据，多环境共用同一份是正常的，不属于
「跨环境复用泄漏」。脚本对它们只做**只读提示**，不修改。

「需要替换」的判定（仅对 PLATFORM_SECRETS 内的键生效）::

    1. 值为空； 2. 值等于对应 ``.example`` 模板值； 3. 命中公开值黑名单
    （复用 ``rotate_secrets.PUBLIC_DEFAULTS``）； 4. 与**其它画像**同名键取值相同
    —— 从别的环境拷贝画像时最常见的错。

默认幂等：跑第二次应报「（无需改动）」。强制全部重签用 ``--fresh``（仍只作用于白名单内）。

用法::

    scripts/security/gen_profile_secrets.py --profile deploy/env/91 --dry-run
    scripts/security/gen_profile_secrets.py --profile deploy/env/91
    scripts/security/gen_profile_secrets.py --profile deploy/env/91 --show-values
    scripts/security/gen_profile_secrets.py --profile deploy/env/91 --fresh
    scripts/security/gen_profile_secrets.py --profile deploy/env/91 --redis-password

退出码：0 = 正常（含「无需改动」）；2 = 参数/文件错误。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import secrets
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rotate_secrets as R  # noqa: E402  （复用 PUBLIC_DEFAULTS / _new_secret / LLMGW_KEY_GROUP）

# ── 平台**自管**密钥：这些由本平台自己签发，各环境必须独立，可以安全重签 ──────────
# 需要「跨文件同值」的组：组内共享同一个新值
SAME_VALUE_GROUPS: list[list[str]] = [
    ["JWT_SECRET"],
    ["GATEWAY_API_KEY"],
    ["INTERNAL_API_KEY"],
    ["MCP_KEY_SECRET"],
    ["KEYCLOAK_CLIENT_SECRET"],
    ["JONEX_WIKI_API_KEY"],
    ["DATA_SOURCE_SECRET_KEY"],
    ["NEO4J_PASSWORD"],
    ["MILVUS_PASSWORD"],
    ["DB_PASSWORD", "POSTGRES_PASSWORD"],          # LightRAG 写同一个 jonex 库
    ["LIGHTRAG_API_KEY"],
    ["MINIO_ROOT_USER", "MINIO_ACCESS_KEY"],       # compose 读 ROOT_*，旧名保持同步
    ["MINIO_ROOT_PASSWORD", "MINIO_SECRET_KEY"],
    sorted(R.LLMGW_KEY_GROUP),                     # 服务端白名单 + 全部客户端令牌
]
# 不属于任何组、但同样由平台自管的键（逐键判定）
PLATFORM_SINGLES = ["TOKEN_SECRET", "AUTH_ACCOUNTS"]
PLATFORM_SECRETS: set[str] = {k for g in SAME_VALUE_GROUPS for k in g} | set(PLATFORM_SINGLES)

# 外部签发：**绝不生成**，仅在结尾列入手工清单
NEVER_GENERATE = {
    "COS_SECRET_ID": "腾讯云控制台的真实 SecretId",
    "COS_SECRET_KEY": "腾讯云控制台的真实 SecretKey",
    "MPS_SECRET_ID": "腾讯云 MPS 凭据",
    "MPS_SECRET_KEY": "腾讯云 MPS 凭据",
    "MINERU_API_TOKEN": "MinerU 解析服务 token",
    "ASR_API_KEY": "ASR 服务 token",
    "TCADP_API_KEY": "TCADP 侧签发（生成会让对端不一致）",
    "TCADP_WEBHOOK_SECRET": "TCADP 侧签发",
    "KEYCLOAK_REALM_ADMIN_CLIENT_SECRET": "Keycloak realm 管理客户端，须与 Keycloak 侧一致",
}
# 名字或取值一看就是外部凭据 → 任何情况下都不改（双保险，防止白名单写错）
_EXTERNAL_NAME = re.compile(r"(UPSTREAM|COS_|MPS_|MINERU|ASR_|TCADP|KWIKI|REALM_ADMIN)", re.I)
_EXTERNAL_VALUE = re.compile(r"^(sk-|AKID|AKLT|eyJ)")

# 新环境里应当**清空**的遗留值（保留它没有收益，只是多一个被接受的盐/口令）
CLEAR_ON_NEW = {
    "MCP_KEY_SECRET_LEGACY": "新环境没有用旧盐签发的 MCP Key；保留它只会多一个被接受的 HMAC 盐",
}

# 允许「在某些文件里为空」的键：引擎可单独部署并自带这些 token，平台侧不给是**合法**的，
# 因此不算漂移（例如 .env.jonex_wiki 的 MINERU_API_TOKEN 只服务于单独部署）。
# 注意：仅豁免「一份有值 + 其余为空」；若两处**非空值不同**，仍按真实漂移报出。
ALLOW_EMPTY = {
    "MINERU_API_TOKEN": "jonex_wiki 单独部署时自带；平台部署不给是合法的",
}

# 模板有、画像缺就必须补的行（缺了会静默回落到硬编码公开默认值）
MUST_PRESENT: dict[str, dict] = {
    "ENV": {"files": ["main"], "value": "prod",
            "why": "缺/非 prod ⇒ 落回代码默认 dev ⇒ `Bearer jonex_test_<任意租户>` 即成通行证"},
    "MCP_KEY_SECRET": {"files": ["main", ".env.mcp"],
                       "why": "缺 ⇒ HMAC 盐回退 JWT_SECRET，轮换 JWT_SECRET 会连带作废全部 ingest/MCP key"},
    "DATA_SOURCE_SECRET_KEY": {"files": ["main"],
                               "why": "缺 ⇒ Fernet 主密钥从 JWT_SECRET 派生，轮换 JWT_SECRET 会让库里已加密凭据全部解不开"},
    "MINIO_ROOT_USER": {"files": ["main"], "why": "缺 ⇒ compose 回落到 minioadmin"},
    "MINIO_ROOT_PASSWORD": {"files": ["main"], "why": "缺 ⇒ compose 回落到 minioadmin"},
    "NEO4J_PASSWORD": {"files": ["main", ".env.rag"],
                       "why": "缺 ⇒ compose 与 neo4j_client 双双回落到硬编码默认口令；主文件是 Neo4j 容器与 KB 的来源，.env.rag 是 LightRAG 的来源，两处必须同值"},
}

# 名字像密钥、实际是配置值：绝不能随机化
_NON_SECRET_SUFFIX = ("_TOKENS", "_TOKEN_LIMIT", "_EXPIRE_HOURS", "_KEY_PREFIX",
                      "_MAX_CONNECTIONS", "_CONNECTIONS", "_POOL_SIZE")
_NON_SECRET_EXACT = {"COS_KEY_PREFIX", "COS_REGION", "COS_BUCKET", "EMBEDDING_TOKEN_LIMIT",
                     "AUTH_TOKEN_EXPIRE_HOURS", "TOKEN_EXPIRE_HOURS"}
_SECRETISH = re.compile(r"(SECRET|PASSWORD|PASSWD|_KEY|KEY_|TOKEN|CREDENTIAL|ACCOUNTS)", re.I)
# 一眼就是占位串的取值（仓库模板/文档里常见），即使没被登记进黑名单也该换掉
_LOOKS_PLACEHOLDER = re.compile(r"(your_|change[-_]?me|placeholder|example|xxx+|todo|dummy)", re.I)

# 派生值：URL 里内嵌的口令（由 DB 口令组统一决定，不能独立随机）
_URL_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^:/@]*)(?::(?P<pw>[^@]*))?@(?P<rest>.+)$")
_DB_URL_KEYS = ("DATABASE_URL", "DB_URL")

BACKUP_STAMP = "%Y%m%dT%H%M%SZ"


# ── env 文件模型 ──────────────────────────────────────────────────
def _active_key(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    return s.split("=", 1)[0].strip()


class EnvFile:
    def __init__(self, path: Path, template: Path | None):
        self.path = path
        self.template = template
        text = path.read_text(encoding="utf-8")
        self.trailing_nl = text.endswith("\n")
        self.lines: list[str] = text.split("\n")
        if self.trailing_nl:
            self.lines.pop()
        self.index: dict[str, int] = {}
        for i, ln in enumerate(self.lines):
            k = _active_key(ln)
            if k:
                self.index[k] = i
        self.tpl = _template_values(template)

    def get(self, key: str) -> str | None:
        i = self.index.get(key)
        return None if i is None else self.lines[i].split("=", 1)[1].strip()

    def set(self, key: str, value: str) -> None:
        self.lines[self.index[key]] = f"{key}={value}"

    def clear(self, key: str) -> None:
        self.lines[self.index[key]] = f"{key}="

    def add(self, key: str, value: str, comment: str = "") -> None:
        if comment:
            self.lines.append(f"# {comment}")
        self.lines.append(f"{key}={value}")
        self.index[key] = len(self.lines) - 1

    def render(self) -> str:
        return "\n".join(self.lines) + ("\n" if self.trailing_nl else "")


def _template_values(path: Path | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path or not path.is_file():
        return out
    for ln in path.read_text(encoding="utf-8").splitlines():
        k = _active_key(ln)
        if k:
            out[k] = ln.split("=", 1)[1].strip()
    return out


def _pick_main(envs: list[EnvFile]) -> EnvFile:
    for e in envs:
        if e.path.name == ".env":
            return e
    return envs[0]


def _gen(key: str) -> str:
    """生成新值；口径与 rotate_secrets._new_secret 一致（MINIO 用 32 位字母数字）。"""
    if key == "ENV":
        return "prod"
    if key == "AUTH_ACCOUNTS":
        return "admin:" + secrets.token_urlsafe(24)
    return R._new_secret(key)


def _is_public(key: str, value: str) -> bool:
    return value.strip() in R.PUBLIC_DEFAULTS.get(key, set())


def _placeholder(key: str, v: str, tpl_v: str | None, sibs: set[str]) -> bool:
    """该取值是否「不是真实密钥」（空/等于模板/命中黑名单/与兄弟画像同值/一眼占位串）。"""
    return (not v.strip() or v == tpl_v or _is_public(key, v)
            or v in sibs or bool(_LOOKS_PLACEHOLDER.search(v)))


def _external(key: str, value: str | None) -> bool:
    return bool(_EXTERNAL_NAME.search(key)) or bool(value and _EXTERNAL_VALUE.match(value))


def _secretish(key: str) -> bool:
    if key in _NON_SECRET_EXACT or key.endswith(_NON_SECRET_SUFFIX) or key.endswith("_URL"):
        return False
    return bool(_SECRETISH.search(key))


def _sibling_values(profile: Path) -> dict[str, set[str]]:
    """同级其它画像里，**平台自管**密钥出现过的取值（用于发现跨环境复用）。"""
    out: dict[str, set[str]] = {}
    root = profile.resolve().parent
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.resolve() == profile.resolve() or d.name.startswith("_"):
            continue
        for f in sorted(d.glob(".env*")):
            if ".bak." in f.name or not f.is_file():
                continue
            for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                k = _active_key(ln)
                if not k or k not in PLATFORM_SECRETS:
                    continue
                v = ln.split("=", 1)[1].strip()
                if v:
                    out.setdefault(k, set()).add(v)
    return out


# ── 主流程 ────────────────────────────────────────────────────────
def run(args: argparse.Namespace) -> int:
    profile = Path(args.profile)
    if not profile.is_dir():
        print(f"🔴 画像目录不存在: {profile}", file=sys.stderr)
        return 2
    tpl_dir = Path(args.templates)
    files = [p for p in sorted(profile.glob(".env*")) if p.is_file() and ".bak." not in p.name]
    if not files:
        print(f"🔴 {profile} 下没有 .env* 文件", file=sys.stderr)
        return 2

    envs = [EnvFile(p, tpl_dir / f"{p.name}.example") for p in files]
    by_name = {e.path.name: e for e in envs}
    main = _pick_main(envs)
    siblings = _sibling_values(profile)
    sibling_names = ", ".join(sorted(d.name for d in profile.resolve().parent.iterdir()
                                     if d.is_dir() and d.resolve() != profile.resolve()
                                     and not d.name.startswith("_")))

    print(f"画像目录: {profile}")
    print(f"模板目录: {tpl_dir}")
    print(f"文件({len(envs)}): {', '.join(e.path.name for e in envs)}")
    print(f"模式: {'--fresh（白名单内全部重签）' if args.fresh else '默认（只换需要替换的）'}\n")

    plan: dict[str, list[tuple[str, str, str]]] = {}
    notes: list[str] = []

    def why_replace(key: str, e: EnvFile) -> str:
        """该键在该文件里需要替换的原因；'' = 不需要。"""
        if args.fresh:
            return "fresh"
        v = e.get(key)
        if v is None or not v.strip():
            return "空值"
        if e.tpl.get(key) == v:
            return "等于模板值"
        if _is_public(key, v):
            return "命中公开值黑名单"
        if v in siblings.get(key, set()):
            return f"与其它画像复用同一值（{sibling_names}）"
        if _LOOKS_PLACEHOLDER.search(v):
            return "一眼是占位串"
        return ""

    group_new: dict[str, str] = {}

    # ── A. 补缺失行（先做：补进来的是新值，后续组逻辑会统一口径）──────────
    # ⚠️ 主文件必须用 `main` 对象取；`by_name` 的键是**文件名**（.env），不是 "main"。
    for key, spec in MUST_PRESENT.items():
        for target in spec["files"]:
            e = main if target == "main" else by_name.get(target)
            if e is None:
                continue
            cur = e.get(key)
            if cur is None:
                value = spec.get("value") or group_new.setdefault(key, _gen(key))
                e.add(key, value, comment=f"补入（{spec['why']}）｜gen_profile_secrets.py")
                plan.setdefault(e.path.name, []).append((key, "(缺行)", value))
            elif spec.get("value") and cur != spec["value"]:
                e.set(key, spec["value"])
                plan.setdefault(e.path.name, []).append((key, cur or "(空)", spec["value"]))

    # ── B. 同值组：任一处需要换 ⇒ 整组统一成同一个新值 ──────────────────
    for group in SAME_VALUE_GROUPS:
        heads = [k for k in group if k in PLATFORM_SECRETS]
        if not heads or _external(heads[0], None):
            continue
        hits = [(e, k) for e in envs for k in group
                if e.get(k) is not None and why_replace(k, e)]
        if not hits:
            continue
        new = group_new.get(heads[0]) or _gen(heads[0])
        group_new.setdefault(heads[0], new)
        hit_ids = {(id(e), k) for e, k in hits}
        unified: list[str] = []
        for e in envs:
            for k in group:
                old = e.get(k)
                if old is None or old == new:
                    continue
                plan.setdefault(e.path.name, []).append((k, old or "(空)", new))
                e.set(k, new)
                if (id(e), k) not in hit_ids:
                    unified.append(f"{e.path.name}:{k}")
        if unified:
            notes.append(
                f"⚠️ 同值组 {heads[0]} 内原本取值不一致，已一并统一为同一新值："
                f"{', '.join(unified[:4])}{'…' if len(unified) > 4 else ''}"
                "（外部系统若有直连消费方需同步）")

    # ── C. 平台自管的独立键 ─────────────────────────────────────────
    for key in PLATFORM_SINGLES:
        if _external(key, None):
            continue
        for e in envs:
            if e.get(key) is None:
                continue
            why = why_replace(key, e)
            if why:
                new = _gen(key)
                plan.setdefault(e.path.name, []).append((key, e.get(key) or "(空)", new))
                e.set(key, new)

    # ── D. 新环境应清空的遗留值 ─────────────────────────────────────
    for key, why in CLEAR_ON_NEW.items():
        for e in envs:
            v = e.get(key)
            if not v:
                continue
            if _placeholder(key, v, e.tpl.get(key), siblings.get(key, set())):
                plan.setdefault(e.path.name, []).append((key, v, "(清空)"))
                e.clear(key)
                notes.append(f"ℹ️ {e.path.name}: {key} 已清空 —— {why}")
            else:
                notes.append(f"ℹ️ {e.path.name}: {key} 是**真实**旧盐，保留不动"
                             "（若有旧盐签发的 key 仍需过渡就应保留；确认不需要可手动词空）")

    # ── E. 派生值 ───────────────────────────────────────────────────
    db_new = next((n for changes in plan.values() for k, _o, n in changes
                   if k == "DB_PASSWORD" and n != "(清空)"), None)
    if db_new:
        for e in envs:
            for key in _DB_URL_KEYS:
                v = e.get(key)
                if v is None or not v.strip():
                    continue                      # 空值 = 该引擎用分项配置拼 URL，无需同步
                m = _URL_RE.match(v)
                if not m:
                    notes.append(f"⚠️ {e.path.name}:{key} 形态无法解析，未改（请手工核对口令段）")
                    continue
                rebuilt = f"{m.group('scheme')}{m.group('user')}:{db_new}@{m.group('rest')}"
                plan.setdefault(e.path.name, []).append((key, "(URL 内嵌口令)", rebuilt))
                e.set(key, rebuilt)

    redis_pw = next(((e.get("REDIS_PASSWORD") or "").strip() for e in envs
                     if (e.get("REDIS_PASSWORD") or "").strip()), None)
    if args.redis_password and not redis_pw:
        redis_pw = _gen("REDIS_PASSWORD")
        main.add("REDIS_PASSWORD", redis_pw,
                 comment="由 gen_profile_secrets.py --redis-password 生成（Redis 启用认证）")
        plan.setdefault(main.path.name, []).append(("REDIS_PASSWORD", "(缺行)", redis_pw))
    for e in envs:
        v = e.get("REDIS_URL")
        if v is None:
            continue
        want = f"redis://:{redis_pw}@redis:6379/0" if redis_pw else "redis://redis:6379/0"
        if v != want:
            plan.setdefault(e.path.name, []).append(("REDIS_URL", v, want))
            e.set("REDIS_URL", want)

    # ── F. 只读提示：白名单外的键看起来仍是空/模板/公开值 ────────────────
    outside: list[str] = []
    for e in envs:
        for key in sorted(e.index):
            if key in PLATFORM_SECRETS or key in NEVER_GENERATE or key in CLEAR_ON_NEW:
                continue
            if not _secretish(key) or _external(key, e.get(key)):
                continue
            v = e.get(key) or ""
            if not v.strip() or e.tpl.get(key) == v or _is_public(key, v):
                outside.append(f"{e.path.name}: {key} —— 不在平台自管清单内，请人工确认是外部签发还是该换")

    # ── F2. 跨文件漂移：脚本按设计不动的键，但取值不一致必须让人知道 ──────────
    # 典型：外部签发凭据（COS/MPS/MINERU/ASR…）在 .env 与引擎专属文件里各持一份，
    # 只改了其中一份 ⇒ 两个容器用不同凭据，而失败现象看起来与凭据无关，极难排查。
    drift: list[str] = []
    optional_empty: list[str] = []
    for key in sorted({k for e in envs for k in e.index}):
        if key in PLATFORM_SECRETS or key in CLEAR_ON_NEW:
            continue                      # 已由同值组/清空逻辑统一，不会漂移
        if not (_secretish(key) or key in NEVER_GENERATE):
            continue                      # 只看凭据类：排除 MINERU_*_URL / POLL_TIMEOUT 这类配置
        # 注意用 is not None（含空值）：「一份有值、一份为空」同样可能是真实漂移
        seen = {e.path.name: (e.get(key) or "") for e in envs if e.get(key) is not None}
        if len(seen) >= 2 and len(set(seen.values())) > 1:
            detail = ", ".join(f"{f}={'（空）' if not v else '（有值）'}"
                               for f, v in sorted(seen.items()))
            nonempty = {v for v in seen.values() if v}
            if key in ALLOW_EMPTY and len(nonempty) == 1:
                optional_empty.append(f"{key} —— {detail}；允许为空（{ALLOW_EMPTY[key]}）")
            else:
                drift.append(f"{key} —— {detail}")

    # ── 输出 ────────────────────────────────────────────────────────
    if not plan:
        print("（无需改动）—— 平台自管密钥均已是生产值，且无跨画像复用。")
    for name in sorted(plan):
        print(f"── {name}")
        for key, old, new in plan[name]:
            if new.startswith("(") or new == "prod":
                shown = new                       # 标记类（(清空)/(URL)/prod）原样显示
            elif len(new) > 80:
                shown = "(URL)"
            else:
                shown = f"新值({len(new)} 字符)"
            print(f"   {key:<36} {old[:20]:<22} → {shown}")
        print()
    if notes:
        print("注意事项：")
        for n in notes:
            print(f"  {n}")
        print()
    if outside:
        print("需要人工确认（脚本按设计**不动**这些键）：")
        for o in outside:
            print(f"  • {o}")
        print()
    if drift:
        print("🔴 跨文件取值不一致（脚本按设计不动这些键，但必须人工确认哪份正确）：")
        for d in drift:
            print(f"  • {d}")
        print()
    if optional_empty:
        print("ℹ️ 允许为空（引擎可单独部署，平台侧不给这些 token 是合法的）：")
        for o in optional_empty:
            print(f"  • {o}")
        print()

    manual: list[str] = []
    for key, why in NEVER_GENERATE.items():
        for e in envs:
            if e.get(key) is not None:
                manual.append(f"{e.path.name}: {key} —— {why}")
                break
    if any(e.get("KEYCLOAK_CLIENT_SECRET") for e in envs):
        manual.append("KEYCLOAK_CLIENT_SECRET —— 须在 Keycloak 客户端里配成同一值")
    if any(e.get("DB_PASSWORD") for e in envs):
        manual.append("DB_PASSWORD —— 新库首次初始化即用此口令；若指向**既有**库，须 "
                      "`ALTER USER <user> WITH PASSWORD '<新值>'`")
    if any(e.get("NEO4J_PASSWORD") for e in envs):
        manual.append("NEO4J_PASSWORD —— `NEO4J_AUTH` 只在**首次初始化**生效；改既有实例需 "
                      "`ALTER CURRENT USER SET PASSWORD FROM '<旧>' TO '<新>'` 或重建数据卷")
    if args.redis_password:
        manual.append("REDIS_PASSWORD 已启用 —— 确认安全组未放开 6379，且无外部直连消费方")
    if manual:
        print("手工清单（脚本不生成，需人工确认）：")
        for m in manual:
            print(f"  • {m}")
        print()

    if args.show_values:
        print("生成的值（⚠️ 立即存入密码库，勿粘贴到聊天/工单）：")
        for name in sorted(plan):
            for key, _old, new in plan[name]:
                print(f"  {name}:{key}={new}")
        print()

    if args.dry_run:
        print("--dry-run：未写入任何文件。")
        return 0
    if not plan:
        return 0

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime(BACKUP_STAMP)
    written = 0
    for e in envs:
        if e.path.name not in plan:
            continue
        if not args.no_backup:
            shutil.copy2(e.path, e.path.with_name(f"{e.path.name}.bak.{stamp}"))
        e.path.write_text(e.render(), encoding="utf-8")
        written += 1
    print(f"✅ 已写入 {written} 个文件" + ("" if args.no_backup else f"（备份后缀 .bak.{stamp}）"))
    print(f"   下一步：scripts/security/audit_profile.py --profile {profile}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="为全新生产环境画像生成平台自管密钥（默认只换「空/模板/公开/跨画像复用」）")
    p.add_argument("--profile", required=True, help="画像目录，如 deploy/env/91")
    p.add_argument("--templates", default="deploy",
                   help="模板目录（默认 deploy，内含 .env*.example）")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    p.add_argument("--fresh", action="store_true",
                   help="把平台自管密钥全部重签（外部签发凭据仍不动）")
    p.add_argument("--redis-password", action="store_true",
                   help="生成 REDIS_PASSWORD 并同步各文件 REDIS_URL（默认不启用 Redis 认证）")
    p.add_argument("--no-backup", action="store_true", help="不生成 .bak.<时间戳> 备份")
    p.add_argument("--show-values", action="store_true",
                   help="打印生成的值（便于存入密码库；注意终端记录）")
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
