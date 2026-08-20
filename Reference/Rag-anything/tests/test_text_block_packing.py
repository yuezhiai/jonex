# -*- coding: utf-8 -*-
"""§block-packing 改动 6a：文本块打包纯函数单测
（docs/text-block-packing-chunk-governance-plan.md §3.2/§4 改动 1）。"""

import pytest

from raganything.utils import (
    is_noise_block,
    is_heading_like,
    pack_text_blocks,
)


class TestIsNoiseBlock:
    """C2 纯数字/罗马/符号、C3 高重复页眉页脚。"""

    @pytest.mark.parametrize(
        "text",
        ["1", "23", "1234", "iv", "IV", "xlii", "...", "——", "· ·", "----"],
    )
    def test_c2_pure_forms(self, text):
        assert is_noise_block(text) is True

    @pytest.mark.parametrize(
        "text",
        ["12345", "1.2", "v-1", "第3页", "abc", "。。。。。", "-----"],
    )
    def test_not_noise(self, text):
        assert is_noise_block(text) is False

    def test_blank_is_noise(self):
        assert is_noise_block("") is True
        assert is_noise_block("   \t ") is True

    def test_c3_repeat_index(self):
        # 出现 3 次才命中（阈值 3）
        repeat = {"岭南画派": 3, "共 12 页": 4, "仅一次": 1}
        assert is_noise_block("岭南画派", repeat) is True
        assert is_noise_block(" 岭南画派 ", repeat) is True  # strip 后计数
        assert is_noise_block("共 12 页", repeat) is True
        assert is_noise_block("仅一次", repeat) is False
        assert is_noise_block("两次的标题", {"两次的标题": 2}) is False

    def test_c3_skipped_without_repeat_index(self):
        # 无预扫结果时只做 C2，长于 4 字的重复页眉不误杀
        assert is_noise_block("岭南画派", None) is False

    def test_c3_length_cap(self):
        # 超过 20 字即使高重复也不按噪声丢弃（20 字整命中，21 字不命中）
        hit = "一二三四五六七八九十一二三四五六七八九十"
        miss = hit + "一"
        assert len(hit) == 20 and len(miss) == 21
        assert is_noise_block(hit, {hit: 10}) is True
        assert is_noise_block(miss, {miss: 10}) is False


class TestIsHeadingLike:
    """C4 编号模式、C5 短 + 无句末符 + 无换行。"""

    @pytest.mark.parametrize(
        "text",
        [
            "第一节 折衷中西",
            "第3章 方法",
            "3. 数据来源",
            "1.2.3 小节标题",
            "（一）背景",
            "(3) 配置",
            "图1 系统架构",
            "表 3 对比结果",
            "附录A 术语表",
            "附表 2 明细",
            "附图一 示意图",
            "A. 概述",
            "B、分类",
        ],
    )
    def test_c4_numbered_patterns(self, text):
        assert is_heading_like(text) is True

    def test_c4_gate_above_max_len_but_below_gate(self):
        # C4 有独立长度闸门 max(max_len*2, 80)：超 max_len 但未超闸门的
        # 编号标题仍判 heading（review P0-1 修正前这里是「完全不受限」）
        assert is_heading_like("第1234567890章 超长编号标题", max_len=5) is True
        assert is_heading_like("1. " + "长" * 70 + "节", max_len=40) is True  # 75 ≤ 80

    def test_c4_long_numbered_body_not_heading(self):
        # review P0-1：编号样式的长正文段超过闸门 → 必须走正文，否则
        # 包头渲染截断会真丢正文
        long_body = "1. " + "正文内容" * 20 + "。"  # 83 字 > 80
        assert len(long_body) > 80
        assert is_heading_like(long_body, max_len=40) is False
        # 更极端的数百字段落（review 复现实测：453 字符被截到 151）
        assert is_heading_like("1. 政策出台背景是" + "字" * 400, max_len=40) is False
        # 闸门与 max_len 联动：max_len 越大闸门越高（122 ≤ max(200,80)=200）
        assert is_heading_like("1. " + "长" * 120, max_len=100) is True

    def test_c5_short_no_sentence_end(self):
        assert is_heading_like("岭南画派研究") is True
        assert is_heading_like("概述") is True

    @pytest.mark.parametrize(
        "text",
        [
            "这是正文。",
            "结尾有感叹号！",
            "结尾有问号？",
            "结尾有分号；",
            "结尾有句号.",
        ],
    )
    def test_sentence_end_rejected(self, text):
        assert is_heading_like(text) is False

    def test_newline_rejected(self):
        assert is_heading_like("标题\n副标题") is False

    def test_max_len_boundary(self):
        assert is_heading_like("一二三四五六七八九十", max_len=10) is True
        assert is_heading_like("一二三四五六七八九十甲", max_len=10) is False

    def test_empty(self):
        assert is_heading_like("") is False
        assert is_heading_like("   ") is False


