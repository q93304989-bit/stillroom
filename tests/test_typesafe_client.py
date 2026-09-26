"""Jev 客户端：问题构造、请求形状、答案读取与置信度分档。"""

from __future__ import annotations

import httpx
import pytest

from app.clients.typesafe_client import (
    TypeSafeClient,
    choice,
    confidence_band,
    noul,
    score,
)
from app.config.credentials import TypeSafeCredentials
from app.net.errors import AuthError, ConfigError, ResponseFormatError
from app.net.http import HttpClient

CREDS = TypeSafeCredentials(api_key="apikey-ts", base_url="https://api.typesafe.ai", model="jev-latest")

ANSWERS = {
    "model": "jev-1.13.0",
    "answers": {
        "fits": {"type": "noul", "noul": 0.78},
        "quality": {
            "type": "score",
            "score": 2.5,
            "confidence": 0.5,
            "probabilities": {"0": 0.02, "1": 0.08, "2": 0.3, "3": 0.6},
            "legend": {"0": "完全不可用", "1": "需要大改", "2": "小修即可", "3": "直接可用"},
        },
        "fix": {
            "type": "choice",
            "choice": "none",
            "confidence": 0.63,
            "probabilities": {"none": 0.7, "style": 0.23},
        },
    },
    "usage": {"input_tokens": 521, "output_tokens": 82},
}


def make_client(handler) -> tuple[TypeSafeClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return TypeSafeClient(HttpClient(transport=httpx.MockTransport(wrapped), backoff=0), CREDS), seen


def test_question_builders_use_right_criteria_shape():
    """踩过的坑：Score 的 criteria 是数组，Choice 的是字典（写成数组会 422）。"""
    assert score("多好用", ["差", "中", "好"])["criteria"] == ["差", "中", "好"]

    picked = choice("改哪里", {"none": "不用改", "style": "风格不对"})
    assert picked["criteria"] == {"none": "不用改", "style": "风格不对"}
    assert picked["type"] == "choice"

    assert noul("符合需求吗") == {"type": "noul", "instructions": "符合需求吗"}


async def test_ask_sends_state_and_questions():
    client, seen = make_client(lambda r: httpx.Response(200, json=ANSWERS))
    result = await client.ask(
        {"requirement": "中秋海报", "image_description": {"subject": "月亮"}},
        {"fits": noul("符合需求吗")},
    )

    assert str(seen[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert seen[0].headers["Authorization"] == "Bearer apikey-ts"
    body = __import__("json").loads(seen[0].content)
    assert body["model"] == "jev-latest"
    assert body["state"]["requirement"] == "中秋海报"
    assert body["questions"]["fits"]["type"] == "noul"
    assert result.model == "jev-1.13.0"


async def test_answer_accessors():
    client, _ = make_client(lambda r: httpx.Response(200, json=ANSWERS))
    result = await client.ask("x", {"fits": noul("q")})

    assert result.noul("fits") == pytest.approx(0.78)
    assert result.yes("fits") is True
    assert result.score("quality") == pytest.approx(2.5)
    assert result.choice("fix") == "none"
    assert result.confidence("quality") == pytest.approx(0.5)
    assert result.probabilities("fix")["none"] == pytest.approx(0.7)
    assert result.confidence("fits") is None          # noul 没有 confidence
    assert result.noul("不存在") is None


def test_confidence_band_boundaries():
    assert confidence_band(0.9) == "high"
    assert confidence_band(0.7) == "high"
    assert confidence_band(0.5) == "medium"
    assert confidence_band(0.4) == "medium"
    assert confidence_band(0.2) == "low"
    assert confidence_band(None) == "unknown"


async def test_judge_image_builds_three_questions():
    client, seen = make_client(lambda r: httpx.Response(200, json=ANSWERS))
    result = await client.judge_image("中秋海报，竖版", {"subject": "月亮下的庭院"})

    body = __import__("json").loads(seen[0].content)
    assert set(body["questions"]) == {"fits", "quality", "fix"}
    assert "中秋海报，竖版" in body["questions"]["fits"]["instructions"]
    assert body["state"]["image_description"]["subject"] == "月亮下的庭院"
    assert result.choice("fix") == "none"


async def test_auth_error_is_classified():
    client, _ = make_client(lambda r: httpx.Response(401, json={"message": "invalid key"}))
    with pytest.raises(AuthError):
        await client.ask("x", {"fits": noul("q")})


async def test_missing_answers_raises_format_error():
    client, _ = make_client(lambda r: httpx.Response(200, json={"model": "jev-1.13.0"}))
    with pytest.raises(ResponseFormatError):
        await client.ask("x", {"fits": noul("q")})


async def test_empty_questions_rejected():
    client, seen = make_client(lambda r: httpx.Response(200, json=ANSWERS))
    with pytest.raises(ResponseFormatError):
        await client.ask("x", {})
    assert not seen


async def test_missing_key_gives_config_error():
    client = TypeSafeClient(
        HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ANSWERS))),
        TypeSafeCredentials(api_key=""),
    )
    with pytest.raises(ConfigError):
        await client.ask("x", {"fits": noul("q")})
