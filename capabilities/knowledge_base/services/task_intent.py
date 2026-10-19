#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""运行方侧的第二个**粘性值**：`extra_metadata["tc_intent"]` —— 这次是哪种操作。

## 为什么需要它

Ontology 库一进任务中心，同一个 `step_code == "parse"` 就背上了**两种含义**：

| 这次是哪种操作 | 那一份文档当初怎么来的 | 运行方的 `parse` 步该发哪个档位 |
| --- | --- | --- |
| `upload` | 用户上传 | atomic-rag `full` —— 解析 + 多模态 + **推切片**，一趟干完 |
| `reparse` | 用户点「重新解析」 | atomic-rag `reparse_strict` —— 解析 **+ 删旧切片 + 推新切片**（带代次 fencing） |

「重新解析」沿用 `parse` 这个步骤代号是**用户定的**（不新加第四个 `step_code`），
代价就是这个语义分叉：**同一个步骤名，档位不一样**（差别在「先删掉旧的那一份」）。
分叉本身不可怕，可怕的是判错它 —— 判成 `upload` 的那一侧会把一次「重新解析」干成
「白解析一遍」（旧切片没删，用户看到的是「点了没反应」）；判成 `reparse` 的那一侧会
把上传的解析步变成一次全量替换（旧切片被删、代次被顶掉）。

## 纪律：写的人只管写，读的人只管读

与 `ingest_backend` 模块**逐字同一条纪律**：

- **写**：只在「提交/发起那一刻」写一次 —— 上传路径
  （`document_service._upload_via_task_center`）写 `upload`，重新解析入口
  （`document_service.reparse_document`）写 `reparse`，任务中心的「任务重试」
  （`task_runner_service._rearm_after_console_retry`）写 `reparse`
  （它就是「再解析一遍」的语义，且必须与 `reparse_strict` 配套才会真的删掉旧切片）。
  三处都与接管行的写入**同一次提交**。
- **读**：只有 handler（`task_runner_service._prepare_step`）读它 —— 它要据此选引擎档位。
  **`不读 env、不猜步骤名`**。看门狗（`task_watch_service`）**已经不读它了**：
  两条链的 `parse` 都推切片，所以「哪一步置 `ready`」不必分来头（2026-09-29 起）。

**选档位只准看这个值、不准看步骤名字** —— 步骤名字在这里分不出来，这是上面那张表
唯一的结论。

## 为什么默认值是 `upload` 而不是「抛」

`backend_for_document` 对缺失/域外值的处置是回落 `rag`，本模块对缺失/域外值的处置
是回落 `upload` —— 两处同一条理由：**存量文档**（这次接入之前上传的、或接管行被
手工动过的）行上没有这个键，而它们绝大多数确实是上传来的。回落 `upload` 让它们
继续按老语义跑；抛异常则会把「一份本来能跑的老文档」变成「一份跑不动的文档」。

⚠️ 回落的**代价**要认清：一份**真的**是 `reparse` 的文档，若那个键丢了，会被按
`upload` 干（不删旧切片）—— 表现为「重新解析点了没反应」。所以这个键必须与
接管行同一次提交写入：能丢它的只有「手工改库」。
"""

from typing import Any, Mapping, Optional

#: 用户上传触发的解析（`full`：解析 + 多模态 + 推切片，一趟干完）。
INTENT_UPLOAD = "upload"

#: 用户点「重新解析」触发的解析（`reparse_strict`：解析 + 删旧切片 + 推新切片）。
INTENT_REPARSE = "reparse"

#: 值域。**唯一出处**：`document_service` 的两处写入与 `task_runner_service` 的读取
#: 都从这两个常量取，不出现裸字符串。
VALID_INTENTS = (INTENT_UPLOAD, INTENT_REPARSE)


def intent_for_document(doc: Any) -> str:
    """这份文档这次是哪种操作（文档行上的**粘性值**）。

    与 `ingest_backend.backend_for_document` 同构：`doc` 可以是 detached 的 ORM 对象、
    也可以是 `None`（文档被删了），缺失 / 域外值一律回落 `INTENT_UPLOAD`。
    """
    metadata = getattr(doc, "extra_metadata", None) or {}
    value = metadata.get("tc_intent")
    return value if value in VALID_INTENTS else INTENT_UPLOAD


def with_intent(metadata: Optional[Mapping[str, Any]], intent: str) -> dict:
    """把 `tc_intent` 写进 `extra_metadata`，**返回整块新 dict**。

    ⚠️ 必须整体重赋值：`extra_metadata` 是 `JSONB`，SQLAlchemy **不追踪就地修改**
    （老路径 `meta["ingest_backend"] = backend` 之后不赋值就丢数据，是同一条坑）。

    `intent` 域外时按 `INTENT_UPLOAD` 写 —— 与读取侧的回落口径保持一致，别让「写进去
    一个读不出来的值」这种事发生（那等于这个键从来没有过，却看起来写成功了）。
    """
    return {
        **(metadata or {}),
        "tc_intent": intent if intent in VALID_INTENTS else INTENT_UPLOAD,
    }


__all__ = [
    "INTENT_REPARSE",
    "INTENT_UPLOAD",
    "VALID_INTENTS",
    "intent_for_document",
    "with_intent",
]
