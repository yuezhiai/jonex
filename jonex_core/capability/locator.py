#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
能力定位器（Capability Locator）

负责根据运行时清单（capability_runtime.yaml）决定每个能力以什么形式被调用：
- LOCAL：进程内直接持有适配器（Local Client）
- REMOTE：通过 Sidecar 反代到独立部署的能力服务（Remote Client）
- MOCK：使用桩实现（用于测试 / 离线开发）

业务/领域代码统一通过 `get_*_client()` 工厂获取 Client，不关心其落地形式。
切换部署画像（单体 / 分层 / 全分布式）只需修改清单，不改业务代码。

清单加载顺序：
1. 环境变量 `CAPABILITY_RUNTIME_FILE` 指定的路径
2. 项目根目录 `capability_runtime.yaml`
3. 包内默认值（全部 LOCAL）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional

from jonex_core.common import get_logger

logger = get_logger("capability.locator")


# ============================================================
# 数据模型
# ============================================================
class CapabilityMode(str, Enum):
    """能力运行模式"""

    LOCAL = "local"   # 进程内直连
    REMOTE = "remote"  # 通过 sidecar 反代
    MOCK = "mock"     # 桩实现（测试 / 离线）


@dataclass
class CapabilitySpec:
    """单个能力的运行时规格"""

    capability_id: str             # 完整能力 ID，如 atomic.llm.qwen.v1
    mode: CapabilityMode = CapabilityMode.LOCAL
    endpoint: Optional[str] = None  # REMOTE 模式下的 Sidecar URL
    options: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# 默认清单
# ============================================================
# 所有原子能力默认 LOCAL，保持现状零破坏。
# 运维想拆分服务时，只需在 capability_runtime.yaml 里覆盖单条配置。
_DEFAULT_SPECS: Dict[str, CapabilitySpec] = {
    "atomic.llm.qwen.v1":      CapabilitySpec("atomic.llm.qwen.v1",      CapabilityMode.LOCAL),
    "atomic.vector.milvus.v1": CapabilitySpec("atomic.vector.milvus.v1", CapabilityMode.LOCAL),
    "atomic.audio.asr.v1":     CapabilitySpec("atomic.audio.asr.v1",     CapabilityMode.LOCAL),
}


