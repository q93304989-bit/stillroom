"""密钥与接口地址的唯一入口（`.env`）。

沿用旧版的键名，保证既有 `.env` 直接可用：

    AGNES_API_KEY / AGNES_BASE_URL / AGNES_CHAT_MODEL
    AGNES_VIDEO_QUERY_URL（可选覆盖） / AGNES_VIDEO_MODEL（可选）
    GITHUB_TOKEN / GITHUB_REPO / GITHUB_BRANCH
    SEE_API_TOKEN（兼容旧 SMMS_API_TOKEN）
    LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（通用）
    DASHSCOPE_* / DEEPSEEK_* / ZHIPU_*（旧版已有的供应商预设，自动识别）

两条与旧版不同的地方，都是修坑：

1. **重复键按「第一处胜出」解析**（dotenv 的默认行为），但写入时会合并重复键，
   避免旧版「同名键写两遍、后面那组静默失效」的问题。
2. 缺少密钥时抛 `ConfigError`（由 `app.net.errors` 定义）而不是 ValueError，
   让上层能区分「配置问题」与「用户输入问题」。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from app.config import paths
from app.net.errors import ConfigError

DEFAULT_AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"
DEFAULT_IMAGE_MODEL = "agnes-image-2.5-flash"
DEFAULT_VIDEO_MODEL = "agnes-video-2.5-flash"
VIDEO_QUERY_PATH = "/agnesapi"

# LLM 供应商预设：键前缀 → (默认 base_url, 默认模型)
LLM_PRESETS: dict[str, tuple[str, str]] = {
    "DASHSCOPE": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "DEEPSEEK": ("https://api.deepseek.com/v1", "deepseek-flash"),
    "ZHIPU": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
}


# --------------------------------------------------------------------------- 解析

def parse_env_text(text: str) -> dict[str, str]:
    """解析 `.env` 文本；**同名键第一处胜出**（与 dotenv 的 override=False 一致）。"""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key or key in values:
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values


def load_env(env_file: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """读取并合并 `.env`：显式文件 > 运行目录 > 打包内置 > 当前目录。

    先出现的文件胜出（与旧版「exe 同级 .env 覆盖内置 .env」一致）。
    """
    merged: dict[str, str] = {}
    candidates = [Path(env_file)] if env_file else paths.env_candidates()
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for key, value in parse_env_text(text).items():
            merged.setdefault(key, value)
    return merged


def write_env_key(key: str, value: str, env_file: str | os.PathLike[str] | None = None) -> Path:
    """写入/更新一个键：替换第一处出现，并删掉后续重复项。

    旧版直接追加，导致同名键出现多次而「第一处胜出」，用户改了后面的键却不生效。
    这里顺手把这个坑堵上：写完之后该键在文件里最多只剩一处。
    """
    target = Path(env_file) if env_file else (paths.runtime_dir() / ".env")
    lines: list[str] = []
    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()

    new_line = f"{key}={value}"
    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        is_same_key = (
            not stripped.startswith("#")
            and "=" in stripped
            and stripped.partition("=")[0].strip().removeprefix("export ").strip() == key
        )
        if is_same_key:
            if not replaced:
                out.append(new_line)
                replaced = True
            continue  # 重复项直接丢弃
        out.append(line)

    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(new_line)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def derive_video_query_url(base_url: str | None = None, explicit: str | None = None) -> str:
    """视频查询端点 = 站点 origin + `/agnesapi`（旧版跨区 bug 的修复逻辑）。

    视频提交走 `{base_url}/videos`，但**查询**在站点根下的 `/agnesapi`，不带 `/v1`。
    必须跟随 `AGNES_BASE_URL` 的站点，否则切到国内版会「提交成功、轮询 401」。
    `AGNES_VIDEO_QUERY_URL` 可显式覆盖（自建网关场景）。
    """
    override = (explicit or os.environ.get("AGNES_VIDEO_QUERY_URL") or "").strip()
    if override:
        return override.rstrip("/")
    raw = (base_url or os.environ.get("AGNES_BASE_URL") or DEFAULT_AGNES_BASE_URL).strip()
    parts = urlsplit(raw)
    if not (parts.scheme and parts.netloc):
        parts = urlsplit(DEFAULT_AGNES_BASE_URL)
    return f"{parts.scheme}://{parts.netloc}{VIDEO_QUERY_PATH}"


# --------------------------------------------------------------------------- 模型

@dataclass(frozen=True)
class AgnesCredentials:
    """Agnes 图像 / 视频接口凭据。"""

    api_key: str = ""
    base_url: str = DEFAULT_AGNES_BASE_URL
    image_model: str = DEFAULT_IMAGE_MODEL
    video_model: str = DEFAULT_VIDEO_MODEL
    video_query_url: str = ""

    @property
    def images_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/images/generations"

    @property
    def videos_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/videos"

    @property
    def query_endpoint(self) -> str:
        return self.video_query_url or derive_video_query_url(self.base_url)

    @property
    def site(self) -> str:
        """站点归属，用于设置页显示与「密钥与站点是否匹配」的提示。"""
        host = urlsplit(self.base_url).netloc.lower()
        if "agnes-ai.cn" in host:
            return "国内版"
        if "agnes-ai.com" in host:
            return "国际版"
        return host or "未知站点"

    def require_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "Agnes API Key 未配置：请在程序目录的 .env 里填写 AGNES_API_KEY。"
            )
        return self.api_key


@dataclass(frozen=True)
class GitHubCredentials:
    token: str = ""
    repo: str = ""
    branch: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.token and self.repo)


@dataclass(frozen=True)
class SeeCredentials:
    token: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.token)


@dataclass(frozen=True)
class LlmCredentials:
    """OpenAI 兼容的 LLM 凭据。`provider` 用于界面显示与排查。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    provider: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url)

    @property
    def chat_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def require(self) -> "LlmCredentials":
        if not self.configured:
            raise ConfigError(
                "LLM 未配置：请在 .env 里填写 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL"
                "（兼容 OpenAI 协议的任意服务）。"
            )
        return self


