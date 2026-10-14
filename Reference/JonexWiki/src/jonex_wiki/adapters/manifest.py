"""ManifestBuilder：Settings + resolve_prompt → engine manifest（含 prompt_shas）。

engine manifest 是编译的输入配置（checksum/models/curation/prompt_shas）。这里把 dev
Settings 与可覆盖的 prompt 模板收敛成 core 引擎消费的形状。48-key 收敛的完整清单见
方案 §10；本阶段先覆盖 dev 三个可覆盖 prompt 键与 engine 消费的 curation 字段。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from jonex_wiki.domain.prompts import (
    COMPILE_MAIN_SKELETON,
    DEFAULT_PROMPTS,
    resolve_prompt,
    template_sha,
)
from jonex_wiki.infra.settings import Settings
from jonex_wiki.kernel.runtime_env import current_env

# 可覆盖 prompt 键（dev PromptRepo 的三把钥匙；core 其余模板内嵌、PROMPT_CHECKSUM 覆盖）
PROMPT_KEYS = tuple(DEFAULT_PROMPTS)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


# [jonex] 编译期模型名读**当下 env**（Settings 字段是进程启动时的快照，只作回落）。
def _resolve_compile_model(settings: Settings) -> str:
    """编译期模型名：以**当下**的环境变量为准，``Settings`` 字段仅作回落。

    ``Settings`` 在 app 工厂里只构造一次（``main.py``），``llm_model`` 等于**进程启动
    那一刻**的 env；而模型名是"部署当下"的属性 —— 每次编译都该按当时的 env 编，并把
    读到的值记进 Manifest。那条记录是**审计/溯源**用的快照，**不再是查询期的权威**
    （查询期同样读当下的 env，见 ``engine._resolve_query_model``）。

    读 ``current_env()`` 而非 ``os.environ``：编译链路把执行包在
    ``runtime_environment(settings.runtime_env())`` 里（``compile_task.py``），注入
    生效时只有前者读得到；本函数在 ``build()`` 里被调用，两条路径共用同一次读取。
    """
    return (current_env().get("LLM_MODEL", "") or "").strip() or settings.llm_model


class ManifestBuilder:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def build(
        self,
        session,
        *,
        tenant_id: str,
        kb_id: str,
        claim_policy: str = "production",
    ) -> Dict[str, Any]:
        prompt_shas: Dict[str, str] = {}
        for key in PROMPT_KEYS:
            if key == "compile_main":
                # The effective system prompt, including the user suffix, is
                # a compiler input. Omitting it makes an explicit compile
                # incorrectly reuse the previous publication.
                resolved = await resolve_prompt(session, tenant_id, kb_id, key)
                suffix = (resolved.template or "").strip()
                effective = COMPILE_MAIN_SKELETON + ("\n\n" + suffix if suffix else "")
                prompt_shas[key] = template_sha(effective)
                continue
            resolved = await resolve_prompt(session, tenant_id, kb_id, key)
            prompt_shas[key] = resolved.template_sha

        manifest: Dict[str, Any] = {
            "models": {
                "curation": _resolve_compile_model(self._settings),
                "query": _resolve_compile_model(self._settings),
            },
            "curation": {
                "claim_policy": claim_policy,
                # 编译调度旋钮:四个值全部由 Settings 下发。引擎侧不再内置「默认并发 4」
                # 这类部署默认值——同一旋钮存在两份默认值正是漂移的温床(两边各写一个
                # 数字、谁都不报错),这里保持唯一入口:Settings → manifest → engine。
                "compile_concurrency": self._settings.compile_concurrency,
                "max_passage_failure_rate": self._settings.compile_max_passage_failure_rate,
                # 唯一编译链路的身份；历史版本仍携带此字段。
                "compile_mode": "layered",
            },
            "query": {
                "strategy": "browse-first",
            },
            "prompt_shas": prompt_shas,
        }
        manifest["checksum"] = _sha256_hex(_canonical_json(manifest))
        return manifest
