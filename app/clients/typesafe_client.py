"""Jev（TypeSafe System One）客户端：把「窄判断」变成代码可直接消费的返回值。

它不是生成模型、也不是 agent——**代码掌控流程，模型只回答窄问题**：

- `noul`：是/否判断，返回「是的概率」（没有 confidence）
- `choice`：从给定选项里选一个，返回选项 + 完整概率分布 + confidence
- `score`：按给定等级打分，返回概率加权的位置 + confidence

一次请求可以并行问很多问题（官方实测：合并提问比逐个问便宜 12 倍、快 10 倍）。

criteria 的形状是实测踩出来的（写错会 422）：
- Score 的 criteria 是**数组**，顺序即等级编号
- Choice 的 criteria 是**字典**（选项名 → 说明）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.config.credentials import TypeSafeCredentials
from app.net.errors import ResponseFormatError
from app.net.http import HttpClient

#: 置信度三分支的默认门槛（按风险分级：这里给通用默认，具体动作可各自收紧）
HIGH_CONFIDENCE = 0.7
LOW_CONFIDENCE = 0.4


def noul(instructions: str, criteria: Mapping[str, str] | None = None) -> dict[str, Any]:
    """是/否问题。`criteria` 可选，用来写清「什么算 yes、什么算 no」。"""
    question: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if criteria:
        question["criteria"] = dict(criteria)
    return question


def choice(instructions: str, options: Mapping[str, str]) -> dict[str, Any]:
    """选择问题。options 是「选项名 → 说明」，说明要能互相区分开。"""
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def score(instructions: str, levels: list[str]) -> dict[str, Any]:
    """打分问题。levels 从低到高，数组顺序就是等级编号。"""
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


@dataclass
class JudgeResult:
    """一次判断请求的全部返回（answers 保留原始结构，便于落库与回放）。"""

    model: str = ""
    answers: dict[str, dict] = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0

    def _answer(self, question_id: str) -> dict:
        return self.answers.get(question_id) or {}

    def noul(self, question_id: str) -> float | None:
        value = self._answer(question_id).get("noul")
        return float(value) if isinstance(value, (int, float)) else None

    def yes(self, question_id: str, threshold: float = 0.5) -> bool | None:
        value = self.noul(question_id)
        return None if value is None else value >= threshold

    def choice(self, question_id: str) -> str | None:
        value = self._answer(question_id).get("choice")
        return str(value) if value is not None else None

    def score(self, question_id: str) -> float | None:
        value = self._answer(question_id).get("score")
        return float(value) if isinstance(value, (int, float)) else None

    def confidence(self, question_id: str) -> float | None:
        value = self._answer(question_id).get("confidence")
        return float(value) if isinstance(value, (int, float)) else None

    def probabilities(self, question_id: str) -> dict:
        value = self._answer(question_id).get("probabilities")
        return dict(value) if isinstance(value, dict) else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "answers": self.answers,
            "usage": dict(self.usage),
            "latency_s": round(self.latency_s, 2),
        }


def confidence_band(value: float | None, *, high: float = HIGH_CONFIDENCE, low: float = LOW_CONFIDENCE) -> str:
    """把置信度折成三档：high 直接使用 / medium 请用户确认 / low 不动手。"""
    if value is None:
        return "unknown"
    if value >= high:
        return "high"
    if value >= low:
        return "medium"
    return "low"


class TypeSafeClient:
    """Jev 的薄封装：拼请求、解析答案、把 HTTP 错误交给统一的错误分类。"""

    def __init__(self, http: HttpClient, credentials: TypeSafeCredentials) -> None:
        self._http = http
        self._creds = credentials

    @property
    def credentials(self) -> TypeSafeCredentials:
        return self._creds

    async def ask(
        self,
        state: Any,
        questions: Mapping[str, Mapping[str, Any]],
        *,
        model: str | None = None,
        timeout: float | None = None,
    ) -> JudgeResult:
        """发一次判断请求。`state` 可以是字符串或 JSON 对象。"""
        creds = self._creds.require()
        if not questions:
            raise ResponseFormatError("没有要问的问题")
        payload = {
            "model": model or creds.model,
            "state": state,
            "questions": {key: dict(value) for key, value in questions.items()},
        }
        started = time.perf_counter()
        data = await self._http.post_json(
            creds.endpoint,
            headers={
                "Authorization": f"Bearer {creds.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout or 30,
        )
        return self._parse(data, time.perf_counter() - started)

    def _parse(self, data: Any, latency: float) -> JudgeResult:
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise ResponseFormatError(f"Jev 没有返回答案：{str(data)[:150]}")
        return JudgeResult(
            model=str(data.get("model") or self._creds.model),
            answers={key: dict(value) for key, value in data["answers"].items() if isinstance(value, dict)},
            usage=dict(data.get("usage") or {}),
            latency_s=latency,
        )

    async def judge_image(
        self,
        requirement: str,
        description: Mapping[str, Any],
        *,
        fix_options: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> JudgeResult:
        """「这张图行不行」的标准三问：是否符合需求 / 可用程度 / 最该改哪里。

        图片描述由视觉模型给出（`VisionDescription.to_state()`），这里只做判断——
        因此换视觉模型不需要改判断标准。
        """
        questions = {
            "fits": noul(f"这张图是否符合用户需求：{requirement}"),
            "quality": score(
                "这张图作为成品的可用程度",
                ["完全不可用", "需要大改", "小修即可", "直接可用"],
            ),
            "fix": choice(
                "最该改进的地方（若不需要改选 none）",
                dict(
                    fix_options
                    or {
                        "none": "不需要改",
                        "subject": "主体不对或太弱",
                        "style": "风格不符",
                        "composition": "构图问题",
                        "lighting": "光线问题",
                        "detail": "细节瑕疵太多（手部、文字、结构）",
                    }
                ),
            ),
        }
        return await self.ask(
            {"requirement": requirement, "image_description": dict(description)},
            questions,
            timeout=timeout,
        )
