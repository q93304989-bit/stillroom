"""偏好设置：默认值、兼容旧字段、原子保存、目录解析。"""

from __future__ import annotations

import json

from app.config import paths, settings


def test_defaults_when_file_missing():
    loaded = settings.load()
    assert loaded["theme"] == "system"
    assert loaded["network_mode"] == "auto"
    assert loaded["data_dir"] == ""


def test_save_and_reload_roundtrip():
    settings.update(theme="dark", vid_seconds="8")
    settings.reset_cache()
    loaded = settings.load()
    assert loaded["theme"] == "dark"
    assert loaded["vid_seconds"] == "8"


def test_unknown_keys_are_filtered_out():
    settings.save({"theme": "light", "unknown_key": 1})
    raw = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert "unknown_key" not in raw
    assert raw["theme"] == "light"


def test_legacy_file_with_extra_keys_still_loads():
    """旧版 settings.json 里有 gh_token/gh_repo 等字段，新版本要能原样读入。"""
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text(
        json.dumps({"theme": "dark", "gh_token": "ghp_old", "img_model": "agnes-image-2.1-flash"}),
        encoding="utf-8",
    )
    settings.reset_cache()
    loaded = settings.load()
    assert loaded["theme"] == "dark"
    assert loaded["img_model"] == "agnes-image-2.1-flash"


def test_corrupted_file_falls_back_to_defaults():
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text("{ this is not json", encoding="utf-8")
    settings.reset_cache()
    assert settings.load()["theme"] == "system"


def test_get_treats_empty_as_missing():
    settings.save({"img_model": ""})
    assert settings.get("img_model", "fallback-model") == "fallback-model"


def test_data_dir_follows_settings_and_env(monkeypatch, tmp_path):
    configured = tmp_path / "configured"
    settings.save({"data_dir": str(configured)})

    # 夹具默认设了 AGNES_HISTORY_DIR（优先级最高），先摘掉才能验到 settings
    monkeypatch.delenv("AGNES_HISTORY_DIR", raising=False)
    assert settings.data_dir() == configured

    monkeypatch.setenv("AGNES_HISTORY_DIR", str(tmp_path / "env-wins"))
    assert settings.data_dir() == tmp_path / "env-wins"
