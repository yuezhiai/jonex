# -*- coding: utf-8 -*-
"""[jonex] 钉子：证明 `import raganything` 解析到的是**本仓这棵树**。

本机 raganything 是 editable 安装，可能指向**另一份副本**
（见仓库根 CLAUDE.md 与 auto-memory `vendored-raganything-test-runbook`）。
跑错树时全部用例仍会绿，只是测的不是本目录的改动 —— 故把这条前提显式断言出来。
（原宿主在 `TestGetTableBodyEquivalence` 里带过这条断言；P2 Task 4 删除那个恒真用例时一并丢失。）
"""
from __future__ import annotations

import pathlib


def test_vendored_raganything_resolves_to_this_repo():
    import raganything

    # tests/test_service/<file> → parents[4] = 仓库根
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    resolved = pathlib.Path(raganything.__file__).resolve()
    assert resolved.is_relative_to(repo_root), (
        f"raganything 解析到了本仓之外：{resolved}（仓库根 {repo_root}）—— "
        "必须 cd Reference/Rag-anything 并带 PYTHONPATH=<仓库根> 运行"
    )
