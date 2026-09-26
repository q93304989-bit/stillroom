"""A-RAG：从自己的历史里找「可用的参考」，用于对生成内容做定向强化。

关键取舍（与通用 RAG 不同）：**检索的产出不是一段说明文字，而是「参考图 + 提示词片段」**
——这两样正好是生成接口原生就吃的东西（图片接口支持最多 5 张参考图，提示词可直接拼接）。

第一版分两步，都不引入新依赖：

1. **元数据筛候选**（纯 SQL）：只看成功的记录，优先收藏过的，排除用户点过「重试」的。
2. **Jev 重排**：候选一次性打包成一次判断请求（每个候选一个 Score 问题，可并行），
   按分数取前 K。官方实测重排能把候选 top-1 命中率从 5% 提到 18%。

**降级**：Jev 不可用时退化成元数据顺序并标记 `reranked=False`——检索失败不该让生成流程挂掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.clients.typesafe_client import TypeSafeClient, score
from app.net.errors import AppError
from app.services.history import HistoryStore, Record

#: 元数据阶段最多取多少候选（Jev 的 state 有 32k 上限，20~30 足够）
DEFAULT_CANDIDATE_LIMIT = 24
#: 最终交给生成接口的参考图数量（图片接口上限 5 张）
DEFAULT_TOP_K = 5
#: 重排用的分档（要有序，且每档描述能独立看懂）
RERANK_LEVELS = ["不相关", "有点相关", "相关", "很相关"]


@dataclass
class RagResult:
    """检索结果：按相关度排序的记录 + 可直接给生成接口用的素材。"""

    records: list[Record] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    reranked: bool = False
    reason: str = ""            # 降级原因（reranked=False 时说明为什么）

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.records),
            "reranked": self.reranked,
            "reason": self.reason,
            "prompts": list(self.prompts),
            "references": list(self.references),
            "scores": list(self.scores),
            "records": [
                {
                    "id": record.id,
                    "kind": record.kind,
                    "prompt": record.prompt,
                    "tags": list(record.tags),
                    "favorite": record.favorite,
                    "media_path": record.media_path,
                    "result_url": record.result_url,
                }
                for record in self.records
            ],
        }


class RagService:
    """历史检索：元数据筛 + Jev 重排。"""

    def __init__(
        self,
        history: HistoryStore,
        judge: TypeSafeClient | None = None,
        *,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> None:
        self._history = history
        self._judge = judge
        self.candidate_limit = candidate_limit

    # ---------------------------------------------------------------- 第一步：筛候选

    def candidates(self, requirement: str, *, kind: str | None = None) -> list[Record]:
        """元数据筛选：只看成功记录；收藏优先；排除被标记为「要重做」的。

        先用 SQL 拿一批（关键词 / 类型过滤），再在内存里按「收藏 > 有标签 > 时间新」排序——
        这一步不需要模型，快且可解释。

        注意出视频时的取法：**参考图必须是图片**，所以这时去图片记录里找，而不是在视频记录里找。
        实测踩到过：视频任务只挑视频记录 → 唯一命中的是那条 mp4，它的地址被当参考图塞进
        视频接口，平台回 400「素材 URL 无法下载或是不支持的媒体格式」。
        """
        lookup = "image" if kind == "video" else kind
        keyword = (requirement or "").strip()
        records = self._history.list(kind=lookup, keyword=keyword, limit=self.candidate_limit * 3)
        if not records and keyword:
            records = self._history.list(kind=lookup, limit=self.candidate_limit * 3)

        usable = [
            record
            for record in records
            if record.status == "success" and record.last_action != "retry"
        ]
        usable.sort(
            key=lambda record: (
                0 if record.favorite else 1,
                0 if record.tags else 1,
                -record.created_at,
            )
        )
        return usable[: self.candidate_limit]

    # ---------------------------------------------------------------- 第二步：重排

    async def search(
        self,
        requirement: str,
        *,
        kind: str | None = None,
        top_k: int = DEFAULT_TOP_K,
        timeout: float | None = None,
    ) -> RagResult:
        """完整检索：筛候选 → Jev 重排 → 产出参考图与提示词片段。"""
        pool = self.candidates(requirement, kind=kind)
        if not pool:
            return RagResult(reason="历史里没有可用的成功记录")

        ordered, scores, reranked, reason = await self._rerank(requirement, pool, timeout=timeout)
        keep = max(1, top_k)
        chosen = ordered[:keep]

        return RagResult(
            records=chosen,
            scores=scores[:keep],
            prompts=[record.prompt for record in chosen if record.prompt],
            references=[
                ref
                for ref in (self.reference_of(record, kind=kind) for record in chosen)
                if ref
            ],
            reranked=reranked,
            reason=reason,
        )

    async def _rerank(
        self, requirement: str, pool: list[Record], *, timeout: float | None
    ) -> tuple[list[Record], list[float], bool, str]:
        """一次 Jev 请求给所有候选打分；失败则退化成元数据顺序。"""
        if self._judge is None:
            return pool, [0.0] * len(pool), False, "未接入判断模型，按元数据顺序返回"

        questions = {
            f"ref_{index}": score(
                f"这张历史作品适不适合当参考，用来强化这个需求？（需求：{requirement}）",
                RERANK_LEVELS,
            )
            for index, _ in enumerate(pool)
        }
        state = {
            "requirement": requirement,
            "candidates": [
                {
                    "id": record.id,
                    "kind": record.kind,
                    "prompt": record.prompt,
                    "tags": list(record.tags),
                    "favorite": record.favorite,
                    "params": record.params,
                }
                for record in pool
            ],
        }
        try:
            result = await self._judge.ask(state, questions, timeout=timeout or 30)
        except AppError as exc:
            return pool, [0.0] * len(pool), False, f"判断不可用（{exc.kind}），按元数据顺序返回"

        scored: list[tuple[float, float, Record]] = []
        for index, record in enumerate(pool):
            value = result.score(f"ref_{index}")
            confidence = result.confidence(f"ref_{index}") or 0.0
            scored.append((value if value is not None else -1.0, confidence, record))

        # 分数相同用置信度兜底；再相同就保持元数据顺序（收藏在前）
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [record for _, _, record in scored], [value for value, _, _ in scored], True, ""

    # ---------------------------------------------------------------- 素材

    @staticmethod
    def reference_of(record: Record, *, kind: str | None = None) -> str:
        """这条记录能当参考用吗？

        两条硬规则（真实数据一跑就暴露出来的）：

        1. **视频产物不能当参考图**——它的 URL 指向 mp4，图片接口用不了，视频接口的
           参考图字段（`images`）同样不收（实测 400：不支持的媒体格式）。所以它一律不参与。
        2. 图片优先用本地缓存文件，没有本地文件才退回公网 URL。
        """
        wants_video = kind == "video"
        if record.kind == "video":
            return ""
        if kind == "video":
            url = (record.result_url or "").strip()
            return url if url.lower().startswith(("http://", "https://")) else ""
        local = (record.media_path or "").strip()
        if local and Path(local).is_file():
            return local
        url = (record.result_url or "").strip()
        return url if url.lower().startswith(("http://", "https://")) else ""