# ============================================================
# Locator 主体
# ============================================================
class CapabilityLocator:
    """能力定位器"""

    def __init__(self, manifest_path: Optional[str] = None):
        self._specs: Dict[str, CapabilitySpec] = dict(_DEFAULT_SPECS)
        self._manifest_path: Optional[Path] = None
        self._load_manifest(manifest_path)

    # ---------- 公开 API ----------
    def get_spec(self, capability_id: str) -> CapabilitySpec:
        """获取能力规格；若清单未声明则返回 LOCAL 默认值。"""
        spec = self._specs.get(capability_id)
        if spec is None:
            logger.debug(
                f"capability_runtime 未声明 {capability_id}，回退到 LOCAL 默认值"
            )
            spec = CapabilitySpec(capability_id=capability_id, mode=CapabilityMode.LOCAL)
            self._specs[capability_id] = spec
        return spec

    def is_local(self, capability_id: str) -> bool:
        return self.get_spec(capability_id).mode == CapabilityMode.LOCAL

    def is_remote(self, capability_id: str) -> bool:
        return self.get_spec(capability_id).mode == CapabilityMode.REMOTE

    def list_specs(self) -> Dict[str, CapabilitySpec]:
        return dict(self._specs)

    def reload(self, manifest_path: Optional[str] = None) -> None:
        """重新加载清单（测试或运维场景使用）"""
        self._specs = dict(_DEFAULT_SPECS)
        self._load_manifest(manifest_path or (str(self._manifest_path) if self._manifest_path else None))

    # ---------- 内部 ----------
    def _load_manifest(self, manifest_path: Optional[str]) -> None:
        path = self._resolve_manifest_path(manifest_path)
        if path is None:
            logger.info("未发现 capability_runtime 清单，使用内置默认（全部 LOCAL）")
            return

        try:
            import yaml  # 延迟导入，未安装时给清晰错误
        except ImportError as e:
            logger.warning(f"PyYAML 未安装，无法加载 {path}：{e}；使用默认清单")
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except OSError as e:
            logger.warning(f"读取 {path} 失败：{e}；使用默认清单")
            return

        atomic_section = raw.get("atomic") or {}
        for short_id, cfg in atomic_section.items():
            full_id = self._normalize_atomic_id(short_id)
            spec = self._build_spec(full_id, cfg or {})
            self._specs[full_id] = spec
            logger.info(
                f"加载能力规格：{full_id} -> mode={spec.mode.value}"
                + (f", endpoint={spec.endpoint}" if spec.endpoint else "")
            )

        # 也允许使用全 ID 段（domain.* / business.*）扩展
        for section in ("domain", "business"):
            for full_id, cfg in (raw.get(section) or {}).items():
                spec = self._build_spec(full_id, cfg or {})
                self._specs[full_id] = spec
                logger.info(
                    f"加载能力规格：{full_id} -> mode={spec.mode.value}"
                    + (f", endpoint={spec.endpoint}" if spec.endpoint else "")
                )

        self._manifest_path = path

    @staticmethod
    def _normalize_atomic_id(short_id: str) -> str:
        """`llm.qwen` -> `atomic.llm.qwen.v1`；`openkb` -> `atomic.openkb.v1`；已完整 ID 原样返回。

        [jonex] 修复：短名 0 个点（如 `openkb`）此前会漏掉 `.v1`（归一成 `atomic.openkb`），
        导致 get_spec("atomic.openkb.v1") 查不到而回退 LOCAL。0/1 点统一补 `.v1`。
        """
        if short_id.startswith("atomic."):
            return short_id if short_id.count(".") >= 3 else f"{short_id}.v1"
        return f"atomic.{short_id}.v1" if short_id.count(".") <= 1 else f"atomic.{short_id}"

    @staticmethod
    def _build_spec(full_id: str, cfg: Dict[str, Any]) -> CapabilitySpec:
        mode_str = (cfg.get("mode") or "local").lower()
        try:
            mode = CapabilityMode(mode_str)
        except ValueError:
            logger.warning(f"未知 mode={mode_str}，{full_id} 回退到 LOCAL")
            mode = CapabilityMode.LOCAL

        endpoint = cfg.get("endpoint")
        if mode == CapabilityMode.REMOTE:
            # 允许使用 ${SIDECAR_URL} 占位符引用环境变量
            endpoint = _expand_env(endpoint) if endpoint else os.getenv("SIDECAR_URL")

        options = cfg.get("options") or {}
        return CapabilitySpec(
            capability_id=full_id,
            mode=mode,
            endpoint=endpoint,
            options=options,
        )

    @staticmethod
    def _resolve_manifest_path(manifest_path: Optional[str]) -> Optional[Path]:
        candidates = []
        if manifest_path:
            candidates.append(Path(manifest_path))
        env_path = os.getenv("CAPABILITY_RUNTIME_FILE")
        if env_path:
            candidates.append(Path(env_path))
        # 项目根目录
        candidates.append(Path.cwd() / "capability_runtime.yaml")

        for p in candidates:
            if p and p.is_file():
                return p
        return None


def _expand_env(value: str) -> str:
    """支持 `${VAR}` 与 `${VAR:-default}` 两种占位写法。"""
    import re

    def repl(match: "re.Match[str]") -> str:
        token = match.group(1)
        if ":-" in token:
            name, default = token.split(":-", 1)
            return os.getenv(name, default)
        return os.getenv(token, "")

    return re.sub(r"\$\{([^}]+)\}", repl, value)


# ============================================================
# 单例入口
# ============================================================
_locator_lock = RLock()
_locator_instance: Optional[CapabilityLocator] = None


def get_locator() -> CapabilityLocator:
    """获取全局能力定位器（单例）"""
    global _locator_instance
    if _locator_instance is None:
        with _locator_lock:
            if _locator_instance is None:
                _locator_instance = CapabilityLocator()
    return _locator_instance


def reset_locator() -> None:
    """重置单例（仅用于测试）"""
    global _locator_instance
    with _locator_lock:
        _locator_instance = None
