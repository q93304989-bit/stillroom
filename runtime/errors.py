"""无头运行时的错误类型。

**不新增错误码**：一律从 `validator.errors.ErrorCode` 取。
构造时会校验传入的码是否真的存在 —— 手写字符串码会立刻抛错，
这样「错误码单一来源」是被强制执行的，而不是靠约定。
"""

from __future__ import annotations

from validator.errors import ErrorCode


def error_code(value: ErrorCode | str) -> str:
    """规范成 ErrorCode 的 value；不是合法错误码就直接抛。"""
    if isinstance(value, ErrorCode):
        return value.value
    try:
        return ErrorCode(str(value)).value
    except ValueError as exc:
        raise ValueError(f"unknown error code: {value!r}") from exc


class StillroomRuntimeError(Exception):
    """无头层可预期失败的基类：调用方按 `code` 分支，不看 message。

    `path` 是**相对于输入对象**的路径（`pipeline/refine`、`creator_context`），
    只在与某个具体输入字段有关时才填 —— 契约 §一 的 `error` 体就这三个键。
    没有 `path` 的错误（执行不存在、状态非法转移）留 `None`。

    `details` 是**给运维看的**，不进 MCP 错误体（见 `as_dict()`）：
    它常含内部状态（`retries_used`、`root_execution_id`、内容指纹），
    暴露给 Agent 既不必要、也让内部结构变成事实上的协议。
    """

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        path: str | None = None,
        **details: object,
    ) -> None:
        self.code = error_code(code)
        self.message = message
        self.path = path
        self.details = details
        super().__init__(f"{self.code}: {message}")

    def as_dict(self) -> dict[str, object]:
        """转成 MCP 业务错误体（`contracts/mcp-tools.md` §一 的 `error` 对象）。

        **这是错误体形状的唯一来源** —— `tools.py` 不自己拼，
        否则"契约改了、只有一个地方跟着改"这件事就不成立。

        刻意**不含 `details`**：那一份走 stderr。理由见类 docstring。
        """
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.path is not None:
            payload["path"] = self.path
        return payload


class RepositoryError(StillroomRuntimeError):
    """执行仓库层的失败（找不到、非法转移、retry 被拒等）。"""


class KernelError(StillroomRuntimeError):
    """内核被错误驱动（在不能收尾的状态下调 `advance()`、游标推导不出来等）。

    刻意**不新增错误码** —— 一律复用 `validator.errors.ErrorCode`，
    最常见的即是 `INVALID_TRANSITION`。
    """


__all__ = ["StillroomRuntimeError", "RepositoryError", "KernelError", "error_code"]
