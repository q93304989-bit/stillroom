"""历史模型：后台加载、缩略图补齐、筛选搜索、删除与清空。"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.bootstrap import build_context
from app.services.history import Record
from app.ui.async_runner import AsyncRunner
from app.ui.models.history_model import HistoryModel


def make_png(path: Path, size=(900, 600), color=(30, 120, 200)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


@pytest.fixture
def model(qt_app, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("AGNES_API_KEY=sk-test\nAGNES_BASE_URL=https://api.test/v1\n", encoding="utf-8")
    context = build_context(env_file=env_file, data_dir=tmp_path / "data", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    runner = AsyncRunner()

    media_dir = context.media.media_dir
    for index in range(3):
        source = make_png(media_dir / f"seed{index}.png", color=(30 + index * 40, 120, 200))
        context.history.add(
            Record(
                kind="image",
                prompt=f"第 {index} 张图",
                media_path=str(source),
                params={"size": "1024x768", "model": "agnes-image-2.5-flash"},
                duration=2.5 + index,
            )
        )
    context.history.add(
        Record(kind="video", prompt="一段海面日落", params={"seconds": "8", "model": "agnes-video-2.5-flash"})
    )

    instance = HistoryModel(context, runner, prefetch_limit=10)
    yield instance, context, runner

    runner.close()
    context.history.close()


def wait_until(qt_app, predicate, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_model_loads_records_off_the_ui_thread(model, qt_app):
    history_model, _, _ = model
    history_model.reload()

    assert wait_until(qt_app, lambda: history_model.count == 4)
    assert history_model.loading is False

    index = history_model.index(0, 0)
    roles = history_model.roleNames()
    assert roles[history_model.PromptRole] == b"prompt"
    assert history_model.recordIdAt(0)


def test_thumbnails_are_filled_in_background(model, qt_app):
    history_model, _, _ = model
    history_model.reload()
    assert wait_until(qt_app, lambda: history_model.count == 4)

    # 初始可能还没有缩略图，后台补完后对应行会被刷新
    assert wait_until(
        qt_app,
        lambda: all(
            history_model.data(history_model.index(row, 0), history_model.ThumbRole)
            for row in range(history_model.count)
            if "图" in history_model.data(history_model.index(row, 0), history_model.PromptRole)
        ),
        timeout=10,
    )

    # 视频目前没有封面图（不做抽帧），所以找第一条有缩略图的记录来断言
    thumb = ""
    for row in range(history_model.count):
        value = history_model.data(history_model.index(row, 0), history_model.ThumbRole)
        if value:
            thumb = value
            break
    assert thumb.startswith("file:///")
    assert "?v=" in thumb                      # 带版本号，换图后不会显示旧缓存


def test_filter_by_kind(model, qt_app):
    history_model, _, _ = model
    history_model.reload("video", "")
    assert wait_until(qt_app, lambda: history_model.count == 1)
    assert history_model.data(history_model.index(0, 0), history_model.KindRole) == "video"


def test_keyword_search(model, qt_app):
    history_model, _, _ = model
    history_model.reload("all", "海面")
    assert wait_until(qt_app, lambda: history_model.count == 1)
    assert "海面" in history_model.data(history_model.index(0, 0), history_model.PromptRole)


def test_reload_twice_keeps_only_latest(model, qt_app):
    history_model, _, _ = model
    history_model.reload("all", "")
    history_model.reload("video", "")
    assert wait_until(qt_app, lambda: history_model.count == 1)
    time.sleep(0.2)
    qt_app.processEvents()
    assert history_model.count == 1            # 早先那批结果不会覆盖后来的筛选


def test_record_at_and_summary(model, qt_app):
    history_model, _, _ = model
    history_model.reload()
    assert wait_until(qt_app, lambda: history_model.count == 4)

    record_id = history_model.recordIdAt(0)
    record = history_model.recordAt(record_id)
    assert record["id"] == record_id
    assert record["prompt"]
    summary = history_model.data(history_model.index(0, 0), history_model.ParamsRole)
    assert summary                                # 参数摘要非空


def test_remove_and_clear(model, qt_app):
    history_model, context, _ = model
    history_model.reload()
    assert wait_until(qt_app, lambda: history_model.count == 4)

    record_id = history_model.recordIdAt(0)
    media_path = history_model.recordAt(record_id).get("media_path")
    assert history_model.remove(record_id) is True
    assert wait_until(qt_app, lambda: history_model.count == 3)
    if media_path:
        assert not Path(media_path).exists()      # 连同缓存文件一起删

    removed = history_model.clearAll()
    assert removed == 3
    assert wait_until(qt_app, lambda: history_model.count == 0)


def test_resize_does_not_regenerate_thumbnails(model, qt_app, tmp_path):
    """退出标准之一：缩放界面不能触发重新解码。

    缩略图按「记录 id」缓存，与展示尺寸无关——这正是旧版的反面（旧版把列宽写进缓存键，
    一改窗口宽度就整屏重解码）。
    """
    history_model, context, _ = model
    history_model.reload()
    assert wait_until(qt_app, lambda: history_model.count == 4)
    assert wait_until(
        qt_app,
        lambda: any(
            history_model.data(history_model.index(row, 0), history_model.ThumbRole)
            for row in range(history_model.count)
        ),
    )

    thumbs = sorted((tmp_path / "data" / "thumbs").glob("*"))
    before = {path.name: path.stat().st_mtime for path in thumbs}

    # 模拟界面缩放：换列宽、换视图，再读一遍缩略图 URL
    for width in (220, 260, 190):
        history_model.reload("all", "", 500)
        wait_until(qt_app, lambda: history_model.count == 4)
        for row in range(history_model.count):
            history_model.data(history_model.index(row, 0), history_model.ThumbRole)
        _ = width

    after = {path.name: path.stat().st_mtime for path in sorted((tmp_path / "data" / "thumbs").glob("*"))}
    assert after == before, "重新布局不应重新生成缩略图"


# --------------------------------------------------------------------------- 评价信号

def test_model_favorite_updates_row_and_publishes_event(model, qt_app):
    history_model, context, _ = model
    history_model.reload()
    assert wait_until(qt_app, lambda: history_model.count == 4)

    record_id = history_model.recordIdAt(0)
    events: list = []
    context.bus.subscribe(lambda event: events.append(event))

    assert history_model.setFavorite(record_id, True) is True

    row = history_model.index(0, 0)
    assert history_model.data(row, history_model.FavoriteRole) is True
    assert context.history.get(record_id).last_action == "accept"   # 收藏即认可
    assert [event.type for event in events] == ["feedback.recorded"]
    assert events[0].get("reason") == "favorite"


def test_model_tags_add_and_remove(model, qt_app):
    history_model, context, _ = model
    history_model.reload()
    assert wait_until(qt_app, lambda: history_model.count == 4)
    record_id = history_model.recordIdAt(0)

    assert history_model.addTag(record_id, "中秋") is True
    assert history_model.addTag(record_id, "中秋") is True      # 重复标签会被去掉
    assert history_model.addTag(record_id, "竖版") is True
    row = history_model.index(0, 0)
    assert history_model.data(row, history_model.TagsRole) == ["中秋", "竖版"]

    assert history_model.removeTag(record_id, "中秋") is True
    assert history_model.data(row, history_model.TagsRole) == ["竖版"]
    assert context.history.get(record_id).tags == ["竖版"]


def test_model_mark_action_and_record_at(model, qt_app):
    history_model, context, _ = model
    history_model.reload()
    assert wait_until(qt_app, lambda: history_model.count == 4)
    record_id = history_model.recordIdAt(0)

    assert history_model.markAction(record_id, "retry") is True
    assert context.history.get(record_id).last_action == "retry"
    assert history_model.recordAt(record_id)["last_action"] == "retry"


def test_model_feedback_on_missing_record_is_safe(model, qt_app):
    history_model, _, _ = model
    history_model.reload()
    assert wait_until(qt_app, lambda: history_model.count == 4)

    assert history_model.setFavorite("不存在", True) is False
    assert history_model.addTag("不存在", "x") is False
    assert history_model.markAction("不存在", "accept") is False
