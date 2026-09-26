"""错误分类矩阵：状态码 / 响应体 / 连接异常 → 类型与可重试性。"""

from __future__ import annotations

import pytest

from app.net import errors


@pytest.mark.parametrize(
    "status,body,expected,retryable",
    [
        (400, {"message": "bad param"}, errors.ValidationError, False),
        (401, {"message": "Invalid token"}, errors.AuthError, False),
        (403, "forbidden", errors.AuthError, False),
        (404, {"message": "任务不存在"}, errors.NotFoundError, True),
        (429, {"message": "rate_limit"}, errors.RateLimitError, True),
        (500, "boom", errors.ServerError, True),
        (502, "", errors.ServerError, True),
        (503, {"error": "video_queue_full"}, errors.QueueFullError, True),
        (503, {"error": "maintenance"}, errors.ServerError, True),
        (418, "teapot", errors.AppError, False),
    ],
)
def test_classify_http(status, body, expected, retryable):
    error = errors.classify_http(status, body=body, endpoint="https://x/y")
    assert isinstance(error, expected)
    assert error.retryable is retryable
    assert error.status == status


def test_queue_full_requires_body_marker():
    """503 只有明确带 video_queue_full 才算队列满，否则是普通服务端错误。"""
    assert isinstance(
        errors.classify_http(503, body={"error": "video_queue_full"}), errors.QueueFullError
    )
    assert isinstance(errors.classify_http(503, body={"error": "nope"}), errors.ServerError)


def test_rate_limit_reads_retry_after_header():
    error = errors.classify_http(429, body="slow down", headers={"Retry-After": "30"})
    assert isinstance(error, errors.RateLimitError)
    assert error.retry_after == 30.0


def test_auth_error_message_mentions_both_sites():
    """跨区问题的提示必须同时给出两个站点，否则用户不知道去哪查。"""
    error = errors.classify_http(401, body={"message": "Invalid token"})
    assert "apihub.agnes-ai.com" in error.user_message
    assert "api.agnes-ai.cn" in error.user_message


def test_body_message_extraction_prefers_message_field():
    error = errors.classify_http(500, body={"error": {"message": "inner detail"}})
    assert "inner detail" in error.message


class _FakeExc(Exception):
    pass


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ConnectError", errors.NetworkError),
        ("ProxyError", errors.NetworkError),
        ("RemoteProtocolError", errors.NetworkError),
        ("ReadTimeout", errors.RequestTimeout),
        ("ConnectTimeout", errors.NetworkError),
        ("TimeoutException", errors.RequestTimeout),
        ("SomethingWeird", errors.AppError),
    ],
)
def test_classify_exception_by_type_name(name, expected):
    exc = type(name, (_FakeExc,), {})("boom")
    assert isinstance(errors.classify_exception(exc), expected)


def test_is_retryable_helper():
    assert errors.is_retryable(errors.NetworkError("x"))
    assert not errors.is_retryable(errors.AuthError("x"))
    assert not errors.is_retryable(ValueError("x"))
