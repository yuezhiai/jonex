#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""出站 URL 安全校验（SSRF 防护）

用于企业身份集成等「平台 → 外部主数据源」的出站拉取场景。发起请求前必须先校验
目标地址，避免被 SSRF 利用探测内网 / 云元数据服务：

- 两条用户可填路径**同档**（均走 :func:`validate_target_url` 的
  ``allow_private=True`` 档位：放行 http 与私网 / 回环 / 链路本地 / CGNAT 等网段，
  仅保留 scheme 白名单 http / https 与主机名校验）：
  * SSO（「单点登录」的服务地址 `issuer_url`）→ :func:`validate_sso_url`；
  * 数据源（「用户与组织同步」的接口地址 `datasource_url`）→
    :func:`validate_datasource_url`。
  客户业务系统与私有化部署的 Keycloak 都可能与平台同机部署，放宽理由一致。
  两档的区别**只在错误码**：都抛 :class:`InvalidTargetUrlError`(1007)，不再抛
  :class:`UnsafeTargetUrlError`(1005)。
- :func:`validate_target_url` 的**默认档**（``allow_private=False``：仅 HTTPS 且
  阻断私网 / 回环 / 链路本地 / 未指定 / 共享地址 / 组播 / 保留地址，覆盖 IPv4 与
  IPv6）仍然保留，服务对 HTTP 30x redirect 目标地址的逐跳校验
  （:func:`validate_redirect_url`），由出站 HTTP 适配器在决定是否跟随 redirect
  前调用 —— 放宽不涉及这一档。
- **知识库数据源**走独立的一档 :func:`validate_kb_ingestion_url`：网段用上面这条
  严格档的阻断逻辑（默认安全），scheme 按 D2 结论沿用 http + https；需要放行内网
  源时用 ``KB_INGESTION_ALLOWED_HOSTS`` 白名单显式开洞，不复用上面两条放宽档。

思路对齐 jonex_core/security/oidc.py 的 issuer 白名单防 SSRF：出站目标必须先通过
安全校验，才允许发起网络请求。
"""

import ipaddress
import socket
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from jonex_core.common.exceptions import InvalidTargetUrlError, UnsafeTargetUrlError

# IPv4 阻断网段：未指定/本网络、私网、回环、共享地址（CGNAT）、链路本地、
# 组播、保留地址（含 255.255.255.255）。
_BLOCKED_IPV4_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
)

# IPv6 阻断网段：未指定、回环、唯一本地地址（fc00::/7 覆盖 fc00::/8 与 fd00::/8）、
# 链路本地、组播。
_BLOCKED_IPV6_NETWORKS = (
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


def _resolve_all_ips(host: str) -> List[str]:
    """解析主机名得到全部 IP 地址（IP 字面量直接返回）。

    DNS 解析失败返回空列表——此时无法判定为私网/回环，交由上层在真正建立连接时
    按「连接失败」处理，不在校验阶段误报为不安全目标。
    """
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    # 剥离 IPv6 zone index（如 fe80::1%eth0），ipaddress 不解析带 zone 的字面量。
    if "%" in host:
        host = host.split("%", 1)[0]
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    ips: List[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in ips:
            ips.append(addr)
    return ips


def _is_blocked_ip(ip_str: str) -> bool:
    """判断单个 IP 是否落在私网/回环/链路本地等阻断网段。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # 无法解析为 IP 字面量，按不安全处理（保守拒绝）。
        return True
    if ip.version == 4:
        return any(ip in net for net in _BLOCKED_IPV4_NETWORKS)
    return any(ip in net for net in _BLOCKED_IPV6_NETWORKS)


