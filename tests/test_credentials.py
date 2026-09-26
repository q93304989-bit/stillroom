"""凭据解析：键名兼容、优先级、重复键、视频端点推导、LLM 预设。"""

from __future__ import annotations

import pytest

from app.config.credentials import (
    DEFAULT_AGNES_BASE_URL,
    derive_video_query_url,
    load_credentials,
    load_env,
    parse_env_text,
    write_env_key,
)
from app.net.errors import ConfigError


def test_parse_env_first_key_wins():
    """旧版的坑：同名键写两遍，第一处胜出（含注释与引号处理）。"""
    text = """
    # 注释行
    AGNES_BASE_URL=https://api.agnes-ai.cn/v1
    AGNES_BASE_URL=https://apihub.agnes-ai.com/v1
    AGNES_API_KEY="sk-quoted"
    export SEE_API_TOKEN=raw
    """
    values = parse_env_text(text)
    assert values["AGNES_BASE_URL"] == "https://api.agnes-ai.cn/v1"
    assert values["AGNES_API_KEY"] == "sk-quoted"
    assert values["SEE_API_TOKEN"] == "raw"


def test_load_env_merges_by_priority(tmp_path):
    high = tmp_path / "high.env"
    low = tmp_path / "low.env"
    high.write_text("A=1\nB=high\n", encoding="utf-8")
    low.write_text("B=low\nC=3\n", encoding="utf-8")

    merged = load_env(high)
    assert merged == {"A": "1", "B": "high"}


def test_write_env_key_replaces_in_place_and_dedupes(env_file):
    """写入后同一个键在文件里最多出现一次，且注释不丢。"""
    env_file.write_text(
        "# 顶部注释\nAGNES_API_KEY=old\nOTHER=keep\nAGNES_API_KEY=duplicated\n",
        encoding="utf-8",
    )

    write_env_key("AGNES_API_KEY", "new", env_file)
    text = env_file.read_text(encoding="utf-8")

    assert text.count("AGNES_API_KEY=") == 1
    assert "AGNES_API_KEY=new" in text
    assert "# 顶部注释" in text
    assert "OTHER=keep" in text


def test_write_env_key_appends_when_missing(env_file):
    env_file.write_text("OTHER=keep\n", encoding="utf-8")
    write_env_key("LLM_API_KEY", "sk-llm", env_file)
    assert "LLM_API_KEY=sk-llm" in env_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://apihub.agnes-ai.com/v1", "https://apihub.agnes-ai.com/agnesapi"),
        ("https://api.agnes-ai.cn/v1", "https://api.agnes-ai.cn/agnesapi"),
        ("https://api.agnes-ai.cn/v1/", "https://api.agnes-ai.cn/agnesapi"),
        ("https://gw.example.com/openai/v1", "https://gw.example.com/agnesapi"),
        ("not-a-url", "https://apihub.agnes-ai.com/agnesapi"),
    ],
)
def test_derive_video_query_url(base, expected):
    """视频查询端点必须跟随 base_url 的站点（跨区 bug 的修复点）。"""
    assert derive_video_query_url(base) == expected


def test_derive_video_query_url_env_override(monkeypatch):
    monkeypatch.setenv("AGNES_VIDEO_QUERY_URL", "https://self-hosted/agnesapi/")
    assert derive_video_query_url("https://api.agnes-ai.cn/v1") == "https://self-hosted/agnesapi"


