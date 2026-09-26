"""视觉模型客户端：**只负责把图变成结构化描述**，是否达标交给 Jev 判断。

为什么这样切（探针实测得出的结论，见 `docs/项目状态与Agent方案汇总.md`）：

1. 视觉模型只会「描述」，让它直接打分既不稳定也没法卡阈值；判断交给带概率与置信度的
   Jev，才谈得上「自动通过 / 请用户确认 / 不动手」三分支。
2. 图片必须先缩小：同一张图原图 66.8 秒、320px 缩略图 4.6 秒，描述质量几乎不变。
3. `max_tokens` 必须给足：设 500 时全部用于 reasoning，正文返回空字符串。

默认供应商是 DeepSeek（用户一套 key 同时做生成与看图），可在设置里换成
DashScope / 智谱 / 任意 OpenAI 兼容服务——换的只是「描述器」，判断标准不受影响。
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config.credentials import VisionCredentials
from app.net.errors import ResponseFormatError, ValidationError
from app.net.http import HttpClient

# 注意：`app.services.media` 要在函数内导入。模块级导入会形成
# registry → vision_client → services.__init__ → generation → registry 的循环。

#: 要模型输出的字段。固定下来才能让下游（Jev）稳定消费。
VISION_FIELDS = ("subject", "style", "composition", "lighting", "flaws")

#: 实测：500 会被 reasoning 吃光导致正文为空，3000 稳定
DEFAULT_MAX_TOKENS = 3000
#: 发送前缩到这个长边（实测比发原图快 14.5 倍，描述质量几乎不变）
DEFAULT_IMAGE_SIZE = 320

_PROMPT = (
    "只输出 JSON，不要任何解释或代码块标记。字段固定为：\n"
    '{"subject": "主体（谁/什么，在做什么）",\n'
    ' "style": "风格（画风/媒介/年代感）",\n'
    ' "composition": "构图（视角/主体位置/引导线）",\n'
    ' "lighting": "光线（光源方向/色调/对比）",\n'
    ' "flaws": ["明显瑕疵，逐条列出；没有就给空数组"]}'
)


@dataclass(frozen=True)
class VisionDescription:
    """一次视觉描述的完整结果（连同元数据一起落库，便于换模型后对比效果）。"""

    subject: str = ""
    style: str = ""
    composition: str = ""
    lighting: str = ""
    flaws: tuple[str, ...] = field(default_factory=tuple)
    raw: str = ""
    model: str = ""
    latency_s: float = 0.0
    usage: dict = field(default_factory=dict)

    def to_state(self) -> dict[str, Any]:
        """给 Jev 当 state 用的紧凑结构（只放判断需要的字段）。"""
        return {
            "subject": self.subject,
            "style": self.style,
            "composition": self.composition,
            "lighting": self.lighting,
            "flaws": list(self.flaws),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_state(),
            "raw": self.raw,
            "model": self.model,
            "latency_s": round(self.latency_s, 2),
            "usage": dict(self.usage),
        }

    @property
    def has_content(self) -> bool:
        return bool(self.subject or self.style or self.composition or self.lighting)


def extract_json_object(text: str) -> dict:
    """从模型输出里抠出 JSON 对象（容忍 ```json 包裹与前后废话）。"""
    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
            if isinstance(data, dict):
                return data
        except ValueError:
            return {}
    return {}


class VisionClient:
    """OpenAI 兼容的视觉描述客户端。"""

    def __init__(
        self,
        http: HttpClient,
        credentials: VisionCredentials,
        *,
        image_size: int = DEFAULT_IMAGE_SIZE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._http = http
        self._creds = credentials
        self.image_size = image_size
        self.max_tokens = max_tokens

    @property
    def credentials(self) -> VisionCredentials:
        return self._creds

    def prepare_image(self, image: str) -> str:
        """图片归一化：公网 URL 原样透传（最省），本地文件缩到 320 再转 data URL。"""
        text = str(image or "").strip()
        if not text:
            raise ValidationError("没有可评估的图片")
        if text.lower().startswith(("http://", "https://")):
            return text
        path = Path(text)
        if not path.is_file():
            raise ValidationError(f"图片不存在：{text}")
        from app.services.media import jpeg_bytes_scaled      # 延迟导入，避免循环依赖

        payload = jpeg_bytes_scaled(path, self.image_size)
        if not payload:
            raise ValidationError(f"图片无法解码：{text}")
        return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")

    def build_payload(self, image: str, *, requirement: str = "") -> dict[str, Any]:
        """拼请求体（单独拎出来便于测试与日志脱敏）。"""
        instruction = _PROMPT
        if requirement:
            instruction += f"\n用户需求（只在相关时用于判断瑕疵）：{requirement}"
        return {
            "model": self._creds.model,
            "max_tokens": self.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url", "image_url": {"url": self.prepare_image(image)}},
                    ],
                }
            ],
        }

    async def describe(
        self, image: str, *, requirement: str = "", timeout: float | None = None
    ) -> VisionDescription:
        """描述一张图；模型没按格式回答时抛 ResponseFormatError（上层据此降级）。"""
        creds = self._creds.require()
        payload = self.build_payload(image, requirement=requirement)
        started = time.perf_counter()
        data = await self._http.post_json(
            creds.endpoint,
            headers={
                "Authorization": f"Bearer {creds.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout or 180,
        )
        return self._parse(data, time.perf_counter() - started)

    def _parse(self, data: Any, latency: float) -> VisionDescription:
        choices = (data or {}).get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise ResponseFormatError(f"视觉模型没有返回候选结果：{str(data)[:150]}")
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise ResponseFormatError(
                "视觉模型正文为空（常见原因：max_tokens 太小，被 reasoning 占满）"
            )
        fields = extract_json_object(content)
        if not fields:
            raise ResponseFormatError(f"视觉模型没有返回可解析的 JSON：{content[:150]}")
        flaws = fields.get("flaws") or []
        if isinstance(flaws, str):
            flaws = [flaws]
        return VisionDescription(
            subject=str(fields.get("subject", "")).strip(),
            style=str(fields.get("style", "")).strip(),
            composition=str(fields.get("composition", "")).strip(),
            lighting=str(fields.get("lighting", "")).strip(),
            flaws=tuple(str(item).strip() for item in flaws if str(item).strip()),
            raw=content,
            model=str((data or {}).get("model") or self._creds.model),
            latency_s=latency,
            usage=dict((data or {}).get("usage") or {}),
        )
