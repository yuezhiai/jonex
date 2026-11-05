#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""数据库数据源的配置与凭据助手。

两个服务都要做同样四件事：丢掉客户端回传的服务端引用键、明文密码 → 密文引用、
密文引用 → 明文、按 engine 取 SchemaSource：
  - `DataSourceService`（知识库级数据源，含存量内联）
  - `DataSourceConnectionService`（平台级连接池）

放在这里是为了让两处**共用同一份实现**——凭证口径一旦分叉，
「改密码只改一处、N 个库一起生效」这个前提就没了。

本模块只依赖 crypto / schema_source / 异常 / 模型常量，**不 import 任何 service**，
所以可以被两个 service 同时 import 而不成环。
"""
from __future__ import annotations

import logging
from typing import Optional

from jonex_core.common.crypto import decrypt_secret, encrypt_secret
from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate

from ..models.data_source import SECRET_KEYS
from .schema_source import get_schema_source, supported_engines
from .schema_source.base import SchemaSource

logger = logging.getLogger(__name__)

#: config_json 里的模式声明：主账号本身即只读账号，用户 SQL 直接用它执行。
#: 不是凭据（不进 `SECRET_KEYS`，不加密、不脱敏），由 `reconcile_readonly_mode` 统一收敛。
READONLY_USE_MAIN_KEY = "readonly_use_main"


def _truthy(value) -> bool:
    """宽松真值：兼容 JSON bool 与 `"true"` / `"1"` 这类字符串。

    直接 `bool("false")` 是 `True` —— 这个键会从 JSON 反序列化回来，不能想当然。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"true", "1", "yes", "on"}


def encrypt_config(access_type: str, cfg: dict) -> dict:
    """把 config_json 里的明文凭据换成密文引用。

    返回新 dict，不改调用方手里那份（嵌套的 `auth` 也另拷一份）。

    空串一律不造引用：`encrypt_secret("")` 只回一个空串，**引用键却照样落下来**，
    而脱敏看的是键名、不是值的真假（`SECRET_KEYS` 里的顶层 `password_ref` 一旦存在
    就被回显成 `"***"`）——于是「没设密码」看起来像「设过密码」。
    顺带记牢这条：明文键 `password` / `credential` / `token` **都不在** `SECRET_KEYS`
    里，脱敏层只认 `*_ref` ⇒ 任何绕开这里把明文写进 config 的路径，没人兜底。
    """
    cfg = dict(cfg or {})
    if access_type == "api":
        auth = dict(cfg.get("auth") or {})
        if auth.get("token"):  # 明文 → 密文引用
            auth["token_ref"] = encrypt_secret(auth.pop("token"))
        cfg["auth"] = auth
    elif access_type == "storage":
        if cfg.get("credential"):  # "ak:sk" 明文 → 密文
            cfg["credential_ref"] = encrypt_secret(cfg.pop("credential"))
    elif access_type == "database":
        # 数据库数据源只读 catalog 出 schema、不取文档；
        # 连接密码与 storage 走同一套密文引用，明文不落库
        if cfg.get("password"):
            cfg["password_ref"] = encrypt_secret(cfg.pop("password"))
        # 只读凭据（② NL→SQL 用）：与写凭据同一套口径 —— 请求里给明文
        # `readonly_password`，服务端换成引用落库。没有这条，「登记只读账号」
        # 在 API 上根本做不到（`strip_client_secret_refs` 会把客户端回传的
        # `readonly_password_ref` 整个丢掉）。
        if cfg.get("readonly_password"):
            cfg["readonly_password_ref"] = encrypt_secret(cfg.pop("readonly_password"))
    return cfg


def strip_client_secret_refs(cfg: dict | None) -> dict:
    """返回一份**去掉服务端管理的引用键**的 config 副本（两个写入口共用）。

    这些键（见 `SECRET_KEYS`；api 的 `token_ref` 还嵌在 `auth` 里）是**服务端管理**的：
    值由本模块的 `encrypt_config` / `crypto.hash_ingest_key` 用服务端密钥现造，
    客户端手里没有这把密钥、**造不出合法值** —— 所以写请求里出现的任何这类键
    都不是凭据，只能是回显（GET 出口的脱敏占位 `"***"` / `null` / `""`）或伪造，
    一律丢掉。

    为什么不能只认 `"***"` 那一个字面量：脱敏看的是**键存在**，不是值的真假
    （`models/data_source.py::mask_secrets` 只认这些键名）。所以客户端回传
    `password_ref: null` 一样能盖掉真密文，而**落库静默成功、回显还是 `***`**
    （键还在），直到某次「测连接」报一句说不清的认证失败 —— 那时没人会想到是
    某次编辑把回显值写进了库。收成「任何值都丢」还顺手解掉了「脱敏占位哪天换了
    写法，写入口的判据就跟着失效」这层耦合。

    改密码不受影响：明文走 `password` / `credential` / `auth.token` 字段，
    本函数在 `encrypt_config` **之前**调用，新引用照样由它造出来。

    键名跟着 `SECRET_KEYS` 走、不另抄一份字面量：以后新增加密字段时写入口自动
    跟上 —— 抄一份的下场就是「脱敏做了、写入口没做」。
    """
    cfg = dict(cfg or {})
    for k in SECRET_KEYS:
        cfg.pop(k, None)
    # api 的 token_ref 嵌在 `auth` 里，上面那圈顶层 pop 扫不到；拷贝后再改，
    # 别把调用方手里那份写脏
    auth = cfg.get("auth")
    if isinstance(auth, dict) and "token_ref" in auth:
        auth = dict(auth)
        auth.pop("token_ref", None)
        cfg["auth"] = auth
    return cfg


