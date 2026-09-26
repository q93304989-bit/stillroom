"""Agnes 图像生成客户端（文生图 / 图生图）。

沿用旧版 `agens_core.py` 的请求形态：

- 端点 `{base_url}/images/generations`
- 认证头是**裸 API Key**（`Authorization: <key>`），与视频接口的 `Bearer ` 不同
- 参考图放 `extra_body.image`：公网 URL 原样、本地文件转 base64 data URI
- `extra_body.response_format = "url"`，响应取 `data[0].url`
"""

from __future__ import annotations

import base64
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.config import paths
from app.config.credentials import AgnesCredentials
from app.net.errors import AppError, ResponseFormatError, ValidationError
from app.net.http import HttpClient

#: 与旧版界面一致的可选模型（第一项为默认）
IMAGE_MODELS = ["agnes-image-2.5-flash", "agnes-image-2.1-flash"]

#: 与旧版界面一致的 7 种尺寸
IMAGE_SIZES = [
    "1024x768",
    "512x512",
    "768x1024",
    "1024x1024",
    "1280x720",
    "720x1280",
    "1920x1080",
]

MAX_REFERENCE_IMAGES = 5
DEFAULT_SIZE = "1024x768"

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def guess_mime(path: str | os.PathLike[str]) -> str:
    """按扩展名猜 MIME；未知时交给 mimetypes，再兜底 png。"""
    ext = Path(path).suffix.lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "image/png"


def encode_reference(value: str) -> str:
    """参考图归一化：本地文件 → base64 data URI；其余（http/https/data）原样返回。"""
    text = str(value or "").strip()
    if not text:
        raise ValidationError("参考图为空")
    if text.lower().startswith(("http://", "https://", "data:")):
        return text
    path = Path(paths.local_path(text))      # 兼容 QML 文件选择器给的 file:// URL
    if not path.is_file():
        raise ValidationError(f"参考图既不是公网 URL，也不存在于本地：{text}")
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{guess_mime(path)};base64,{payload}"


@dataclass(frozen=True)
class ImageRequest:
    """一次图片生成的输入。`images` 是参考图（URL 或本地路径），空 = 文生图。"""

    prompt: str
    images: tuple[str, ...] = ()
    model: str = ""
    size: str = DEFAULT_SIZE
    extra: dict = field(default_factory=dict)

    def validated(self, default_model: str) -> "ImageRequest":
        prompt = (self.prompt or "").strip()
        if not prompt:
            raise ValidationError("提示词不能为空")
        size = (self.size or DEFAULT_SIZE).strip()
        if size not in IMAGE_SIZES:
            raise ValidationError(f"不支持的图片尺寸：{size}（可选：{', '.join(IMAGE_SIZES)}）")
        images = tuple(i for i in (self.images or ()) if str(i).strip())
        if len(images) > MAX_REFERENCE_IMAGES:
            raise ValidationError(f"参考图最多 {MAX_REFERENCE_IMAGES} 张（当前 {len(images)} 张）")
        model = (self.model or default_model).strip()
        if not model:
            raise ValidationError("未指定图片模型")
        return ImageRequest(
            prompt=prompt, images=images, model=model, size=size, extra=dict(self.extra)
        )


class ImageClient:
    """文生图 / 图生图（多参考图）。"""

    def __init__(self, http: HttpClient, credentials: AgnesCredentials) -> None:
        self._http = http
        self._creds = credentials

    @property
    def credentials(self) -> AgnesCredentials:
        return self._creds

    def build_payload(self, request: ImageRequest) -> dict[str, Any]:
        """拼出接口要求的 JSON（单独拎出来便于测试与日志脱敏）。"""
        payload: dict[str, Any] = {
            "model": request.model or self._creds.image_model,
            "prompt": request.prompt,
            "size": request.size,
        }
        images = [encode_reference(item) for item in request.images]
        extra: dict[str, Any] = {"response_format": "url"}
        if images:
            extra["image"] = images
        extra.update(request.extra or {})
        payload["extra_body"] = extra
        return payload

    async def generate(self, request: ImageRequest, *, timeout: float | None = None) -> str:
        """生成图片，返回图片 URL。"""
        prepared = request.validated(self._creds.image_model)
        payload = self.build_payload(prepared)
        data = await self._http.post_json(
            self._creds.images_endpoint,
            headers={
                "Authorization": self._creds.require_key(),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout or 120,
        )
        return extract_image_url(data)


def extract_image_url(data: Any) -> str:
    """从响应里取出图片 URL；结构不符时抛 ResponseFormatError。"""
    if isinstance(data, dict):
        items: Iterable[Any] = data.get("data") or []
        for item in items:
            if isinstance(item, dict):
                url = item.get("url") or item.get("image_url")
                if isinstance(url, str) and url.strip():
                    return url.strip()
        error = data.get("error") or data.get("message")
        if error:
            raise AppError(f"生成图片失败：{error}")
    raise ResponseFormatError(f"响应里没有图片 URL：{str(data)[:200]}")
