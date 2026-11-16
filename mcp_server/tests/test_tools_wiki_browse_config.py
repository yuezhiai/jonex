#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""MCP 配置：Jonex Wiki 列式浏览环境变量的读取与回落。"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module

_WIKI_VARS = (
    "WIKI_BROWSE_ENABLED",
    "WIKI_BROWSE_DEPTH_MAX",
    "WIKI_BROWSE_REQUEST_TIMEOUT_SECONDS",
    "WIKI_BROWSE_DEADLINE_SECONDS",
)


@pytest.fixture(autouse=True)
def _restore_config(monkeypatch):
    yield
    for name in _WIKI_VARS:
        monkeypatch.delenv(name, raising=False)
    importlib.reload(config_module)


def _reload_with(monkeypatch, **env):
    for name in _WIKI_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    importlib.reload(config_module)
    return config_module.settings


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, True),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("garbage", True),  # 非法值回落默认
    ],
)
def test_wiki_browse_enabled_bool(monkeypatch, raw, expected):
    env = {} if raw is None else {"WIKI_BROWSE_ENABLED": raw}
    assert _reload_with(monkeypatch, **env).WIKI_BROWSE_ENABLED is expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 3),
        ("1", 1),
        ("20", 20),
        ("0", 3),      # 低于下界回落
        ("21", 3),     # 超过上界回落
        ("abc", 3),    # 非整数回落
    ],
)
def test_wiki_browse_depth_max(monkeypatch, raw, expected):
    env = {} if raw is None else {"WIKI_BROWSE_DEPTH_MAX": raw}
    assert _reload_with(monkeypatch, **env).WIKI_BROWSE_DEPTH_MAX == expected


@pytest.mark.parametrize(
    "name,default,low,high",
    [
        ("WIKI_BROWSE_REQUEST_TIMEOUT_SECONDS", 30, "0", "121"),
        ("WIKI_BROWSE_DEADLINE_SECONDS", 60, "0", "181"),
    ],
)
def test_wiki_browse_timeout_ranges(monkeypatch, name, default, low, high):
    assert getattr(_reload_with(monkeypatch), name) == default
    assert getattr(_reload_with(monkeypatch, **{name: low}), name) == default
    assert getattr(_reload_with(monkeypatch, **{name: high}), name) == default
