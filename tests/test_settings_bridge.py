"""设置页后端：写 `.env`、热更新凭据、切换网络模式与主题、连接探测。"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from app.bootstrap import build_context
from app.config import settings
from app.config.credentials import load_credentials
from app.ui.async_runner import AsyncRunner
from app.ui.settings_bridge import SettingsBridge
from app.ui.theme import Theme


@pytest.fixture
def bridge(qt_app, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGNES_API_KEY=sk-old\nAGNES_BASE_URL=https://api.test/v1\n", encoding="utf-8"
    )
    context = build_context(
        env_file=env_file,
        data_dir=tmp_path / "data",
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"message": "任务不存在"})),
    )
    runner = AsyncRunner()
    theme = Theme(dark=True)
    settings_bridge = SettingsBridge(context, runner, theme)
    yield settings_bridge, context, runner, theme, env_file, tmp_path
    runner.close()
    context.history.close()


def wait_until(qt_app, predicate, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- 读取

def test_current_values_never_leaks_secrets(bridge):
    settings_bridge, _, _, _, _, _ = bridge
    values = settings_bridge.currentValues()

    assert values["agnes_key_set"] is True
    assert values["site"] == "api.test"          # 非官方站点时直接显示主机名
    assert "sk-old" not in str(values.values())  # 明文密钥不出现在返回值里
    assert values["github_token_set"] is False
    assert values["network_mode"] in ("auto", "direct", "proxy")


def test_data_dir_property_matches_context(bridge):
    settings_bridge, context, _, _, _, _ = bridge
    assert settings_bridge.dataDir == str(context.data_dir)


def test_search_providers_and_defaults(bridge):
    """设置页的 provider 下拉与「有没有配过 key」：没配过时要一眼看出来。"""
    settings_bridge, _, _, _, _, _ = bridge
    values = settings_bridge.currentValues()

    assert settings_bridge.searchProviders[0] == "tavily"
    assert "off" in settings_bridge.searchProviders
    assert values["search_provider"] == "tavily"
    assert values["search_key_set"] is False
    assert "版权" in settings_bridge.webImagesDisclaimer


# --------------------------------------------------------------------------- 保存

def test_save_agnes_writes_env_and_hot_reloads(bridge):
    settings_bridge, context, _, _, env_file, _ = bridge

    settings_bridge.saveAgnes(
        {"api_key": "sk-new", "base_url": "https://api.agnes-ai.cn/v1"}
    )

    text = env_file.read_text(encoding="utf-8")
    assert "AGNES_API_KEY=sk-new" in text
    assert "AGNES_BASE_URL=https://api.agnes-ai.cn/v1" in text
    assert text.count("AGNES_API_KEY=") == 1          # 不留重复键

    # 运行时凭据已热更新：站点、查询端点、注册表里的客户端都跟着变
    assert context.credentials.agnes.site == "国内版"
    assert context.credentials.agnes.query_endpoint == "https://api.agnes-ai.cn/agnesapi"
    assert context.generation.credentials.agnes.api_key == "sk-new"
    assert context.generation.registry is context.registry


def test_save_agnes_ignores_empty_fields(bridge):
    """密码框留空表示「不修改」，不能把已有密钥清掉。"""
    settings_bridge, context, _, _, env_file, _ = bridge

    settings_bridge.saveAgnes({"api_key": "", "base_url": ""})

    assert "AGNES_API_KEY=sk-old" in env_file.read_text(encoding="utf-8")
    assert context.credentials.agnes.api_key == "sk-old"


def test_save_hosting_and_llm_write_expected_keys(bridge):
    settings_bridge, context, _, _, env_file, _ = bridge

    settings_bridge.saveHosting(
        {"github_token": "ghp_x", "github_repo": "me/images", "see_token": "see_x"}
    )
    settings_bridge.saveLlm(
        {"llm_api_key": "sk-llm", "llm_base_url": "https://llm.example/v1", "llm_model": "m1"}
    )

    creds = load_credentials(env_file)
    assert creds.github.configured and creds.github.repo == "me/images"
    assert creds.see.token == "see_x"
    assert creds.llm.configured and creds.llm.model == "m1"
    assert context.credentials.llm.model == "m1"      # 上下文里的也已刷新


def test_save_search_writes_env_and_keeps_the_capability(bridge):
    """保存联网设置后要**立刻**能用：注册表重建 + 搜索客户端换上新 provider。"""
    settings_bridge, context, _, _, env_file, _ = bridge

    settings_bridge.saveSearch({"provider": "bocha", "api_key": "sk-bocha", "base_url": ""})

    text = env_file.read_text(encoding="utf-8")
    assert "SEARCH_PROVIDER=bocha" in text
    assert "SEARCH_API_KEY=sk-bocha" in text
    assert context.credentials.search.provider == "bocha"
    assert context.credentials.search.configured
    assert context.search.provider == "bocha"
    assert "web.search" in context.registry.names()    # 重建后能力还在


def test_save_search_blank_key_keeps_the_existing_one(bridge):
    settings_bridge, context, _, _, env_file, _ = bridge
    settings_bridge.saveSearch({"provider": "bocha", "api_key": "sk-bocha"})

    settings_bridge.saveSearch({"provider": "serper", "api_key": ""})

    creds = load_credentials(env_file)
    assert creds.search.provider == "serper"
    assert creds.search.api_key == "sk-bocha"          # 留空 = 不改
    assert context.credentials.search.provider == "serper"


def test_save_search_unknown_provider_falls_back(bridge):
    settings_bridge, context, _, _, env_file, _ = bridge
    settings_bridge.saveSearch({"provider": "tavly", "api_key": "sk-x"})
    assert load_credentials(env_file).search.provider == "tavily"
    assert context.credentials.search.provider == "tavily"


def test_web_images_acknowledgement_is_remembered(bridge):
    """免责声明确认过一次就不再弹——这条状态存在 settings.json 里。"""
    settings_bridge, _, _, _, _, _ = bridge
    assert settings_bridge.webImagesAcknowledged is False

    settings_bridge.acknowledgeWebImages()

    assert settings_bridge.webImagesAcknowledged is True
    assert settings.get("web_images_ack") is True


def test_save_network_mode_updates_settings_and_client(bridge):
    settings_bridge, context, _, _, _, _ = bridge

    settings_bridge.saveNetworkMode("direct")

    assert settings.get("network_mode") == "direct"
    assert context.http.network_mode.value == "direct"


def test_save_network_mode_falls_back_on_unknown_value(bridge):
    settings_bridge, context, _, _, _, _ = bridge
    settings_bridge.saveNetworkMode("nonsense")
    assert context.http.network_mode.value == "auto"


def test_save_theme_applies_immediately(bridge, qt_app):
    settings_bridge, _, _, theme, _, _ = bridge

    settings_bridge.saveTheme("light")
    assert settings.get("theme") == "light"
    assert theme.dark is False

    settings_bridge.saveTheme("dark")
    assert theme.dark is True


def test_save_defaults_persists_generation_options(bridge):
    settings_bridge, _, _, _, _, _ = bridge

    settings_bridge.saveDefaults(
        {"img_model": "agnes-image-2.1-flash", "img_size": "512x512",
         "vid_model": "agnes-video-2.5", "vid_seconds": "8", "vid_aspect": "9:16"}
    )

    stored = settings.load()
    assert stored["img_model"] == "agnes-image-2.1-flash"
    assert stored["vid_seconds"] == "8"
    assert stored["vid_aspect"] == "9:16"


def test_env_file_target_is_existing_one(bridge):
    settings_bridge, _, _, _, env_file, _ = bridge
    assert settings_bridge._env_file() == env_file


# --------------------------------------------------------------------------- 连接探测

def test_probe_reports_success_when_both_endpoints_answer(bridge, qt_app):
    settings_bridge, context, _, _, _, _ = bridge
    seen: list[tuple[bool, str]] = []
    settings_bridge.probeFinished.connect(lambda ok, message: seen.append((ok, message)))

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + str(request.url).split("?")[0])
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"message": "任务不存在"})   # 视频查询的预期结果

    # 换掉底层 transport，模拟真实接口
    context.http._transport = httpx.MockTransport(handler)

    settings_bridge.testConnection()
    assert wait_until(qt_app, lambda: bool(seen))
    ok, message = seen[0]

    assert ok is True
    assert "图片端点：正常" in message
    assert "视频查询端点：正常" in message
    assert any("/models" in call for call in calls)
    assert any("/agnesapi" in call for call in calls)


def test_probe_reports_authentication_failure(bridge, qt_app):
    settings_bridge, context, _, _, _, _ = bridge
    seen: list[tuple[bool, str]] = []
    settings_bridge.probeFinished.connect(lambda ok, message: seen.append((ok, message)))

    context.http._transport = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"message": "Invalid token"})
    )

    settings_bridge.testConnection()
    assert wait_until(qt_app, lambda: bool(seen))
    ok, message = seen[0]

    assert ok is False
    assert "认证失败" in message
    assert "站点" in message                       # 提示用户去核对站点


def test_probe_without_key_reports_immediately(qt_app, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("AGNES_BASE_URL=https://api.test/v1\n", encoding="utf-8")
    context = build_context(
        env_file=env_file,
        data_dir=tmp_path / "data",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    runner = AsyncRunner()
    bridge = SettingsBridge(context, runner, Theme())
    seen: list[tuple[bool, str]] = []
    bridge.probeFinished.connect(lambda ok, message: seen.append((ok, message)))

    bridge.testConnection()
    assert wait_until(qt_app, lambda: bool(seen))

    assert seen[0][0] is False
    assert "尚未配置" in seen[0][1]
    runner.close()
    context.history.close()


# --------------------------------------------------------------------------- 找参考与上下文

def test_current_values_expose_context_defaults(bridge):
    """没设过的时候给出方案里的默认值：草稿模式 / 三个来源开 / 配图关 / 阈值 2。"""
    settings_bridge, _, _, _, _, _ = bridge
    values = settings_bridge.currentValues()

    assert values["context_mode"] == "draft"
    assert values["context_sources"] == {
        "history": True, "knowledge": True, "web": True, "web_images": False,
    }
    assert values["min_local_refs"] == 2


def test_save_context_persists_mode_sources_and_threshold(bridge):
    """设置页那张卡片开关一次，三个偏好都要落进 settings.json。"""
    settings_bridge, _, _, _, _, _ = bridge
    seen: list[str] = []
    settings_bridge.saved.connect(lambda section: seen.append(section))

    settings_bridge.saveContext({
        "mode": "auto",
        "sources": {"history": True, "knowledge": False, "web": True, "web_images": True},
        "min_local_refs": 4,
    })

    stored = settings.load()
    assert stored["context_mode"] == "auto"
    assert stored["context_sources"]["knowledge"] is False
    assert stored["context_sources"]["web_images"] is True
    assert stored["min_local_refs"] == 4
    assert settings_bridge.currentValues()["context_sources"]["knowledge"] is False
    assert seen == ["context"]


def test_save_context_clamps_threshold_and_filters_unknown_sources(bridge):
    """界面与脚本都可能给脏值：阈值夹到 0~5，未知来源键丢掉，来源缺项用默认补上。"""
    settings_bridge, _, _, _, _, _ = bridge

    settings_bridge.saveContext({
        "mode": "nonsense",
        "sources": {"knowledge": True, "evil": True},
        "min_local_refs": 99,
    })
    stored = settings.load()
    assert stored["context_mode"] == "draft", "未知模式要回到默认的草稿模式"
    assert stored["min_local_refs"] == 5
    assert "evil" not in stored["context_sources"]
    assert stored["context_sources"] == {
        "history": True, "knowledge": True, "web": True, "web_images": False,
    }

    settings_bridge.saveContext({"mode": "auto", "sources": {}, "min_local_refs": -3})
    assert settings.load()["min_local_refs"] == 0
