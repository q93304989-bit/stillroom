"""自更新建议：证据收集、10 次阈值、LLM 草案解析与 sanitize。全部走 MockTransport。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.agent import self_update
from app.bootstrap import build_context
from app.services.history import Record

SUGGESTION = {
    "composer_suffix": "光线描述必须具体（光源、色温、方向）",
    "aspect_preference": "9:16",
    "question_overrides": {"evaluate.fix": "最影响成品可用性的一处问题是什么？"},
    "reason": "最近 12 次里 7 次被判光线问题",
}


def seed_runs(context, count: int, *, with_feedback: bool = True) -> None:
    """造 count 次「带信号的运行」：meta 里有评估结论，用户标了采纳/重试。"""
    for index in range(count):
        record = context.history.add(Record(
            kind="image",
            prompt=f"海报 {index}",
            meta={"agent": {
                "run_id": f"run-{index}",
                "requirement": f"海报 {index}",
                "evaluation": {"fits": 0.8, "quality": 2.0, "fix": "lighting", "confidence": 0.8},
            }},
        ))
        if with_feedback:
            context.history.set_feedback(record.id, action="accept" if index % 2 else "retry")


def make_context(tmp_path, llm_payload) :
    """llm_payload: dict 走 JSON 正常返回；str 原样当正文；None 返回 401。"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/chat/completions" in url:
            if llm_payload is None:
                return httpx.Response(401, json={"message": "Invalid token"})
            content = (
                json.dumps(llm_payload, ensure_ascii=False)
                if isinstance(llm_payload, dict)
                else llm_payload
            )
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return httpx.Response(404, json={"message": "no route"})

    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGNES_API_KEY=sk-agnes\n"
        "AGNES_BASE_URL=https://api.test/v1\n"
        "DEEPSEEK_API_KEY=sk-ds\n"
        "TYPESAFE_API_KEY=apikey-ts\n",
        encoding="utf-8",
    )
    return build_context(
        env_file=env_file,
        data_dir=tmp_path / "data",
        transport=httpx.MockTransport(handler),
    )


# --------------------------------------------------------------------------- 证据

def test_collect_evidence_keeps_only_signaled_runs(tmp_path):
    context = make_context(tmp_path, SUGGESTION)
    seed_runs(context, 3)
    context.history.add(Record(kind="image", prompt="手动生成还没评价"))     # 无信号
    samples = self_update.collect_evidence(context.history)
    assert len(samples) == 3
    assert samples[0]["evaluation"]["fix"] == "lighting"
    assert samples[0]["action"] in ("accept", "retry")


def test_favorite_alone_counts_as_a_signal(tmp_path):
    context = make_context(tmp_path, SUGGESTION)
    record = context.history.add(Record(kind="image", prompt="手动但收藏了"))
    context.history.set_feedback(record.id, favorite=True)
    assert self_update.sample_count(context.history) == 1


def test_evidence_stats_counts_actions_and_fixes(tmp_path):
    context = make_context(tmp_path, SUGGESTION)
    seed_runs(context, 4)
    stats = self_update.evidence_stats(self_update.collect_evidence(context.history))
    assert stats["samples"] == 4
    assert stats["accepts"] == 2
    assert stats["retries"] == 2
    assert stats["top_fixes"] == [("lighting", 4)]


# --------------------------------------------------------------------------- 阈值

async def test_below_threshold_never_calls_the_llm(tmp_path):
    context = make_context(tmp_path, SUGGESTION)
    seed_runs(context, self_update.MIN_SAMPLES - 1)
    outcome = await self_update.generate_suggestion(context.registry, context.history)
    assert outcome["ok"] is False
    assert outcome["reason"] == "samples"
    assert outcome["short_by"] == 1


async def test_at_threshold_produces_a_sanitized_patch(tmp_path):
    context = make_context(tmp_path, SUGGESTION)
    seed_runs(context, self_update.MIN_SAMPLES)
    outcome = await self_update.generate_suggestion(context.registry, context.history)
    assert outcome["ok"] is True
    assert outcome["patch"]["composer_suffix"].startswith("光线描述必须具体")
    assert outcome["patch"]["aspect_preference"] == "9:16"
    assert "evaluate.fix" in outcome["patch"]["question_overrides"]
    assert outcome["reason"].startswith("最近 12 次")
    assert outcome["evidence"]["samples"] == self_update.MIN_SAMPLES


async def test_out_of_whitelist_keys_are_dropped(tmp_path):
    payload = dict(SUGGESTION)
    payload["question_overrides"] = {"flows.structure": "改成一步", "evaluate.fits": "好不好？"}
    payload["hack"] = True
    context = make_context(tmp_path, payload)
    seed_runs(context, self_update.MIN_SAMPLES)
    outcome = await self_update.generate_suggestion(context.registry, context.history)
    assert outcome["ok"] is True
    assert outcome["patch"]["question_overrides"] == {"evaluate.fits": "好不好？"}
    assert "hack" not in outcome["patch"]


async def test_code_fenced_json_is_tolerated(tmp_path):
    context = make_context(tmp_path, "```json\n" + json.dumps(SUGGESTION, ensure_ascii=False) + "\n```")
    seed_runs(context, self_update.MIN_SAMPLES)
    outcome = await self_update.generate_suggestion(context.registry, context.history)
    assert outcome["ok"] is True


async def test_unparseable_answer_gives_no_suggestion(tmp_path):
    context = make_context(tmp_path, "我觉得都挺好的，不用改")
    seed_runs(context, self_update.MIN_SAMPLES)
    outcome = await self_update.generate_suggestion(context.registry, context.history)
    assert outcome["ok"] is False
    assert outcome["reason"] == "format"


async def test_empty_patch_means_nothing_to_change(tmp_path):
    context = make_context(tmp_path, {"composer_suffix": "", "reason": "没有规律"})
    seed_runs(context, self_update.MIN_SAMPLES)
    outcome = await self_update.generate_suggestion(context.registry, context.history)
    assert outcome["ok"] is False
    assert outcome["reason"] == "empty"


async def test_llm_failure_is_reported_not_raised(tmp_path):
    context = make_context(tmp_path, None)           # 401
    seed_runs(context, self_update.MIN_SAMPLES)
    outcome = await self_update.generate_suggestion(context.registry, context.history)
    assert outcome["ok"] is False
    assert outcome["reason"] == "llm"


# --------------------------------------------------------------------------- 消息

def test_suggestion_messages_describe_shape_and_samples(tmp_path):
    samples = [{
        "requirement": "中秋海报",
        "evaluation": {"fits": 0.9, "quality": 2.0, "fix": "lighting"},
        "action": "retry",
        "favorite": False,
    }]
    messages = self_update.build_suggestion_messages(samples)
    assert len(messages) == 1 and messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert "中秋海报" in content and "lighting" in content and "retry" in content
    assert "composer_suffix" in content and "JSON" in content
