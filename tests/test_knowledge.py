"""知识库：上传 → 解析 → 切片 → 入库 → 检索（关键词召回 + Jev 重排 + 降级）。

切片规则来自方案 5.2 节：按空行 / 标题 / 代码块分段 → 长段按句边界切、片间重叠 100 字
→ 短段与相邻段合并 → 每片记 ordinal。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.clients.typesafe_client import TypeSafeClient
from app.config.credentials import TypeSafeCredentials
from app.net.http import HttpClient
from app.services.knowledge import (
    CHUNK_MAX_CHARS,
    CHUNK_OVERLAP,
    KnowledgeError,
    KnowledgeStore,
    slice_text,
)

CREDS = TypeSafeCredentials(api_key="apikey-ts", model="jev-latest")

PARA_ONE = "国潮海报的配色以红金为主，饱和度要压低，留白要足够。"
PARA_TWO = "构图居中对称，主体放在画面中央偏上，四周留白均衡。"
LONG_PARA = "国潮风格讲究对称与留白，红金配色要克制。" * 60      # 1200 字，用来验长段切分


def make_store(tmp_path, judge=None) -> KnowledgeStore:
    return KnowledgeStore(tmp_path / "history.db", root=tmp_path / "knowledge", judge=judge)


def write_doc(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def judge_preferring(needle: str) -> TypeSafeClient:
    """造一个「哪片里有 needle 就给高分」的假 Jev。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        candidates = body.get("state", {}).get("candidates", [])
        answers = {
            key: {
                "type": "score",
                "score": 3.0 if needle in candidates[index].get("text", "") else 0.0,
                "confidence": 0.9,
                "probabilities": {"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.4},
            }
            for index, key in enumerate(body["questions"])
        }
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": answers, "usage": {}})

    return TypeSafeClient(HttpClient(transport=httpx.MockTransport(handler), backoff=0), CREDS)


def failing_judge() -> TypeSafeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "judge down"})

    return TypeSafeClient(HttpClient(transport=httpx.MockTransport(handler), backoff=0), CREDS)


# --------------------------------------------------------------------------- 切片

def test_blank_line_separates_paragraphs():
    chunks = slice_text(f"{PARA_ONE}\n\n{PARA_TWO}")

    assert len(chunks) == 2
    assert chunks[0].startswith("国潮海报的配色")
    assert chunks[1].startswith("构图居中对称")


def test_short_paragraph_merges_into_the_next():
    chunks = slice_text(f"短句一段\n\n{PARA_ONE}")

    assert len(chunks) == 1
    assert "短句一段" in chunks[0] and "国潮海报" in chunks[0]


def test_long_paragraph_splits_on_sentence_boundaries_with_overlap():
    chunks = slice_text(LONG_PARA)

    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_MAX_CHARS for chunk in chunks)
    for previous, following in zip(chunks, chunks[1:]):
        assert following.startswith(previous[-CHUNK_OVERLAP:]), "相邻两片没有重叠"
    assert "".join(chunks).count("国潮风格") >= LONG_PARA.count("国潮风格")   # 内容没丢


def test_fenced_code_block_is_kept_whole():
    text = (
        "用法如下：\n\n```\n第一行\n\n第二行\n\n第三行\n```\n\n"
        "这就是全部用法，写长一点免得被合并掉这段说明。"
    )
    chunks = slice_text(text)
    block = next(chunk for chunk in chunks if "```" in chunk)

    assert "第一行" in block and "第三行" in block, "代码块被从中间切开了"


def test_markdown_heading_goes_with_its_section():
    text = f"# 一、配色\n\n{PARA_ONE}\n\n# 二、构图\n\n{PARA_TWO}"
    chunks = slice_text(text)

    assert len(chunks) == 2
    # 契约在 2026-09-25 调整过一次：**分段语义不变**（标题仍与它下面那节同片，
    # 所以仍是 2 片、各自含自己的正文），但**标题行不再留在片里**。
    # 起因：「别人收集的提示词」这类资料天然是 `## 1. xxx` + 正文，
    # 整片塞进提示词会变成「可参考的风格线索：## 1. xxx 一个穿风衣的侦探…」——
    # 模型会把 `##` 当正文读。而「这片出自哪」已由 origin（《文件名》第 N 片）交代。
    assert PARA_ONE in chunks[0] and PARA_TWO in chunks[1]
    assert not chunks[0].startswith("#"), "标题行不该混进片段（会污染提示词）"
    assert not chunks[1].startswith("#")


