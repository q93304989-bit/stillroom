"""设置页的后端：读写 `.env` 与 `settings.json`、切换网络模式、探测连接。

与界面层其他部分一致：所有真实动作都在 asyncio 线程里做，界面只收结果。

密钥仍然只属于 `.env`（硬规则 2）；这个对象是唯一允许写它的地方。
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

from PySide6.QtCore import Property, QObject, Signal, Slot

from app.bootstrap import AppContext
from app.config import paths, settings
from app.config.credentials import write_env_key
from app.net.errors import AppError, AuthError, NotFoundError
from app.services.context_store import DEFAULT_SOURCES
from app.ui.async_runner import AsyncRunner
from app.ui.theme import Theme, detect_system_dark

NETWORK_MODES = ["auto", "direct", "proxy"]
THEMES = ["system", "light", "dark"]
CONTEXT_MODES = ["draft", "auto"]
#: 本地参考阈值（少于几条就联网）的可调范围
MIN_LOCAL_REFS_RANGE = (0, 5)
#: 联网搜索 provider（顺序即下拉里的顺序；tavily 默认，off 是「我自己关掉」）
SEARCH_PROVIDERS = ["tavily", "bocha", "serper", "custom", "off"]

#: 「联网配图」的免责声明（方案 6.2 节原文，界面与确认框共用，避免两处说法不一致）
WEB_IMAGES_DISCLAIMER = (
    "联网抓取的图片来自第三方网站，本软件不保证其版权、授权与可用性，"
    "也不对由此产生的任何版权纠纷负责。请自行确认授权后再使用；商用请务必自查来源。"
)


def merged_sources(raw: object) -> dict[str, bool]:
    """来源开关：设置里缺的键用代码默认补上，未知键丢掉。

    缺项补默认（而不是当成关闭）是刻意的：以后新增一个来源时，老用户的
    settings.json 里没有这个键，应该按默认值走，而不是静默变成关掉。
    """
    values = raw if isinstance(raw, dict) else {}
    return {
        **DEFAULT_SOURCES,
        **{key: bool(values[key]) for key in DEFAULT_SOURCES if key in values},
    }


class SettingsBridge(QObject):
    """QML 上下文对象 `settings`。"""

    saved = Signal(str)                       # 保存成功（分区名）
    credentialsChanged = Signal()             # 凭据变了（顶栏徽标要跟着更新）
    settingsChanged = Signal()                # 偏好变了（默认值、主题等）
    probeFinished = Signal(bool, str)         # 连接测试结果

    def __init__(
        self,
        context: AppContext,
        runner: AsyncRunner,
        theme: Theme,
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._ctx = context
        self._runner = runner
        self._theme = theme

    # ---------------------------------------------------------------- 只读信息

    @Property(str, constant=True)
    def dataDir(self) -> str:                 # noqa: N802 - QML 命名
        return str(self._ctx.data_dir)

    @Property("QVariantList", constant=True)
    def searchProviders(self) -> list[str]:   # noqa: N802 - QML 命名
        """联网搜索的 provider 列表（下拉用；顺序与界面文案一一对应）。"""
        return list(SEARCH_PROVIDERS)

    @Property(str, constant=True)
    def webImagesDisclaimer(self) -> str:     # noqa: N802 - QML 命名
        return WEB_IMAGES_DISCLAIMER

    @Property(bool, notify=settingsChanged)
    def webImagesAcknowledged(self) -> bool:  # noqa: N802 - QML 命名
        """免责声明有没有被确认过（没确认过时，打开配图开关要先弹一次）。"""
        return bool(settings.get("web_images_ack", False))

    @Slot(result="QVariantMap")
    def currentValues(self) -> dict:
        """给界面回填用的当前值（密钥不回传明文，只回传「有没有配」）。"""
        creds = self._ctx.credentials
        prefs = settings.load()
        return {
            "agnes_base_url": creds.agnes.base_url,
            "agnes_key_set": bool(creds.agnes.api_key),
            "site": creds.agnes.site,
            "github_repo": creds.github.repo,
            "github_token_set": bool(creds.github.token),
            "see_token_set": bool(creds.see.token),
            "llm_base_url": creds.llm.base_url,
            "llm_model": creds.llm.model,
            "llm_provider": creds.llm.provider,
            "llm_key_set": bool(creds.llm.api_key),
            "network_mode": prefs.get("network_mode", "auto"),
            "theme": prefs.get("theme", "system"),
            "img_model": prefs.get("img_model", ""),
            "img_size": prefs.get("img_size", ""),
            "vid_model": prefs.get("vid_model", ""),
            "vid_seconds": prefs.get("vid_seconds", ""),
            "vid_aspect": prefs.get("vid_aspect", ""),
            "context_mode": prefs.get("context_mode", "draft"),
            "context_sources": merged_sources(prefs.get("context_sources")),
            "min_local_refs": int(prefs.get("min_local_refs", 2)),
            "search_provider": creds.search.provider,
            "search_key_set": bool(creds.search.api_key),
            "search_base_url": creds.search.base_url,
            "data_dir": str(self._ctx.data_dir),
            "video_query_url": creds.agnes.query_endpoint,
        }

    # ---------------------------------------------------------------- 保存

    @Slot("QVariantMap")
    def saveAgnes(self, values: dict) -> None:      # noqa: N802 - QML 调用
        """密钥 / 接口地址。空字符串表示「不改」，避免密码框没填就清空已有密钥。"""
        env_file = self._env_file()
        api_key = str(values.get("api_key") or "").strip()
        base_url = str(values.get("base_url") or "").strip()
        if api_key:
            write_env_key("AGNES_API_KEY", api_key, env_file)
        if base_url:
            write_env_key("AGNES_BASE_URL", base_url, env_file)
        creds = self._ctx.reload_credentials(env_file)
        self.credentialsChanged.emit()
        self.saved.emit("agnes")
        self._notice_site(creds.agnes.site, creds.agnes.query_endpoint)

    @Slot("QVariantMap")
    def saveHosting(self, values: dict) -> None:    # noqa: N802
        """图床（GitHub / S.E.E）。"""
        env_file = self._env_file()
        for key, field in (
            ("GITHUB_TOKEN", "github_token"),
            ("GITHUB_REPO", "github_repo"),
            ("SEE_API_TOKEN", "see_token"),
        ):
            value = str(values.get(field) or "").strip()
            if value:
                write_env_key(key, value, env_file)
        self._ctx.reload_credentials(env_file)
        self.credentialsChanged.emit()
        self.saved.emit("hosting")

    @Slot("QVariantMap")
    def saveLlm(self, values: dict) -> None:        # noqa: N802
        """OpenAI 兼容的 LLM 配置。"""
        env_file = self._env_file()
        for key, field in (
            ("LLM_API_KEY", "llm_api_key"),
            ("LLM_BASE_URL", "llm_base_url"),
            ("LLM_MODEL", "llm_model"),
        ):
            value = str(values.get(field) or "").strip()
            if value:
                write_env_key(key, value, env_file)
        self._ctx.reload_credentials(env_file)
        self.credentialsChanged.emit()
        self.saved.emit("llm")

    @Slot(str)
    def saveNetworkMode(self, mode: str) -> None:   # noqa: N802
        mode = mode if mode in NETWORK_MODES else "auto"
        settings.update(network_mode=mode)
        self._ctx.http.set_network_mode(mode)
        self.settingsChanged.emit()
        self.saved.emit("network")

    @Slot(str)
    def saveTheme(self, mode: str) -> None:         # noqa: N802
        mode = mode if mode in THEMES else "system"
        settings.update(theme=mode)
        if mode == "dark":
            self._theme.setDark(True)
        elif mode == "light":
            self._theme.setDark(False)
        else:
            self._theme.setDark(detect_system_dark())
        self.settingsChanged.emit()
        self.saved.emit("theme")

    @Slot("QVariantMap")
    def saveDefaults(self, values: dict) -> None:   # noqa: N802
        """生成默认值（留空 = 用列表第一项）。"""
        settings.update(
            img_model=str(values.get("img_model") or ""),
            img_size=str(values.get("img_size") or ""),
            vid_model=str(values.get("vid_model") or ""),
            vid_seconds=str(values.get("vid_seconds") or ""),
            vid_aspect=str(values.get("vid_aspect") or ""),
        )
        self.settingsChanged.emit()
        self.saved.emit("defaults")

    @Slot("QVariantMap")
    def saveContext(self, values: dict) -> None:    # noqa: N802
        """找参考与上下文：草稿/自动模式、四个来源的默认开关、本地参考阈值。

        与密钥不同，这些是纯偏好，非法值一律**夹到合法范围**而不是报错——界面上
        本来就给不出非法值，这条兜底留给手改 settings.json 的情况。
        """
        mode = str(values.get("mode") or "").strip()
        if mode not in CONTEXT_MODES:
            mode = "draft"

        threshold = values.get("min_local_refs")
        if threshold in (None, ""):
            threshold = settings.get("min_local_refs", 2)
        try:
            threshold = int(threshold)
        except (TypeError, ValueError):
            threshold = 2
        low, high = MIN_LOCAL_REFS_RANGE

        settings.update(
            context_mode=mode,
            context_sources=merged_sources(values.get("sources")),
            min_local_refs=max(low, min(high, threshold)),
        )
        self.settingsChanged.emit()
        self.saved.emit("context")

    @Slot("QVariantMap")
    def saveSearch(self, values: dict) -> None:    # noqa: N802
        """联网搜索：provider / key / base_url。

        与其它密钥同一套约定：空字符串表示「不改」，避免密码框没填就把已有 key 清掉。
        `SEARCH_PROVIDER` 例外——它总是写，因为「切到 off」本身就是一个要保存的选择。
        """
        provider = str(values.get("provider") or "").strip().lower()
        if provider not in SEARCH_PROVIDERS:
            provider = SEARCH_PROVIDERS[0]
        env_file = self._env_file()
        write_env_key("SEARCH_PROVIDER", provider, env_file)
        for key, field in (("SEARCH_API_KEY", "api_key"), ("SEARCH_BASE_URL", "base_url")):
            value = str(values.get(field) or "").strip()
            if value:
                write_env_key(key, value, env_file)

        creds = self._ctx.reload_credentials(env_file)
        self.credentialsChanged.emit()
        self.saved.emit("search")
        search = creds.search
        self.probeFinished.emit(
            True,
            f"已保存联网搜索：provider={search.provider}"
            + ("" if search.configured else f"\n提示：{search.unavailable_reason()}"),
        )

    @Slot()
    def acknowledgeWebImages(self) -> None:        # noqa: N802
        """记下「用户已读过联网配图的免责声明」，之后不再弹确认框。"""
        settings.update(web_images_ack=True)
        self.settingsChanged.emit()

    # ---------------------------------------------------------------- 连接测试

    @Slot()
    def testConnection(self) -> None:               # noqa: N802
        """同时探测图片端点与视频查询端点。

        旧版的教训：只测图片端点会出现「测试全绿，但视频功能失效」——因为视频查询在
        站点根下的 /agnesapi，跨区切换时两个地址可能不一致。所以这里必须两个都测。
        """

        async def _probe() -> None:
            creds = self._ctx.credentials.agnes
            if not creds.api_key:
                self.probeFinished.emit(False, "尚未配置 API Key")
                return
            lines: list[str] = []
            ok = True

            # 1) 图片侧：用 /models 判断「站点可达 + 密钥被接受」
            import time

            started = time.perf_counter()
            try:
                await self._ctx.http.get_json(
                    f"{creds.base_url.rstrip('/')}/models",
                    headers={"Authorization": creds.api_key},
                    timeout=15,
                )
                lines.append(f"图片端点：正常（{creds.base_url}）")
            except AuthError:
                ok = False
                lines.append(f"图片端点：认证失败（{creds.base_url}）——密钥与站点不匹配")
            except NotFoundError:
                lines.append(f"图片端点：可达（{creds.base_url}，无 /models 接口）")
            except AppError as exc:
                ok = False
                lines.append(f"图片端点：{exc.message}")
            lines.append(f"耗时 {time.perf_counter() - started:.2f}s")

            # 2) 视频侧：假 video_id 探测 —— 404「任务不存在」= 端点存在且密钥有效
            try:
                await self._ctx.http.get_json(
                    creds.query_endpoint,
                    params={"video_id": "agnes-studio-probe"},
                    headers={"Authorization": f"Bearer {creds.api_key}"},
                    timeout=15,
                )
                lines.append(f"视频查询端点：正常（{creds.query_endpoint}）")
            except NotFoundError:
                lines.append(f"视频查询端点：正常（{creds.query_endpoint}，任务不存在属预期）")
            except AuthError:
                ok = False
                lines.append(f"视频查询端点：认证失败（{creds.query_endpoint}）——多为密钥与站点不一致")
            except AppError as exc:
                ok = False
                lines.append(f"视频查询端点：{exc.message}")

            lines.append(f"当前站点：{creds.site}")
            self.probeFinished.emit(ok, "\n".join(lines))

        self._runner.submit(_probe())

    # ---------------------------------------------------------------- 其它

    @Slot()
    def openDataDir(self) -> None:                  # noqa: N802
        """打开数据目录（Windows 资源管理器）。"""
        try:
            paths.ensure_dir(self._ctx.data_dir)
            webbrowser.open(Path(self._ctx.data_dir).as_uri())
        except Exception as exc:                    # pragma: no cover
            self.probeFinished.emit(False, f"打开目录失败：{exc}")

    # ---------------------------------------------------------------- 内部

    def _env_file(self) -> Path:
        """写回的 `.env` 位置：优先已有的运行目录 `.env`，否则在运行目录新建。"""
        if self._ctx.env_file:
            return Path(self._ctx.env_file)
        for candidate in paths.env_candidates():
            if candidate.exists():
                return candidate
        return paths.runtime_dir() / ".env"

    def _notice_site(self, site: str, query_url: str) -> None:
        self.probeFinished.emit(True, f"已保存 · 当前站点：{site}\n视频查询端点：{query_url}")