def merge_config_preserving_secret_refs(
    stored: dict | None, incoming: dict, access_type: str,
) -> dict:
    """把 `incoming`（已 strip + 已 `encrypt_config`）并进 `stored`。

    顶层是浅合并：`incoming` 里已不含客户端回传的引用键，所以用户这次没重设的
    `password_ref` / `credential_ref` / `ingest_key_hash` 自然由 `stored` 保留 ——
    「不传即保留」正是改个主机名不丢密码的前提。

    但 **api 的 `token_ref` 嵌在 `auth` 里**：`{**stored, **incoming}` 会把整个
    `auth` 换掉，旧引用既没被覆盖、也没被保住，而是**整个丢掉**。后果与「回显
    占位盖掉真密文」一模一样（下次拉文档认证失败），只是路径更隐蔽：库里剩下的
    config 看着干干净净，就是没有 token 了。

    所以 api 再并一层：`incoming` 的 auth 里没有引用（用户没重设 token）时，
    把 `stored` 的旧引用带回来。
    """
    merged = {**(stored or {}), **incoming}
    if access_type != "api":
        return merged
    stored_auth = (stored or {}).get("auth")
    incoming_auth = incoming.get("auth")
    if not isinstance(stored_auth, dict) or not isinstance(incoming_auth, dict):
        return merged
    old_ref = stored_auth.get("token_ref")
    # `incoming` 里已有 token_ref = 用户这次真敲了新 token（encrypt_config 刚造的），
    # 新值赢；只有它空着（本次没重设）才回填旧引用。
    if old_ref and not incoming_auth.get("token_ref"):
        merged["auth"] = {**incoming_auth, "token_ref": old_ref}
    return merged


def resolve_readonly_database_config(cfg: dict) -> Optional[dict]:
    """只读凭据 → 明文，**仅供执行用户 SQL（② NL→SQL）使用**。

    与 `resolve_database_config` 的区别是安全边界、不是方便：
    它只认 `readonly_user` / `readonly_password_ref`，**绝不回退**到
    `user` / `password_ref` —— 那两份是写凭据（实测某源登记的是生产账号）。
    没登记只读凭据时返回 `None`，由调用方判「答不了」，而不是拿写凭据去跑。

    返回值只活在调用方进程内，不出现在任何返回给前端的结构里。
    """
    cfg = dict(cfg or {})
    if _truthy(cfg.get(READONLY_USE_MAIN_KEY)):
        # 登记方显式声明「主账号即只读账号」：直接用主凭据执行用户 SQL。
        # 这不是对 `user` 的回退——没有这条声明时下面仍然 fail-closed。
        # 只读性由数据库权限保证：演化评测的真值 SQL **不经过安全闸**
        # （evolution/upstream.py 直接 run_query），库权限是唯一边界。
        user = str(cfg.get("user") or "").strip()
        if not user:
            logger.warning(
                "%s 已开启但主账号 user 为空：拒绝执行（无可用身份）", READONLY_USE_MAIN_KEY)
            return None
        out = resolve_database_config(cfg)
        # 返回值必须是「一份干净的主账号配置」：残留的只读凭据或模式声明都不许留，
        # 否则谁对它再跑一次解析，凭据类别会被静默换掉（同下文的形状约束）。
        for key in ("readonly_user", "readonly_password", "readonly_password_ref",
                    READONLY_USE_MAIN_KEY):
            out.pop(key, None)
        # 只打印定位信息，绝不打印口令或任何 *_ref
        logger.warning(
            "[%s] 用户 SQL 将用主账号执行（engine=%s host=%s database=%s user=%s）；"
            "只读性仅由数据库权限保证（演化评测真值 SQL 不经过安全闸）",
            READONLY_USE_MAIN_KEY, out.get("engine"), out.get("host"),
            out.get("database"), user,
        )
        return out

    user = str(cfg.get("readonly_user") or "").strip()
    ref = cfg.pop("readonly_password_ref", "") or ""
    if not user or not ref:
        return None
    # ⚠️ 只读性是**登记方的口头承诺**：把写账号原样填进 `readonly_user`（或只差大小写）
    # 在数据上跟「真登记了只读账号」一模一样 —— 四层安全（非 SELECT / 关键字 / 上限 /
    # 只读凭据）当场退化成三层。**不硬拒**：演示与测试库常常只有一个账号，硬拒等于给
    # 它们加流程，而且换成另一个同权限的账号照样绕得过去；但这件事必须在日志里看得见
    # （本函数是只读凭据唯一的解析出口，警告放这里一处即全覆盖）。
    writer = str(cfg.get("user") or "").strip()
    if writer and writer.casefold() == user.casefold():
        logger.warning(
            "数据源的 readonly_user 与写账号 user 是同一个（%s）：只读凭据这一层形同不存在，"
            "模型生成的 SQL 将由该账号执行 —— 请核对只读账号是不是真只读", user,
        )
    cfg["user"] = user
    cfg["password"] = decrypt_secret(ref)
    # 返回的配置里**不许再带任何凭据引用**：留下的 `password_ref` 是**写凭据**的密文，
    # 一旦这个 dict 进日志 / 异常 details 就跟着外泄；更要害的是**形状上的回退** ——
    # 谁对它再跑一次 `resolve_database_config`（同模块的写凭据解析），就会 pop 掉
    # `password_ref` 并把 `password` 覆盖成写凭据的明文，而 `user` 仍是只读账号，
    # **静默换了凭据类别**。去掉引用键，这条路在数据上不可达，不靠调用方自觉。
    cfg.pop("password_ref", None)
    return cfg


