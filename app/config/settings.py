"""偏好设置（`settings.json`）的唯一入口。

存储位置沿用旧版：`%APPDATA%\\AgnesGenerator\\settings.json`（可用
`AGNES_SETTINGS_FILE` 覆盖，便于测试与绿色版）。

字段与旧版完全一致，因此**用户已有的 settings.json 可以原样读进来**。
注意：密钥不放这里——它只属于 `.env`（见 `credentials`）。旧版曾把 GitHub
token 也写进 settings，新版本读取时忽略这些字段。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from app.config import paths

_LOCK = threading.RLock()
_cache: dict | None = None

DEFAULTS: dict = {
    "theme": "system",             # system / light / dark
    "theme_auto_restart": False,   # 切换主题后重建窗口
    "network_mode": "auto",        # auto / direct / proxy
    "data_dir": "",                # 空 = 使用 paths.default_data_dir()
    "context_mode": "draft",       # draft（先出上下文再跑）/ auto（直接跑）
    "context_sources": {},         # 来源开关的默认值；空 = 用 context_store.DEFAULT_SOURCES
    "min_local_refs": 2,           # 本地参考少于几条就联网（0~5）
    "web_images_ack": False,       # 「联网配图」的免责声明确认过一次就不再弹
    "img_model": "",               # 空 = 用列表第一项
    "img_size": "",
    "vid_model": "",
    "vid_seconds": "",
    "vid_aspect": "",
    # 兼容旧版字段：新版不再从这里读密钥，仅保留键位避免旧文件被判为损坏
    "gh_token": "",
    "gh_repo": "",
}


def load() -> dict:
    """读取设置（默认值兜底；文件缺失或损坏返回默认值）。"""
    global _cache
    with _LOCK:
        if _cache is not None:
            return dict(_cache)
        merged = dict(DEFAULTS)
        try:
            raw = json.loads(paths.settings_file().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key in merged:
                        merged[key] = value
        except (OSError, ValueError):
            pass
        _cache = merged
        return dict(_cache)


def save(new_settings: dict) -> None:
    """整体保存（覆盖式，原子替换）。"""
    global _cache
    with _LOCK:
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in (new_settings or {}).items() if k in merged})
        path = paths.settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
        _cache = merged


def update(**fields) -> dict:
    """局部更新若干项并保存，返回更新后的完整设置。"""
    merged = load()
    merged.update({k: v for k, v in fields.items() if k in DEFAULTS})
    save(merged)
    return merged


def get(key: str, fallback=None):
    """读取单项（空串视为未设置）。"""
    value = load().get(key, DEFAULTS.get(key))
    return value if value not in (None, "") else fallback


def reset_cache() -> None:
    """清空内存缓存（外部改了 settings.json 后强制重读）。"""
    global _cache
    with _LOCK:
        _cache = None


def data_dir() -> Path:
    """当前生效的数据目录（环境变量 > 设置 > 默认）。"""
    return paths.data_dir(load())
