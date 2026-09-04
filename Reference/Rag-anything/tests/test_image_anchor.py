# -*- coding: utf-8 -*-
"""§image-refs P0-6：图片版面主题锚点提取纯函数单测
（docs/image-reference-accuracy-fix-plan.md E 方案 改动 1）。

锚点规则优先级：cap/foot（MinerU 图注）→ col（x 重叠列 + 图下方最近邻
短文本）→ heading（仅本页标题，不跨页回溯）。
bbox 约定：MinerU content_list 的 0-1000 归一化坐标 [x0, y0, x1, y1]。
"""

import pytest

from raganything.modalprocessors import _extract_image_anchor


def _img(page_idx, bbox, caption=None, footnote=None):
    """构造 MinerU image item（MinerU 顶层 item 无 item_info 键，
    模拟 _process_one 产物 item_info={}, original=完整 item）。"""
    item = {"type": "image", "page_idx": page_idx, "bbox": bbox}
    if caption:
        item["img_caption"] = caption
    if footnote:
        item["img_footnote"] = footnote
    return {"index": 0, "item_info": {}, "original": item}


def _text(page_idx, bbox, text, level=0):
    return {"type": "text", "page_idx": page_idx, "bbox": bbox, "text": text,
            "text_level": level}


# ── 四屏案例（真实数据精简版）：page 6 左列 4 屏大图、中列小图及其图注、
#    右列四屏标题。左列图与中列图注 x 不重叠，必须走 heading。
def _siping_content_list():
    return [
        _img(6, [29, 54, 133, 564], caption=["2"]),       # 四屏之一（编号噪声）
        _img(6, [545, 413, 637, 612]),                    # 中列小图（下方有图注）
        _text(6, [545, 620, 726, 660], "高剑父：《鹰》，《时事画报》1907 年第 23 期，封面"),
        _text(6, [836, 64, 900, 96], "高剑父花瓜鱼蟹四屏", level=2),
        _text(6, [836, 100, 970, 300], "绢本设色99 cm×27.8 cm×41905 年香港艺术馆藏"),
    ]


class TestCapFootRule:
    def test_footnote_wins(self):
        cl = [_img(6, [643, 636, 726, 740],
                   footnote=["高剑父：《苗松》，《时事画报》1907年第4期，第 15 页"])]
        anchor, src = _extract_image_anchor(cl, cl[0])
        assert src == "foot"
        assert "苗松" in anchor

    def test_numeric_caption_filtered(self):
        # 图内编号噪声（实测会劫持四屏第一屏）：应跳过 cap 走后续规则
        cl = _siping_content_list()
        item = cl[0]
        anchor, src = _extract_image_anchor(cl, item)
        assert src != "cap"
        assert src == "heading"
        assert "四屏" in anchor

    @pytest.mark.parametrize("noise", [["2"], ["1-1"], ["3-1"], ["—"]])
    def test_short_numeric_patterns(self, noise):
        cl = [_img(3, [29, 54, 131, 625], caption=noise),
              _text(3, [836, 64, 900, 96], "高剑父花卉四屏", level=2)]
        anchor, src = _extract_image_anchor(cl, cl[0])
        assert src == "heading"

    def test_short_caption_filtered(self):
        cl = [_img(3, [29, 54, 131, 625], caption=["花"]),
              _text(3, [836, 64, 900, 96], "高剑父花卉四屏", level=2)]
        anchor, src = _extract_image_anchor(cl, cl[0])
        assert src == "heading"

    def test_caption_list_takes_first_nonempty(self):
        cl = [_img(2, [86, 159, 395, 790], caption=["", "缺高清图/香港艺术馆/7.17雅昌借小图"])]
        anchor, src = _extract_image_anchor(cl, cl[0])
        assert src == "cap"
        assert "香港艺术馆" in anchor


class TestColRule:
    """规则②：x 重叠列过滤 + 图下方 y 最近邻（用户评审补充的 y 约束）。"""

    def test_x_overlap_below_nearest(self):
        # 纵向排版（A4 论文）：全页文本 x 都与图重叠 → y 约束区分：
        # 只取图下方且 y 距离最小者，不取上方正文
        cl = [
            _img(4, [100, 400, 900, 600]),
            _text(4, [100, 100, 900, 150], "1 引言", level=1),
            _text(4, [100, 200, 900, 380], "这里是图片上方的正文段落，介绍背景内容。"),
            _text(4, [100, 620, 900, 660], "图 1 系统架构示意"),
            _text(4, [100, 680, 900, 900], "这里是图片下方的正文段落，继续讨论细节内容。"),
        ]
        anchor, src = _extract_image_anchor(cl, cl[0])
        assert src == "col"
        assert anchor == "图 1 系统架构示意"

    def test_x_no_overlap_excluded(self):
        # 四屏案例：左列图 → 中列图注（x 545-726）不重叠 → 不命中 col
        cl = _siping_content_list()
        item = cl[0]
        anchor, src = _extract_image_anchor(cl, item)
        assert src != "col"
        assert src == "heading"

    def test_long_paragraph_skipped(self):
        # 图下方最近的若是长正文段落（>160 字符），跳过交给 heading
        long_text = "图下方是一个很长的正文段落。" + "内容" * 100
        assert len(long_text) > 160
        cl = [
            _img(4, [100, 400, 900, 600]),
            _text(4, [100, 620, 900, 900], long_text),
            _text(4, [100, 100, 900, 150], "2 相关研究", level=2),
        ]
        anchor, src = _extract_image_anchor(cl, cl[0])
        assert src == "heading"
        assert "相关研究" in anchor

    def test_y_tolerance_overlap(self):
        # 图注与图框轻微重叠（gap 负值在容忍内）仍可命中
        cl = [
            _img(4, [100, 400, 900, 600]),
            _text(4, [100, 585, 900, 620], "图注紧贴图框"),
        ]
        anchor, src = _extract_image_anchor(cl, cl[0])
        assert src == "col"


