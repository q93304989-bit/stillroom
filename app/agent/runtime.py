"""Agent 运行时：把六个阶段按顺序跑完，每步的判断交给 Jev，动作交给能力注册表。

两条与「让模型自己决定下一步」不同的地方：

1. **流程在代码里**：阶段顺序、回修上限、什么条件算通过，全是代码写死的；模型只回答
   「够不够开工」「这张行不行」「该改哪里」这类窄问题。
2. **一切对外动作都经注册表**：参数校验、预算、审批、循环检测、结果验收都在同一条收口上
   生效（`PhaseGate` 还保证当前阶段碰不到别的工具）。

长任务（出图 20~60 秒、视频 1~3 分钟）用 `await handle.wait()` 等，运行时跑在 asyncio
线程里，界面线程不受影响；每步都往事件总线发事件，界面照着画时间线。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from app.agent.phases import MAX_REFINE_ROUNDS, PHASE_BY_KEY, Phase, tools_of
from app.agent.prompts import composer_messages, evaluate_questions, understand_questions
from app.clients.image_client import ImageRequest
from app.clients.typesafe_client import confidence_band
from app.config import settings
from app.config.logs import log_error
from app.net.errors import AppError, BudgetExceeded, NeedsApproval, ToolNotAllowed
from app.services.generation import GenerationService
from app.services.context_store import DEFAULT_MIN_LOCAL_REFS, DEFAULT_SOURCES, ContextItem
from app.state.events import Event, EventBus
from app.state.jobs import Job, JobHandle, JobStatus

#: 置信度低于这个值就请用户确认，而不是自作主张
CONFIRM_CONFIDENCE = 0.4
#: 可用程度达到这一档以上且符合需求 → 直接交付
ACCEPT_QUALITY = 2.0


def prepare_from_context(context: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    """把用户确认过的上下文翻译成运行时输入：参考图 + 文字线索。

    这里只认 `user_state != removed` 的条目——**用户在草稿上删掉的，运行时不复活**。
    """
    items = [ContextItem.from_dict(item) for item in (context.get("items") or [])]
    kept = [item for item in items if item.user_state != "removed"]
    references = [
        item.ref
        for item in kept
        if item.kind in ("history", "web_image")
        and item.ref
        and not (kind == "video" and not item.ref.lower().startswith(("http://", "https://")))
    ]
    hints = [item.ref for item in kept if item.kind in ("history_text", "kb", "web") and item.ref]
    dropped = len(items) - len(kept)
    detail = f"用你确认的上下文：参考图 {len(references)} 张、线索 {len(hints)} 条"
    if dropped:
        detail += f"（你删掉了 {dropped} 条）"
    return {
        "references": references,
        "hints": hints,
        "items": kept,
        "detail": detail,
        "decide": dict(context.get("decide") or {}),
    }


@dataclass
class StepRecord:
    """时间线上的一步（落库后就是可回放的运行记录）。"""

    phase: str
    title: str
    ok: bool = True
    detail: str = ""
    data: dict = field(default_factory=dict)
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "title": self.title,
            "ok": self.ok,
            "detail": self.detail,
            "data": dict(self.data),
            "seconds": round(self.seconds, 2),
        }


@dataclass
class AgentResult:
    """一次运行的结论。`status` 决定界面怎么接：交付 / 追问 / 等确认 / 等审批 / 失败。"""

    run_id: str
    status: str
    requirement: str = ""
    kind: str = "image"
    prompt: str = ""
    references: list[str] = field(default_factory=list)
    result_url: str = ""
    local_path: str = ""
    description: dict = field(default_factory=dict)
    evaluation: dict = field(default_factory=dict)
    steps: list[StepRecord] = field(default_factory=list)
    message: str = ""
    attempts: int = 0
    #: 请示内容：界面据此决定「补一句话 / 看一眼 / 批准一次」。三种请示各有一份，
    #: 不是请示的话就是空字典。字段形状见 `_pending_*`。
    pending: dict = field(default_factory=dict)
    #: 本次运行各工具调用的次数（闸门的计费口径），界面直接展示，避免「不知道烧了多少」
    usage: dict = field(default_factory=dict)
    #: 这次运行**实际用了什么上下文**（需求 / 条目 / 备注 / 开关 / 决策）。
    #: 自动模式跑完靠它落一份快照，于是「改参考再跑一次」在没有草稿时也能用。
    context: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "ok": self.ok,
            "requirement": self.requirement,
            "kind": self.kind,
            "prompt": self.prompt,
            "references": list(self.references),
            "result_url": self.result_url,
            "local_path": self.local_path,
            "description": dict(self.description),
            "evaluation": dict(self.evaluation),
            "message": self.message,
            "attempts": self.attempts,
            "pending": dict(self.pending),
            "usage": dict(self.usage),
            "context": dict(self.context),
            "steps": [step.to_dict() for step in self.steps],
        }


class AgentRuntime:
    """六步流水线的执行者。一个实例服务整个应用。"""

    def __init__(
        self,
        *,
        registry,
        generation: GenerationService,
        bus: EventBus,
        history=None,
        prompt_store=None,
        max_refine_rounds: int = MAX_REFINE_ROUNDS,
    ) -> None:
        self._registry = registry
        self._generation = generation
        self._bus = bus
        self._history = history
        self._prompt_store = prompt_store       # 提示词补丁（自更新）；None = 永远出厂行为
        self.max_refine_rounds = max_refine_rounds
        self._runs: dict[str, Job] = {}

    def start(
        self,
        request: str,
        *,
        kind: str | None = None,
        approved: Iterable[str] = (),
        force: bool = False,
        context: Mapping[str, Any] | None = None,
    ) -> JobHandle:
        """起一次运行，返回可取消的句柄（与生成任务同一套语义）。

        `approved` 是用户已明确批准的**工具名**（界面上的「允许并重跑」）：
        它只影响审批闸门，不放宽预算、不改变阶段白名单——所以批准一次上传，
        不会顺带把别的闸门打开。

        `force` 是用户说「不补了，直接开跑」：**只**跳过「信息够不够」这一道追问，
        别的闸门（预算、白名单、审批）一个都不放宽。

        `context` 是用户改完确认的那份上下文（`draft()` 的返回值原样传回来即可）：
        给了它就**不再重新理解、也不再重新检索**——理解与找参考在出草稿时已经做过，
        用户看到的、改过的那份就是这次运行要用的。
        """
        run = Job(tool="agent.run", params={"request": request, "kind": kind or ""})
        run.context["allowed_tools"] = tools_of("understand")
        if approved:
            run.context["approved"] = set(approved)
        task = asyncio.create_task(
            self.run(request, kind=kind, run=run, force=force, context=context)
        )
        self._runs[run.id] = run
        return JobHandle(run, task)

    async def draft(
        self,
        request: str,
        *,
        kind: str | None = None,
        force: bool = False,
        sources: Mapping[str, bool] | None = None,
    ) -> dict[str, Any]:
        """只跑「理解 + 找参考」，交出一份用户可编辑的上下文草稿（不生成、不落运行记录）。

        为什么单独跑这两步：用户要能改的正是这两步的产物。等生成完再让他改，只能重跑。
        """
        job = Job(tool="agent.draft", params={"request": request, "kind": kind or ""})
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        job.context["allowed_tools"] = tools_of("understand")
        steps: list[StepRecord] = []
        effective_sources = {**DEFAULT_SOURCES, **dict(sources or {})}
        self._emit(job, "agent.started", request=request)

        with self._phase(job, "understand"):
            understanding = await self._understand(job, request, kind, steps, force=force)
        base = {
            "requirement": understanding["requirement"],
            "kind": understanding["kind"],
            "aspect": understanding["aspect"],
            "items": [],
            "sources": effective_sources,
            "decide": {},
            "steps": [step.to_dict() for step in steps],
        }
        if understanding["needs_input"]:
            return {
                **base,
                "status": understanding.get("status", "needs_input"),
                "message": understanding["question"],
            }

        with self._phase(job, "prepare"):
            prepared = await self._prepare(
                job,
                understanding["requirement"],
                understanding["kind"],
                steps,
                sources=effective_sources,
                reference_need=int(understanding.get("reference_need") or 0),
                wants_web=bool(understanding.get("wants_web")),
            )
        return {
            **base,
            "status": "ok",
            "message": "",
            "items": [item.to_dict() for item in prepared["items"]],
            "decide": prepared["decide"],
            "steps": [step.to_dict() for step in steps],
        }

    def run_of(self, run_id: str) -> Job | None:
        return self._runs.get(run_id)

    # ---------------------------------------------------------------- 主流程

    async def run(
        self,
        request: str,
        *,
        kind: str | None = None,
        run: Job | None = None,
        force: bool = False,
        context: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        run = run or Job(tool="agent.run", params={"request": request})
        run.status = JobStatus.RUNNING
        run.started_at = time.time()
        self._emit(run, "agent.started", request=request)

        steps: list[StepRecord] = []
        result = AgentResult(run_id=run.id, status="failed", requirement=request)
        result.steps = steps
        generation: dict[str, Any] = {}
        # 混合检索的两个输入：需要几条参考、用户有没有点名要联网（草稿路径已问过，这里兜底）
        reference_need = 0
        wants_web = False
        # 自动模式（没有草稿）时，来源开关取设置里存的默认值——否则「记住为默认」只在
        # 草稿模式下生效，切到自动模式就又全开了。
        settings_sources = {**DEFAULT_SOURCES, **dict(settings.get("context_sources", {}) or {})}

        try:
            if context is not None:
                # 用户确认过草稿：理解与找参考都做过了，按那份上下文直接往下跑
                base_requirement = str(context.get("requirement") or request).strip()
                notes = str(context.get("notes") or "").strip()
                requirement = (
                    f"{base_requirement}。补充要求：{notes}" if notes else base_requirement
                )
                result.requirement = requirement
                result.kind = str(context.get("kind") or kind or "image")
                aspect = str(context.get("aspect") or "16:9")
                sources_used = {**DEFAULT_SOURCES, **dict(context.get("sources") or {})}
            else:
                # 1 理解
                with self._phase(run, "understand"):
                    understanding = await self._understand(run, request, kind, steps, force=force)
                if understanding["needs_input"]:
                    result.status = understanding.get("status", "needs_input")
                    result.message = understanding["question"]
                    return self._finish(run, result)
                base_requirement = understanding["requirement"]
                notes = ""
                result.requirement = base_requirement
                result.kind = understanding["kind"]
                aspect = understanding["aspect"]
                reference_need = int(understanding.get("reference_need") or 0)
                wants_web = bool(understanding.get("wants_web"))
                sources_used = settings_sources

            # 2 备参（有上下文就用用户那份，不再检索）
            with self._phase(run, "prepare"):
                if context is not None:
                    prepared = prepare_from_context(context, kind=result.kind)
                    steps.append(
                        StepRecord(
                            "prepare",
                            "找参考",
                            detail=prepared["detail"],
                            data={
                                "references": prepared["references"],
                                "prompts": prepared["hints"],
                            },
                        )
                    )
                else:
                    prepared = await self._prepare(
                        run,
                        result.requirement,
                        result.kind,
                        steps,
                        sources=sources_used,
                        reference_need=reference_need,
                        wants_web=wants_web,
                    )
            references = prepared["references"]
            style_hints = prepared["hints"]
            result.references = references
            # 留一份「这次实际用了什么」：自动模式跑完拿它落快照，「改参考再跑一次」才有底本。
            result.context = {
                "requirement": base_requirement,
                "notes": notes,
                "items": [item.to_dict() for item in prepared["items"]],
                "sources": dict(sources_used),
                "decide": dict(prepared.get("decide") or {}),
                "kind": result.kind,
                "aspect": aspect,
            }
            prompt = await self._compose_prompt(run, result.requirement, style_hints, steps)

            # 3~5 生成 → 评估 → 精修
            round_index = 0
            while True:
                round_index += 1
                result.attempts = round_index

                with self._phase(run, "generate"):
                    generation = await self._generate(
                        run, prompt, references, result.kind, aspect, steps
                    )
                if generation["status"] != "ok":
                    result.status = generation["status"]
                    result.message = generation["message"]
                    result.prompt = prompt
                    return self._finish(run, result)
                result.prompt = prompt
                result.result_url = generation["url"]
                result.local_path = generation["local_path"]

                with self._phase(run, "evaluate"):
                    evaluation = await self._evaluate(
                        run, result.requirement, generation, steps, kind=result.kind
                    )
                result.description = evaluation["description"]
                result.evaluation = evaluation["verdict"]

                decision = evaluation["verdict"]["decision"]
                self._emit(run, "agent.decision", round=round_index, **evaluation["verdict"])
                if decision == "accept" or round_index > self.max_refine_rounds:
                    break
                if decision == "confirm":
                    result.status = "needs_confirmation"
                    result.message = evaluation["verdict"].get("reason", "评估不确定，需要你确认")
                    return self._finish(run, result)

                with self._phase(run, "refine"):
                    prompt = await self._refine(
                        run, result.requirement, prompt, evaluation, style_hints, steps
                    )

            # 6 交付
            with self._phase(run, "deliver"):
                await self._deliver(run, generation, result, steps)
            result.status = "succeeded"
            result.message = "已完成"
            return self._finish(run, result)

        except NeedsApproval as exc:
            steps.append(StepRecord("deliver", "等待人工确认", ok=False, detail=exc.message))
            result.status = "needs_approval"
            result.message = exc.message
            result.pending = {"kind": "approval", "tool": exc.tool, "params": dict(exc.params)}
            return self._finish(run, result)
        except BudgetExceeded as exc:
            steps.append(StepRecord("deliver", "超出预算", ok=False, detail=exc.message))
            result.status = "budget_exceeded"
            result.message = exc.message
            result.pending = {
                "kind": "budget",
                "tool": exc.tool,
                "used": exc.used,
                "limit": exc.limit,
            }
            return self._finish(run, result)
        except asyncio.CancelledError:
            steps.append(StepRecord("deliver", "已取消", ok=False))
            result.status = "canceled"
            run.status = JobStatus.CANCELED
            self._emit(run, "agent.finished", status="canceled", message="已取消")
            raise
        except AppError as exc:
            steps.append(StepRecord("deliver", "失败", ok=False, detail=str(exc)))
            result.status = "failed"
            result.message = getattr(exc, "user_message", str(exc))
            return self._finish(run, result)
        except Exception as exc:              # 兜底：一次运行绝不允许「静默消失」
            # 界面打包后没有控制台，异常要是直接冒出去，用户看到的只是「点了没反应」。
            # 所以这里把意料之外的异常也收成一次**看得见的失败**，并留一份现场到日志。
            log_error("agent.run", exc)
            detail = f"{type(exc).__name__}: {exc}"
            steps.append(StepRecord("deliver", "运行出错", ok=False, detail=detail))
            result.status = "failed"
            result.message = f"运行出错（{type(exc).__name__}）：{exc}"
            return self._finish(run, result)

    # ---------------------------------------------------------------- 各阶段

    async def _understand(
        self,
        run: Job,
        request: str,
        kind: str | None,
        steps: list[StepRecord],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        patch = self._patch()
        answers = await self._ask(
            run,
            {"request": request},
            understand_questions(request, overrides=patch.get("question_overrides")),
        )
        enough = answers.yes("enough")
        enough_score = answers.noul("enough")
        # 第一步是类型，第二步是可行性，第三步才是「信息够不够」——顺序就是判断的顺序：
        # 类型判错，后面全歪（「帮我想个剧本」被当出视频，编出来的画面提示词毫无意义）。
        task = answers.choice("task") or ""
        feasible = answers.yes("feasible")
        chosen_kind = kind or ("video" if task == "video" else "image")
        # 画幅兜底顺序：模型判断 → 补丁里的用户偏好 → 16:9
        aspect = answers.choice("aspect") or patch.get("aspect_preference") or "16:9"
        # 第三期加的两问：需要几条参考、用户有没有点名要联网。
        # 这两项决定「要不要联网」，所以必须在这一步就问清楚——等找完参考再问就晚了。
        reference_need = _reference_need_of(answers.choice("reference_need"))
        wants_web = answers.yes("web_search") is True
        out_of_scope = task in ("text", "other") or feasible is False
        needs_input = (out_of_scope or enough is False) and not force
        steps.append(
            StepRecord(
                "understand",
                "理解需求",
                ok=not needs_input,
                detail=(
                    f"类型={_TASK_LABELS.get(task, chosen_kind)}"
                    + ("" if out_of_scope else f" 画幅={aspect}")
                    + f" 充分度={enough_score}"
                    + (" · 你点名要联网" if wants_web else "")
                    + (" · 我做不了这个" if out_of_scope else "")
                    + ("（你选择直接开跑）" if force and enough is False else "")
                ),
                data={
                    "enough": enough_score,
                    "task": task,
                    "feasible": feasible,
                    "kind": chosen_kind,
                    "aspect": aspect,
                    "reference_need": reference_need,
                    "web_search": wants_web,
                },
            )
        )
        if out_of_scope and not force:
            # 不是出图/出视频的活（或者模型判了「做不到」）：说清「我做不了这个」，
            # 而不是编一条画面提示词硬跑
            return {
                "needs_input": True,
                "status": "out_of_scope",
                "question": _out_of_scope_message(request.strip(), task),
                "requirement": request.strip(),
                "kind": chosen_kind,
                "aspect": aspect,
            }
        return {
            "needs_input": needs_input,
            "question": _needs_input_message(request.strip(), enough_score),
            "requirement": request.strip(),
            "kind": chosen_kind,
            "aspect": aspect,
            "reference_need": reference_need,
            "wants_web": wants_web,
        }

    async def _prepare(
        self,
        run: Job,
        requirement: str,
        kind: str,
        steps: list[StepRecord],
        *,
        sources: Mapping[str, bool] | None = None,
        reference_need: int = 0,
        wants_web: bool = False,
    ) -> dict[str, Any]:
        """找参考：按来源开关取用，产出「参考图 + 提示词线索 + 可编辑的条目」。

        关掉的来源**根本不检索**（省额度省时间），不是查完再藏起来。
        顺序是先历史（A-RAG）再知识库：两者产出同一形状，命中的条目一起摊在上下文卡上，
        用户删哪条都只删那条。

        第三期加的是最后一段：本地查完先数一数够不够（阈值取用户设置与模型判断里更高的），
        不够就联网补齐、用户点名要联网就直接联网。**为什么联网 / 为什么没联网**会写进
        `decide["reason"]`，草稿卡顶上一眼看得到——省额度这件事不该是黑箱。
        """
        effective = {**DEFAULT_SOURCES, **dict(sources or {})}
        references: list[str] = []
        hints: list[str] = []
        items: list[ContextItem] = []
        decide: dict[str, Any] = {}
        notes: list[str] = []
        failed = False

        if effective.get("history", True):
            found = await self._call(run, "rag.search",
                                     {"requirement": requirement, "kind": kind, "top_k": 3})
            if found is None:
                decide["history"] = False
                notes.append("历史检索不可用")
                failed = True
            else:
                found_references = [str(ref) for ref in (found.get("references") or [])]
                found_prompts = [str(text) for text in (found.get("prompts") or [])]
                references.extend(found_references)
                hints.extend(found_prompts)
                items.extend(
                    ContextItem(kind="history", ref=ref, title=f"历史参考图 {index}",
                                origin="历史作品")
                    for index, ref in enumerate(found_references, 1)
                )
                items.extend(
                    ContextItem(kind="history_text", ref=text, title=text[:40],
                                origin="历史提示词片段")
                    for text in found_prompts
                )
                decide["history"] = True
                decide["history_count"] = int(found.get("count") or 0)
                notes.append(
                    f"历史 {found.get('count', 0)} 条（参考图 {len(found_references)} 张"
                    + ("，已重排）" if found.get("reranked") else "，按元数据顺序）")
                )
        else:
            decide["history"] = False
            notes.append("历史参考已关闭")

        if effective.get("knowledge", True):
            found = await self._call(run, "kb.search", {"requirement": requirement, "top_k": 3})
            if found is None:
                decide["knowledge"] = False
                notes.append("知识库不可用")
                failed = True
            else:
                fragments = [str(text) for text in (found.get("fragments") or [])]
                sources_meta = list(found.get("sources") or [])
                hints.extend(fragments)
                for index, text in enumerate(fragments):
                    meta = sources_meta[index] if index < len(sources_meta) else {}
                    name = str(meta.get("name") or "知识库")
                    ordinal = int(meta.get("ordinal") or 0)
                    items.append(
                        ContextItem(kind="kb", ref=text, title=text[:40],
                                    origin=f"知识库《{name}》第 {ordinal + 1} 片")
                    )
                decide["knowledge"] = True
                decide["knowledge_count"] = int(found.get("count") or 0)
                notes.append(
                    f"知识库 {found.get('count', 0)} 片"
                    + ("（已重排）" if found.get("reranked") else "")
                )
        else:
            decide["knowledge"] = False
            notes.append("知识库已关闭")

        # ---- 混合检索：本地不够才联网（方案四）。
        # 阈值取「用户设置」与「模型判断」里更高的那个：模型可以要求更多参考，
        # 但**不能把用户设的下限压低**——那等于用户白设了。
        user_threshold = int(settings.get("min_local_refs", DEFAULT_MIN_LOCAL_REFS) or 0)
        threshold = max(user_threshold, int(reference_need or 0))
        local_hits = len(references) + sum(1 for item in items if item.kind == "kb")
        want_images = bool(effective.get("web_images"))
        decide["reference_need"] = int(reference_need or 0)
        decide["min_local_refs"] = user_threshold
        decide["threshold"] = threshold
        decide["local_hits"] = local_hits
        decide["web_images"] = False       # 默认「没下」；真下到图才置 True

        if not effective.get("web", True):
            decide["web"] = False
            notes.append(f"联网线索已关闭（本地命中 {local_hits} 条）")
        elif local_hits >= threshold and not wants_web:
            decide["web"] = False
            notes.append(
                f"本地命中 {local_hits} 条 ≥ 阈值 {threshold}，没联网（省额度）"
            )
        else:
            why = (
                "你点名要联网，本地够也搜"
                if (wants_web and local_hits >= threshold)
                else (
                    "本地没有命中，自动联网补齐"
                    if local_hits == 0
                    else f"本地只有 {local_hits} 条（< 阈值 {threshold}），自动联网补齐"
                )
            )
            found = await self._call(
                run,
                "web.search",
                {"query": requirement, "max_results": 3, "want_images": want_images},
            )
            if found is None:
                decide["web"] = False
                notes.append(f"{why}，但联网搜索这一步没能执行")
                failed = True
            elif not found.get("ok"):
                # 没配 key / provider 出错：不假装搜过，原话交代清楚
                decide["web"] = False
                notes.append(f"{why}，但联网没成：{found.get('reason') or '未知原因'}")
            else:
                decide["web"] = True
                web_rows = [row for row in (found.get("items") or []) if isinstance(row, Mapping)]
                for row in web_rows:
                    title = str(row.get("title") or "").strip()
                    url = str(row.get("url") or "").strip()
                    snippet = str(row.get("snippet") or "").strip()
                    hints.append(snippet or title)
                    items.append(
                        ContextItem(
                            kind="web",
                            ref=snippet or title,
                            title=title[:60] or url,
                            origin=f"外部线索 · {url}",
                            meta={"url": url},
                        )
                    )
                decide["web_count"] = len(web_rows)
                notes.append(f"{why}：{found.get('reason') or f'搜到 {len(web_rows)} 条'}")

                # 联网配图：开关关着时**连候选图都不请求**，更不会下载
                url_pool = list(found.get("images") or []) if want_images else []
                saved = await self._fetch_web_images(run, url_pool, references, items)
                if saved:
                    decide["web_images"] = True
                    notes.append(
                        f"联网配图 {len(saved)} 张（版权归原站，见免责声明）"
                    )

        detail = " · ".join(notes) or "没有来源可查"
        steps.append(
            StepRecord(
                "prepare",
                "找参考",
                ok=not failed,
                detail=detail,
                data={"references": references, "prompts": hints},
            )
        )
        return {
            "references": references,
            "hints": hints,
            "items": items,
            "detail": detail,
            "decide": {**decide, "reason": detail},
        }

    async def _fetch_web_images(
        self,
        run: Job,
        urls: list[str],
        references: list[str],
        items: list[ContextItem],
    ) -> list[str]:
        """下载联网配图（最多 4 张，闸门另有预算）。单张失败就跳过，不让整条流程挂掉。"""
        saved: list[str] = []
        for url in urls[:4]:
            try:
                result = await self._registry.invoke(
                    "web.fetch_image", {"url": url}, context=run.context
                )
            except AppError as exc:
                log_error("web.fetch_image", exc)
                continue
            path = str((result or {}).get("path") or "")
            if not path:
                continue
            references.append(path)
            items.append(
                ContextItem(
                    kind="web_image",
                    ref=path,
                    title=f"联网配图 {len(saved) + 1}",
                    origin=f"外部图片来源：{url}",
                    meta={"url": url},
                )
            )
            saved.append(path)
        return saved

    async def _call(self, run: Job, tool: str, params: Mapping[str, Any]) -> dict | None:
        """调一项能力；不可用就返回 None（检索失败不该让整条流程挂掉）。"""
        try:
            return await self._registry.invoke(tool, params, context=run.context)
        except AppError:
            return None

    async def _compose_prompt(
        self, run: Job, requirement: str, style_hints: list[str], steps: list[StepRecord]
    ) -> str:
        """写提示词：先试生成模型，不可用就直接用需求原文（不让流程卡住）。"""
        patch = self._patch()
        prompt = requirement
        try:
            text = await self._registry.invoke(
                "llm.chat",
                {"messages": composer_messages(
                    requirement,
                    style_hints=style_hints,
                    suffix=patch.get("composer_suffix", ""),
                    aspect_preference=patch.get("aspect_preference", ""),
                )},
                context=self._context_with(run, "refine"),     # 写提示词属于精修类动作
            )
            prompt = str(text).strip() or requirement
        except AppError as exc:
            steps.append(
                StepRecord(
                    "prepare", "写提示词", ok=False,
                    detail=f"生成模型不可用，直接用原需求（{exc.kind}）",
                )
            )
        steps.append(StepRecord("prepare", "写提示词", detail=prompt[:60]))
        return prompt

    async def _generate(
        self,
        run: Job,
        prompt: str,
        references: list[str],
        kind: str,
        aspect: str,
        steps: list[StepRecord],
    ) -> dict[str, Any]:
        started = time.time()
        if kind == "video":
            from app.clients.video_client import VideoRequest

            handle = self._generation.start_video(
                VideoRequest(prompt=prompt, images=tuple(references), aspect_ratio=aspect),
                context=run.context,
                parent_job_id=run.id,
            )
        else:
            handle = self._generation.start_image(
                ImageRequest(prompt=prompt, images=tuple(references)),
                context=run.context,
                parent_job_id=run.id,
            )

        job = await handle.wait()
        seconds = time.time() - started
        if job.status is JobStatus.CANCELED:
            return {"status": "canceled", "message": "已取消", "url": "", "local_path": "", "job_id": job.id}
        if (
            job.status is not JobStatus.SUCCEEDED
            and kind == "video"
            and references
            and _material_url_rejected(job.error)
        ):
            # 平台取不到那张参考图（链接过期 / 格式不接受）：退回纯文本再试一次。
            # 实测踩到过：历史里唯一那条视频记录的 mp4 地址被当成参考图塞进视频接口，
            # 平台直接 400，用户拿到的是一次「本来能出片、被一张参考图拖死」的失败。
            steps.append(
                StepRecord(
                    "generate",
                    "生成（去掉参考图重试）",
                    detail=f"参考图被平台拒了：{job.error}",
                    data={"job_id": job.id},
                )
            )
            started = time.time()
            handle = self._generation.start_video(
                VideoRequest(prompt=prompt, images=(), aspect_ratio=aspect),
                context=run.context,
                parent_job_id=run.id,
            )
            job = await handle.wait()
            seconds = time.time() - started
            if job.status is JobStatus.CANCELED:
                return {
                    "status": "canceled", "message": "已取消",
                    "url": "", "local_path": "", "job_id": job.id,
                }
        if job.status is not JobStatus.SUCCEEDED:
            steps.append(StepRecord("generate", "生成", ok=False, detail=job.error, seconds=seconds))
            # 闸门是在子任务里拦下的，所以这里要按错误类型还原成运行级状态：
            # 预算不足与需要审批都不是「失败」，界面要分别处理
            if job.error_kind == "budget":
                return {
                    "status": "budget_exceeded",
                    "message": job.error,
                    "url": "", "local_path": "", "job_id": job.id,
                }
            if job.error_kind == "approval":
                return {
                    "status": "needs_approval",
                    "message": job.error,
                    "url": "", "local_path": "", "job_id": job.id,
                }
            return {
                "status": "failed",
                "message": job.error or "生成失败",
                "url": "",
                "local_path": "",
                "job_id": job.id,
            }

        payload = job.result or {}
        steps.append(
            StepRecord(
                "generate",
                "生成",
                detail=str(payload.get("url", ""))[:80],
                data={"job_id": job.id},
                seconds=seconds,
            )
        )
        return {
            "status": "ok",
            "message": "",
            "url": str(payload.get("url") or ""),
            "local_path": str(payload.get("media_path") or ""),
            "job_id": job.id,
        }

    async def _evaluate(
        self,
        run: Job,
        requirement: str,
        generation: Mapping[str, Any],
        steps: list[StepRecord],
        *,
        kind: str = "image",
    ) -> dict[str, Any]:
        started = time.time()
        if kind == "video":
            # 视频没法自动评判：视觉模型只吃图片（DeepSeek / DashScope / 智谱那套
            # OpenAI 兼容接口都不收 mp4），抽帧又要新依赖。所以**根本不去试**——
            # 试了只会白烧一次视觉调用，还会把原因说成「视觉模型不可用」（它其实好着）。
            steps.append(
                StepRecord(
                    "evaluate",
                    "评估",
                    ok=False,
                    detail="视频暂不能自动评估（只能看图），直接交给你判断",
                )
            )
            return {
                "description": {},
                "verdict": {
                    "decision": "confirm",
                    "reason": "视频已经出好了，但自动评估只支持图片——请你自己看一眼",
                },
            }
        description, failure = await self._describe(run, requirement, generation)
        if description is None:
            # 看不了图就退化成「交给用户判断」，绝不假装通过
            steps.append(
                StepRecord("evaluate", "评估", ok=False, detail=f"视觉不可用：{failure}")
            )
            return {
                "description": {},
                "verdict": {
                    "decision": "confirm",
                    "reason": "视觉模型不可用，无法自动评估，请你确认是否可用",
                },
            }

        answers = await self._ask(
            run,
            {"requirement": requirement, "image_description": description},
            evaluate_questions(requirement, overrides=self._patch().get("question_overrides")),
        )
        quality = answers.score("quality")
        confidence = answers.confidence("quality")
        verdict: dict[str, Any] = {
            "fits": answers.noul("fits"),
            "quality": quality,
            "confidence": confidence,
            "fix": answers.choice("fix"),
            "band": confidence_band(confidence),
        }
        if (
            answers.yes("fits")
            and (quality or 0) >= ACCEPT_QUALITY
            and (confidence or 1) >= CONFIRM_CONFIDENCE
        ):
            verdict["decision"] = "accept"
        elif confidence is not None and confidence < CONFIRM_CONFIDENCE:
            verdict["decision"] = "confirm"
            verdict["reason"] = "模型对这张图的判断不确定，请你确认"
        else:
            verdict["decision"] = "refine"

        steps.append(
            StepRecord(
                "evaluate",
                "评估",
                detail=(
                    f"符合度={verdict['fits']} 可用度={quality} "
                    f"置信度={confidence} → {verdict['decision']}"
                ),
                data=verdict,
                seconds=time.time() - started,
            )
        )
        return {"description": dict(description), "verdict": verdict}

    async def _describe(
        self, run: Job, requirement: str, generation: Mapping[str, Any]
    ) -> tuple[dict | None, str]:
        """描述成图。返回 (描述, 失败原因)；失败原因只在不成功时有意义。

        优先用本地缓存文件（视觉客户端会把它缩到 320px 再发，实测比发原图快 14.5 倍）。
        本地文件读不出来时（下载被截断、格式罕见）**退回远端地址再试一次**——
        让判断少一次，总好过让整轮评估直接没了。这一退只在「解码失败」这类问题上做，
        认证、网络这类错误退也没用，只会白烧一次额度。
        """
        targets = [
            target
            for target in (str(generation.get("local_path") or ""), str(generation.get("url") or ""))
            if target
        ]
        reason = "没有可评估的图片"
        for index, target in enumerate(targets):
            try:
                described = await self._registry.invoke(
                    "vision.describe",
                    {"image": target, "requirement": requirement},
                    context=run.context,
                )
                return dict(described), ""
            except AppError as exc:
                reason = exc.kind
                if index + 1 < len(targets) and exc.kind == "validation":
                    continue
                break
        return None, reason       # 用 None 而不是 {}：空描述不是「描述成功」

    async def _refine(
        self,
        run: Job,
        requirement: str,
        prompt: str,
        evaluation: Mapping[str, Any],
        style_hints: list[str],
        steps: list[StepRecord],
    ) -> str:
        verdict = evaluation.get("verdict") or {}
        description = evaluation.get("description") or {}
        patch = self._patch()
        try:
            text = await self._registry.invoke(
                "llm.chat",
                {
                    "messages": composer_messages(
                        requirement,
                        style_hints=style_hints,
                        previous_prompt=prompt,
                        evaluation=verdict,
                        description=description,
                        suffix=patch.get("composer_suffix", ""),
                        aspect_preference=patch.get("aspect_preference", ""),
                    )
                },
                context=run.context,
            )
            refined = str(text).strip() or prompt
        except AppError as exc:
            # 改不动就保持原提示词再来一轮，而不是直接失败
            steps.append(StepRecord("refine", "精修", ok=False, detail=f"生成模型不可用（{exc.kind}）"))
            return prompt
        steps.append(
            StepRecord("refine", "精修", detail=refined[:60], data={"fix": verdict.get("fix")})
        )
        return refined

    async def _deliver(
        self,
        run: Job,
        generation: Mapping[str, Any],
        result: AgentResult,
        steps: list[StepRecord],
    ) -> None:
        """把这次运行的判断写进历史记录的 meta（供回放与自更新使用）。"""
        record = self._record_for_job(str(generation.get("job_id") or ""))
        if record is not None and self._history is not None:
            meta = dict(record.meta or {})
            meta["agent"] = {
                "run_id": run.id,
                "requirement": result.requirement,
                "prompt": result.prompt,
                "attempts": result.attempts,
                "evaluation": result.evaluation,
                "description": result.description,
            }
            try:
                self._history.update(record.id, meta=meta)
            except Exception:
                pass
        steps.append(StepRecord("deliver", "交付", detail=result.result_url[:80]))

    # ---------------------------------------------------------------- 辅助

    def _context_with(self, run: Job, phase_key: str) -> dict:
        """按另一个阶段的白名单调用（写提示词属于精修类动作）。"""
        context = dict(run.context)
        context["allowed_tools"] = tools_of(phase_key)
        return context

    def _patch(self) -> dict:
        """当前生效的提示词补丁（没有 store 或没有生效补丁 = 出厂行为）。"""
        if self._prompt_store is None:
            return {}
        try:
            return self._prompt_store.current()
        except Exception:                         # 补丁读不出来不该拖垮一次运行
            return {}

    def _phase(self, run: Job, phase_key: str):
        """进入某阶段：设置工具白名单并广播事件。"""
        phase: Phase | None = PHASE_BY_KEY.get(phase_key)
        runtime = self

        class _Scope:
            def __enter__(self_inner):
                run.context["allowed_tools"] = tools_of(phase_key)
                runtime._emit(
                    run, "agent.phase", phase=phase_key, title=phase.title if phase else phase_key
                )
                return self_inner

            def __exit__(self_inner, *_exc):
                return False

        return _Scope()

    async def _ask(self, run: Job, state: Mapping[str, Any], questions: Mapping[str, Any]):
        payload = await self._registry.invoke(
            "judge.ask",
            {
                "state": dict(state),
                "questions": {key: dict(value) for key, value in questions.items()},
            },
            context=run.context,
        )
        return _JudgeView(payload)

    def _record_for_job(self, job_id: str):
        if self._history is None or not job_id:
            return None
        for record in self._history.list(limit=20):
            if record.job_id == job_id:
                return record
        return None

    def _emit(self, run: Job, type_: str, **payload: Any) -> Event:
        return self._bus.emit(Event(type=type_, job_id=run.id, payload=dict(payload)))

    def _finish(self, run: Job, result: AgentResult) -> AgentResult:
        if run.status is JobStatus.RUNNING:
            run.status = JobStatus.SUCCEEDED if result.ok else JobStatus.FAILED
        run.finished_at = time.time()
        result.usage = dict(run.context.get("usage") or {})
        run.result = result.to_dict()
        run.message = result.message
        self._emit(run, "agent.finished", status=result.status, message=result.message)
        return result


class _JudgeView:
    """把注册表返回的判断结果包一层，方便代码取值。"""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._answers = (payload or {}).get("answers") or {}

    def _answer(self, key: str) -> dict:
        return self._answers.get(key) or {}

    def noul(self, key: str) -> float | None:
        value = self._answer(key).get("noul")
        return float(value) if isinstance(value, (int, float)) else None

    def yes(self, key: str) -> bool | None:
        value = self.noul(key)
        return None if value is None else value >= 0.5

    def choice(self, key: str) -> str | None:
        value = self._answer(key).get("choice")
        return str(value) if value is not None else None

    def score(self, key: str) -> float | None:
        value = self._answer(key).get("score")
        return float(value) if isinstance(value, (int, float)) else None

    def confidence(self, key: str) -> float | None:
        value = self._answer(key).get("confidence")
        return float(value) if isinstance(value, (int, float)) else None


def _needs_input_message(request: str, enough: float | None) -> str:
    """「信息不够」时给用户的话：说清**我判的是哪一句、判了多少分**。

    只说「能再具体一点吗」是不够的：用户补完一句再点，画面跟上次一模一样，根本看不出
    这一下有没有生效、卡在哪。把判定的原文和分数摆出来，用户才知道该怎么补。
    """
    score = "模型没给分" if enough is None else f"{enough:.2f}"
    return (
        "能再具体一点吗？比如：主体是什么、想要什么风格、用在什么场合"
        "（竖版海报还是横版配图）。\n"
        f"我判它「还不够开工」：充分度 {score}（低于 0.5）。"
        f"判定用的原文是：「{request}」\n"
        "补的时候可以直接写一整句更具体的需求，我会把它跟上面这句合起来看；"
        "也可以点「不补了，直接开跑」。"
    )


def _reference_need_of(raw: Any) -> int:
    """把模型给的「要几条参考」压成 0~3 的整数。

    模型可能回 `"2"` / `"3+"` / `"3 条以上"` / 一句认不出的话。这里按最保守的方式解析：
    取第一个数字，出现「+ / 以上 / 多」就按 3 算，认不出来就是 0——
    宁可少联网（用户设置的下限还在），也不因为一句没读懂的话就去烧额度。
    """
    text = str(raw or "").strip()
    if not text:
        return 0
    numbers = re.findall(r"\d+", text)
    value = int(numbers[0]) if numbers else 0
    if any(token in text for token in ("+", "以上", "多", "尽")):
        value = max(value, 3)
    return max(0, min(3, value))


#: 任务类型的中文说法（Jev 判出来的类型 → 时间线与提示语里给人看的词）
_TASK_LABELS = {
    "image": "出图",
    "video": "出视频",
    "text": "写文字",
    "other": "其他事情",
}

#: 判成这些类型就是「我做不了」
_OUT_OF_SCOPE_TASKS = ("text", "other")


def _out_of_scope_message(request: str, task: str) -> str:
    """「这不是出图/出视频的活」时给用户的话。

    这类需求（写剧本、写文案、答疑）硬跑只会得到一条跑偏的提示词和一张莫名其妙的图，
    所以直接说清能力边界，同时留一条路：补充一句画面需求，或者按原话硬跑。
    """
    what = {
        "text": "你要的是文字产物（剧本、文案这类）",
        "other": "这件事不是出图或出视频",
    }.get(task, "这件事要求的效果我做不出来")
    return (
        f"这件事我做不了：{what}，而我会的只有「出一张图」和「出一段视频」。\n"
        f"你说的是：「{request}」\n"
        "要是你其实是想让我把它做成一幅画面（或一段视频），把画面说具体一点再来一次——"
        "例如「雨夜山门，月光，修仙少年，电影感」。"
        "也可以点「不补了，直接开跑」，我就按原话硬跑一次。"
    )


#: 平台把「参考图取不到」这类错误写在这些词里（实测：素材 URL 无法下载或是不支持的媒体格式）
_MATERIAL_HINTS = ("素材", "媒体格式", "参考图")


def _material_url_rejected(message: str) -> bool:
    """错误是不是「那张参考图平台用不了」。"""
    text = message or ""
    return any(hint in text for hint in _MATERIAL_HINTS)
