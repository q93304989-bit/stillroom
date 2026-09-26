"""统一错误分类：把「HTTP 状态码 + 响应体 + 连接异常」翻译成可判定的类型。

为什么需要它（旧版的教训）：

- 旧版把 401/403 当成临时故障退了 90 次（约 15 分钟），把「配置写错」伪装成「卡住」；
- 旧版把 503 `video_queue_full` 与真正的 500 混为一谈，无法决定要不要重试；
- 旧版把错误拼成字符串，界面只能整段展示，无法据此换策略。

`retryable` 是**唯一**允许上层编排重试的依据。
"""

from __future__ import annotations

import json
from typing import Any


class AppError(Exception):
    """所有应用级异常的基类。"""

    kind = "app"
    retryable = False
    default_hint = ""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        endpoint: str = "",
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.endpoint = endpoint
        self.detail = detail

    @property
    def user_message(self) -> str:
        """可直接展示给用户的文案（含下一步动作）。"""
        parts = [self.message]
        if self.default_hint:
            parts.append(self.default_hint)
        return "\n".join(parts)

    def __str__(self) -> str:  # pragma: no cover - 便于日志阅读
        suffix = f" [HTTP {self.status}]" if self.status else ""
        return f"{type(self).__name__}: {self.message}{suffix}"


class ConfigError(AppError):
    """配置缺失或错误（缺密钥、地址不合法）。用户必须去改配置。"""

    kind = "config"


class ValidationError(AppError):
    """用户输入不合法（空提示词、不支持的尺寸）。"""

    kind = "validation"


class NotFoundError(AppError):
    """资源不存在。视频查询里很常见：任务尚未注册完成。"""

    kind = "not_found"
    retryable = True  # 拥堵期任务注册延迟，退避重试是正确策略


class NetworkError(AppError):
    """连接类失败（DNS、连接被拒、SSL 被掐断、代理无服务）。"""

    kind = "network"
    retryable = True
    default_hint = "请检查网络或代理设置（设置页可切换「自动 / 仅直连 / 仅系统代理」）。"


class RequestTimeout(AppError):
    """请求超时。"""

    kind = "timeout"
    retryable = True
    default_hint = "请求超时，可稍后重试；若持续超时请检查网络。"


class AuthError(AppError):
    """认证失败（401/403）：配置类硬故障，重试无用。"""

    kind = "auth"
    default_hint = (
        "请确认「接口地址」与「API 密钥」属于同一站点："
        "国际版 apihub.agnes-ai.com / 国内版 api.agnes-ai.cn。"
    )


class RateLimitError(AppError):
    """触发限流（429）。可按 `retry_after` 退避后重试。"""

    kind = "rate_limit"
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class QueueFullError(AppError):
    """服务端队列满（503 video_queue_full）。可退避重试。"""

    kind = "queue_full"
    retryable = True
    default_hint = (
        "平台的生成队列当前排队较多（免费通道常见）。稍等一两分钟再点一次「生成视频」即可，"
        "刚刚这次没有创建任务、也没有产生费用。"
    )


class ServerError(AppError):
    """服务端 5xx。可退避重试。"""

    kind = "server"
    retryable = True


class ResponseFormatError(AppError):
    """响应结构不符合预期（200 但没有 data[0].url 之类）。"""

    kind = "response"


