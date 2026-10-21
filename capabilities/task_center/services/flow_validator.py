#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""编排整体校验器（发版用）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §6.5、附录 A.14。

`publish_version` 必须全过，任何一条不过就打回草稿。**附录 A.14 是这份校验的全表**
（`N0`~`N22` 加 `N8b` 共 24 条，其中 **`N20` 已作废**、实际生效 **23 条**）——
**发版时一次性查清楚，跑起来就不存在"跑不通的编排"**。本模块按 N 编号逐条落地，
每条 issue 文案都带编号，方便对着 A.14 核对：

| 分组 | 编号 | 查什么 |
|---|---|---|
| 结构 | N0 / N1 / N2 / N3 / N4 | `node_id` 唯一 / 无死循环 / 引用存在（结构）/ 同层 `sort` / 对偶 |
| 入口 | N5 / N6 / N7 | `entry` 必填且存在 / `entry` 无依赖 / 没有孤儿节点 |
| 引用 | N8 / N8b / N9 / N10 | 引用的节点存在 / `@task.inputs` 的键存在 / 字段已声明 / 无自引用 |
| 绑定 | N11 / N12 | `bind_inputs` 对得上 `input_schema` / 类型对得上 |
| 字段定义 | N13 / N14 / N15 / N16 / N17 | `type` / `required` / `default` / `enum` / object·array 子结构 |
| 其他 | N18 / N19 / N21 / N22 | 枚举 / 超时必填正整数 / 步骤合法 / 运行方合法 |

**两处刻意不校验，别加回来**：

- **N20 已作废**：`max_concurrency` 定义在内置只读表 `task_catalog_steps`、
  **不在编排 `config.steps[]` 里**，发版校验管不到它；
- **`required` 不进发版校验**：§6.2 末尾写明「引用一个没有产出的上游」时
  **即使字段是 `required: true` 也照样按类型填空值、不拒绝这条编排** ——
  所以"必填字段没人填"不是发版问题，`required` 只在运行期收口
  （`normalize_by_schema`）和触发入参校验（§3.3.1）里起作用。
  N18 说明 `fail_policy` 已从编排里删掉、不再校验它，这条留给将来新增的枚举字段。

一期**没有自动重试**（§十二「待规划」）：`on_timeout` / `on_failure` 这两个字段既不认、
也不校验——写进编排里会被忽略，不会因为"配了重试"而影响发版或运行。

另外这里还提供两件运行期也要用的工具（校验和运行共用同一份 schema 语义，
不许出现"发版时说行、跑起来说不通"）：

- `normalize_by_schema`：按 FieldSpec 收口一份输入值（补默认值 / 卡必填 / 卡枚举 / 卡类型）；
- `evaluate_bindings`：按 `bind_inputs` 把上游产出解析成本步骤的真实入参。

