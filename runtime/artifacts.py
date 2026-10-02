"""内容寻址的产物仓库（P1）。

## 定位：blob store，不是 manifest store

`schemas/artifact-manifest.schema.json` 描述的是**执行产物**的清单条目，它的
`required` 里有 `execution_id` 与 `step`（六步枚举之一）。
而工作流定义**不属于任何一次执行、也不属于任何一步** —— 所以本模块
**刻意不为工作流定义伪造 manifest**：硬塞一个 `execution_id` 只会把
"这是执行产物"这件事说谎。工作流定义只需要"内容 + 一个能用 hash 找回它的地址"，
这正是 blob store 提供的。

于是分工是：

| 东西 | 存法 |
|---|---|
| 任意内容（工作流定义、将来的中间产物） | 本模块：`sha256 → 文件` |
| 执行产物的**清单**（谁产的、哪一步、多大） | 将来由 `artifact-manifest.schema.json` 描述，属评估/交付链 |

## 地址方案

`ref = "artifact:sha256:<64 位裸 hex>"`（hex 与两份 schema 的 `^[a-f0-9]{64}$` 同源）。
落盘布局 `<root>/sha256/<前两位>/<完整 hex>` —— 分两层是为了单个目录别堆几十万个文件。

## 两条硬性质

1. **内容寻址**：同样的字节永远同一个地址，重复写入是空操作（幂等）。
2. **读时校验**：`get_bytes()` 会重算 hash 并比对，对不上就抛 ——
   内容寻址只有在"地址是内容的函数"被真正强制时才成立，
   否则磁盘损坏会静默给出错内容。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from validator.errors import ErrorCode

from .errors import RepositoryError
from .hashing import canonical_json, sha256_of_bytes

REF_PREFIX = "artifact:sha256:"


@dataclass(frozen=True)
class StoredArtifact:
    """一次 `put_*` 的结果。"""

    sha256: str
    ref: str
    bytes: int
    path: Path
    created: bool
    """本次是否真的写了盘。同内容重复写入时为 `False`（内容寻址的幂等）。"""


class ArtifactStore:
    """内容寻址的本地 blob 仓库。没有索引表 —— 文件名就是索引。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # -- 地址 -------------------------------------------------------------

    @staticmethod
    def ref_for(sha256: str) -> str:
        return f"{REF_PREFIX}{sha256}"

    @staticmethod
    def parse_ref(ref: str) -> str:
        """从 `artifact:sha256:<hex>` 取回 hex。格式不对是编程错误，直接抛。"""
        if not isinstance(ref, str) or not ref.startswith(REF_PREFIX):
            raise ValueError(f"not an artifact ref: {ref!r}")
        digest = ref[len(REF_PREFIX):]
        if len(digest) != 64:
            raise ValueError(f"artifact ref has a malformed digest: {ref!r}")
        return digest

    def path_for(self, sha256: str) -> Path:
        self._check_digest(sha256)
        return self._root / "sha256" / sha256[:2] / sha256

    # -- 写 ---------------------------------------------------------------

    def put_bytes(self, data: bytes) -> StoredArtifact:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"put_bytes expects bytes, got {type(data).__name__}")
        payload = bytes(data)
        digest = sha256_of_bytes(payload)
        target = self.path_for(digest)
        if target.exists():
            return StoredArtifact(digest, self.ref_for(digest), len(payload), target, False)

        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写同目录临时文件再 rename：避免"读到写了一半的内容"。
        # 用 os.replace（rename）而不是先写再删 —— 删除类调用会被 safe-delete 拦。
        staging = target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex[:8]}")
        with open(staging, "wb") as handle:
            handle.write(payload)
        os.replace(staging, target)
        return StoredArtifact(digest, self.ref_for(digest), len(payload), target, True)

    def put_json(self, value: Any) -> StoredArtifact:
        """规范化 JSON 落盘。同一个逻辑对象永远同一个地址（键序不影响）。"""
        text = canonical_json(value)
        return self.put_bytes(text.encode("utf-8"))

    # -- 读 ---------------------------------------------------------------

    def has(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()

    def get_bytes(self, sha256: str) -> bytes:
        """取回内容并**重算校验**。对不上说明内容与地址不再对应，必须炸。"""
        path = self.path_for(sha256)
        if not path.exists():
            raise RepositoryError(
                ErrorCode.EXECUTION_NOT_FOUND,
                "artifact content is missing from the store",
                sha256=sha256,
                path=str(path),
            )
        data = path.read_bytes()
        actual = sha256_of_bytes(data)
        if actual != sha256:
            raise RepositoryError(
                ErrorCode.SCHEMA_INVALID,
                "artifact content does not match its content address",
                sha256=sha256,
                actual=actual,
                path=str(path),
            )
        return data

    def get_json(self, sha256: str) -> Any:
        return json.loads(self.get_bytes(sha256).decode("utf-8"))

    def get_by_ref(self, ref: str) -> bytes:
        return self.get_bytes(self.parse_ref(ref))

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _check_digest(sha256: str) -> None:
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError(f"not a sha256 digest: {sha256!r}")
        try:
            int(sha256, 16)
        except ValueError as exc:
            raise ValueError(f"not a sha256 digest: {sha256!r}") from exc


__all__ = ["ArtifactStore", "StoredArtifact", "REF_PREFIX"]