def test_heading_only_chunk_is_not_emptied():
    """整片只有标题、没有正文时，宁可原样保留，也不交出一片空白。

    空白片段会污染检索（召回一条什么都看不到的线索），比留着标题更糟。
    """
    chunks = slice_text("# 只有一个标题")
    assert chunks == ["# 只有一个标题"]


def test_hash_inside_body_is_not_treated_as_heading():
    """正文中间出现 # 不是标题，不该被动。"""
    text = "这一段正文里提到 c# 语言，也有个 # 号，但都不在行首。"
    chunks = slice_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_multi_level_headings_are_all_stripped():
    text = f"# 一级\n## 二级\n### 三级\n\n{PARA_ONE}"
    chunks = slice_text(text)
    assert len(chunks) == 1
    assert chunks[0] == PARA_ONE

def test_tiny_document_still_yields_one_chunk():
    assert slice_text("就一句话。") == ["就一句话。"]


# --------------------------------------------------------------------------- 入库

def test_add_copies_the_file_and_records_sha256(tmp_path):
    store = make_store(tmp_path)
    try:
        origin = Path(write_doc(tmp_path, "国潮说明.md", f"# 配色\n\n{PARA_ONE}"))
        doc = store.add(origin)

        assert doc.name == "国潮说明.md"
        assert doc.kind == "md"
        assert doc.state == "pending"
        copied = Path(doc.path)
        assert copied.is_file() and copied.parent.name == "knowledge"
        assert doc.sha256 == hashlib.sha256(origin.read_bytes()).hexdigest()
        assert [item.id for item in store.documents()] == [doc.id]
    finally:
        store.close()


def test_duplicate_upload_is_recognized_by_sha256(tmp_path):
    store = make_store(tmp_path)
    try:
        first = store.add(write_doc(tmp_path, "a.txt", PARA_ONE))
        again = store.add(write_doc(tmp_path, "b.txt", PARA_ONE))

        assert again.id == first.id, "同一份内容不该入库两次"
        assert len(store.documents()) == 1
    finally:
        store.close()


def test_unsupported_suffix_is_refused_with_a_clear_message(tmp_path):
    store = make_store(tmp_path)
    try:
        with pytest.raises(KnowledgeError) as info:
            store.add(write_doc(tmp_path, "报告.docx", "x"))

        message = str(info.value)
        assert "txt" in message and "md" in message, "要把支持的类型说清楚"
    finally:
        store.close()


def test_build_slices_and_marks_ready(tmp_path):
    store = make_store(tmp_path)
    try:
        doc = store.add(write_doc(tmp_path, "说明书.md", f"# 配色\n\n{PARA_ONE}\n\n# 构图\n\n{PARA_TWO}"))
        built = store.build(doc.id)

        assert built.state == "ready"
        assert built.chunk_count == 2
        assert built.built_at is not None
        rows = store.chunks(doc.id)
        assert [row.ordinal for row in rows] == [0, 1]
        assert PARA_ONE in rows[0].text
    finally:
        store.close()


def test_build_reports_missing_file_as_failed(tmp_path):
    store = make_store(tmp_path)
    try:
        doc = store.add(write_doc(tmp_path, "会丢的文件.txt", PARA_ONE))
        Path(doc.path).unlink()

        built = store.build(doc.id)

        assert built.state == "failed"
        assert built.error, "失败要说清理由，不能空着"
        assert store.chunks(doc.id) == []
    finally:
        store.close()


def test_pdf_without_parser_gives_a_clear_error(tmp_path):
    """没装 PDF 解析组件时要说「提不出文字」，而不是入库成功却搜不到。"""
    store = make_store(tmp_path)
    try:
        doc = store.add(write_doc(tmp_path, "扫描件.pdf", "%PDF-1.4\n假的 PDF 内容"))

        built = store.build(doc.id)

        assert built.state == "failed"
        assert "PDF" in built.error
    finally:
        store.close()