class NeedsApproval(AppError):
    """这个动作需要人工确认后才能执行（上传到公网、删除、覆盖等不可回滚的操作）。

    上层应当把它排进审批队列等用户点确认，而不是当成失败——所以它不是 retryable，
    但也不是「出错」。`tool` 与 `params` 带着原样信息，确认后可以原封不动重放。
    """

    kind = "approval"

    def __init__(self, message: str, *, tool: str = "", params: dict | None = None, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.tool = tool
        self.params = dict(params or {})


class BudgetExceeded(AppError):
    """本次运行超出了预算（生成次数 / 模型调用次数）。停下来问人，不要静默烧额度。"""

    kind = "budget"

    def __init__(self, message: str, *, tool: str = "", used: int = 0, limit: int = 0, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.tool = tool
        self.used = used
        self.limit = limit


class LoopDetected(AppError):
    """同一工具用相同参数反复调用——多半是打转，先停下来重新规划或问用户。"""

    kind = "loop"

    def __init__(self, message: str, *, tool: str = "", repeats: int = 0, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.tool = tool
        self.repeats = repeats


class ToolNotAllowed(AppError):
    """当前阶段不允许调用这个工具。

    阶段白名单的作用是**物理上够不到**不该碰的能力，而不是靠提示词反复叮嘱模型。
    """

    kind = "phase"

    def __init__(self, message: str, *, tool: str = "", allowed: tuple[str, ...] = (), **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.tool = tool
        self.allowed = tuple(allowed)


#: 允许编排层自动重试的类型
RETRYABLE: tuple[type[AppError], ...] = (
    NetworkError,
    RequestTimeout,
    NotFoundError,
    RateLimitError,
    QueueFullError,
    ServerError,
)


def is_retryable(error: BaseException) -> bool:
    return isinstance(error, AppError) and error.retryable


def _body_text(body: Any, limit: int = 200) -> str:
    """把响应体压成一小段可读文本（优先取 message / error 字段）。"""
    if body is None:
        return ""
    if isinstance(body, (dict, list)):
        data = body
    else:
        text = str(body).strip()
        if not text:
            return ""
        try:
            data = json.loads(text)
        except ValueError:
            return text[:limit]
    if isinstance(data, dict):
        for field in ("message", "error", "detail", "msg"):
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()[:limit]
            if isinstance(value, dict):
                inner = value.get("message") or value.get("msg")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()[:limit]
        return json.dumps(data, ensure_ascii=False)[:limit]
    return json.dumps(data, ensure_ascii=False)[:limit]


def classify_http(
    status: int,
    *,
    body: Any = None,
    endpoint: str = "",
    headers: Any = None,
) -> AppError:
    """把 HTTP 状态码翻译成类型化异常。调用方只需 `raise classify_http(...)`。"""
    text = _body_text(body)
    suffix = f"：{text}" if text else ""

    if status in (401, 403):
        return AuthError(f"认证失败（HTTP {status}）{suffix}", status=status, endpoint=endpoint, detail=body)
    if status == 404:
        return NotFoundError(f"资源不存在（HTTP 404）{suffix}", status=status, endpoint=endpoint, detail=body)
    if status == 429:
        retry_after = None
        try:
            raw = headers.get("Retry-After") if headers is not None else None
            retry_after = float(raw) if raw else None
        except (TypeError, ValueError):
            retry_after = None
        return RateLimitError(
            f"触发限流（HTTP 429）{suffix}", retry_after=retry_after, status=status, endpoint=endpoint, detail=body
        )
    if status == 503 and "video_queue_full" in str(body).lower():
        return QueueFullError(f"服务端队列已满（HTTP 503）{suffix}", status=status, endpoint=endpoint, detail=body)
    if status >= 500:
        return ServerError(f"服务端错误（HTTP {status}）{suffix}", status=status, endpoint=endpoint, detail=body)
    if status == 400:
        return ValidationError(f"请求被拒绝（HTTP 400）{suffix}", status=status, endpoint=endpoint, detail=body)
    return AppError(f"请求失败（HTTP {status}）{suffix}", status=status, endpoint=endpoint, detail=body)


def classify_exception(exc: BaseException, *, endpoint: str = "") -> AppError:
    """把 httpx / 系统异常翻译成类型化异常（未知类型原样包一层）。"""
    if isinstance(exc, AppError):
        return exc
    name = type(exc).__name__
    if name in ("ConnectError", "ConnectTimeout", "ProxyError", "ProtocolError", "ReadError", "RemoteProtocolError"):
        return NetworkError(f"网络连接失败：{exc}", endpoint=endpoint, detail=exc)
    if name in ("TimeoutException", "ReadTimeout", "WriteTimeout", "PoolTimeout"):
        return RequestTimeout(f"请求超时：{exc}", endpoint=endpoint, detail=exc)
    if isinstance(exc, (TimeoutError,)):
        return RequestTimeout(f"请求超时：{exc}", endpoint=endpoint, detail=exc)
    if isinstance(exc, OSError):
        return NetworkError(f"网络异常：{exc}", endpoint=endpoint, detail=exc)
    return AppError(f"{name}：{exc}", endpoint=endpoint, detail=exc)