def validate_target_url(
    url: str,
    allowed_hosts: Optional[List[str]] = None,
    allow_private: bool = False,
    error_cls: type = UnsafeTargetUrlError,
    allow_http: bool = False,
) -> None:
    """校验出站目标 URL，不合法则抛 ``error_cls``（默认 :class:`UnsafeTargetUrlError`）。

    规则：
    - scheme 必须为 https（``allow_private=True`` 或 ``allow_http=True`` 时放宽为
      http / https）；
    - 必须含主机名；
    - 主机名解析出的任一 IP 落在阻断网段即拒绝（``allow_private=True`` 时跳过）。

    白名单例外：``allowed_hosts`` 中的主机名（大小写不敏感、不含 scheme 与端口）
    命中时跳过 scheme 与私网/回环双重检查（完全信任），用于放行私有化部署的内网
    Keycloak。白名单为空或未命中时行为不变；公网目标不受白名单影响。

    ``allow_private=True`` 是「数据源（用户与组织同步）出站」专用档位：客户业务系统
    可能与平台同机部署，此时目标恒落在私网 / 回环 / 链路本地网段内，且不存在绕开
    阻断的写法。该档位**只**保留 scheme 与主机名两项校验 —— 注意这**不等于**放开
    scheme：``file://`` / ``gopher://`` / ``ftp://`` 与无主机地址仍被拒绝。

    ⚠️ 默认分支（``allow_private=False``）必须与新增本开关之前逐字一致：SSO
    ``issuer_url`` 路径不传该参数，其行为不得受数据源放宽影响。

    ``allow_http=True`` **只**放宽 scheme（http / https 均接受），**不**放宽网段阻断
    （私网 / 回环 / 链路本地 / 云元数据照旧拦截）—— 供
    :func:`validate_kb_ingestion_url` 使用：内网 API 常是 http，但目标网段必须收紧。
    默认 ``False`` 时 scheme 口径与新增该参数之前逐字一致。

    校验通过无返回值；DNS 解析失败不在此拒绝（连接阶段按连接失败处理）。
    """
    if not isinstance(url, str) or not url:
        raise error_cls()

    try:
        parsed = urlparse(url)
    except ValueError:
        raise error_cls()

    host = parsed.hostname
    if not host:
        raise error_cls()

    if allowed_hosts and host.lower() in {h.lower() for h in allowed_hosts}:
        # 白名单命中：完全放行（跳过 scheme + 私网/回环检查）。
        return

    allowed_schemes = ("http", "https") if (allow_private or allow_http) else ("https",)
    if parsed.scheme.lower() not in allowed_schemes:
        raise error_cls()

    if allow_private:
        # 数据源档位：私网 / 回环 / 链路本地 / CGNAT / 未指定 / 组播 / 保留一并放行。
        return

    for ip in _resolve_all_ips(host):
        if _is_blocked_ip(ip):
            raise error_cls()


def validate_datasource_url(url: str) -> None:
    """校验「用户与组织同步」数据源出站地址（放行 http 与私网）。

    数据源的三处调用点（保存配置 / 测试连接 / 同步执行前）统一走这里，避免「口径
    一致」靠人工对齐多处分支维持。规则 = :func:`validate_target_url` 的
    ``allow_private=True`` 档位：接受 http / https 与私网 / 回环 / 链路本地地址。

    scheme 非法（``file://`` / ``gopher://`` / ``ftp://`` ……）或缺少主机名时抛
    :class:`InvalidTargetUrlError`(1007) —— 与 SSO 路径（:func:`validate_sso_url`）
    同档、同错误码；1005 的「仅支持 HTTPS 且禁止私网」文案已不适用于这两条路径，
    它现在只剩 redirect 逐跳校验在用。
    """
    validate_target_url(url, allow_private=True, error_cls=InvalidTargetUrlError)


