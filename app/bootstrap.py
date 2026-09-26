"""唯一装配点：把各层按顺序拼起来，其他地方不再自己 new 对象。

    credentials ─→ http ─→ clients ─→ capabilities ─→ services
                                  ↘ state（bus + app_state）

界面与命令行都从这里拿 `AppContext`，因此「怎么装配」只有一处，改网络模式、换目录、
注入测试替身都只动这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.capabilities.registry import ToolRegistry, build_registry
from app.agent.prompt_store import PromptStore
from app.agent.runtime import AgentRuntime
from app.clients.search_client import SearchClient
from app.config import paths, settings
from app.config.credentials import Credentials, load_credentials
from app.net.http import HttpClient
from app.services.generation import GenerationService
from app.services.history import HistoryStore
from app.services.knowledge import KnowledgeStore
from app.services.media import MediaStore
from app.services.rate_limit import RateLimiter
from app.services.context_store import ContextStore
from app.state.app_state import AppState
from app.state.events import EventBus


@dataclass
class AppContext:
    """一次运行期内的全部依赖。谁需要什么就取什么，不用再关心构造顺序。"""

    credentials: Credentials
    http: HttpClient
    registry: ToolRegistry
    bus: EventBus
    state: AppState
    history: HistoryStore
    media: MediaStore
    generation: GenerationService
    agent: AgentRuntime
    prompts: PromptStore
    contexts: ContextStore
    knowledge: KnowledgeStore
    search: SearchClient
    data_dir: Path
    env_file: Path | None = None

    def reload_credentials(self, env_file: str | Path | None = None) -> Credentials:
        """重新读取 `.env` 并把新凭据推给所有客户端与能力注册表。

        设置页保存密钥后调用：不需要重启，也不需要重建整个上下文——客户端与注册表都很轻，
        重建注册表能保证里面的闭包（图床、LLM 那几条）也拿到新凭据。
        """
        from app.capabilities.registry import build_registry as _build

        target = env_file or self.env_file
        credentials = load_credentials(target)
        # 搜索客户端跟着凭据走（provider/key/base_url 都可能刚改）；存放目录沿用原来的
        search = SearchClient(self.http, credentials.search, image_dir=self.search.image_dir)
        # 历史库与知识库要一起传：它们是 rag.search / kb.search 两项能力的依赖，
        # 漏传的话「设置页保存密钥」之后助手就再也检索不到了（要重启才恢复）。
        registry = _build(
            http=self.http,
            credentials=credentials,
            history=self.history,
            knowledge=self.knowledge,
            search=search,
        )

        self.credentials = credentials
        self.search = search
        self.registry = registry
        self.generation.credentials = credentials
        self.generation.registry = registry
        self.generation.limiter.configure_from(registry.rate_limits())
        return credentials

    async def aclose(self) -> None:
        """释放网络连接与数据库句柄。"""
        try:
            await self.http.aclose()
        finally:
            self.history.close()
            self.prompts.close()
            self.contexts.close()
            self.knowledge.close()


def build_context(
    *,
    env_file: str | Path | None = None,
    data_dir: str | Path | None = None,
    network_mode: str | None = None,
    db_name: str = "history.db",
    cache_images: bool = True,
    cache_videos: bool = False,
    transport=None,
) -> AppContext:
    """组装应用上下文（同步，可在任何线程调用）。

    `transport` 只在测试里用：传一个 `httpx.MockTransport` 就能整条链路离线跑通
    （界面测试因此不需要网络，也不会误发真实请求）。
    """
    credentials = load_credentials(env_file)
    mode = network_mode or settings.get("network_mode", "auto")
    http = HttpClient(network_mode=mode, transport=transport)

    root = Path(data_dir) if data_dir else settings.data_dir()
    paths.ensure_dir(root)
    history = HistoryStore(root / db_name)
    media = MediaStore(root)
    # 知识库与它们共用一个 history.db（v5 迁移建表）；原文件放 <数据目录>/knowledge/
    knowledge = KnowledgeStore(root / db_name, root=root / "knowledge")
    # 联网搜索：下载的参考图放 <数据目录>/web_refs/（默认不下载，见来源开关）
    search = SearchClient(http, credentials.search, image_dir=root / "web_refs")
    # 注册表要拿到历史库、知识库、搜索客户端：rag.search / kb.search / web.* 靠它们干活
    # （所以顺序是 历史 + 知识库 + 联网 → 注册表）
    registry = build_registry(
        http=http,
        credentials=credentials,
        history=history,
        knowledge=knowledge,
        search=search,
    )
    bus = EventBus()
    state = AppState(bus)
    generation = GenerationService(
        registry=registry,
        credentials=credentials,
        http=http,
        bus=bus,
        history=history,
        media=media,
        limiter=RateLimiter().configure_from(registry.rate_limits()),
        cache_images=cache_images,
        cache_videos=cache_videos,
    )
    # Agent 运行时：六步流水线，复用同一套注册表与生成服务（因此闸门一并生效）
    # 提示词补丁与历史记录共用一个 history.db（v3 迁移建表）
    prompt_store = PromptStore(root / db_name)
    # 可编辑上下文（先看后跑）与它们共用同一个 history.db（v4 迁移建表）
    context_store = ContextStore(root / db_name)
    agent = AgentRuntime(
        registry=registry,
        generation=generation,
        bus=bus,
        history=history,
        prompt_store=prompt_store,
    )

    return AppContext(
        credentials=credentials,
        http=http,
        registry=registry,
        bus=bus,
        state=state,
        history=history,
        media=media,
        generation=generation,
        agent=agent,
        prompts=prompt_store,
        contexts=context_store,
        knowledge=knowledge,
        search=search,
        data_dir=root,
        env_file=Path(env_file) if env_file else None,
    )
