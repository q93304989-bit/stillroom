"""知识库页面后端：上传 / 构建 / 删除 / 检索试跑（全部走 MockTransport，不联网）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from app.bootstrap import build_context
from app.ui.async_runner import AsyncRunner
from app.ui.knowledge_bridge import KnowledgeBridge

KB_TEXT = (
    "# 配色\n\n国潮海报的配色以红金为主，饱和度要压低，留白要足够。\n\n# 构图\n\n居中对称。"
)


def wait_until(qt_app, predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def kb(qt_app, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/v1/systemone" in str(request.url):
            body = json.loads(request.content)
            answers = {
                key: {"type": "score", "score": 3.0, "confidence": 0.9}
                for key in body["questions"]
            }
            return httpx.Response(
                200, json={"model": "jev-1.13.0", "answers": answers, "usage": {}}
            )
        return httpx.Response(404, json={"message": "no route"})

    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGNES_API_KEY=sk-test\n"
        "AGNES_BASE_URL=https://api.test/v1\n"
        "TYPESAFE_API_KEY=apikey-test\n",
        encoding="utf-8",
    )
    context = build_context(
        env_file=env_file, data_dir=tmp_path / "data", transport=httpx.MockTransport(handler)
    )
    runner = AsyncRunner()
    bridge = KnowledgeBridge(context, runner)
    notices: list[tuple[str, str]] = []
    bridge.noticeRaised.connect(lambda level, text: notices.append((level, text)))
    yield bridge, context, runner, tmp_path, notices
    runner.close()
    context.history.close()


def make_file(tmp_path, name: str = "国潮风格说明.md", text: str = KB_TEXT) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- 上传与列表

def test_add_builds_and_lists_the_document(kb, qt_app):
    """导入即构建：用户上传就是为了能搜到，不该再多一步。"""
    bridge, _, _, tmp_path, _ = kb

    bridge.addFiles([make_file(tmp_path)])
    assert wait_until(qt_app, lambda: not bridge.busy and len(bridge.documents) == 1)

    doc = bridge.documents[0]
    assert doc["name"] == "国潮风格说明.md"
    assert doc["kindLabel"] == "Markdown"
    assert doc["state"] == "ready"
    assert doc["chunkCount"] >= 1
    assert doc["sizeText"]
    assert doc["builtText"]


def test_rebuild_after_the_file_changes(kb, qt_app):
    """重建：按当前切片规则重来一遍（原文件留着就是为了这个）。"""
    bridge, _, _, tmp_path, _ = kb
    bridge.addFiles([make_file(tmp_path)])
    assert wait_until(qt_app, lambda: not bridge.busy and len(bridge.documents) == 1)
    doc = bridge.documents[0]
    stored = bridge.documentPath(doc["id"])

    Path(stored).write_text("# 改过的说明\n\n灯笼元素要亮一点，写长一点好成为独立一片。", encoding="utf-8")
    bridge.build(doc["id"])
    assert wait_until(qt_app, lambda: not bridge.busy)

    bridge.testSearch("灯笼")
    assert wait_until(qt_app, lambda: not bridge.busy and bool(bridge.results))
    assert "灯笼" in bridge.results[0]["text"]


def test_unsupported_file_is_reported_without_crashing(kb, qt_app):
    bridge, _, _, tmp_path, notices = kb
    bad = tmp_path / "报告.docx"
    bad.write_text("x", encoding="utf-8")

    bridge.addFiles([str(bad)])
    assert wait_until(qt_app, lambda: bool(notices))

    assert bridge.documents == []
    level, text = notices[0]
    assert level == "warn" and "txt" in text


def test_failed_build_keeps_the_reason(kb, qt_app):
    bridge, _, _, tmp_path, _ = kb
    broken = tmp_path / "扫描件.pdf"
    broken.write_bytes("%PDF-1.4\n假的 PDF".encode("utf-8"))

    bridge.addFiles([str(broken)])
    assert wait_until(qt_app, lambda: not bridge.busy and len(bridge.documents) == 1)

    doc = bridge.documents[0]
    assert doc["state"] == "failed"
    assert "PDF" in doc["error"]


def test_remove_drops_the_document_and_the_file(kb, qt_app):
    bridge, _, _, tmp_path, notices = kb
    bridge.addFiles([make_file(tmp_path)])
    assert wait_until(qt_app, lambda: len(bridge.documents) == 1)
    doc = bridge.documents[0]
    stored = bridge.documentPath(doc["id"])

    bridge.remove(doc["id"], True)
    assert wait_until(qt_app, lambda: bridge.documents == [])

    # 删文件可能被索引/杀毒短暂占用（Windows 上偶发），桥里重试几次；
    # 真没删掉时必须留下 warn 提示，而不是让用户以为磁盘已经干净了。
    deleted = wait_until(qt_app, lambda: not Path(stored).exists())
    assert deleted, f"原文件没删掉，界面提示：{notices}"
    assert not [level for level, _ in notices if level == "warn"]


# --------------------------------------------------------------------------- 检索试跑

def test_search_returns_hits_with_sources(kb, qt_app):
    bridge, _, _, tmp_path, _ = kb
    bridge.addFiles([make_file(tmp_path)])
    assert wait_until(qt_app, lambda: not bridge.busy and len(bridge.documents) == 1)

    bridge.testSearch("国潮 海报 配色")
    assert wait_until(qt_app, lambda: not bridge.busy and bool(bridge.results))

    hit = bridge.results[0]
    assert hit["name"] == "国潮风格说明.md"
    assert hit["ordinal"] == 0
    assert "国潮海报" in hit["text"]
    assert hit["sourceLabel"] == "国潮风格说明.md · 第 1 片"
    assert bridge.resultReason == ""


def test_search_on_empty_library_explains_why(kb, qt_app):
    bridge, _, _, _, _ = kb

    bridge.testSearch("国潮 海报")
    assert wait_until(qt_app, lambda: not bridge.busy)

    assert bridge.results == []
    assert "知识库" in bridge.resultReason


def test_search_without_a_query_warns(kb, qt_app):
    bridge, _, _, _, notices = kb

    bridge.testSearch("   ")
    assert wait_until(qt_app, lambda: bool(notices))

    assert notices[0][0] == "warn"
    assert bridge.results == []


def test_local_path_converts_file_urls(kb):
    """文件框给的是 file:// URL，Windows 盘符也要能转成本地路径。"""
    bridge, _, _, tmp_path, _ = kb
    target = tmp_path / "带 空格的 文件.md"

    assert bridge.localPath(target.as_uri()) == str(target)
    assert bridge.localPath(str(target)) == str(target)      # 已经是路径就原样返回
