"""A-RAG：元数据筛候选 + Jev 重排 + 降级；产出参考图与提示词片段。"""

from __future__ import annotations

import httpx
import pytest

from app.clients.typesafe_client import TypeSafeClient
from app.config.credentials import TypeSafeCredentials
from app.net.http import HttpClient
from app.services.history import HistoryStore, Record
from app.services.rag import RERANK_LEVELS, RagService

CREDS = TypeSafeCredentials(api_key="apikey-ts", model="jev-latest")


def make_history(tmp_path) -> HistoryStore:
    store = HistoryStore(tmp_path / "h.db")
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    (media / "a.png").write_bytes(b"png")

    store.add(Record(id="fav", kind="image", status="success", prompt="中秋海报 竖版", media_path=str(media / "a.png"), created_at=100))
    store.set_feedback("fav", favorite=True, tags=["中秋"], action="accept")
    store.add(Record(id="ok", kind="image", status="success", prompt="中秋 月亮", media_path=str(media / "a.png"), created_at=200))
    store.add(Record(id="retried", kind="image", status="success", prompt="中秋 灯笼", media_path=str(media / "a.png"), created_at=300))
    store.set_feedback("retried", action="retry")
    store.add(Record(id="failed", kind="image", status="failed", prompt="中秋 失败", created_at=400))
    store.add(Record(id="video", kind="video", status="success", prompt="一段海面日落", result_url="https://cdn/v.mp4", created_at=500))
    return store


def judge_with(scores: dict[str, float], confidences: dict[str, float] | None = None) -> TypeSafeClient:
    """造一个「按 id 返回指定分数」的假 Jev。"""
    confidences = confidences or {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        answers = {
            key: {
                "type": "score",
                "score": scores.get(key, 0.0),
                "confidence": confidences.get(key, 0.8),
                "probabilities": {"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.4},
                "legend": {str(index): level for index, level in enumerate(RERANK_LEVELS)},
            }
            for key in body["questions"]
        }
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": answers, "usage": {}})

    return TypeSafeClient(HttpClient(transport=httpx.MockTransport(handler), backoff=0), CREDS)


# --------------------------------------------------------------------------- 元数据筛

def test_candidates_filter_and_order(tmp_path):
    service = RagService(make_history(tmp_path))
    found = service.candidates("中秋", kind="image")
    ids = [record.id for record in found]

    assert "failed" not in ids          # 失败的不算参考
    assert "retried" not in ids         # 用户点过重试的排除
    assert "video" not in ids           # kind 过滤
    assert ids[0] == "fav"              # 收藏优先


def test_candidates_fall_back_when_keyword_misses(tmp_path):
    service = RagService(make_history(tmp_path))
    found = service.candidates("完全不相关的词", kind="image")
    assert found, "关键词没命中时应当回退到不带关键词的候选，而不是返回空"


def test_candidates_respect_limit(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    for index in range(30):
        store.add(Record(id=f"r{index}", status="success", prompt=f"p{index}", created_at=index))
    service = RagService(store, candidate_limit=5)
    assert len(service.candidates("")) == 5


# --------------------------------------------------------------------------- 重排

async def test_search_reranks_and_returns_references(tmp_path):
    store = make_history(tmp_path)
    judge = judge_with({"ref_0": 3.0, "ref_1": 0.5})     # 第 2 个候选更相关
    service = RagService(store, judge)

    result = await service.search("中秋 月亮", kind="image", top_k=2)

    assert result.reranked is True
    assert result.records[0].id == "ok"                   # 被重排到前面
    assert result.scores[0] == pytest.approx(3.0)
    assert result.prompts[0] == "中秋 月亮"
    assert all(reference.endswith(".png") for reference in result.references)


async def test_search_sends_one_batched_request(tmp_path):
    store = make_history(tmp_path)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        import json

        body = json.loads(request.content)
        answers = {
            key: {"type": "score", "score": 1.0, "confidence": 0.9}
            for key in body["questions"]
        }
        return httpx.Response(200, json={"answers": answers})

    judge = TypeSafeClient(HttpClient(transport=httpx.MockTransport(handler), backoff=0), CREDS)
    service = RagService(store, judge)
    await service.search("中秋", kind="image", top_k=2)

    assert len(seen) == 1, "所有候选必须打包成一次请求（官方实测：合并提问便宜 12 倍）"


async def test_search_degrades_without_judge(tmp_path):
    service = RagService(make_history(tmp_path), None)
    result = await service.search("中秋", kind="image", top_k=2)

    assert result.reranked is False
    assert result.reason
    assert result.records[0].id == "fav"                  # 退回元数据顺序（收藏优先）


async def test_search_degrades_when_judge_fails(tmp_path):
    judge = TypeSafeClient(
        HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "bad key"})), backoff=0),
        CREDS,
    )
    result = await RagService(make_history(tmp_path), judge).search("中秋", kind="image")

    assert result.reranked is False
    assert "auth" in result.reason
    assert result.records                                  # 仍然给出结果，不抛异常