@dataclass(frozen=True)
class Credentials:
    agnes: AgnesCredentials
    github: GitHubCredentials
    see: SeeCredentials
    llm: LlmCredentials
    # 这两项给默认值：已有的调用点（测试、脚本）不必逐个改
    typesafe: "TypeSafeCredentials" = field(default_factory=lambda: TypeSafeCredentials())
    vision: "VisionCredentials" = field(default_factory=lambda: VisionCredentials())
    search: "SearchCredentials" = field(default_factory=lambda: SearchCredentials())


#: 视觉模型的供应商预设：前缀 → (默认 base_url, 默认模型)
VISION_PRESETS: dict[str, tuple[str, str]] = {
    "deepseek": ("https://api.deepseek.com", "deepseek-v4-flash-vision-exp"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-vl-max"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4v"),
}

DEFAULT_VISION_PROVIDER = "deepseek"


@dataclass(frozen=True)
class TypeSafeCredentials:
    """Jev（System One）凭据：把「窄判断」变成代码可直接消费的返回值。"""

    api_key: str = ""
    base_url: str = "https://api.typesafe.ai"
    model: str = "jev-latest"

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/systemone"

    def require(self) -> "TypeSafeCredentials":
        if not self.configured:
            raise ConfigError("Jev 未配置：请在 .env 里填写 TYPESAFE_API_KEY。")
        return self


@dataclass(frozen=True)
class VisionCredentials:
    """视觉模型凭据：只负责「把图变成结构化描述」，不负责判断（判断交给 Jev）。

    默认 DeepSeek；用户可以在设置里换成 DashScope / 智谱 / 任意 OpenAI 兼容服务。
    """

    provider: str = DEFAULT_VISION_PROVIDER
    api_key: str = ""
    base_url: str = ""
    model: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def require(self) -> "VisionCredentials":
        if not self.configured:
            raise ConfigError(
                f"视觉模型未配置（供应商 {self.provider}）："
                "请在 .env 里补上对应供应商的 API Key，或改用其他供应商。"
            )
        return self