def reconcile_readonly_mode(merged: dict, incoming: dict,
                            access_type: str) -> dict:
    """写入侧收敛「主账号即只读」开关与显式只读凭据的互斥关系。

    两个写入口（连接池 / 知识库级内联）在 merge **之后**调用 —— 必须在 merge 之后，
    否则存量里那份 `readonly_user` / `readonly_password_ref` 会从 `stored` 漏回来。

    规则（`incoming` 是**已过** `strip_client_secret_refs` + `encrypt_config` 的配置）：

    - 本次显式置真 → 开关真，**清空**只读账号/密码/密文引用。刻意不可逆：
      避免以后关掉开关时把一份过期口令复活。
    - 本次带了显式只读凭据 → 开关置假（显式凭据是更具体的意图）。
    - 本次显式置假 → 开关假；不复活任何凭据。
    - 本次两者都没带（改名、回显）→ 保留存量开关状态。

    只对 `database` 生效，其它接入类型原样返回。
    """
    cfg = dict(merged or {})
    if access_type != "database":
        return cfg
    incoming = dict(incoming or {})
    if _truthy(incoming.get(READONLY_USE_MAIN_KEY)):
        cfg[READONLY_USE_MAIN_KEY] = True
        for key in ("readonly_user", "readonly_password", "readonly_password_ref"):
            cfg.pop(key, None)
        return cfg
    if (str(incoming.get("readonly_user") or "").strip()
            or incoming.get("readonly_password_ref")):
        cfg[READONLY_USE_MAIN_KEY] = False
        return cfg
    if READONLY_USE_MAIN_KEY in incoming:
        cfg[READONLY_USE_MAIN_KEY] = False
    return cfg


def resolve_database_config(cfg: dict) -> dict:
    """密文引用 → 明文，仅供**本次连库**使用。

    返回值只活在调用方进程内，绝不出现在任何返回给前端的结构里、
    也不跨系统传（生成器只吃 catalog，不吃凭证）。
    """
    cfg = dict(cfg or {})
    ref = cfg.pop("password_ref", "") or ""
    cfg["password"] = decrypt_secret(ref) if ref else ""
    return cfg


def catalog_source_for(engine: str) -> SchemaSource:
    """按引擎取 SchemaSource；引擎不支持时抛可读的 400。"""
    engine = str(engine or "")
    src = get_schema_source(engine)
    if src is None:
        raise InvalidParameterError(message=translate(
            "err.datasource.engine_unsupported",
            params={"engine": engine, "supported": ", ".join(supported_engines())},
            fallback=f"不支持的数据库引擎: {engine}（支持 {', '.join(supported_engines())}）",
        ))
    return src


__all__ = [
    "encrypt_config",
    "strip_client_secret_refs",
    "merge_config_preserving_secret_refs",
    "resolve_database_config",
    "resolve_readonly_database_config",
    "catalog_source_for",
]