def validate_sso_url(url: str, allowed_hosts: Optional[List[str]] = None) -> None:
    """校验 SSO `issuer_url` 出站探测地址（放行 http 与私网）。

    服务「企业身份集成 → 单点登录 → 服务地址」的出站探测（测试连接 / 发现
    ``.well-known`` 端点）路径。规则与 :func:`validate_datasource_url` **同档**：
    接受 http / https 与私网 / 回环 / 链路本地 / CGNAT 地址 —— 私有化部署的
    Keycloak 常与平台同机或在同一内网（例如本地 ``http://localhost:8081``），
    严格档的 1005 会把它一律挡下。

    scheme 非法（``file://`` / ``gopher://`` / ``ftp://`` ……）或缺少主机名时抛
    :class:`InvalidTargetUrlError`(1007)，不再抛 :class:`UnsafeTargetUrlError`(1005)。

    ``allowed_hosts`` 白名单机制按原样保留：指名信任的内网 Keycloak 主机（大小写
    不敏感、不含 scheme 与端口）命中时跳过 scheme 与私网双重检查、完全放行。放宽
    私网后它与「整类放行」不再冲突，但**并未失去效力** —— 本档位下它的唯一剩余
    作用是**跳过 scheme 检查**，即放行非 http / https 的地址（如
    ``ftp://keycloak.internal/``）。注意本档在 scheme 检查后直接返回、不做 DNS
    解析，故内网域名本就已放行、用不着白名单。留空或未命中不影响上述规则。
    """
    validate_target_url(
        url,
        allowed_hosts=allowed_hosts,
        allow_private=True,
        error_cls=InvalidTargetUrlError,
    )


def validate_kb_ingestion_url(url: str, allowed_hosts: Optional[List[str]] = None) -> None:
    """校验知识库「数据源」出站地址（严格档：阻断私网 / 回环 / 链路本地 / 云元数据）。

    ⛔ **不得**改用 :func:`validate_datasource_url` / :func:`validate_sso_url`：那两条是
    ``allow_private=True`` 的**产品放宽档**（客户业务系统与平台同机部署），本次修复
    有意**不**收紧它们，KB 数据源也**不**复用它们。

    KB 数据源的 ``config_json.endpoint``、外部 API 响应体里的文件下载地址（``rf.uri``）
    与 S3 ``endpoint_url`` 都是**用户可控的出站目标**（渗透报告 3.1，SSRF，严重），
    默认**安全**取向是全部阻断内网 —— 需要拉私有化部署内网源时，由部署方通过
    ``KB_INGESTION_ALLOWED_HOSTS``（见 ``jonex_core/common/config.py``）显式登记主机，
    白名单是**逃生阀**，而不是把整类私网地址放开。

    规则 = :func:`validate_target_url` 的 ``allow_private=False`` 阻断逻辑
    （:data:`_BLOCKED_IPV4_NETWORKS` / :data:`_BLOCKED_IPV6_NETWORKS`：私网 / 回环 /
    链路本地 / CGNAT / 未指定 / 组播 / 保留地址，覆盖 IPv4 与 IPv6）；scheme 按 D2
    结论沿用既有 ``_is_valid_http_url`` 的 http + https 口径（内网 API 常为 http，
    收紧为 https-only 会打断更多现存数据源）。

    违规（网段命中、scheme 非法如 ``file://`` / ``gopher://`` / ``ftp://``、缺主机名）
    抛 :class:`InvalidTargetUrlError`(1007)。``allowed_hosts`` 命中的主机名
    （大小写不敏感、不含 scheme 与端口）跳过 scheme 与网段双重检查、完全放行；
    为空或未命中不影响上述规则。
    """
    validate_target_url(
        url,
        allowed_hosts=allowed_hosts,
        allow_private=False,
        allow_http=True,
        error_cls=InvalidTargetUrlError,
    )


def validate_redirect_url(base_url: str, location: str) -> None:
    """校验 HTTP 30x redirect 的目标地址。

    先把 ``location`` 相对解析到 ``base_url``，再复用 :func:`validate_target_url`
    做同一套 HTTPS + 私网/回环阻断校验。出站 HTTP 适配器在决定是否跟随 redirect
    前必须对每个目标逐跳调用；若业务不希望跟随 redirect，则直接禁止即可。
    """
    resolved = urljoin(base_url, location)
    validate_target_url(resolved)