class TestPackTextBlocks:
    def _blocks(self, *items):
        """items: (text, page_idx) 简写构造块序列。"""
        return [
            {"text": text, "page_idx": page, "char_start": i * 10, "char_end": i * 10 + len(text),
             "line_start": i * 2, "line_end": i * 2 + 1}
            for i, (text, page) in enumerate(items)
        ]

    def test_basic_pack_no_heading(self):
        blocks = self._blocks(("第一句正文，比较长。", 1), ("第二句正文。", 1))
        packs, dropped = pack_text_blocks(blocks, budget=1000)
        assert len(packs) == 1
        assert packs[0]["text"] == "第一句正文，比较长。\n第二句正文。"
        assert packs[0]["heading"] is None
        assert packs[0]["page_start"] == 1
        assert packs[0]["page_end"] is None
        assert packs[0]["pspans"] is None
        assert packs[0]["block_count"] == 2
        assert packs[0]["char_start"] == 0
        assert packs[0]["char_end"] == 10 + len("第二句正文。")
        assert dropped == []

    def test_heading_becomes_pack_head(self):
        blocks = self._blocks(("第一节 折衷中西", 1), ("正文内容。", 1))
        packs, _ = pack_text_blocks(blocks, budget=1000)
        assert len(packs) == 1
        assert packs[0]["text"] == "【第一节 折衷中西】\n正文内容。"
        assert packs[0]["heading"] == "【第一节 折衷中西】"
        assert packs[0]["page_start"] == 1

    def test_two_heading_levels(self):
        blocks = self._blocks(("第一章 总论", 1), ("第一节 折衷中西", 1), ("正文。", 1))
        packs, _ = pack_text_blocks(blocks, budget=1000)
        assert len(packs) == 1
        assert packs[0]["text"].startswith("【第一章 总论 / 第一节 折衷中西】\n")
        assert packs[0]["heading"] == "【第一章 总论 / 第一节 折衷中西】"

    def test_heading_levels_one(self):
        blocks = self._blocks(("第一章 总论", 1), ("第一节 折衷中西", 1), ("正文。", 1))
        packs, _ = pack_text_blocks(blocks, budget=1000, heading_levels=1)
        assert packs[0]["heading"] == "【第一节 折衷中西】"

    def test_budget_split_keeps_head(self):
        # 预算紧：正文拆两包，每包都带同一包头（正文带句末符，避免 C5
        # 把无标点短正文误判成标题）
        blocks = self._blocks(("第一节 标题", 1), ("十二个字十二个字十二个字。", 1), ("十三个字十三个字十三个字。", 1))
        packs, _ = pack_text_blocks(blocks, budget=20)
        assert len(packs) == 2
        for p in packs:
            assert p["text"].startswith("【第一节 标题】\n")

    def test_continuation_pack_page_from_first_body(self):
        # review P0-2：续包 page_start 取首个正文块页，不拉回标题页。
        # 甲（页 2）随标题入包 1；丙（页 4）+丁（页 5）为续包——
        # 预算 38：包 1 装不下丙（9+15=24，+15=39>38 冲刷）。
        blocks = self._blocks(
            ("第一节 标题", 2),
            ("正文甲一二三四五六七八九十。", 2),
            ("正文丙一二三四五六七八九十。", 4),
            ("正文丁。", 5),
        )
        packs, _ = pack_text_blocks(blocks, budget=38)
        assert len(packs) == 2
        assert packs[0]["page_start"] == 2        # 新收标题 → 标题页
        assert packs[0]["page_end"] is None
        assert packs[0]["pspans"] is None
        p1 = packs[1]
        assert p1["text"].startswith("【第一节 标题】\n")
        assert p1["page_start"] == 4              # 继承标题 → 首正文块页
        assert p1["page_end"] == 5
        # 丙 14 字 → 丁起点 = 包头 9 + 14 + 1 = 24
        assert p1["pspans"] == "0@4;24@5"

    def test_head_overflow_kept_as_first_body_line(self):
        # review P0-1：超长包头截断渲染，被截掉的完整包头转正文首行
        # （零信息损失），而不是直接丢弃
        h1 = "1. " + "长" * 70   # 73 ≤ 80 闸门 → 判 heading
        h2 = "2. " + "长" * 70
        blocks = self._blocks((h1, 1), (h2, 1), ("正文。", 1))
        packs, _ = pack_text_blocks(blocks, budget=1260)
        assert len(packs) == 1
        p = packs[0]
        assert p["heading"].endswith("…")
        assert len(p["heading"]) <= max(16, int(1260 * 0.08)) + 1
        full = "【" + " / ".join([h1, h2]) + "】"
        assert p["text"].startswith(p["heading"] + "\n")
        assert full in p["text"]      # 完整包头未丢失
        assert p["text"].endswith("正文。")

    def test_carry_heads_across_calls(self):
        # review P1：标题跨硬边界（table/image 冲刷）继承——调用 1 收
        # 标题+正文，调用 2 只正文，包头延续且页码不拉回标题页
        carry: dict = {}
        packs1, _ = pack_text_blocks(
            self._blocks(("第一章 总论", 1), ("正文甲。", 1)),
            budget=1000, carry=carry,
        )
        assert len(packs1) == 1
        assert packs1[0]["text"].startswith("【第一章 总论】\n")
        assert packs1[0]["page_start"] == 1
        packs2, _ = pack_text_blocks(
            self._blocks(("正文乙。", 3)),
            budget=1000, carry=carry,
        )
        assert len(packs2) == 1
        p2 = packs2[0]
        assert p2["text"].startswith("【第一章 总论】\n")
        assert p2["page_start"] == 3              # 不拉回标题页 1
        assert p2["pspans"] is None
        assert carry["heads"] == ["第一章 总论"]
        assert carry["heads_fresh"] is False

    def test_carry_fresh_head_after_cross_call(self):
        # review P1 变体：调用 1 只收标题（无包产出），调用 2 正文——
        # 标题视为新收（heads_fresh），包首页取标题页
        carry: dict = {}
        packs1, _ = pack_text_blocks(
            self._blocks(("第三章 方法", 1)), budget=1000, carry=carry,
        )
        assert packs1 == []                       # 只有标题不产出包
        packs2, _ = pack_text_blocks(
            self._blocks(("正文丙。", 5)), budget=1000, carry=carry,
        )
        assert len(packs2) == 1
        assert packs2[0]["text"].startswith("【第三章 方法】\n")
        assert packs2[0]["page_start"] == 1       # 新收标题 → 标题页
        assert carry["heads_fresh"] is False      # 已渲染进包

    def test_long_numbered_body_packs_as_body(self):
        # review P0-1 端到端：超长编号正文不再判 heading，整体走正文
        # （修复前会被 C4 命中 → 包头截断 → 数百字符丢失）
        long_body = "1. " + "正文内容" * 30 + "。"
        blocks = self._blocks(("第一节 标题", 1), (long_body, 1))
        packs, dropped = pack_text_blocks(blocks, budget=1260)
        assert len(packs) == 1
        p = packs[0]
        assert p["heading"] == "【第一节 标题】"
        assert long_body in p["text"]             # 正文一字不丢
        assert dropped == []

    def test_oversized_single_block_not_lost(self):
        # 单块超预算也完整落包（不拆分、不丢弃）
        long_text = "很长的正文" * 30
        packs, _ = pack_text_blocks(self._blocks((long_text, 1)), budget=50)
        assert len(packs) == 1
        assert packs[0]["text"] == long_text

    def test_cross_page_pspans(self):
        # §3.2.1 走查：标题页 1 + 正文页 2 → 首 entry 0@1、正文起点 20@2
        # （包头「【岭南画派研究 / 第一节 折衷中西】\n」= 19 字符 + 换行 = 20）
        blocks = self._blocks(
            ("岭南画派研究", 1), ("第一节 折衷中西", 1),
            ("正文第一块。", 2), ("正文第二块。", 2),
        )
        packs, _ = pack_text_blocks(blocks, budget=1260)
        assert len(packs) == 1
        p = packs[0]
        assert p["page_start"] == 1
        assert p["page_end"] == 2
        assert p["pspans"] == "0@1;20@2"

    def test_same_page_no_pspans(self):
        blocks = self._blocks(("第一节 标题", 1), ("正文。", 1))
        packs, _ = pack_text_blocks(blocks, budget=1000)
        assert packs[0]["pspans"] is None
        assert packs[0]["page_end"] is None

    def test_three_page_spans(self):
        blocks = self._blocks(("正文甲。", 1), ("正文乙。", 1), ("正文丙。", 2), ("正文丁。", 3))
        packs, _ = pack_text_blocks(blocks, budget=1000)
        p = packs[0]
        # 正文丙（页 2）起点 = len("正文甲。")+1 + len("正文乙。")+1 = 10；
        # 正文丁（页 3）起点 = 10 + len("正文丙。")+1 = 15
        assert p["pspans"] == "0@1;10@2;15@3"
        assert p["page_end"] == 3

    def test_noise_dropped(self):
        blocks = self._blocks(("1", 1), ("第一节 标题", 1), ("正文。", 1), ("2", 2))
        packs, dropped = pack_text_blocks(blocks, budget=1000)
        assert len(packs) == 1
        assert "1" not in packs[0]["text"] and "2" not in packs[0]["text"]
        assert ("1", "noise") in dropped and ("2", "noise") in dropped

    def test_repeat_noise_dropped(self):
        repeat = {"岭南画派": 3}
        blocks = self._blocks(("岭南画派", 1), ("正文。", 1), ("岭南画派", 1))
        packs, dropped = pack_text_blocks(blocks, budget=1000, repeat_index=repeat)
        assert len(packs) == 1
        assert packs[0]["text"] == "正文。"
        assert dropped == [("岭南画派", "noise"), ("岭南画派", "noise")]

    def test_drop_noise_false_absorbs(self):
        # 零信息损失模式：噪声按正文吸附进包
        blocks = self._blocks(("1", 1), ("正文。", 1))
        packs, dropped = pack_text_blocks(blocks, budget=1000, drop_noise=False)
        assert len(packs) == 1
        assert packs[0]["text"] == "1\n正文。"
        assert dropped == []

    def test_empty_input(self):
        packs, dropped = pack_text_blocks([], budget=1000)
        assert packs == []
        assert dropped == []

    def test_heading_only_produces_no_pack(self):
        # 只有标题没有正文 → 不产出空包
        packs, _ = pack_text_blocks(self._blocks(("第一节 标题", 1)), budget=1000)
        assert packs == []

    def test_heading_belongs_to_next_pack(self):
        # 标题在正文前先冲刷：标题不进前一包
        blocks = self._blocks(("正文甲。", 1), ("第二节 新标题", 1), ("正文乙。", 1))
        packs, _ = pack_text_blocks(blocks, budget=1000)
        assert len(packs) == 2
        assert packs[0]["text"] == "正文甲。"
        assert packs[0]["heading"] is None
        assert packs[1]["text"] == "【第二节 新标题】\n正文乙。"

    def test_missing_char_anchors_allowed(self):
        # O2 内嵌表格分支：字符范围缺省不炸，透出 None
        blocks = [
            {"text": "正文。", "page_idx": 1},
            {"text": "下一块。", "page_idx": 1, "line_start": 5, "line_end": 6},
        ]
        packs, _ = pack_text_blocks(blocks, budget=1000)
        assert len(packs) == 1
        assert packs[0]["char_start"] is None
        assert packs[0]["char_end"] is None
        assert packs[0]["line_start"] == 5
        assert packs[0]["line_end"] == 6
