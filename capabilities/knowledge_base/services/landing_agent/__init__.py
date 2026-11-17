"""落点 Agent（evidence-driven landing）：召回索引与确定性打分。

任务边界：

- **纯函数**：`build_index` / `score_candidates` / `tier_of` 不读配置、不查库、
  不调模型、无随机 —— 同一输入必须给出同一顺序（调用方要能复算与对账）。
- **唯一实现**：五路信号直接复用 `nl2sql/schema_retrieval.py`，不在这里另写一份。
  落点 Agent 与 NL→SQL 看到的是同一套词面/图信号，打分口径才不会漂移。
"""
