"""OpenAI 兼容的 LLM 客户端。

只认协议、不认厂商：`{base_url}/chat/completions`，`Authorization: Bearer <key>`。
换模型、换供应商只改 `.env`，代码不动。

Phase 1 只提供最小可用的 `chat()` 与 `models()`；具体功能（提示词扩写之类）等有明确
用法时再排期，避免为一个还没定义的功能先写一堆抽象。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.config.credentials import LlmCredentials
from app.net.errors import ResponseFormatError, ValidationError
from app.net.http import HttpClient


class LlmClient:
    """OpenAI 兼容对话客户端（非流式）。"""

    def __init__(self, http: HttpClient, credentials: LlmCredentials) -> None:
        self._http = http
        self._creds = credentials

    @property
    def credentials(self) -> LlmCredentials:
        return self._creds

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._creds.require().api_key}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        messages: Iterable[Mapping[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> str:
        """发一轮对话，返回首条回复的文本。"""
        creds = self._creds.require()
        payload: dict[str, Any] = {
            "model": model or creds.model,
            "messages": [dict(m) for m in messages],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra:
            payload.update(dict(extra))
        if not payload["model"]:
            raise ValidationError("未指定 LLM 模型（.env 里的 LLM_MODEL 为空）")

        data = await self._http.post_json(
            creds.chat_endpoint, headers=self._headers(), json=payload, timeout=timeout or 60
        )
        return extract_chat_text(data)

    async def models(self, *, timeout: float | None = None) -> list[str]:
        """列出可用模型（`GET /models`），用于设置页下拉。"""
        creds = self._creds.require()
        data = await self._http.get_json(
            f"{creds.base_url.rstrip('/')}/models", headers=self._headers(), timeout=timeout or 20
        )
        items = (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ResponseFormatError(f"模型列表格式异常：{str(data)[:150]}")
        out: list[str] = []
        for item in items:
            if isinstance(item, dict):
                name = item.get("id") or item.get("name")
                if isinstance(name, str) and name.strip():
                    out.append(name.strip())
        return out


def extract_chat_text(data: Any) -> str:
    """从 OpenAI 兼容响应里取首条回复文本。"""
    if isinstance(data, dict):
        choices = data.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                text = choice.get("text")
                if isinstance(text, str):
                    return text
    raise ResponseFormatError(f"响应里没有回复内容：{str(data)[:200]}")