# --------------------------------------------------------------------------- 入口

#: 联网搜索的 provider 预设：名字 → 默认 base_url（搜索路径不同，见 clients/search_client）
SEARCH_PRESETS: dict[str, str] = {
    "tavily": "https://api.tavily.com",
    "bocha": "https://api.bochaai.com/v1",
    "serper": "https://google.serper.dev",
    "custom": "",          # 自建端点必须自己填 SEARCH_BASE_URL
}

DEFAULT_SEARCH_PROVIDER = "tavily"

#: 关掉联网时用这个值（明确写出来，而不是留空靠猜）
SEARCH_OFF = "off"


@dataclass(frozen=True)
class SearchCredentials:
    """联网搜索凭据：provider 可插拔，key 与其他凭据同一入口（`.env`）。

    三种「搜不了」要在界面上一眼分清，所以这里把判断集中成一个方法：
    关掉了（off） / 没配 key / custom 没填 base_url。上层拿 `unavailable_reason()`
    直接写进草稿，不假装搜过。
    """

    provider: str = DEFAULT_SEARCH_PROVIDER
    api_key: str = ""
    base_url: str = ""

    @property
    def enabled(self) -> bool:
        """provider 是否为「要联网」的那几个（off = 用户主动关掉）。"""
        return self.provider != SEARCH_OFF

    @property
    def configured(self) -> bool:
        """能不能真的发请求：开了、有 key、有端点。"""
        return self.enabled and bool(self.api_key) and bool(self.resolved_base_url)

    @property
    def resolved_base_url(self) -> str:
        """显式 `SEARCH_BASE_URL` 优先；否则用 provider 预设（custom 没有预设）。"""
        return (self.base_url or SEARCH_PRESETS.get(self.provider, "")).strip().rstrip("/")

    @property
    def endpoint(self) -> str:
        """搜索端点。各家的路径不同，这里按 provider 拼——`custom` 按 OpenAI 兼容约定。"""
        base = self.resolved_base_url
        if self.provider == "bocha":
            return f"{base}/web-search"
        return f"{base}/search"

    def unavailable_reason(self) -> str:
        """不能搜时的说明；能搜返回空串。文案要能直接给用户看。"""
        if not self.enabled:
            return "联网搜索已关闭（SEARCH_PROVIDER=off），这次只用本地结果。"
        if not self.api_key:
            return (
                f"没配联网搜索的 key（provider={self.provider}），已跳过联网。"
                "想用就在 .env 里填 SEARCH_API_KEY；不想用就把 SEARCH_PROVIDER 设成 off。"
            )
        if not self.resolved_base_url:
            return (
                "自定义搜索端点没填地址：provider=custom 时必须给 SEARCH_BASE_URL，已跳过联网。"
            )
        return ""

    def require(self) -> "SearchCredentials":
        reason = self.unavailable_reason()
        if reason:
            raise ConfigError(reason)
        return self


def _resolve_llm(env: dict[str, str]) -> LlmCredentials:
    """通用 LLM_* 优先；否则按预设顺序识别旧版已有的供应商键。"""
    key = (env.get("LLM_API_KEY") or "").strip()
    if key:
        return LlmCredentials(
            api_key=key,
            base_url=(env.get("LLM_BASE_URL") or "").strip(),
            model=(env.get("LLM_MODEL") or "").strip(),
            provider=(env.get("LLM_PROVIDER") or "openai-compatible").strip(),
        )
    for prefix, (default_base, default_model) in LLM_PRESETS.items():
        preset_key = (env.get(f"{prefix}_API_KEY") or "").strip()
        if preset_key:
            return LlmCredentials(
                api_key=preset_key,
                base_url=(env.get(f"{prefix}_BASE_URL") or default_base).strip(),
                model=(env.get(f"{prefix}_CHAT_MODEL") or default_model).strip(),
                provider=prefix.lower(),
            )
    return LlmCredentials()