def test_load_credentials_reads_legacy_keys(env_file):
    env_file.write_text(
        "\n".join(
            [
                "AGNES_API_KEY=sk-agnes",
                "AGNES_BASE_URL=https://api.agnes-ai.cn/v1/",
                "AGNES_CHAT_MODEL=agnes-image-2.1-flash",
                "GITHUB_TOKEN=ghp_x",
                "GITHUB_REPO=me/images/",
                "SMMS_API_TOKEN=smm",
            ]
        ),
        encoding="utf-8",
    )

    creds = load_credentials(env_file)

    assert creds.agnes.api_key == "sk-agnes"
    assert creds.agnes.base_url == "https://api.agnes-ai.cn/v1"      # 去尾斜杠
    assert creds.agnes.image_model == "agnes-image-2.1-flash"
    assert creds.agnes.images_endpoint == "https://api.agnes-ai.cn/v1/images/generations"
    assert creds.agnes.videos_endpoint == "https://api.agnes-ai.cn/v1/videos"
    assert creds.agnes.query_endpoint == "https://api.agnes-ai.cn/agnesapi"
    assert creds.agnes.site == "国内版"
    assert creds.github.repo == "me/images"                          # 去首尾斜杠
    assert creds.see.token == "smm"                                  # 兼容旧键名


def test_load_credentials_defaults(env_file):
    env_file.write_text("", encoding="utf-8")
    creds = load_credentials(env_file)
    assert creds.agnes.base_url == DEFAULT_AGNES_BASE_URL
    assert creds.agnes.site == "国际版"
    assert not creds.github.configured and not creds.see.configured and not creds.llm.configured


def test_require_key_raises_config_error(env_file):
    creds = load_credentials(env_file)
    with pytest.raises(ConfigError) as excinfo:
        creds.agnes.require_key()
    assert "AGNES_API_KEY" in str(excinfo.value)


def test_llm_generic_keys_win(env_file):
    env_file.write_text(
        "LLM_API_KEY=sk-generic\nLLM_BASE_URL=https://llm.example/v1\nLLM_MODEL=my-model\n"
        "DASHSCOPE_API_KEY=sk-dash\n",
        encoding="utf-8",
    )
    llm = load_credentials(env_file).llm
    assert llm.api_key == "sk-generic"
    assert llm.chat_endpoint == "https://llm.example/v1/chat/completions"
    assert llm.provider == "openai-compatible"


def test_llm_provider_preset_fallback(env_file):
    """没写通用键时，自动识别旧版 .env 里已有的供应商键。"""
    env_file.write_text(
        "DASHSCOPE_API_KEY=sk-dash\nDASHSCOPE_CHAT_MODEL=qwen-max\n",
        encoding="utf-8",
    )
    llm = load_credentials(env_file).llm
    assert llm.configured
    assert llm.provider == "dashscope"
    assert llm.model == "qwen-max"
    assert llm.base_url.endswith("/compatible-mode/v1")


def test_search_credentials_defaults_to_tavily(env_file):
    """没配过联网搜索时：provider 默认 tavily、没有 key → 明确判为「搜不了」。"""
    env_file.write_text("", encoding="utf-8")
    search = load_credentials(env_file).search
    assert search.provider == "tavily"
    assert not search.configured
    assert "SEARCH_API_KEY" in search.unavailable_reason()


def test_search_credentials_read_keys(env_file):
    env_file.write_text(
        "SEARCH_PROVIDER=bocha\nSEARCH_API_KEY=sk-bocha\n", encoding="utf-8"
    )
    search = load_credentials(env_file).search
    assert search.provider == "bocha"
    assert search.configured
    assert search.endpoint == "https://api.bochaai.com/v1/web-search"
    assert search.unavailable_reason() == ""


def test_search_provider_typo_falls_back(env_file):
    """手改 .env 把 provider 写错一个字母时不该用不了，而是落回默认值。"""
    env_file.write_text("SEARCH_PROVIDER=tavly\nSEARCH_API_KEY=sk\n", encoding="utf-8")
    assert load_credentials(env_file).search.provider == "tavily"


def test_search_provider_off_is_kept(env_file):
    env_file.write_text("SEARCH_PROVIDER=off\nSEARCH_API_KEY=sk\n", encoding="utf-8")
    search = load_credentials(env_file).search
    assert search.provider == "off"
    assert not search.enabled and not search.configured