**`node_id` 与 `step_code` 的分工**（§3.5）：连线（`depends_on` / `next_steps`）与
引用（`@<node_id>.outputs.x`）一律按 **`node_id`**；`step_code` 只是「这个节点用的是
哪个内置步骤」，用来查 `task_catalog_steps`（schema / runner_code / 并发上限）。
**同一个 `step_code` 可以出现在多个节点上**（`parse_1` / `parse_2`），所以它做不了
唯一键，编排里唯一的是 `node_id`。
"""

from typing import Any, Dict, List, Optional, Tuple

try:  # pydantic>=1.10 的 v1 兼容命名空间优先（与各 DTO 的引入口径一致）
    from pydantic.v1 import ValidationError
except ImportError:  # pragma: no cover
    from pydantic import ValidationError

from ..dtos.definition import FlowConfig
from ..models import RunnerDefinition, TaskCatalogStep
from .expressions import (
    Binding,
    ExpressionError,
    MISSING,
    Ref,
    parse_binding,
    resolver_from_sources,
)

IN_FIELD_TYPES = ("string", "number", "boolean", "file", "object", "array")

#: 节点级「下游联动」的两个开关（2026-10-15 加，校验编号 N23）。
#: 语义是出边（挂在上游节点上），不填 = `True`；两个名字与 `FlowStep` 的字段逐字一致。
DOWNSTREAM_SWITCHES = ("downstream_on_retry", "downstream_on_stop")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_array_type(field_type: Optional[str]) -> bool:
    return field_type == "array"


def _type_bucket(field_type: Optional[str]) -> str:
    """把字段类型归到 N12 认的那三个桶里（§6.5 N12）。

    N12 的原话是「来源值的类型跟字段 `type` 对得上（**标量 / `file` / 数组**）」——
    所以分三桶，**`file` 自己一桶**：`file` 的来源填进 `string` 字段（或反着来）
    是形态对不上，不能因为"都不是数组"就放过。

    `object` 归在标量桶里，是**照文档字面**来的（它既不是 `file` 也不是数组）；
    `object` 自己的子结构由 N17 另外保证（`type=object` 必须有 `properties`），
    那是结构校验，跟这里的形态比对不是一回事。
    """
    if field_type == "array":
        return "array"
    if field_type == "file":
        return "file"
    return "scalar"


def empty_value_for(field_type: Optional[str]) -> Any:
    """按字段类型给「空值」（§6.2 末尾的填空规则）。

    `array` → `[]`、`object` → `{}`、其余（`string` / `number` / `boolean` / `file`）
    → `null`。上游给不出这个字段又不配 `default` 时就用它 —— **`required: true`
    也照样填空值，不拒绝这条编排**：编排者自己接受"这个字段可能拿不到"，
    判断权留给下游。
    """
    if field_type == "array":
        return []
    if field_type == "object":
        return {}
    return None


def type_matches(value: Any, field_type: str) -> bool:
    """值跟 FieldSpec 的 `type` 对不对得上。

    `file` 的值是**文件引用对象**（§3.2.1：固定 `storage_key` + `file_name` 两个键），
    所以按对象判——它的内部结构由平台约定死，这里不再往下拆（跟标量一样是最底层）。
    """
    if field_type == "string":
        return isinstance(value, str)
    if field_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "file":
        return isinstance(value, dict)
    if field_type == "object":
        return isinstance(value, dict)
    if field_type == "array":
        return isinstance(value, (list, tuple))
    return False


# ============================================================
# N13~N17：字段定义（FieldSpec）合法
# ============================================================


def validate_field_spec(spec: Any, path: str, issues: List[str]) -> None:
    """递归校验一个 FieldSpec，一直查到标量（含 `file`）为止（N13~N17）。"""
    if not isinstance(spec, dict):
        issues.append(f"{path} 必须是一个字段定义对象（FieldSpec）")
        return
    field_type = spec.get("type")
    if field_type not in IN_FIELD_TYPES:
        issues.append(
            f"{path}.type 必须是 {'/'.join(IN_FIELD_TYPES)} 之一，收到 {field_type!r}（N13）"
        )
        return
    if "required" in spec and not isinstance(spec["required"], bool):
        issues.append(f"{path}.required 必须是布尔值（N14）")
    if "enum" in spec and spec["enum"] is not None:
        enum_values = spec["enum"]
        if not isinstance(enum_values, list):
            issues.append(f"{path}.enum 必须是数组（N16）")
        else:
            for index, item in enumerate(enum_values):
                if not type_matches(item, field_type):
                    issues.append(
                        f"{path}.enum[{index}] 的值类型跟 type={field_type} 对不上（N16）"
                    )
    if "default" in spec and spec["default"] is not None:
        if not type_matches(spec["default"], field_type):
            issues.append(f"{path}.default 的类型跟 type={field_type} 对不上（N15）")
    if field_type == "object":
        properties = spec.get("properties")
        if not isinstance(properties, dict) or not properties:
            issues.append(f"{path} 是 object，必须给出 properties（N17）")
        else:
            for name, child in properties.items():
                validate_field_spec(child, f"{path}.properties.{name}", issues)
    if field_type == "array":
        items = spec.get("items")
        if not isinstance(items, dict):
            issues.append(f"{path} 是 array，必须给出 items（N17）")
        else:
            validate_field_spec(items, f"{path}.items", issues)


def validate_schema(schema: Any, path: str, issues: List[str]) -> None:
    """校验一整份 `{ 字段名: FieldSpec }`。"""
    if schema is None:
        return
    if not isinstance(schema, dict):
        issues.append(f"{path} 必须是 {{ 字段名: 字段定义 }} 形式的对象")
        return
    for name, spec in schema.items():
        validate_field_spec(spec, f"{path}.{name}", issues)


# ============================================================
# 输入值收口（运行期与校验共用）
# ============================================================


def normalize_by_schema(
    schema: Optional[Dict[str, Any]],
    values: Optional[Dict[str, Any]],
    *,
    path: str,
) -> Tuple[Dict[str, Any], List[str]]:
    """按 schema 收口一份值：`required` 卡必填、`enum` 卡可选值、`default` 补默认值。

    返回 `(收口后的值, 问题列表)`。§3.3.1 的三条规则就在这里——
    触发时校验 `task_triggers.inputs`、派活前收口步骤入参，走的是同一个函数。
    **两处对问题的处置不同**：触发侧（§3.3.1）把问题当拒绝理由；派活侧
    （`evaluate_bindings`）**只管填值、不看问题**（§6.2 末尾的填空规则）。
    """
    issues: List[str] = []
    source = values if isinstance(values, dict) else {}
    if values is not None and not isinstance(values, dict):
        return {}, [f"{path} 必须是对象"]

    normalized: Dict[str, Any] = {}
    for name, spec in (schema or {}).items():
        if not isinstance(spec, dict):
            issues.append(f"{path}.{name} 的字段定义不合法")
            continue
        field_type = spec.get("type")
        present = name in source and source[name] is not None
        if not present:
            if "default" in spec and spec["default"] is not None:
                normalized[name] = spec["default"]
            elif spec.get("required"):
                issues.append(f"{path}.{name} 是必填字段，但没给值")
            continue
        value = source[name]
        if field_type and not type_matches(value, field_type):
            issues.append(
                f"{path}.{name} 的类型跟声明对不上（要求 {field_type}）"
            )
            continue
        enum_values = spec.get("enum")
        if isinstance(enum_values, list) and enum_values and value not in enum_values:
            issues.append(
                f"{path}.{name} 只能是 {'/'.join(map(str, enum_values))} 之一，收到 {value!r}"
            )
            continue
        if field_type == "object":
            nested, nested_issues = normalize_by_schema(
                spec.get("properties"), value, path=f"{path}.{name}"
            )
            issues.extend(nested_issues)
            normalized[name] = nested
            continue
        if field_type == "array":
            items_spec = spec.get("items")
            if isinstance(items_spec, dict) and items_spec.get("type") == "object":
                collected = []
                for index, item in enumerate(value):
                    nested, nested_issues = normalize_by_schema(
                        items_spec.get("properties"), item, path=f"{path}.{name}[{index}]"
                    )
                    issues.extend(nested_issues)
                    collected.append(nested)
                normalized[name] = collected
                continue
        normalized[name] = value
    # schema 没声明的键**不进结果**：`normalized` 只从 schema 装，源里多出来的那些键
    # 就此消失 —— 既不报错、也不保留（源里的值仍然照收，只是这个返回值里没有）。
    # N11 拦「`bind_inputs` 绑了个 schema 没声明的字段」要防的就是这个**静默失踪**。
    return normalized, issues


# ============================================================
# N8 / N8b / N9 / N10：引用能解析
# ============================================================


def _declared_type(
    ref: Ref,
    *,
    task_schema: Dict[str, Any],
    catalog_map: Dict[str, TaskCatalogStep],
    node_step_code: Dict[str, str],
) -> Optional[str]:
    """取一个引用指向的字段声明类型（取不到就返回 None，不参与类型比对）。

    引用上游写的是 `node_id`，而 schema 挂在它用的那个**内置步骤**上 ——
    所以要先经 `node_step_code` 换成 `step_code` 再去 `catalog_map` 里查（§3.5）。
    """
    root = ref.path[0] if ref.path else None
    if root is None:
        return None
    if ref.kind == "task":
        spec = (task_schema or {}).get(root)
        return spec.get("type") if isinstance(spec, dict) else None
    upstream = catalog_map.get(node_step_code.get(ref.step or "", ""))
    output_schema = (upstream.output_schema if upstream else None) or {}
    spec = output_schema.get(root)
    return spec.get("type") if isinstance(spec, dict) else None


def _check_ref(
    ref: Ref,
    *,
    where: str,
    node_id: str,
    task_schema: Dict[str, Any],
    catalog_map: Dict[str, TaskCatalogStep],
    node_step_code: Dict[str, str],
    steps_by_node: Dict[str, Dict[str, Any]],
    issues: List[str],
) -> None:
    """一条引用能不能解析（N8 / N8b / N9 / N10）。"""
    if ref.kind == "task":
        root = ref.path[0] if ref.path else None
        if root not in (task_schema or {}):
            issues.append(
                f"{where} 引用了任务级入参 {ref.text()}，"
                f"但 config.input_schema 里没声明 {root!r}（N8b）"
            )
        return
    if ref.kind == "step":
        upstream = ref.step or ""
        if upstream == node_id:
            issues.append(f"{where} 引用了自己的产出 {ref.text()}（自己引用自己，N10）")
            return
        if upstream not in steps_by_node:
            issues.append(
                f"{where} 引用了不存在的节点 {upstream!r}（{ref.text()}），"
                "该节点不在本编排的 steps[] 里（N8）"
            )
            return
        upstream_step_code = node_step_code.get(upstream, "")
        upstream_catalog = (catalog_map or {}).get(upstream_step_code)
        field = ".".join(ref.path)
        output_schema = (upstream_catalog.output_schema if upstream_catalog else None) or {}
        if field not in output_schema:
            issues.append(
                f"{where} 引用了节点 {upstream!r} 的产出字段 {field!r}，"
                f"但它的内置步骤 {upstream_step_code!r} 的 output_schema 里没声明这个字段（N9）"
            )
        return
    issues.append(f"{where} 里的引用写法不认识: {ref.text()}（N2）")


# ============================================================
# 主入口：A.14 的 23 条一次过
# ============================================================


def _string_list(value: Any, where: str, issues: List[str], code: str) -> List[str]:
    """把 `depends_on` / `next_steps` 收成字符串数组（写法不对就报一条、当空处理）。"""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        issues.append(f"{where} 必须是字符串数组（{code}）")
        return []
    return value


def validate_flow_config(
    config: Optional[Dict[str, Any]],
    *,
    task_catalog_code: str,
    catalog_steps: Dict[str, TaskCatalogStep],
    runners: Dict[str, RunnerDefinition],
) -> List[str]:
    """校验一版编排。返回问题列表（空列表 = 全过，可以发版）。"""
    issues: List[str] = []
    if not isinstance(config, dict):
        return ["config 必须是一个对象"]

    # ---- N22 + N13~N17：内置步骤侧 ----
    for step_code, row in (catalog_steps or {}).items():
        if row.runner_code not in (runners or {}):
            issues.append(
                f"内置步骤 {step_code!r} 绑定的运行方 {row.runner_code!r} "
                "不在内置运行方清单里（N22）"
            )
        # 内置步骤的入参 / 产出 schema 也要合法（编排侧拿它们做绑定与引用校验）
        validate_schema(row.input_schema, f"内置步骤 {step_code!r} 的 input_schema", issues)
        validate_schema(
            row.output_schema, f"内置步骤 {step_code!r} 的 output_schema", issues
        )

    # ---- N13~N17：任务级 input_schema ----
    task_schema = config.get("input_schema") or {}
    validate_schema(task_schema, "config.input_schema", issues)
    # `validate_schema` 对非 dict 只记一条 issue、**不中止**，而下面 `_check_ref` 要拿
    # 它当字典查（`root not in task_schema`）—— 不降级就是 `TypeError`，直接破掉本函数
    # 「返回 List[str]、不抛异常」的契约，调用方拿到的是「执行失败」而不是校验问题清单。
    # 降级成 `{}`：issue 已经记过了，后续引用任务级入参一律按 N8b 报，语义也对。
    if not isinstance(task_schema, dict):
        task_schema = {}

    steps = config.get("steps")
    if not isinstance(steps, list) or not steps:
        issues.append("config.steps 不能为空——一版编排至少要选一个节点（N0）")
        return issues

    # ---- N0：node_id 在本编排内唯一 ----
    steps_by_node: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate(steps):
        where = f"config.steps[{index}]"
        if not isinstance(item, dict):
            issues.append(f"{where} 必须是对象")
            continue
        node_id = item.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            issues.append(f"{where}.node_id 必须是非空字符串（N0）")
            continue
        if node_id in steps_by_node:
            issues.append(
                f"节点 {node_id!r} 在 steps[] 里重复出现了（N0：node_id 在本编排内必须唯一）"
            )
            continue
        steps_by_node[node_id] = item
        sort = item.get("sort")
        if sort is not None and not _is_int(sort):
            issues.append(f"{where}.sort 必须是整数（N3）")

    # ---- N21：每个节点的 step_code 必须在该任务的内置步骤清单里 ----
    # 注意：`step_code` 是「用哪个内置步骤」、**可以重复**（parse_1 / parse_2 都是 parse），
    # 所以查的是"在不在清单里"，不是"唯不唯一"。
    node_step_code: Dict[str, str] = {}
    for node_id, item in steps_by_node.items():
        step_code = item.get("step_code")
        if not isinstance(step_code, str) or not step_code:
            issues.append(f"节点 {node_id!r} 的 step_code 必须是非空字符串（N21）")
            continue
        node_step_code[node_id] = step_code
        if step_code not in (catalog_steps or {}):
            issues.append(
                f"节点 {node_id!r} 用的步骤 {step_code!r} "
                f"不在内置任务 {task_catalog_code!r} 的可选步骤清单里（N21）"
            )

    # ---- 逐节点：N2 / N3 / N4 / N11 / N12 / N19 ----
    depends_by_node: Dict[str, List[str]] = {}
    next_by_node: Dict[str, List[str]] = {}
    # 「谁引用了谁的产出」——建这张表只为了 N10 查引用链的环（跟 `depends_by_node`
    # 是**两张不同的图**，见下面 N10 那段）。
    refs_by_node: Dict[str, List[str]] = {}
    for node_id, item in steps_by_node.items():
        where = f"节点 {node_id!r}"
        step_code = node_step_code.get(node_id, "")
        catalog_step = (catalog_steps or {}).get(step_code)
        step_schema = (catalog_step.input_schema if catalog_step else None) or {}

        depends_on = _string_list(
            item.get("depends_on"), f"{where}.depends_on", issues, "N2"
        )
        next_steps = _string_list(
            item.get("next_steps"), f"{where}.next_steps", issues, "N2"
        )
        depends_by_node[node_id] = depends_on
        next_by_node[node_id] = next_steps
        refs_by_node[node_id] = []

        # ---- N23：下游联动的两个开关（2026-10-15 加）只查类型 ----
        # 不填 = true = 从前那条口径，所以存量编排一个字不改也照样过。
        # ⚠️ **一致性那半边不在这里**：它要等 `depends_by_node` 全部建完，
        #    放在本循环之后（见那里）。
        for switch in DOWNSTREAM_SWITCHES:
            raw = item.get(switch)
            if raw is not None and not isinstance(raw, bool):
                issues.append(f"{where}.{switch} 必须是 true / false（N23）")

        # ---- N2：depends_on / next_steps 引用的节点都要存在、不能指自己 ----
        for name, targets in (("depends_on", depends_on), ("next_steps", next_steps)):
            for target in targets:
                if target not in steps_by_node:
                    issues.append(f"{where}.{name} 里引用了不存在的节点 {target!r}（N2）")
                if target == node_id:
                    issues.append(f"{where}.{name} 里引用了自己（N2）")

        # ---- N19：超时时间必填、且必须是正整数 ----
        timeout = item.get("timeout_seconds")
        if timeout is None:
            issues.append(
                f"{where}.timeout_seconds 必填（N19：超时巡检靠它；建编排时"
                "默认取内置步骤清单的值预填）"
            )
        elif not _is_int(timeout) or timeout <= 0:
            issues.append(f"{where}.timeout_seconds 必须是正整数（N19）")

        # 一期不做自动重试（§十二「待规划」）：编排里若出现 on_timeout / on_failure，
        # 既不看也不校验——发版不受它影响，运行期也不认这两个字段。

        # ---- N11 / N12：bind_inputs 字段对得上、来源能解析、类型对得上 ----
        bind_inputs = item.get("bind_inputs")
        if bind_inputs is None:
            bind_inputs = {}
        if not isinstance(bind_inputs, dict):
            issues.append(f"{where}.bind_inputs 必须是对象")
            bind_inputs = {}
        for field_name, raw in bind_inputs.items():
            spec = (step_schema or {}).get(field_name)
            if spec is None:
                issues.append(
                    f"{where}.bind_inputs 里写的 {field_name!r} "
                    f"不在这步（步骤 {step_code!r}）的 input_schema 里（N11）"
                )
            try:
                binding = parse_binding(field_name, raw)
            except ExpressionError as exc:
                issues.append(f"{where}.bind_inputs.{field_name} 写法不对：{exc.message}（N12）")
                continue

            target_type = spec.get("type") if isinstance(spec, dict) else None
            # ---- N12：多个来源只能配给数组字段 ----
            # 合并语义是「把多个值拼成一个数组」，跟单值字段的 `type` 对不上（§6.2）。
            if binding.is_merge and target_type and not _is_array_type(target_type):
                issues.append(
                    f"{where}.bind_inputs.{field_name} 配了多个来源，"
                    f"但目标字段是 {target_type}（不是数组）——多来源合并只给数组字段用（N12）"
                )

            for ref in binding.sources:
                # 记一条引用边（N10 查环用）。**自引用不收**：那一种上面 `_check_ref`
                # 已经用「引用了自己的产出」报过了，再进图会被查成一个长度为 1 的环、
                # 同一件事报两遍。
                if ref.kind == "step" and ref.step and ref.step != node_id:
                    refs_by_node[node_id].append(ref.step)
                _check_ref(
                    ref,
                    where=f"{where}.bind_inputs.{field_name}",
                    node_id=node_id,
                    task_schema=task_schema,
                    catalog_map=catalog_steps or {},
                    node_step_code=node_step_code,
                    steps_by_node=steps_by_node,
                    issues=issues,
                )

            # ---- N12：**常量**来源也要跟目标字段的 `type` 对得上 ----
            #
            # ⚠️ **这一段是补的，不是多余的**：下面那个 `for ref in binding.sources` 只遍历
            # **引用**，而常量的 `sources` 是**空元组**（`parse_binding` 对常量走
            # `Binding(constant=..., is_constant=True, sources=())`），于是
            # `bind_inputs: {"top_k": "10"}`（数字字段给字符串）这类写法**一个都查不到**。
            #
            # 后果不是「发版时报错」，而是**静默丢值**：运行期 `normalize_by_schema` 遇到
            # 类型对不上的值会把它丢掉、连 `default` 都轮不上（那一支只走「键不在源里」），
            # 再由填空那一步补一个空值 —— 该步骤拿到 `null` / `[]` 照跑。而 A.14 的 N12
            # 原话正是「**来源值**的类型跟字段 `type` 对得上」+「拦在发版是为了**别静默
            # 变成空值**」，常量是 §6.2「三种来源形态」里的第一种，本来就该在一起查。
            #
            # ⚠️ **`constant is not None` 不能省**：`bind_inputs: {"x": null}` 的语义是
            # 「**没给值**」而不是「给了一个类型不对的值」—— 运行期 `normalize_by_schema`
            # 对 `None` 走的是「键不在源里」那一支（`source[name] is not None`），`default`
            # 与必填判定都正常生效，值**不会**被静默丢掉。把它一并拒掉是另一种行为改变，
            # 不在本段的射程里。
            #
            # 判据复用 `type_matches`（`normalize_by_schema` 读的是同一份实现）：这样
            # 「发版放过」与「运行期保留」是**同一个集合**，不会出现「查了一套、跑的是另一套」。
            #
            # ⚠️ **常量这一支是「精确比对」，与下面引用那一支的「标量 / file / 数组」三桶
            # 颗粒度刻意不同**（`_type_bucket`）—— 别看着别扭就把它对齐成三桶：
            # 常量在发版这一刻**值就摆在眼前**，而运行期丢值用的是**精确**判据
            # （`normalize_by_schema` 里那句 `type_matches`），所以 `{"top_k": "10"}`
            # 这种写法 100% 会被丢掉、`default` 都轮不上。按三桶放过它，等于留着
            # 「发版说没事、跑起来拿到 `null`」这条最容易被忽略的路。引用那一支之所以
            # 只能到三桶，是因为发版时拿到的是**声明类型**而不是真值（§6.5 N12 的括号
            # 就写着「标量 / file / 数组」）——两者不是同一个前提。
            if (
                target_type
                and binding.is_constant
                and binding.constant is not None
                and not type_matches(binding.constant, target_type)
            ):
                issues.append(
                    f"{where}.bind_inputs.{field_name} 的常量 {binding.constant!r} "
                    f"跟目标字段的 {target_type} 对不上——"
                    "运行期这个值会被丢掉、按空值填（N12）"
                )

            # ---- N12：来源声明类型与目标字段的形态要对得上（标量 / file / 数组）----
            if not target_type:
                continue
            for ref in binding.sources:
                source_type = _declared_type(
                    ref,
                    task_schema=task_schema,
                    catalog_map=catalog_steps or {},
                    node_step_code=node_step_code,
                )
                if (
                    source_type
                    and _type_bucket(source_type) != _type_bucket(target_type)
                ):
                    issues.append(
                        f"{where}.bind_inputs.{field_name} 目标字段是 {target_type}，"
                        f"但来源 {ref.text()} 是 {source_type}——"
                        "标量 / file / 数组的形态对不上（N12）"
                    )

    # ---- N18：枚举合法 ----
    # `config` 里**现在没有枚举型字段**（`fail_policy` 已从编排里删掉、不再校验它）。
    # 这条留给"将来新增的枚举字段"，以及 `steps[].step_code` 这类取值约束的兜底 ——
    # 后者的取值约束由 N21 查（在不在内置步骤清单里）。

    # ---- N5 / N6 / N7：入口合法 ----
    # `entry` 是**单个 `node_id` 字符串**（§6.1），不是列表。
    entry = config.get("entry")
    if not isinstance(entry, str) or not entry:
        issues.append(
            "config.entry 必填、且必须是单个节点 id（node_id）字符串（N5）"
        )
        entry = None
    elif entry not in steps_by_node:
        issues.append(f"config.entry 里的 {entry!r} 不在 steps[] 里（N5）")

    if entry in steps_by_node:
        if depends_by_node.get(entry):
            issues.append(
                f"入口节点 {entry!r} 不该有 depends_on——入口就是没上游的那一个（N6）"
            )
        # ---- N7：没有孤儿节点：depends_on 为空的节点有且只有 entry 一个 ----
        for node_id in steps_by_node:
            if node_id == entry:
                continue
            if not depends_by_node.get(node_id):
                issues.append(
                    f"节点 {node_id!r} 没有上游、但又不是 config.entry({entry!r})"
                    "——它永远不会被跑到（N7）"
                )

    # ---- N4：next_steps 与 depends_on 互为对偶 ----
    for node_id, targets in next_by_node.items():
        for target in targets:
            if target not in steps_by_node:
                continue
            if node_id not in depends_by_node.get(target, []):
                issues.append(
                    f"{node_id!r}.next_steps 里有 {target!r}，"
                    f"但 {target!r}.depends_on 里没有 {node_id!r}——两者必须互为对偶（N4）"
                )
    for node_id, upstreams in depends_by_node.items():
        for upstream in upstreams:
            if upstream not in steps_by_node:
                continue
            if node_id not in next_by_node.get(upstream, []):
                issues.append(
                    f"{node_id!r}.depends_on 里有 {upstream!r}，"
                    f"但 {upstream!r}.next_steps 里没有 {node_id!r}——两者必须互为对偶（N4）"
                )

    # ---- N3：同层 sort 不能重复，且多对一合并时必须填 ----
    # 只查「汇入同一个下游的那组上游之间」：全图不要求 `sort` 唯一，只要同层不撞；
    # 但下游有多个上游时，参与合并的这几个**都必须填了 `sort`**（缺了没法定顺序）。
    for node_id, upstreams in depends_by_node.items():
        if len(upstreams) < 2:
            continue
        seen_sort: Dict[int, str] = {}
        for upstream in upstreams:
            upstream_item = steps_by_node.get(upstream)
            if upstream_item is None:
                continue
            sort = upstream_item.get("sort")
            if not _is_int(sort):
                issues.append(
                    f"节点 {node_id!r} 有多个上游（{len(upstreams)} 个）要合并，"
                    f"但上游 {upstream!r} 没填 sort——没法定稳定顺序（N3）"
                )
                continue
            if sort in seen_sort:
                issues.append(
                    f"节点 {node_id!r} 的上游 {seen_sort[sort]!r} 和 {upstream!r} "
                    f"用了同一个 sort={sort}——同层顺序必须唯一确定（N3）"
                )
            else:
                seen_sort[sort] = upstream

    # ---- N1：无死循环 ----
    issues.extend(_detect_cycles(depends_by_node))

    # ---- N10：引用链也不能有环 ----
    # N1 查的是「依赖图」，这条查的是「引用图」，**两者的边不是一回事**：
    # `depends_on` 说的是调度顺序，而 `bind_inputs` 里的 `@<node>.outputs.<field>`
    # 只要求那个节点**存在**（N8）——**并不要求它是上游**。所以「依赖图无环」推不出
    # 「引用图无环」：A 依赖 B、A 又引用 C 的产出、C 再引用 A 的产出，依赖图照样无环，
    # 引用链却是个圈。文档要的是两条都无环（§6.5 N10：「引用链里没有自己引用自己、
    # 也没有循环」—— 前半句 `_check_ref` 报了，后半句就是这里）。
    #
    # ⚠️ **「引用不必是上游」是刻意的，但它的后果要看清**：运行期只从本步骤的
    # `depends_on` 取产出（`RunContext.upstream_outputs`），所以引用了非上游节点时
    # **取值必然 MISSING、按缺值静默落 `default` / 空值**。校验器**不拦**这种编排 ——
    # 「引用不是依赖」是设计（见设计文档 §6.2「引用一个『不是本步骤上游』的节点怎么办」），
    # 要让值真的到位，得让使用者把被引用的节点写进 `depends_on`。**别在这里补一条
    # 「引用必须是上游」的判据**：那会把上面这条 N10 变成死代码，也与该设计冲突。
    issues.extend(_detect_cycles(refs_by_node, label="引用关系", rule="N10"))

    # ---- N23：同一个节点的所有上游，下游联动开关必须同值（2026-10-15 加）----
    #
    # 一条边（上游 U → 下游 D）跟不跟，只看 **U 自己**那个字段（见
    # `TaskService._downstream_chain`）。于是「D 有多个上游、而它们态度不一致」时，
    # D 到底跟不跟**说不清** —— 点 A 重跑时从 A 看该跟、从 B 看不该跟。
    # 这种编排当场拦下，别让它发出去。
    #
    # ⚠️ **只有「汇聚」才可能触发**：线性链（解析 → 编译）每个节点只有一个上游；
    #    分叉（A 同时喂 B 和 C）也不会 —— B、C 各自只有一个上游。
    #    所以库里那几条预置编排永远不会碰到这一条。
    #
    # ⚠️ **上游里那些不存在的节点先摘掉**：那种情况上面 N2 已经报过了，
    #    这里再报一遍只是噪音（而且「不存在的节点」的开关根本无从取值）。
    for switch in DOWNSTREAM_SWITCHES:
        for node_id, upstreams in depends_by_node.items():
            known = [up for up in upstreams if up in steps_by_node]
            if len(known) < 2:
                continue
            values = {up: steps_by_node[up].get(switch, True) for up in known}
            if len(set(values.values())) < 2:
                continue
            detail = "、".join(
                f"{up}={str(value).lower()}" for up, value in sorted(values.items())
            )
            issues.append(
                f"节点 {node_id!r} 的上游对 {switch} 的配置不一致（{detail}）——"
                "同一个节点的所有上游必须同值，否则「它跟不跟」说不清（N23）"
            )
    # ---- 最后一道：**类型外壳自己也要认**（2026-09-30 接进来）----
    #
    # `FlowConfig` / `FlowStep` / `FieldSpec` 从前是「声明了但全仓没人构造」的类型外壳，
    # 真链路一律吃裸字典 —— 改了其中一边，另一边不会报错、测试也抓不到。这一道把它们
    # 接成真链路的一部分。
    #
    # ⚠️ **它们在前面那些 N 判据之后跑，顺序不能反**：N 判据是**业务规则**（节点存不存在 /
    # 有没有环 / 引用能不能解析 / 上游对不对偶），报错文案带 N 编号、落在 `details.issues`
    # 里 —— 那是**发版被拒的契约**，调用方与文档都按它读。类型外壳只多一层**形状**闸，
    # 失败**也只追加一条 issue**（不回 pydantic 的 `details.errors`，那是另一套契约）。
    #
    # ⚠️ **两边判据必须一致**：外壳一律用 `Strict*`，所以「N 判据今天拒掉的非法类型」
    # 在这里同样被拒（`timeout_seconds="300"` / `300.5` / `true`、`sort="3"`、
    # `node_id=123`、`depends_on=[1,2]`、`entry=123`、`required="yes"` …）—— 用裸类型
    # 接进来会把这些**全收下**，等于把发版校验放松。逐条对照见
    # `tests/unit/task_center/test_task_center_flow_config_dto_parity.py`。
    #
    # ⚠️ 走到这里说明 N 判据已经全过。**两道闸的覆盖范围不一样、别混**：
    #   · **类型层**两边一致 —— 由 `test_task_center_flow_config_dto_parity.py` 逐条钉住
    #     （含 `properties` / `items` 的嵌套：`type` 对不上时两边**都忽略**）；
    #   · **N 独有的规则**类型外壳**管不着**（非空 / 正数 / 枚举取值 / 节点存不存在 /
    #     有没有环 / 引用能不能解析…）—— 那些**只**由 N 判据负责，别指望这里兜。
    # 所以这一道是**防御性的第二道形状闸**：将来谁把类型外壳改松（把 `Strict*` 换回裸类型、
    # 让非法值被悄悄洗成合法值），这里会兜住。
    try:
        FlowConfig(**config)
    except ValidationError as exc:
        issues.append(f"编排的结构不符合类型定义：{exc}")

    return issues
def _detect_cycles(
    edges: Dict[str, List[str]], *, label: str = "依赖关系", rule: str = "N1"
) -> List[str]:
    """查一张「节点 → 它指向的节点」的表里有没有环（DFS 三色标记）。

    同一份实现服务两条校验，靠 `label` / `rule` 区分（两处报出来的话不一样）：

    - `depends_on` 那张表 → N1（依赖图无死循环）
    - `bind_inputs` 引用那张表 → N10（引用链无环）

    **表里没有的节点当"无边"跳过**（指向不存在的节点由 N2 / N8 各自报，不在这里报）。
    """
    issues: List[str] = []
    WHITE, GREY, BLACK = 0, 1, 2
    color = {key: WHITE for key in edges}

    def visit(node: str, trail: List[str]) -> None:
        color[node] = GREY
        trail.append(node)
        for target in edges[node]:
            if target not in edges:
                continue
            if color[target] == GREY:
                cycle = " → ".join(trail[trail.index(target):] + [target])
                issues.append(f"{label}里有死循环：{cycle}（{rule}）")
            elif color[target] == WHITE:
                visit(target, trail)
        trail.pop()
        color[node] = BLACK

    for key in list(edges):
        if color[key] == WHITE:
            visit(key, [])
    return issues


# ============================================================
# 运行期：按 bind_inputs 填值
# ============================================================


def evaluate_bindings(
    step: Dict[str, Any],
    *,
    task_inputs: Dict[str, Any],
    upstream_outputs: Dict[str, Dict[str, Any]],
    step_sort: Dict[str, int],
    input_schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把 `bind_inputs` 解析成本步骤的真实入参，再按 `input_schema` 收口。

    `upstream_outputs` 是「**节点 id（node_id）** → 那个节点的产出」。多个来源按上游
    `sort` 稳定排序后收口（§6.2）——目标字段是数组就把各来源拼成数组。取不到值的
    来源按 `MISSING` 处理、不参与合并；常量原样带上。

    **取不到值就填空值**（§6.2 末尾）：某字段的引用拿不到、又没配 `default`（或者
    压根没绑），按字段类型给空值（`array` → `[]`、`object` → `{}`、其余 → `null`）
    ——**`required: true` 也照样填，不在这里拒绝**。编排者自己接受"这个字段可能拿不到"，
    判断权留给下游：管理方填不上就填空。

    和 `normalize_by_schema` 的分工：**这里只看收口结果、不看它返回的问题列表** ——
    发版时 `required` 已经不在校验范围里（见模块 docstring），运行期再拒就等于换个地方拒。
    """
    resolver = resolver_from_sources(task_inputs, upstream_outputs)
    sort_key = lambda ref: step_sort.get(ref.step or "", 0)  # noqa: E731

    bound: Dict[str, Any] = {}
    for field_name, raw in (step.get("bind_inputs") or {}).items():
        binding: Binding = parse_binding(field_name, raw)
        spec = (input_schema or {}).get(field_name)
        merge_as_list = isinstance(spec, dict) and _is_array_type(spec.get("type"))
        value = binding.resolve(resolver, sort_key=sort_key, merge_as_list=merge_as_list)
        if value is MISSING or value is None:
            # 取不到值就先不写进结果——交给 schema 收口决定是补默认值还是填空值
            continue
        bound[field_name] = value

    normalized, _issues = normalize_by_schema(
        input_schema, bound, path=f"节点 {step.get('node_id', '?')} 的 inputs"
    )

    # ---- 填空：schema 里声明了、收口后仍没有值的字段 ----
    # 走到这里说明既没绑到值、也没配 `default`（配了就已被 normalize 补上）。
    for field_name, spec in (input_schema or {}).items():
        if field_name in normalized:
            continue
        field_type = spec.get("type") if isinstance(spec, dict) else None
        normalized[field_name] = empty_value_for(field_type)
    return normalized


__all__ = [
    "ExpressionError",
    "IN_FIELD_TYPES",
    "empty_value_for",
    "evaluate_bindings",
    "normalize_by_schema",
    "parse_binding",
    "type_matches",
    "validate_flow_config",
]
