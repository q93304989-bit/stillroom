"""规范化 JSON 与内容哈希。

单独成模块是因为这套规则有**两个**消费者，而它们必须逐字节一致：

- `runtime/repository.py` —— 算 `input_hash`、`payload_hash` 落库；
- `runtime/stub_kernel.py` —— 给每步产物算确定性 `payload_hash`。

规则本身是协议级的：同一个逻辑输入永远同一个 hash（键排序、不转义非 ASCII、
紧凑分隔符），否则幂等判定和 Replay 比对都会漂。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["canonical_json", "sha256_of_bytes", "sha256_of_text"]


def canonical_json(value: Any) -> str:
    """规范化 JSON：键排序、不转义非 ASCII、紧凑分隔符。

    同一个逻辑输入永远同一个字符串 —— 这是它能被用来算稳定 hash 的前提。
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_of_bytes(data: bytes) -> str:
    """字节的 sha256，**裸 hex**（不带 `sha256:` 前缀）。"""
    return hashlib.sha256(data).hexdigest()


def sha256_of_text(text: str) -> str:
    """UTF-8 文本的 sha256。等价于 `sha256_of_bytes(text.encode("utf-8"))`。

    前缀问题在 P1 审查里被提过一次，结论是两份 schema
    （`execution-event.schema.json` 的 `hash`、`artifact-manifest.schema.json` 的
    `sha256`）**都**要求 `^[a-f0-9]{64}$`，所以裸 hex 就是协议口味。
    """
    return sha256_of_bytes(text.encode("utf-8"))
