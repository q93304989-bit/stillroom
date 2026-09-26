"""提示词库导入工具：把开源提示词 JSON 转成可入库的 Markdown。

这个工具存在的理由来自一次实测：同一份 53 条的 JSON，直接丢进知识库是 513 片、
检索「赛博朋克霓虹」0 命中；先转成这里产出的 Markdown 再入库是 92 片、命中 1 条。
"""

from __future__ import annotations

import json

from tools.import_prompt_library import collect, to_markdown

SAMPLE = [
    {
        "id": "src:abc",
        "sourceId": "src",
        "title": "雨夜霓虹人像",
        "prompt": "一个穿着复古风衣的侦探站在雨夜的霓虹灯小巷中，电影剧照风格，8k分辨率。",
        # 真实体：像句子的描述（带标点）——这种要保留
        "description": "一个用于生成雨夜霓虹人像的提示词。人物位于小巷中，侧逆光勾勒轮廓，地面有雨水反射。",
        "coverUrl": "https://example.com/a.jpg",
        "referenceImageUrls": ["https://example.com/a.jpg"],
        "tags": ["摄影与照片级写实", "@somebody"],
        "author": "@somebody",
        "sourceUrl": "https://x.com/somebody/status/1",
        "createdAt": "2025-11-21",
        "imageMode": "",
        "imageModel": "gpt-image-2",
    },
    {
        "id": "src:def",
        "sourceId": "src",
        "title": "国潮中秋海报",
        "prompt": "国潮插画风格中秋海报，一轮满月悬在中式庭院上空，暖黄灯笼光晕。",
        # 这条的 description 其实是模型名——不该被当描述写进去
        "description": "Nano Banana 2",
        "tags": ["海报"],
        "author": "",
        "sourceUrl": "",
    },
]


def test_one_entry_becomes_one_section():
    """一条一段：标题做锚点、正文完整——这是检索能命中的关键。"""
    text = to_markdown(SAMPLE, title="测试库")

    assert text.startswith("# 测试库")
    assert text.count("\n## ") == 2
    assert "## 雨夜霓虹人像" in text
    assert SAMPLE[0]["prompt"] in text


def test_metadata_noise_is_dropped():
    """id / sourceId / coverUrl 这类字段对「找参考」没用，留着只稀释命中。"""
    text = to_markdown(SAMPLE, title="测试库")

    for noise in ("src:abc", "sourceId", "coverUrl", "referenceImageUrls", "gpt-image-2"):
        assert noise not in text, f"{noise} 不该出现在产出里"


def test_model_name_as_description_is_not_used():
    """`description` 有时是模型名（如 "Nano Banana 2"）——那是噪声，不是描述。"""
    text = to_markdown(SAMPLE, title="测试库")

    assert "Nano Banana 2" not in text
    # 像句子的真描述要保留
    assert "一个用于生成雨夜霓虹人像的提示词。" in text


def test_tags_keep_topics_and_drop_handles():
    """标签是检索最强的命中词，但 `@xxx` 是社交账号、不是主题词。"""
    text = to_markdown(SAMPLE, title="测试库")

    assert "摄影与照片级写实" in text
    assert "标签：摄影与照片级写实" in text          # @somebody 已被剔除


def test_source_url_is_kept_as_credit():
    """来源可追溯，但不混进正文（用引用块）。"""
    text = to_markdown(SAMPLE, title="测试库")

    assert "> 来源：" in text
    assert "https://x.com/somebody/status/1" in text


def test_entries_without_prompt_are_skipped():
    items = [{"title": "只有标题，没有提示词"}, *SAMPLE]
    text = to_markdown(items, title="测试库")

    assert text.count("\n## ") == 2
    assert "只有标题" not in text


def test_hash_inside_a_title_does_not_break_structure(tmp_path):
    """标题里带 `#` 会破坏 Markdown 层级（切片也会把它当新标题）——要剥掉。"""
    text = to_markdown([{"title": "### 带井号的标题", "prompt": "正文"}], title="测试库")

    assert "## 带井号的标题" in text
    assert "### 带井号" not in text


def test_collect_reads_a_directory_and_merges(tmp_path):
    """目录模式：把多个源的 JSON 合并成一份文档。"""
    (tmp_path / "a.json").write_text(
        json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "b.json").write_text(
        json.dumps([{"title": "另一条", "prompt": "另一段提示词"}], ensure_ascii=False),
        encoding="utf-8",
    )

    items, title = collect(tmp_path)

    assert len(items) == 3
    assert "2 个源" in title
    assert to_markdown(items, title=title).count("\n## ") == 3


def test_unknown_json_shape_gives_a_clear_error(tmp_path):
    """顶层不是数组（也不是 items/data/prompts）时，说清楚，不要静默产出空文件。"""
    bad = tmp_path / "bad.json"
    bad.write_text('{"hello": "world"}', encoding="utf-8")

    import pytest

    with pytest.raises(SystemExit) as excinfo:
        collect(bad)
    assert "顶层不是数组" in str(excinfo.value)
