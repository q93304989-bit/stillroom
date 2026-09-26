"""自更新的「建议」是怎么来的：攒够样本 → 让 LLM 总结 → 交出一份补丁草案。

两条硬规则：

1. **样本不足 10 次绝不出建议**（`MIN_SAMPLES`）——数据太少时 LLM 只会编故事。
2. **LLM 只产出草案，落地要走 `PromptStore`**：这里的输出经过 `sanitize_patch`
   裁剪白名单后才登记为 `proposed`，接受/拒绝永远是人说了算。

样本从哪来：历史记录里「助手跑过的运行」（`meta.agent` 带评估结论）加上
用户的评价信号（采纳 / 重试 / 收藏）——前者告诉它「模型觉得哪里不行」，
后者告诉它「人觉得行不行」。
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from app.agent.prompt_store import OVERRIDABLE_QUESTIONS, sanitize_patch
from app.clients.vision_client import extract_json_object
from app.net.errors import AppError
from app.services.history import HistoryStore, Record

#: 攒够多少次「带信号的运行」才允许出建议
MIN_SAMPLES = 10

#: 一次最多喂给 LLM 多少条样本（从最新往回数）
MAX_EVIDENCE = 30


def collect_evidence(history: HistoryStore, *, limit: int = 200) -> list[dict[str, Any]]:
    """从历史里收集「带信号的运行」，最新在前。

    一条样本 = 一次助手运行（meta 里有评估结论）或一次用户评价（采纳/重试/收藏）。
    两头都没有的记录（比如手动生成还没评价）不算样本。
    """
    samples: list[dict[str, Any]] = []
    for record in history.list(limit=limit):
        sample = _sample_of(record)
        if sample is not None:
            samples.append(sample)
    return samples


def sample_count(history: HistoryStore) -> int:
    return len(collect_evidence(history))


def evidence_stats(samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    """证据数字（设置页与 LLM 都要看）：总样本、采纳/重试次数、问题方向分布。"""
    fixes: dict[str, int] = {}
    accepts = retries = 0
    for sample in samples:
        action = sample.get("action")
        if action == "accept":
            accepts += 1
        elif action == "retry":
            retries += 1
        fix = str((sample.get("evaluation") or {}).get("fix") or "")
        if fix and fix != "none":
            fixes[fix] = fixes.get(fix, 0) + 1
    return {
        "samples": len(samples),
        "accepts": accepts,
        "retries": retries,
        "top_fixes": sorted(fixes.items(), key=lambda item: -item[1])[:3],
    }


def build_suggestion_messages(samples: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    """给 LLM 的消息：样本摘要 + 严格的输出格式要求。"""
    lines = []
    for index, sample in enumerate(samples[:MAX_EVIDENCE], 1):
        evaluation = sample.get("evaluation") or {}
        parts = [f"{index}. 需求「{str(sample.get('requirement') or '')[:40]}」"]
        if evaluation:
            parts.append(
                f"评估: 符合度={evaluation.get('fits')} 可用度={evaluation.get('quality')}"
                f" 最该改={evaluation.get('fix')}"
            )
        if sample.get("action"):
            parts.append(f"用户: {sample['action']}")
        if sample.get("favorite"):
            parts.append("已收藏")
        lines.append("，".join(parts))

    allowed = "、".join(OVERRIDABLE_QUESTIONS)
    prompt = (
        "你是提示词运维工程师。下面是一个出图助手最近的运行记录"
        "（模型评估结论 + 用户真实反应）。\n"
        "请总结规律，给出一份「提示词补丁」草案，让以后的运行少犯同样的错。\n\n"
        "只输出一个 JSON 对象，不要任何解释。字段：\n"
        '- "composer_suffix": 字符串，写提示词时要固定附加的一句指令（没有好建议就空串）\n'
        '- "aspect_preference": 字符串，用户明显偏好的画幅，只能从 '
        '"16:9","9:16","1:1","4:3","3:4","21:9" 里选，看不出偏好就空串\n'
        '- "question_overrides": 对象，把问得不好的判断问题换成更好的问法；'
        f"键只能从 {allowed} 里选，没有要改的就空对象\n"
        '- "reason": 字符串，一句话说明这份补丁依据什么规律（会展示给用户）\n\n'
        "运行记录：\n" + "\n".join(lines)
    )
    return [{"role": "user", "content": prompt}]


async def generate_suggestion(registry, history: HistoryStore) -> dict[str, Any]:
    """生成一份建议草案。返回 dict，`ok=False` 时带原因（样本不够 / LLM 不可用 / 没法解析）。"""
    samples = collect_evidence(history)
    if len(samples) < MIN_SAMPLES:
        return {
            "ok": False,
            "reason": "samples",
            "short_by": MIN_SAMPLES - len(samples),
            "samples": len(samples),
        }

    try:
        text = await registry.invoke(
            "llm.chat",
            {"messages": build_suggestion_messages(samples)},
            context=None,                # 自更新是独立动作，不进任何一次运行的预算
        )
    except AppError as exc:
        return {"ok": False, "reason": "llm", "message": exc.message}

    raw = extract_json_object(str(text))
    if not raw:
        return {"ok": False, "reason": "format", "message": "模型没有返回可解析的 JSON"}

    patch = sanitize_patch(raw)
    if not patch:
        return {"ok": False, "reason": "empty", "message": "模型认为现在没有值得改的（空补丁）"}

    return {
        "ok": True,
        "patch": patch,
        "reason": str(raw.get("reason") or "").strip(),
        "evidence": evidence_stats(samples),
    }


def _sample_of(record: Record) -> dict[str, Any] | None:
    agent = (record.meta or {}).get("agent") or {}
    evaluation = agent.get("evaluation") or {}
    action = record.last_action
    if not agent and not action and not record.favorite:
        return None
    return {
        "requirement": str(agent.get("requirement") or record.prompt),
        "attempts": agent.get("attempts"),
        "evaluation": evaluation,
        "action": action,
        "favorite": record.favorite,
        "created_at": record.created_at or time.time(),
    }
