"""产物落盘与缩略图。

- 把下载到的字节安全写进数据目录（原子替换，文件名用记录 id，避免与用户文件重名）；
- 生成列表 / 画廊用的缩略图（**这是流畅度的关键**：旧版在渲染循环里用 PIL 同步解码，
  实测 19.79ms/张；这里改成读取时就按目标尺寸解码，并且可以在工作线程里批量预热）。

图像处理用 Qt（`QImageReader.setScaledSize` 让解码器直接按目标尺寸解），不引入新依赖；
导入放在函数内部，这样不碰缩略图的场合（例如 Phase 0 的脚本）不需要 Qt 也能 import 本模块。

所有删除都限制在数据目录内部（旧版 `_safe_remove` 的做法，防止误删用户文件）。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 缩略图长边（同时服务列表 56px 与画廊 ~300px 两种展示）
THUMB_SIZE = 320
THUMB_QUALITY = 88

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}


def guess_ext(url: str, kind: str = "image") -> str:
    """从 URL 猜扩展名；猜不出时按类型给默认值。"""
    path = (url or "").split("?")[0]
    ext = os.path.splitext(path)[1].lower()
    if kind == "video":
        return ext if ext in _VIDEO_EXTS else ".mp4"
    return ext if ext in _IMAGE_EXTS else ".png"


class MediaStore:
    """数据目录下的媒体缓存。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.media_dir = self.root / "media"
        self.thumb_dir = self.root / "thumbs"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.thumb_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, record_id: str, ext: str) -> Path:
        if not ext.startswith("."):
            ext = "." + ext
        return self.media_dir / f"{record_id}{ext}"

    def save_bytes(self, data: bytes, record_id: str, *, ext: str = ".png") -> Path:
        """把字节写入缓存（临时文件 + 原子替换，避免半截文件）。"""
        target = self.path_for(record_id, ext)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return target

    def remove(self, path: str | os.PathLike[str] | None) -> bool:
        """删除缓存文件；只允许删数据目录内部的路径。"""
        if not path:
            return False
        try:
            target = Path(path).resolve()
            root = self.media_dir.resolve()
            if root != target.parent and root not in target.parents:
                return False
            if target.is_file():
                target.unlink()
                return True
        except OSError:
            return False
        return False

    # ---------------------------------------------------------------- 缩略图

    def thumb_path_for(self, record_id: str, *, ext: str = ".jpg") -> Path:
        return self.thumb_dir / f"{record_id}{ext}"

    def existing_thumb(self, record_id: str) -> Path | None:
        """已有的缩略图（.jpg / .png 都认）。"""
        for ext in (".jpg", ".png"):
            candidate = self.thumb_path_for(record_id, ext=ext)
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def thumbnail_for(
        self,
        record_id: str,
        source: str | os.PathLike[str],
        *,
        size: int = THUMB_SIZE,
    ) -> Path | None:
        """生成（或复用）缩略图，返回路径；源文件不可用时返回 None。

        源文件比缩略图新时会重新生成，避免用户替换了缓存文件后仍显示旧图。
        """
        src = Path(source)
        if not src.is_file():
            return None

        existing = self.existing_thumb(record_id)
        if existing is not None and existing.stat().st_mtime >= src.stat().st_mtime:
            return existing

        image = _read_scaled(src, size)
        if image is None:
            return None

        has_alpha = image.hasAlphaChannel()
        target = self.thumb_path_for(record_id, ext=".png" if has_alpha else ".jpg")
        tmp = target.with_suffix(target.suffix + ".part")
        ok = image.save(str(tmp), "PNG" if has_alpha else "JPEG", THUMB_QUALITY)
        if not ok:
            tmp.unlink(missing_ok=True)
            return None
        os.replace(tmp, target)

        # 源文件是 png、这次生成了 jpg（反之亦然）：把另一种扩展名的旧图清掉
        stale = self.thumb_path_for(record_id, ext=".jpg" if has_alpha else ".png")
        stale.unlink(missing_ok=True)
        return target


def _read_scaled(src: Path, size: int):
    """按目标尺寸解码（能交给解码器做的就不要自己缩）。"""
    try:
        from PySide6.QtCore import QSize, Qt
        from PySide6.QtGui import QImageReader
    except ImportError:                     # pragma: no cover - 没有 Qt 时退化为「不做缩略图」
        return None

    reader = QImageReader(str(src))
    reader.setAutoTransform(True)
    original = reader.size()
    if original.isValid() and original.width() > 0 and original.height() > 0:
        scaled = original.scaled(QSize(size, size), Qt.KeepAspectRatio)
        reader.setScaledSize(scaled)
    image = reader.read()
    if image.isNull():
        return None
    if image.width() > size or image.height() > size:
        image = image.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return image


def jpeg_bytes_scaled(path: str | os.PathLike[str], size: int = THUMB_SIZE) -> bytes | None:
    """把本地图片按目标长边解码成 JPEG 字节（给「发给视觉模型」用）。

    实测对比：同一张图发原图要 66.8 秒，发 320px 只要 4.6 秒，而描述质量几乎不变
    （见 `docs/项目状态与Agent方案汇总.md` 的探针结果）。所以发送前一律缩到 320。
    """
    try:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    except ImportError:                     # pragma: no cover
        return None

    image = _read_scaled(Path(path), size)
    if image is None or image.isNull():
        return None

    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, "JPEG", 85):
        return None
    buffer.close()
    return bytes(payload)