class TestHeadingRule:
    def test_heading_below_image_top_level(self):
        # 标题与图顶同水平（画册常见，四屏案例实测）：标题起点不晚于
        # 图顶 + 容忍 → 视为图上方
        cl = _siping_content_list()
        item = cl[0]
        anchor, src = _extract_image_anchor(cl, item)
        assert src == "heading"
        assert anchor == "高剑父花瓜鱼蟹四屏"

    def test_page_top_heading_fallback(self):
        # 本页无图上方标题时取页面最靠上的标题（本页主题代理）
        cl = [
            _img(7, [112, 500, 392, 800]),
            _text(7, [836, 64, 900, 96], "高剑父蟹爪水仙写生", level=2),
        ]
        anchor, src = _extract_image_anchor(cl, cl[0])
        assert src == "heading"
        assert "蟹爪水仙" in anchor

    def test_no_cross_page_backtrack(self):
        # 本页无标题 → 无锚点（不做跨页回溯：实测画册页页主题独立，
        # 回溯页标题 14 例全错配，宁缺毋滥）
        cl = [
            _img(5, [30, 55, 117, 220]),
            _text(5, [545, 100, 726, 200], "时报图注"),
            _text(4, [836, 900, 970, 946], "陈树人 蔬果图册", level=2),
        ]
        anchor, src = _extract_image_anchor(cl, cl[0])
        assert src == ""


class TestEdgeCases:
    def test_empty_content_list(self):
        assert _extract_image_anchor(None, {}) == ("", "")
        assert _extract_image_anchor([], None) == ("", "")

    def test_missing_bbox_or_page(self):
        cl = [_img(6, None)]
        assert _extract_image_anchor(cl, cl[0]) == ("", "")
        cl2 = [{"type": "image", "bbox": [1, 2, 3, 4]}]  # 无 page_idx
        assert _extract_image_anchor(cl2, cl2[0]) == ("", "")

    def test_anchor_length_capped(self):
        long_caption = "很长的图注文本" * 30
        assert len(long_caption) > 80
        cl = [_img(2, [86, 159, 395, 790], caption=[long_caption])]
        anchor, src = _extract_image_anchor(cl, cl[0])
        assert src == "cap"
        assert len(anchor) <= 81  # 80 + 省略号
        assert anchor.endswith("…")

    def test_item_info_missing_uses_original(self):
        # MinerU 链路 item_info={}，全部信息在 original
        cl = [_img(6, [29, 54, 133, 564])]
        assert _extract_image_anchor(cl, {"index": 0, "original": cl[0]["original"]}) == ("", "")

    def test_whitespace_collapsed(self):
        cl = [_img(2, [86, 159, 395, 790], caption=["  多行\n图注  文本  "])]
        anchor, src = _extract_image_anchor(cl, cl[0])
        assert src == "cap"
        assert anchor == "多行 图注 文本"


class TestE2TopLevelItem:
    """§image-refs E2：ImageModalProcessor 侧 item_info 即 MinerU 顶层
    item（F0-1 直传，无 item_info/original 包装），函数需直接使用。"""

    def test_top_level_item_cap_direct(self):
        item = {"type": "image", "page_idx": 6, "bbox": [29, 54, 133, 564],
                "img_caption": ["高剑父 花瓜鱼蟹四屏（一）"]}
        anchor, src = _extract_image_anchor([item], item)
        assert src == "cap"
        assert "四屏" in anchor

    def test_top_level_item_heading_fallback(self):
        item = {"type": "image", "page_idx": 6, "bbox": [29, 54, 133, 564]}
        cl = [item, _text(6, [29, 30, 970, 50], "高剑父花瓜鱼蟹四屏", level=2)]
        anchor, src = _extract_image_anchor(cl, item)
        assert src == "heading"
        assert anchor == "高剑父花瓜鱼蟹四屏"


class TestE2ContextPriority:
    """§image-refs E2：上下文本页优先重排 + window 收窄（有锚点 window=0）。"""

    def _extractor(self, window=1):
        from raganything.modalprocessors import ContextConfig, ContextExtractor
        return ContextExtractor(ContextConfig(
            context_window=window, context_mode="page"))

    def test_current_page_comes_first(self):
        cl = [
            _text(4, [10, 10, 90, 30], "前一页内容", level=2),
            _text(5, [10, 10, 90, 30], "本页内容", level=0),
            _text(6, [10, 10, 90, 30], "后一页内容", level=2),
        ]
        ctx = self._extractor().extract_context(
            cl, {"page_idx": 5}, content_format="minerU")
        assert ctx.index("本页内容") < ctx.index("前一页内容")
        assert ctx.index("本页内容") < ctx.index("后一页内容")
        # 跨页文本带 [Page N] 前缀；text_level>0 带 # 层级前缀
        assert "[Page 4] ## 前一页内容" in ctx

    def test_window_zero_current_page_only(self):
        cl = [
            _text(4, [10, 10, 90, 30], "前一页内容", level=0),
            _text(5, [10, 10, 90, 30], "本页内容", level=0),
            _text(6, [10, 10, 90, 30], "后一页内容", level=0),
        ]
        ctx = self._extractor().extract_context(
            cl, {"page_idx": 5}, content_format="minerU", window=0)
        assert "本页内容" in ctx
        assert "前一页内容" not in ctx
        assert "后一页内容" not in ctx
