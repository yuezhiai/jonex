# -*- coding:utf-8 -*-
"""指标构建 Agent：有界循环 + 只读工具，产出**提案**而不是直接写指标。

入口在 `runner.MetricAgentService`；协议与工具见 `loop` / `tools`。
"""
from __future__ import annotations

__all__ = ["loop", "runner", "tools"]
