#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MCP 配置：同 KB 多跳（D11）两个环境变量的读取与回落。

覆盖 ``config._env_int`` 的默认值 / 合法值 / 缺失 / 非整数 / 越界回落：
  - ``ONTOLOGY_NEIGHBOR_DEPTH_MAX``（默认 3，合法 1–20）
  - ``ONTOLOGY_MULTIHOP_DEADLINE_SECONDS``（默认 25，合法 1–120）

写法仿 ``test_config_ontology_fanout.py``：设置环境变量后 ``importlib.reload``
配置模块，读取 ``settings`` 上的解析结果。
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module

_ENV_VARS = (
    "ONTOLOGY_NEIGHBOR_DEPTH_MAX",
    "ONTOLOGY_MULTIHOP_DEADLINE_SECONDS",
)


@pytest.fixture(autouse=True)
def _restore_config(monkeypatch):
    yield
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    importlib.reload(config_module)


def _reload_with(monkeypatch, name, value):
    """设置环境变量后重载配置模块，返回解析后的值（``None`` = 不设置）。"""
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    importlib.reload(config_module)
    return getattr(config_module.settings, name)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 3),      # 未配置 → 默认 3
        ("5", 5),       # 合法值
        ("1", 1),       # 下界
        ("20", 20),     # 上界
        ("abc", 3),     # 非整数 → 回落
        ("", 3),        # 空串 → 回落
        ("  ", 3),      # 空白 → 回落
        ("0", 3),       # 低于下界 → 回落
        ("-1", 3),      # 负数 → 回落
        ("21", 3),      # 超过上界 → 回落
    ],
)
def test_ontology_neighbor_depth_max(monkeypatch, raw, expected):
    assert _reload_with(monkeypatch, "ONTOLOGY_NEIGHBOR_DEPTH_MAX", raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 25),     # 未配置 → 默认 25
        ("60", 60),     # 合法值
        ("1", 1),       # 下界
        ("120", 120),   # 上界
        ("abc", 25),    # 非整数 → 回落
        ("", 25),       # 空串 → 回落
        ("  ", 25),     # 空白 → 回落
        ("0", 25),      # 低于下界 → 回落
        ("-1", 25),     # 负数 → 回落
        ("121", 25),    # 超过上界 → 回落
    ],
)
def test_ontology_multihop_deadline_seconds(monkeypatch, raw, expected):
    assert (
        _reload_with(monkeypatch, "ONTOLOGY_MULTIHOP_DEADLINE_SECONDS", raw)
        == expected
    )