def load_credentials(env_file: str | os.PathLike[str] | None = None) -> Credentials:
    """读取全部凭据。缺失项返回空串，由使用方决定是否 `require`。"""
    env = load_env(env_file)
    base_url = (env.get("AGNES_BASE_URL") or DEFAULT_AGNES_BASE_URL).strip().rstrip("/")
    return Credentials(
        agnes=AgnesCredentials(
            api_key=(env.get("AGNES_API_KEY") or "").strip(),
            base_url=base_url,
            image_model=(env.get("AGNES_CHAT_MODEL") or DEFAULT_IMAGE_MODEL).strip(),
            video_model=(env.get("AGNES_VIDEO_MODEL") or DEFAULT_VIDEO_MODEL).strip(),
            video_query_url=(env.get("AGNES_VIDEO_QUERY_URL") or "").strip(),
        ),
        github=GitHubCredentials(
            token=(env.get("GITHUB_TOKEN") or "").strip(),
            repo=(env.get("GITHUB_REPO") or "").strip().strip("/"),
            branch=(env.get("GITHUB_BRANCH") or "").strip(),
        ),
        see=SeeCredentials(
            token=(env.get("SEE_API_TOKEN") or env.get("SMMS_API_TOKEN") or "").strip()
        ),
        llm=_resolve_llm(env),
        typesafe=TypeSafeCredentials(
            api_key=(env.get("TYPESAFE_API_KEY") or env.get("TYPESAFE_API") or "").strip(),
            base_url=(env.get("TYPESAFE_BASE_URL") or "https://api.typesafe.ai").strip().rstrip("/"),
            model=(env.get("TYPESAFE_MODEL") or "jev-latest").strip(),
        ),
        vision=_resolve_vision(env),
        search=_resolve_search(env),
    )


def _resolve_search(env: dict[str, str]) -> SearchCredentials:
    """联网搜索配置：`SEARCH_PROVIDER` / `SEARCH_API_KEY` / `SEARCH_BASE_URL`。

    未知 provider 一律落回默认（tavily）而不是报错：手改 `.env` 写错一个字母时，
    应该还能用，且界面上显示的是「实际生效的 provider」。
    """
    provider = (env.get("SEARCH_PROVIDER") or DEFAULT_SEARCH_PROVIDER).strip().lower()
    if provider != SEARCH_OFF and provider not in SEARCH_PRESETS:
        provider = DEFAULT_SEARCH_PROVIDER
    return SearchCredentials(
        provider=provider,
        api_key=(env.get("SEARCH_API_KEY") or "").strip(),
        base_url=(env.get("SEARCH_BASE_URL") or "").strip(),
    )


def _resolve_vision(env: dict[str, str]) -> VisionCredentials:
    """视觉模型配置：先看通用 VISION_*，再按供应商预设回落到各自的键。

    默认 DeepSeek（用户已有 key，一套配置同时做生成与看图）；可换成 DashScope / 智谱 /
    任意 OpenAI 兼容服务——换的只是「描述器」，判断标准不受影响。
    """
    provider = (env.get("VISION_PROVIDER") or DEFAULT_VISION_PROVIDER).strip().lower()
    if provider not in VISION_PRESETS:
        provider = DEFAULT_VISION_PROVIDER
    default_base, default_model = VISION_PRESETS[provider]

    api_key = (env.get("VISION_API_KEY") or env.get(f"{provider.upper()}_API_KEY") or "").strip()
    base_url = (env.get("VISION_BASE_URL") or env.get(f"{provider.upper()}_BASE_URL") or default_base).strip()
    model = (env.get("VISION_MODEL") or default_model).strip()
    return VisionCredentials(
        provider=provider,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
    )
