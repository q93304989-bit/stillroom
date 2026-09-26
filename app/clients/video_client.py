"""Agnes 视频生成客户端（异步任务：提交 + 轮询查询）。

沿用旧版 `video_core.py` 的关键约定（都是踩坑换来的）：

1. **提交**走 `{base_url}/videos`，认证是 `Bearer <key>`；
   **查询**走站点根下的 `/agnesapi`（不带 `/v1`），必须跟随 base_url 的站点。
2. 平台限制：**每分钟只允许提交 1 个任务**（429 rate_limit），队列满返回
   503 `video_queue_full`——两者都可退避重试。
3. 参考图**只接受公网 http(s) URL**（本地文件 / base64 实测被 400 拒绝）。
4. flash 系模型在 reference/keyframe 模式下查询必须带 `model_name`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config.credentials import AgnesCredentials
from app.net.errors import ValidationError
from app.net.http import HttpClient

#: 可用视频模型（第一项为默认，限时免费）
VIDEO_MODELS = ["agnes-video-2.5-flash", "agnes-video-2.5", "agnes-video-v2.0"]
ASPECT_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
SECONDS_OPTIONS = ["4", "5", "8", "10", "12"]

#: 查询时必须附带 model_name 的模型
FLASH_VIDEO_MODELS = {"agnes-video-2.5-flash"}

MAX_REFERENCE_IMAGES = 5
DONE_STATUSES = {"completed", "succeeded", "success", "failed", "error", "canceled", "cancelled"}
FAILED_STATUSES = {"failed", "error", "canceled", "cancelled"}


def validate_reference_images(images: Any) -> tuple[str, ...]:
    """参考图必须是可公开访问的 http(s) URL。"""
    out: list[str] = []
    for value in images or []:
        text = str(value or "").strip()
        if not text:
            continue
        if not text.lower().startswith(("http://", "https://")):
            raise ValidationError(
                "视频参考图仅支持可公开访问的 http(s) URL，不支持本地文件路径。\n"
                "本地图片请先上传到图床，或改用「图片生成」页的图生图（支持本地文件）。"
            )
        out.append(text)
    if len(out) > MAX_REFERENCE_IMAGES:
        raise ValidationError(f"参考图最多 {MAX_REFERENCE_IMAGES} 张（当前 {len(out)} 张）")
    return tuple(out)


@dataclass(frozen=True)
class VideoRequest:
    """一次视频生成的输入。有参考图 → reference 模式，无 → text 模式（自动）。"""

    prompt: str
    images: tuple[str, ...] = ()
    model: str = ""
    seconds: str = "5"
    aspect_ratio: str = "16:9"
    seed: int | None = None
    extra: dict = field(default_factory=dict)

    def validated(self, default_model: str) -> "VideoRequest":
        prompt = (self.prompt or "").strip()
        if not prompt:
            raise ValidationError("提示词不能为空")
        seconds = str(self.seconds or "5")
        if seconds not in SECONDS_OPTIONS:
            raise ValidationError(f"视频时长必须是 {SECONDS_OPTIONS} 之一")
        aspect = (self.aspect_ratio or "16:9").strip()
        if aspect not in ASPECT_RATIOS:
            raise ValidationError(f"画幅必须是 {ASPECT_RATIOS} 之一")
        images = validate_reference_images(self.images)
        model = (self.model or default_model).strip()
        if not model:
            raise ValidationError("未指定视频模型")
        return VideoRequest(
            prompt=prompt,
            images=images,
            model=model,
            seconds=seconds,
            aspect_ratio=aspect,
            seed=self.seed,
            extra=dict(self.extra),
        )


class VideoClient:
    """视频任务提交与查询。轮询节奏由上层编排（Phase 2）决定，本类不 sleep。"""

    def __init__(self, http: HttpClient, credentials: AgnesCredentials) -> None:
        self._http = http
        self._creds = credentials

    @property
    def credentials(self) -> AgnesCredentials:
        return self._creds

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._creds.require_key()}",
            "Content-Type": "application/json",
        }

    def build_payload(self, request: VideoRequest) -> dict[str, Any]:
        images = list(request.images or ())
        mode = "reference" if images else "text"
        payload: dict[str, Any] = {
            "model": request.model or self._creds.video_model,
            "prompt": request.prompt,
            "seconds": request.seconds,
            "mode": mode,
            "size": "720P",
            "aspect_ratio": request.aspect_ratio,
        }
        if images:
            payload["images"] = images
        if request.seed is not None:
            payload["seed"] = request.seed
        payload.update(request.extra or {})
        return payload

    async def submit(self, request: VideoRequest, *, timeout: float | None = None) -> dict:
        """提交任务，返回服务端响应（含 video_id）。"""
        prepared = request.validated(self._creds.video_model)
        return await self._http.post_json(
            self._creds.videos_endpoint,
            headers=self._headers(),
            json=self.build_payload(prepared),
            timeout=timeout or 30,
        )

    async def query(
        self, video_id: str, *, model_name: str = "", timeout: float | None = None
    ) -> dict:
        """查询任务状态。flash 系模型必须带 model_name，否则查不到任务。"""
        if not (video_id or "").strip():
            raise ValidationError("video_id 不能为空")
        params: dict[str, Any] = {"video_id": video_id.strip()}
        if model_name and model_name in FLASH_VIDEO_MODELS:
            params["model_name"] = model_name
        return await self._http.get_json(
            self._creds.query_endpoint,
            headers=self._headers(),
            params=params,
            timeout=timeout or 15,
        )


def extract_video_id(data: Any) -> str:
    """从提交响应里取 video_id（兼容顶层与嵌套结构）。"""
    if isinstance(data, dict):
        for key in ("video_id", "id", "task_id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for nested_key in ("data", "task", "result"):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                found = extract_video_id(nested)
                if found:
                    return found
    return ""
