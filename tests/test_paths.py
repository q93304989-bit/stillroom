"""路径解析的行为，重点是「优先级」和「打包/源码两种形态」。"""

from __future__ import annotations

from pathlib import Path

from app.config import paths


def test_env_candidates_order(monkeypatch, tmp_path):
    """运行目录 > 资源目录 > 当前目录，且不重复。"""
    monkeypatch.setattr(paths, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "resource_dir", lambda: tmp_path / "bundled")
    monkeypatch.chdir(tmp_path)

    candidates = paths.env_candidates()

    assert candidates[0] == tmp_path / ".env"
    assert len(candidates) == len(set(candidates))


def test_env_candidates_skips_duplicate_when_source_run(monkeypatch, tmp_path):
    """源码运行时运行目录 == 资源目录，候选里只能出现一次。"""
    monkeypatch.setattr(paths, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "resource_dir", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)

    assert paths.env_candidates() == [tmp_path / ".env"]


def test_settings_file_override(monkeypatch, tmp_path):
    target = tmp_path / "custom-settings.json"
    monkeypatch.setenv("AGNES_SETTINGS_FILE", str(target))
    assert paths.settings_file() == target


def test_settings_file_default_under_appdata(monkeypatch, tmp_path):
    monkeypatch.delenv("AGNES_SETTINGS_FILE", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert paths.settings_file() == tmp_path / paths.CONFIG_DIR_NAME / "settings.json"


def test_data_dir_precedence(monkeypatch, tmp_path):
    """环境变量 > settings.data_dir > 默认目录。"""
    monkeypatch.setenv("AGNES_HISTORY_DIR", str(tmp_path / "from-env"))
    assert paths.data_dir({"data_dir": str(tmp_path / "from-settings")}) == tmp_path / "from-env"

    monkeypatch.delenv("AGNES_HISTORY_DIR")
    assert paths.data_dir({"data_dir": str(tmp_path / "from-settings")}) == tmp_path / "from-settings"

    assert paths.data_dir({"data_dir": ""}) == paths.default_data_dir()


def test_default_data_dir_matches_legacy_on_windows():
    """交付一致性：Windows 默认数据目录与旧版一致。"""
    if paths.os.name == "nt":
        assert paths.default_data_dir() == Path(paths.DEFAULT_DATA_DIR_WINDOWS)


def test_ensure_dir_creates(tmp_path):
    target = tmp_path / "a" / "b"
    assert paths.ensure_dir(target) == target
    assert target.is_dir()