async def test_empty_history_returns_empty_result(tmp_path):
    store = HistoryStore(tmp_path / "empty.db")
    result = await RagService(store).search("任何需求")

    assert result.records == []
    assert result.reranked is False
    assert "没有可用" in result.reason


def test_reference_of_prefers_local_then_url(tmp_path):
    local = tmp_path / "a.png"
    local.write_bytes(b"x")
    assert RagService.reference_of(Record(media_path=str(local))) == str(local)

    assert RagService.reference_of(Record(result_url="https://cdn/a.png")) == "https://cdn/a.png"
    assert RagService.reference_of(Record(media_path=str(tmp_path / "缺失.png"))) == ""

    # 视频产物一律不当参考图：mp4 既喂不进图片接口，也喂不进视频接口的参考图字段
    assert RagService.reference_of(Record(kind="video", media_path=str(local)), kind="video") == ""
    assert RagService.reference_of(
        Record(kind="video", result_url="https://cdn/v.mp4"), kind="video"
    ) == ""


def test_video_result_is_never_used_as_image_reference(tmp_path):
    """真实数据跑出来的坑：视频产物的 mp4 链接被当成了参考图。

    一开始只挡住了「图片接口」这一侧，出视频时还允许拿 mp4 当参考；后来真实跑视频又被平台
    挡下来（HTTP 400「素材 URL 无法下载或是不支持的媒体格式」），所以现在**两边都挡**。
    """
    video = Record(kind="video", result_url="https://cdn/v.mp4", media_path=str(tmp_path / "v.mp4"))
    assert RagService.reference_of(video) == ""                 # 不指定类型时按图片用途处理
    assert RagService.reference_of(video, kind="image") == ""
    assert RagService.reference_of(video, kind="video") == ""


async def test_search_for_image_skips_video_references(tmp_path):
    store = make_history(tmp_path)
    judge = judge_with({})                                       # 全部同分，保持元数据顺序
    result = await RagService(store, judge).search("海面日落", top_k=5)

    assert any(record.kind == "video" for record in result.records)      # 视频仍可被检索到（供视频生成用）
    assert all(not reference.endswith(".mp4") for reference in result.references)


async def test_video_target_looks_for_reference_images(tmp_path):
    """出视频时去**图片**记录里找参考：mp4 不能当参考图（平台实测 400）。

    真实踩坑：视频任务只在视频记录里筛候选，唯一命中的就是那条 mp4，它的地址被塞进视频接口的
    `images`，平台回「素材 URL 无法下载或是不支持的媒体格式」，一次本该出片的运行就这么废了。
    """
    store = make_history(tmp_path)
    # 再补一条「有公网地址」的图片：视频参考图必须是图片的公网 URL
    store.add(Record(
        id="public", kind="image", status="success", prompt="海面日落 远景",
        result_url="https://cdn/sunset.png", created_at=600,
    ))
    service = RagService(store, judge_with({}))

    pool = service.candidates("海面日落", kind="video")
    assert pool, "视频任务应该能找到图片素材"
    assert all(record.kind == "image" for record in pool)
    assert not any(record.id == "video" for record in pool)

    result = await service.search("海面日落", kind="video", top_k=5)
    assert result.references, "视频任务也该拿到参考图（图片的公网地址）"
    assert all(reference.startswith("http") for reference in result.references)
    assert all(not reference.endswith(".mp4") for reference in result.references)

    # 只有本地文件的图片当不了视频参考（本地路径喂不进视频接口）
    local_only = Record(kind="image", prompt="本地图", media_path=str(tmp_path / "a.png"))
    assert RagService.reference_of(local_only, kind="video") == ""
