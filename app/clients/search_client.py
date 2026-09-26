"""联网搜索客户端：provider 可插拔，产出与本地检索同一种形状。

为什么单开一个客户端而不是把搜索塞进 `RagService`：搜索是「另一个来源」，
它的**失败方式**与本地检索完全不同（没配 key、provider 挂了、超时、返回空），
而这些失败必须能原样交代给用户（草稿上要写清「为什么没联网」），
所以这里的所有出口都带 `reason`，而不是抛异常让上层猜。

五个 provider 的差别只在「请求怎么发、响应怎么解析」，对上层是同一件事：

    tavily（默认）  POST {base}/search        body 里带 api_key，返回 results[].content 摘要
    bocha            POST {base}/web-search   Bearer 鉴权，返回 data.webPages.value[].summary
    serper           POST {base}/search       X-API-KEY 鉴权，返回 organic[].snippet
    custom           POST {base}/search       Bearer 鉴权，按 OpenAI 兼容约定返回 results[]
    off              不发请求（用户主动关掉）

配图（`fetch_image`）是另一件事：**只在这一个地方落盘**，且由调用方决定要不要下载——
界面上的开关默认关，关掉时连候选图都不请求（方案 6.2）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from app.config.credentials import SearchCredentials
from app.net.errors import AppError, ResponseFormatError, ValidationError
from app.net.http import HttpClient

#: 一条线索的摘要长度上限：太长会把提示词预算吃光，太短又看不出内容
MAX_SNIPPET_CHARS = 300
#: 一次搜索最多返回几条线索（方案：每次运行最多 3 次搜索，每次几条）
DEFAULT_MAX_RESULTS = 3
#: 一次运行最多下载几张配图（进预算表 web.fetch_image: 4）
MAX_IMAGES = 4
#: 单张图片的下载上限：8MB。超过多半是下错了东西（网页 / 视频片段）
MAX_IMAGE_BYTES = 8 * 1024 * 1024


@dataclass
class SearchItem:
    """一条外部线索：标题 + 链接 + 摘要。进提示词的是摘要，给人看的是标题与链接。"""

    title: str
    url: str
    snippet: str
    source: str = ""
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "score": self.score,
        }


@dataclass
class SearchResult:
    """一次搜索的结果。`ok` 表示「真的联网搜了」——关掉 / 没 key / 出错都是 False。"""

    items: list[SearchItem] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    provider: str = ""
    reason: str = ""
    ok: bool = True

    @property
    def count(self) -> int:
        return len(self.items)

    def snippets(self) -> list[str]:
        return [item.snippet or item.title for item in self.items]

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "ok": self.ok,
            "provider": self.provider,
            "reason": self.reason,
            "items": [item.to_dict() for item in self.items],
            "images": list(self.images),
        }


def _suffix_of(data: bytes) -> str | None:
    """按文件头判图片类型；认不出来返回 None（宁可拒收也不把网页当图片存下来）。"""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis", b"heic", b"mif1"):
        return ".avif"
    return None


class SearchClient:
    """一次运行里所有联网搜索都走这里。生命周期与其它客户端一致（注入 HttpClient）。"""

    def __init__(
        self,
        http: HttpClient,
        credentials: SearchCredentials,
        *,
        image_dir: str | Path | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._http = http
        self._credentials = credentials
        self._image_dir = Path(image_dir) if image_dir else None
        self._timeout = timeout

    # ---------------------------------------------------------------- 状态

    @property
    def credentials(self) -> SearchCredentials:
        return self._credentials

    @property
    def provider(self) -> str:
        return self._credentials.provider

    @property
    def enabled(self) -> bool:
        return self._credentials.enabled

    @property
    def configured(self) -> bool:
        return self._credentials.configured

    @property
    def image_dir(self) -> Path | None:
        return self._image_dir

    def availability(self) -> tuple[bool, str]:
        """能不能搜？不能就给一句能直接显示给用户的原因。"""
        reason = self._credentials.unavailable_reason()
        return (not reason), reason

    # ---------------------------------------------------------------- 搜索

    async def search(
        self,
        query: str,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        want_images: bool = False,
        timeout: float | None = None,
    ) -> SearchResult:
        """搜一次。**任何失败都变成 `ok=False` + `reason`**，不抛异常。"""
        ok, reason = self.availability()
        if not ok:
            return SearchResult(provider=self.provider, ok=False, reason=reason)
        query = (query or "").strip()
        if not query:
            return SearchResult(
                provider=self.provider, ok=False, reason="没有可搜的内容（需求是空的）"
            )

        limit = max(1, int(max_results))
        try:
            payload = await self._http.post_json(
                self._credentials.endpoint,
                json=self._payload(query, limit, want_images),
                headers=self._headers(),
                timeout=timeout or self._timeout,
            )
        except AppError as exc:
            return SearchResult(
                provider=self.provider, ok=False, reason=f"联网搜索失败（{exc.kind}）：{exc.message}"
            )

        items, images = self._parse(payload, want_images=want_images)
        if not items:
            return SearchResult(
                provider=self.provider, ok=True, images=images,
                reason="联网搜了，但没有能用的结果（换个说法再试一次）",
            )
        return SearchResult(
            provider=self.provider,
            ok=True,
            items=items[:limit],
            images=images,
            reason=f"联网搜到 {len(items[:limit])} 条线索（{self.provider}）",
        )

    # ---------------------------------------------------------------- 请求形状

    def _headers(self) -> dict[str, str]:
        key = self._credentials.api_key
        if self.provider == "serper":
            return {"X-API-KEY": key}
        return {"Authorization": f"Bearer {key}"}

    def _payload(self, query: str, limit: int, want_images: bool) -> dict[str, Any]:
        """各家 body 不同，但都只传「搜什么、要几条、要不要图」。"""
        if self.provider == "tavily":
            # tavily 同时接受 header 与 body 里的 key；两处都带，省得遇到只认一种的版本
            body: dict[str, Any] = {
                "api_key": self._credentials.api_key,
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
                "include_answer": False,
            }
            if want_images:
                body["include_images"] = True
            return body
        if self.provider == "bocha":
            body = {"query": query, "count": limit, "summary": True, "page": 1}
            if want_images:
                body["imageSearch"] = True
            return body
        if self.provider == "serper":
            # serper 的图片在另一个端点（/images），这里只搜网页；图片留给 fetch_image 的候选去取
            return {"q": query, "num": limit}
        return {"query": query, "max_results": limit, "want_images": want_images}

    # ---------------------------------------------------------------- 响应解析

    def _parse(self, payload: Any, *, want_images: bool) -> tuple[list[SearchItem], list[str]]:
        """把各家五花八门的响应压成同一个形状；认不出的字段直接跳过，不猜。"""
        if not isinstance(payload, Mapping):
            return [], []
        if self.provider == "tavily":
            items = _from_list(
                payload.get("results"),
                url_keys=("url",),
                title_keys=("title",),
                text_keys=("content", "snippet", "raw_content"),
            )
            images = _images_of(payload.get("images")) if want_images else []
            return items, images
        if self.provider == "bocha":
            data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
            pages = data.get("webPages") if isinstance(data, Mapping) else None
            rows = pages.get("value") if isinstance(pages, Mapping) else None
            items = _from_list(
                rows,
                url_keys=("url",),
                title_keys=("name", "title"),
                text_keys=("summary", "snippet"),
                source_keys=("siteName",),
            )
            images = []
            if want_images:
                images = _images_of(data.get("images") if isinstance(data, Mapping) else None)
            return items, images
        if self.provider == "serper":
            items = _from_list(
                payload.get("organic"),
                url_keys=("link",),
                title_keys=("title",),
                text_keys=("snippet",),
            )
            images = _images_of(payload.get("images")) if want_images else []
            return items, images
        # custom：按 OpenAI 兼容约定，results / data 两种都认
        rows = payload.get("results") if payload.get("results") is not None else payload.get("data")
        items = _from_list(
            rows,
            url_keys=("url", "link"),
            title_keys=("title", "name"),
            text_keys=("snippet", "content", "summary", "text"),
        )
        images = _images_of(payload.get("images")) if want_images else []
        return items, images

    # ---------------------------------------------------------------- 配图

    async def fetch_image(self, url: str, *, timeout: float | None = None) -> Path:
        """下载一张参考图到 `image_dir`，返回落盘路径。

        两个刻意的限制：**认不出图片头就拒收**（否则会把网页、错误页当图存下来，
        生成时才报看不懂的错）；超过 `MAX_IMAGE_BYTES` 也拒收（多半是下错了东西）。
        """
        if self._image_dir is None:
            raise ValidationError("没有配置联网配图的存放目录（web_refs），无法下载图片")
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValidationError(f"不是可下载的图片地址：{url[:120]}")

        data = await self._http.download(url, timeout=timeout or self._timeout)
        if not data:
            raise ResponseFormatError("这张图下载下来是空的", endpoint=url)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValidationError(
                f"这张图超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB，已跳过（{url[:80]}）"
            )
        suffix = _suffix_of(data)
        if suffix is None:
            raise ResponseFormatError("下载到的不是图片（认不出文件头），已跳过", endpoint=url)

        self._image_dir.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        path = self._image_dir / f"{name}{suffix}"
        path.write_bytes(data)
        return path


# --------------------------------------------------------------------------- 解析工具

def _clean(value: Any, limit: int = MAX_SNIPPET_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _pick(row: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _from_list(
    rows: Any,
    *,
    url_keys: tuple[str, ...],
    title_keys: tuple[str, ...],
    text_keys: tuple[str, ...],
    source_keys: tuple[str, ...] = (),
) -> list[SearchItem]:
    """把一列原始结果转成 `SearchItem`；没有 url 的行丢掉（点不开的线索没法用）。"""
    if not isinstance(rows, list):
        return []
    items: list[SearchItem] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        url = _pick(row, url_keys)
        if not url:
            continue
        title = _clean(_pick(row, title_keys), 120) or url
        snippet = _clean(_pick(row, text_keys)) or title
        score = row.get("score")
        items.append(
            SearchItem(
                title=title,
                url=url,
                snippet=snippet,
                source=_clean(_pick(row, source_keys), 60) if source_keys else "",
                score=float(score) if isinstance(score, (int, float)) else None,
            )
        )
    return items


def _images_of(value: Any) -> list[str]:
    """图片候选：可能是 `["url"]`，也可能是 `[{url/imageUrl/src: ...}]`，两种都认。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for row in value:
        url = ""
        if isinstance(row, str):
            url = row.strip()
        elif isinstance(row, Mapping):
            url = _pick(row, ("url", "imageUrl", "image_url", "src", "link"))
        if url.lower().startswith(("http://", "https://")) and url not in out:
            out.append(url)
        if len(out) >= MAX_IMAGES:
            break
    return out
