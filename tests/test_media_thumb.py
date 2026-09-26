"""缩略图服务：生成、复用、失效重算、只删数据目录内文件。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from PIL import Image

from app.services.media import MediaStore


def make_png(path: Path, size=(1600, 1200), color=(40, 90, 200)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def test_thumbnail_is_generated_and_cached(tmp_path):
    store = MediaStore(tmp_path / "data")
    source = make_png(tmp_path / "src" / "a.png")

    first = store.thumbnail_for("rec1", source)
    assert first is not None and first.is_file()

    with Image.open(first) as thumb:
        assert max(thumb.size) <= 320
        assert thumb.size == (320, 240)          # 保持宽高比

    second = store.thumbnail_for("rec1", source)
    assert second == first                        # 命中缓存，不重复生成


def test_thumbnail_regenerates_when_source_is_newer(tmp_path):
    store = MediaStore(tmp_path / "data")
    source = make_png(tmp_path / "src" / "a.png")

    thumb = store.thumbnail_for("rec1", source)
    assert thumb is not None
    before = thumb.stat().st_mtime

    time.sleep(0.01)
    source.touch()
    os.utime(source, (time.time() + 5, time.time() + 5))

    again = store.thumbnail_for("rec1", source)
    assert again == thumb
    assert again.stat().st_mtime > before


def test_missing_source_returns_none(tmp_path):
    store = MediaStore(tmp_path / "data")
    assert store.thumbnail_for("nope", tmp_path / "missing.png") is None
    assert store.existing_thumb("nope") is None


def test_alpha_image_keeps_png(tmp_path):
    store = MediaStore(tmp_path / "data")
    source = tmp_path / "src" / "alpha.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (600, 600), (255, 0, 0, 128)).save(source)

    thumb = store.thumbnail_for("rec2", source)
    assert thumb is not None and thumb.suffix == ".png"


def test_existing_thumb_prefers_generated_file(tmp_path):
    store = MediaStore(tmp_path / "data")
    source = make_png(tmp_path / "src" / "a.png")
    store.thumbnail_for("rec3", source)
    assert store.existing_thumb("rec3") is not None


def test_save_bytes_is_atomic_and_guess_ext(tmp_path):
    from app.services.media import guess_ext

    store = MediaStore(tmp_path / "data")
    target = store.save_bytes(b"hello", "rec4", ext=guess_ext("https://x/a.png?v=1"))
    assert target.read_bytes() == b"hello"
    assert target.suffix == ".png"
    # 不带 kind 时按图片判断（视频要靠调用方说明类型）
    assert guess_ext("https://x/video.mp4", "video") == ".mp4"
    assert guess_ext("https://x/photo.png?token=1") == ".png"
    assert guess_ext("https://x/no-ext", "video") == ".mp4"


def test_remove_only_touches_data_dir(tmp_path):
    store = MediaStore(tmp_path / "data")
    inside = store.save_bytes(b"x", "rec5", ext=".png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep me")

    assert store.remove(outside) is False
    assert outside.is_file()
    assert store.remove(inside) is True
    assert not inside.exists()
