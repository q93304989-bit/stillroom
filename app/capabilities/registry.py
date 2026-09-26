"""能力注册表实现。

一个 `ToolSpec` 描述「能做什么、要什么参数、有什么副作用、有什么限额」；
一个 `handler` 是真正干活的异步函数。两者一起注册，名字唯一。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from app.clients.image_client import (
    DEFAULT_SIZE,
    IMAGE_MODELS,
    IMAGE_SIZES,
    ImageClient,
    ImageRequest,
)
from app.clients.image_host import upload_to_image_host
from app.clients.llm_client import LlmClient
from app.clients.search_client import SearchClient
from app.clients.typesafe_client import TypeSafeClient
from app.clients.vision_client import DEFAULT_IMAGE_SIZE, VisionClient
from app.clients.video_client import (
    ASPECT_RATIOS,
    MAX_REFERENCE_IMAGES,
    SECONDS_OPTIONS,
    VIDEO_MODELS,
    VideoClient,
    VideoRequest,
)
from app.config.credentials import Credentials
from app.net.errors import AppError, ConfigError, ValidationError
from app.net.http import HttpClient

# --------------------------------------------------------------------------- 副作用标记

SIDE_NETWORK = "network"        # 会联网
SIDE_UPLOAD = "upload"          # 会把本地文件传到公网（不可回滚，未来需要人工审批）
SIDE_PAID = "paid"              # 会消耗付费/限免额度
SIDE_DISK_READ = "disk_read"    # 会读本地文件
SIDE_DISK_WRITE = "disk_write"  # 会写本地文件


def _side(*items: str) -> frozenset[str]:
    return frozenset(items)


@dataclass(frozen=True)
class ToolSpec:
    """一项能力的声明。字段与 MCP 的 tool 语义对齐。"""

    name: str
    description: str
    params: dict[str, Any]
    side_effects: frozenset[str] = field(default_factory=frozenset)
    idempotent: bool = True
    rate_limit: Mapping[str, int] | None = None
    timeout_s: float = 30.0

    @property
    def mcp_tool(self) -> dict[str, Any]:
        """MCP tool 形态（name / description / inputSchema）。"""
        return {"name": self.name, "description": self.description, "inputSchema": self.params}

    def has_side_effect(self, kind: str) -> bool:
        return kind in self.side_effects


ToolHandler = Callable[[Mapping[str, Any]], Awaitable[Any]]


class ToolRegistry:
    """能力注册表。名字唯一，重名直接报错（避免悄悄覆盖）。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._middlewares: list = []

    # ---------------------------------------------------------------- 闸门

    def use(self, middleware) -> "ToolRegistry":
        """挂一个中间件（`before` 拦截、`after` 验收）。顺序即执行顺序。"""
        self._middlewares.append(middleware)
        return self

    @property
    def middlewares(self) -> tuple:
        return tuple(self._middlewares)

    def register(self, spec: ToolSpec, handler: ToolHandler) -> ToolSpec:
        if spec.name in self._specs:
            raise ValueError(f"能力重名：{spec.name}")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler
        return spec

    def spec(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise AppError(f"未注册的能力：{name}") from exc

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return [spec.mcp_tool for spec in self._specs.values()]

    def rate_limits(self) -> dict[str, Mapping[str, int]]:
        """需要限流的能力及其限额（调度器 / 限流器将来直接用这份）。"""
        return {
            name: spec.rate_limit
            for name, spec in self._specs.items()
            if spec.rate_limit
        }

    async def invoke(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Any:
        """调用一项能力。所有闸门与验收都在这里过一遍。

        `context` 是「一次运行」共享的上下文（预算计数、调用历史、已批准清单）；
        不传就每次独立，等价于没有跨调用状态。
        """
        handler = self._handlers.get(name)
        if handler is None:
            raise AppError(f"未注册的能力：{name}")

        if self._middlewares:
            from app.capabilities.middleware import ToolCall

            call = ToolCall(
                tool=name,
                params=dict(params or {}),
                spec=self._specs[name],
                context=context if context is not None else {},
            )
            for middleware in self._middlewares:
                await middleware.before(call)
            result = await handler(call.params)
            for middleware in reversed(self._middlewares):
                result = await middleware.after(call, result)
            return result

        return await handler(dict(params or {}))


# --------------------------------------------------------------------------- 注册内容

def build_registry(
    *,
    http: HttpClient,
    credentials: Credentials,
    history: Any = None,
    knowledge: Any = None,
    image: ImageClient | None = None,
    video: VideoClient | None = None,
    llm: LlmClient | None = None,
    vision: VisionClient | None = None,
    judge: TypeSafeClient | None = None,
    search: SearchClient | None = None,
) -> ToolRegistry:
    """把现有客户端包装成能力。Phase 2 的服务层与 Phase 6 的调度器都从这里取。"""
    image = image or ImageClient(http, credentials.agnes)
    video = video or VideoClient(http, credentials.agnes)
    llm = llm or LlmClient(http, credentials.llm)
    vision = vision or VisionClient(http, credentials.vision)
    judge = judge or TypeSafeClient(http, credentials.typesafe)
    # 联网搜索：`image_dir` 由 bootstrap 注入（`<数据目录>/web_refs/`），
    # 这里不建默认目录——没配目录时 fetch_image 会明确报错，而不是把图丢到某处
    search = search or SearchClient(http, credentials.search)
    registry = ToolRegistry()

    # 历史检索（A-RAG）：只在拿到历史库时注册；延迟导入避免
    # registry → rag → services.__init__ → generation → registry 的循环
    rag = None
    if history is not None:
        from app.services.rag import RagService

        rag = RagService(history, judge)

    # 知识库：同样只在拿到库时注册。重排用的判断模型跟着注册表走——
    # 设置页改完密钥会重建注册表，这里把新模型推到知识库上，不用重启。
    if knowledge is not None:
        knowledge.use_judge(judge)

    async def _generate_image(params: Mapping[str, Any]) -> str:
        request = ImageRequest(
            prompt=str(params.get("prompt", "")),
            images=tuple(params.get("images") or ()),
            model=str(params.get("model") or ""),
            size=str(params.get("size") or DEFAULT_SIZE),
        )
        return await image.generate(request, timeout=float(params.get("timeout") or 120))

    registry.register(
        ToolSpec(
            name="image.generate",
            description=(
                "根据提示词生成图片；带参考图则为图生图。参考图可以是公网 URL 或本地文件路径"
                "（本地文件会自动转 base64）。"
            ),
            params={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "提示词"},
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_REFERENCE_IMAGES,
                        "description": "参考图（URL 或本地路径），最多 5 张",
                    },
                    "model": {"type": "string", "enum": list(IMAGE_MODELS)},
                    "size": {"type": "string", "enum": list(IMAGE_SIZES)},
                },
                "required": ["prompt"],
            },
            side_effects=_side(SIDE_NETWORK, SIDE_PAID, SIDE_DISK_READ),
            idempotent=False,
            timeout_s=120,
        ),
        _generate_image,
    )

    async def _submit_video(params: Mapping[str, Any]) -> dict:
        request = VideoRequest(
            prompt=str(params.get("prompt", "")),
            images=tuple(params.get("images") or ()),
            model=str(params.get("model") or ""),
            seconds=str(params.get("seconds") or "5"),
            aspect_ratio=str(params.get("aspect_ratio") or "16:9"),
            seed=params.get("seed"),
        )
        return await video.submit(request, timeout=float(params.get("timeout") or 30))

    registry.register(
        ToolSpec(
            name="video.submit",
            description=(
                "提交视频生成任务（异步）。有参考图走 reference 模式，无则 text 模式。"
                "参考图必须是公网 http(s) URL。平台限制：每分钟只能提交 1 个任务。"
            ),
            params={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "images": {
                        "type": "array",
                        "items": {"type": "string", "format": "uri"},
                        "maxItems": MAX_REFERENCE_IMAGES,
                        "description": "公网参考图 URL，最多 5 张（本地文件请先上传图床）",
                    },
                    "model": {"type": "string", "enum": list(VIDEO_MODELS)},
                    "seconds": {"type": "string", "enum": list(SECONDS_OPTIONS)},
                    "aspect_ratio": {"type": "string", "enum": list(ASPECT_RATIOS)},
                    "seed": {"type": "integer"},
                },
                "required": ["prompt"],
            },
            side_effects=_side(SIDE_NETWORK, SIDE_PAID),
            idempotent=False,
            # 平台硬限制写进元数据：调度器据此排队，agent 不必自己猜
            rate_limit={"per_minute": 1},
            timeout_s=30,
        ),
        _submit_video,
    )

    async def _query_video(params: Mapping[str, Any]) -> dict:
        return await video.query(
            str(params.get("video_id", "")),
            model_name=str(params.get("model_name") or ""),
            timeout=float(params.get("timeout") or 15),
        )

    registry.register(
        ToolSpec(
            name="video.query",
            description="查询视频任务状态（status / progress / url）。flash 系模型需带 model_name。",
            params={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "model_name": {"type": "string", "enum": list(VIDEO_MODELS)},
                },
                "required": ["video_id"],
            },
            side_effects=_side(SIDE_NETWORK),
            idempotent=True,
            timeout_s=15,
        ),
        _query_video,
    )

    async def _fetch(params: Mapping[str, Any]) -> bytes:
        url = str(params.get("url") or "").strip()
        if not url:
            raise ValidationError("url 不能为空")
        return await http.download(url, timeout=float(params.get("timeout") or 60))

    registry.register(
        ToolSpec(
            name="media.fetch",
            description="下载图片或视频的原始字节（只负责取回内容，落盘由媒体服务决定）。",
            params={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "timeout": {"type": "number"},
                },
                "required": ["url"],
            },
            side_effects=_side(SIDE_NETWORK),
            idempotent=True,
            timeout_s=60,
        ),
        _fetch,
    )

    async def _upload(params: Mapping[str, Any]) -> str:
        return await upload_to_image_host(
            http,
            credentials.github,
            credentials.see,
            str(params.get("path", "")),
            provider=str(params.get("provider") or "auto"),
        )

    registry.register(
        ToolSpec(
            name="image_host.upload",
            description=(
                "把本地图片上传到图床（GitHub / S.E.E），返回公网直链。"
                "这是把本地参考图交给视频接口的唯一途径，会把文件传到公网，不可回滚。"
            ),
            params={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "本地图片路径"},
                    "provider": {"type": "string", "enum": ["auto", "github", "see"]},
                },
                "required": ["path"],
            },
            side_effects=_side(SIDE_NETWORK, SIDE_UPLOAD, SIDE_DISK_READ),
            idempotent=False,
            timeout_s=120,
        ),
        _upload,
    )

    async def _chat(params: Mapping[str, Any]) -> str:
        if not credentials.llm.configured:
            raise ConfigError(
                "LLM 未配置：在 .env 里填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL。"
            )
        return await llm.chat(
            params.get("messages") or [],
            model=params.get("model"),
            temperature=params.get("temperature"),
            max_tokens=params.get("max_tokens"),
            timeout=float(params.get("timeout") or 60),
        )

    registry.register(
        ToolSpec(
            name="llm.chat",
            description="调用 OpenAI 兼容的对话模型（提示词润色、改写等）。需在 .env 配 LLM_*。",
            params={
                "type": "object",
                "properties": {
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                                "content": {"type": "string"},
                            },
                            "required": ["role", "content"],
                        },
                    },
                    "model": {"type": "string"},
                    "temperature": {"type": "number"},
                    "max_tokens": {"type": "integer"},
                },
                "required": ["messages"],
            },
            side_effects=_side(SIDE_NETWORK),
            idempotent=False,
            timeout_s=60,
        ),
        _chat,
    )

    async def _describe_image(params: Mapping[str, Any]) -> dict:
        description = await vision.describe(
            str(params.get("image", "")),
            requirement=str(params.get("requirement") or ""),
            timeout=float(params.get("timeout") or 180),
        )
        return description.to_dict()

    registry.register(
        ToolSpec(
            name="vision.describe",
            description=(
                "把一张图变成结构化描述（主体/风格/构图/光线/瑕疵）。"
                "本地文件会自动缩到 320px 再发送（实测比发原图快 14.5 倍，描述质量几乎不变）；"
                "公网 URL 原样透传。**只做描述，不做判断**——是否达标交给 judge.ask。"
            ),
            params={
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "本地路径或公网 URL"},
                    "requirement": {"type": "string", "description": "用户需求，可选，用于描述时留意相关细节"},
                },
                "required": ["image"],
            },
            side_effects=_side(SIDE_NETWORK, SIDE_PAID, SIDE_DISK_READ),
            idempotent=True,
            timeout_s=180,
        ),
        _describe_image,
    )

    async def _judge(params: Mapping[str, Any]) -> dict:
        result = await judge.ask(
            params.get("state") or {},
            params.get("questions") or {},
            model=params.get("model"),
            timeout=float(params.get("timeout") or 30),
        )
        return result.to_dict()

    registry.register(
        ToolSpec(
            name="judge.ask",
            description=(
                "问 Jev 窄问题并拿回类型化判断（是/否概率、选项+概率、打分）+ 置信度。"
                "一次请求可以并行问多个问题；同一 state 下的独立问题应当合并成一次调用。"
            ),
            params={
                "type": "object",
                "properties": {
                    "state": {"type": "object", "description": "要判断的内容（字符串或对象）"},
                    "questions": {
                        "type": "object",
                        "description": (
                            "问题 id → {type, instructions, criteria}；"
                            "type 取 noul / choice / score，"
                            "score 的 criteria 是等级数组，choice 的是「选项→说明」字典"
                        ),
                    },
                    "model": {"type": "string"},
                },
                "required": ["state", "questions"],
            },
            side_effects=_side(SIDE_NETWORK, SIDE_PAID),
            idempotent=True,
            timeout_s=30,
        ),
        _judge,
    )

    if rag is not None:

        async def _rag_search(params: Mapping[str, Any]) -> dict:
            result = await rag.search(
                str(params.get("requirement", "")),
                kind=params.get("kind") or None,
                top_k=int(params.get("top_k") or 5),
                timeout=float(params.get("timeout") or 30),
            )
            return result.to_dict()

        registry.register(
            ToolSpec(
                name="rag.search",
                description=(
                    "在自己过去生成的历史里找参考：先用元数据筛候选（只要成功的、收藏优先、"
                    "排除标过重试的），再用 Jev 重排。产出的是**参考图 + 提示词片段**，"
                    "可直接喂给 image.generate 做定向强化。判断不可用时会降级为元数据顺序"
                    "（结果里 reranked=false）。"
                ),
                params={
                    "type": "object",
                    "properties": {
                        "requirement": {"type": "string", "description": "要强化的需求描述"},
                        "kind": {"type": "string", "enum": ["image", "video"]},
                        "top_k": {"type": "integer", "description": "返回前几条，默认 5"},
                    },
                    "required": ["requirement"],
                },
                side_effects=_side(SIDE_NETWORK, SIDE_PAID, SIDE_DISK_READ),
                idempotent=True,
                timeout_s=30,
            ),
            _rag_search,
        )

    if knowledge is not None:

        async def _kb_search(params: Mapping[str, Any]) -> dict:
            result = await knowledge.search(
                str(params.get("requirement", "")),
                top_k=int(params.get("top_k") or 4),
                timeout=float(params.get("timeout") or 30),
            )
            return result.to_dict()

        registry.register(
            ToolSpec(
                name="kb.search",
                description=(
                    "在用户自己上传的知识库里找资料：先按关键词召回（中文二元组 + 西文词），"
                    "再让 Jev 重排，取前几片。产出的是**能直接拼进提示词的片段**和"
                    "「文件名 + 第几片」的来源。判断不可用时会降级为关键词顺序"
                    "（结果里 reranked=false），不会让生成流程挂掉。"
                ),
                params={
                    "type": "object",
                    "properties": {
                        "requirement": {"type": "string", "description": "要查的需求描述"},
                        "top_k": {"type": "integer", "description": "返回前几片，默认 4"},
                    },
                    "required": ["requirement"],
                },
                side_effects=_side(SIDE_NETWORK, SIDE_PAID, SIDE_DISK_READ),
                idempotent=True,
                timeout_s=30,
            ),
            _kb_search,
        )

    # 联网搜索（第三期）：本地不够时的兜底来源。两项能力都注册，但**要不要调用由
    # 运行时按来源开关和阈值决定**——闸门只管「调用合不合规」，不管「该不该搜」。
    async def _web_search(params: Mapping[str, Any]) -> dict:
        result = await search.search(
            str(params.get("query", "")),
            max_results=int(params.get("max_results") or 3),
            want_images=bool(params.get("want_images")),
            timeout=float(params.get("timeout") or 25),
        )
        return result.to_dict()

    registry.register(
        ToolSpec(
            name="web.search",
            description=(
                "联网搜索，拿到外部线索（标题 + 链接 + 摘要），用在本地参考不够时。"
                "产出与 rag.search / kb.search 对齐。**搜不了时不会抛错**：返回里 ok=false "
                "和一句 reason（没配 key / 关掉了 / provider 出错），据此向用户交代。"
            ),
            params={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要搜的话"},
                    "max_results": {"type": "integer", "description": "返回前几条，默认 3"},
                    "want_images": {"type": "boolean", "description": "是否顺带要图片候选"},
                },
                "required": ["query"],
            },
            side_effects=_side(SIDE_NETWORK, SIDE_PAID),
            idempotent=True,
            timeout_s=25,
        ),
        _web_search,
    )

    async def _web_fetch_image(params: Mapping[str, Any]) -> dict:
        path = await search.fetch_image(
            str(params.get("url", "")), timeout=float(params.get("timeout") or 30)
        )
        return {"path": str(path), "source_url": str(params.get("url", ""))}

    registry.register(
        ToolSpec(
            name="web.fetch_image",
            description=(
                "把一张网络图片下载到本地当参考（存 <数据目录>/web_refs/）。"
                "这一步会写磁盘，且图片版权归原站——只在用户明确打开「联网配图」时才调用。"
            ),
            params={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "图片地址（http/https）"}},
                "required": ["url"],
            },
            side_effects=_side(SIDE_NETWORK, SIDE_DISK_WRITE),
            idempotent=True,
            timeout_s=30,
        ),
        _web_fetch_image,
    )

    # 挂上默认闸门与验收：参数校验 → 预算 → 审批 → 循环检测 → 真调用 → 结果验收。
    # 放在注册表这一层，是因为这里是所有工具调用的唯一收口（手动界面、流水线、agent 都走它）。
    from app.capabilities.middleware import default_middlewares

    for middleware in default_middlewares():
        registry.use(middleware)

    return registry