def test_rebuild_replaces_old_chunks(tmp_path):
    store = make_store(tmp_path)
    try:
        doc = store.add(write_doc(tmp_path, "手册.txt", PARA_ONE))
        store.build(doc.id)
        Path(doc.path).write_text(PARA_TWO, encoding="utf-8")

        rebuilt = store.rebuild(doc.id)

        assert rebuilt.state == "ready"
        rows = store.chunks(doc.id)
        assert len(rows) == 1 and PARA_TWO in rows[0].text
    finally:
        store.close()


def test_remove_drops_document_chunks_and_file(tmp_path):
    store = make_store(tmp_path)
    try:
        doc = store.add(write_doc(tmp_path, "临时.txt", PARA_ONE))
        store.build(doc.id)

        assert store.remove(doc.id) is True
        assert store.documents() == []
        assert store.chunks(doc.id) == []
        assert not Path(doc.path).exists()
    finally:
        store.close()


# --------------------------------------------------------------------------- 检索

async def test_search_returns_fragments_and_sources(tmp_path):
    store = make_store(tmp_path)
    try:
        doc = store.add(write_doc(tmp_path, "国潮风格说明.md", f"# 配色\n\n{PARA_ONE}\n\n# 构图\n\n{PARA_TWO}"))
        store.build(doc.id)

        found = await store.search("国潮 海报 配色", top_k=3)

        assert found.hits, "关键词命中却什么都没返回"
        assert found.hits[0].doc_name == "国潮风格说明.md"
        assert found.hits[0].ordinal == 0
        assert found.reranked is False
        assert "判断" in found.reason
        assert found.fragments() == [hit.text for hit in found.hits]
        assert found.sources()[0]["name"] == "国潮风格说明.md"
    finally:
        store.close()


async def test_search_on_empty_library_explains_why(tmp_path):
    store = make_store(tmp_path)
    try:
        found = await store.search("国潮 海报")

        assert found.hits == []
        assert "知识库" in found.reason
    finally:
        store.close()


async def test_search_reranks_with_judge(tmp_path):
    store = make_store(tmp_path, judge=judge_preferring("灯笼"))
    try:
        doc = store.add(write_doc(
            tmp_path, "两片.md",
            f"# 甲\n\n{PARA_ONE}\n\n# 乙\n\n国潮海报的灯笼元素说明，写长一点好成为独立一片。",
        ))
        store.build(doc.id)

        found = await store.search("国潮 海报", top_k=2)

        assert found.reranked is True
        assert "灯笼" in found.hits[0].text, "判断模型偏好的那片没有排到第一"
    finally:
        store.close()


async def test_search_falls_back_when_judge_is_unavailable(tmp_path):
    store = make_store(tmp_path, judge=failing_judge())
    try:
        doc = store.add(write_doc(tmp_path, "国潮风格说明.md", PARA_ONE))
        store.build(doc.id)

        found = await store.search("国潮 配色")

        assert found.hits, "判断挂了就什么都搜不到，是把降级做丢了"
        assert found.reranked is False
        assert "判断" in found.reason
    finally:
        store.close()


def test_remove_retries_transient_file_lock(tmp_path, monkeypatch):
    """Windows 上「刚读完就删」偶尔会被瞬时占用：退一步重试，别在磁盘上留孤儿文件。"""
    target = tmp_path / "国潮风格说明.md"
    target.write_text(PARA_ONE, encoding="utf-8")
    real = Path.unlink
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("文件正被另一个进程使用")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)
    assert KnowledgeStore.delete_file(target) == ""

    assert calls["n"] == 2                      # 第一次失败、第二次成功
    assert not target.exists()


def test_delete_file_reports_why_it_failed(tmp_path, monkeypatch):
    """真删不掉时要把原因交出去——库里那条已经删了，孤儿文件不能没人知道。"""
    target = tmp_path / "锁死的.md"
    target.write_text(PARA_ONE, encoding="utf-8")

    def always_locked(self, *args, **kwargs):
        raise PermissionError("文件正被另一个进程使用")

    monkeypatch.setattr(Path, "unlink", always_locked)
    reason = KnowledgeStore.delete_file(target, attempts=2)

    assert "PermissionError" in reason
    assert "文件正被另一个进程使用" in reason
