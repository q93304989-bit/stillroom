"""图床上传：本地图片 → 公网直链（视频参考图的前提）。

两个提供方，沿用旧版行为与文案：

1. **GitHub 公开仓库**（免费推荐）：Contents API 上传，取 raw 直链；
2. **S.E.E**（付费）：`POST https://s.ee/api/v1/file/upload`，multipart 字段 `file`，
   鉴权是裸 key。

自动路由：只配了 S.E.E 就用 S.E.E，否则配了 GitHub 用 GitHub；都没配时把两份指引
一起抛出去（旧版做法，用户照着填即可）。
"""

from __future__ import annotations

import base64
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

from app.config.credentials import GitHubCredentials, SeeCredentials
from app.config import paths
from app.net.errors import AuthError, ConfigError, ValidationError
from app.net.http import HttpClient

SEE_UPLOAD_URL = "https://s.ee/api/v1/file/upload"
GH_API = "https://api.github.com"
MAX_MB = 5  # 保守上限：图床只用于塞给视频接口，没必要传大图

GH_GUIDE = (
    "GitHub 图床尚未配置。开通步骤（免费，约 5 分钟）：\n"
    "  1. GitHub 新建一个【公开】仓库，如 my-images\n"
    "  2. 右上角头像 → Settings → Developer settings →\n"
    "     Personal access tokens → Generate new token：\n"
    "     · 经典 token：勾选 repo 权限；或\n"
    "     · Fine-grained：仅选中该仓库，Contents 权限选 Read and write\n"
    "  3. 用记事本打开软件目录下的 .env，加两行：\n"
    "     GITHUB_TOKEN=你的token\n"
    "     GITHUB_REPO=你的用户名/my-images\n"
    "  4. 重启软件，再选本地文件即可自动上传\n"
)

SEE_GUIDE = (
    "S.EE 图床（原 SM.MS）自 2026-08 起需付费方案（$5.99/月起）。\n"
    "推荐改用免费的 GitHub 图床（见下方 GitHub 配置指引）。\n"
    "如仍想用 S.EE：登录 https://s.ee → 用户中心生成 API Key →\n"
    ".env 加一行 SEE_API_TOKEN=你的Key"
)


def _check_file(path: str | os.PathLike[str]) -> Path:
    target = Path(paths.local_path(path))
    if not target.is_file():
        raise ValidationError(f"文件不存在：{target}")
    size_mb = target.stat().st_size / (1024 * 1024)
    if size_mb > MAX_MB:
        raise ValidationError(f"图片超过 {MAX_MB}MB（当前 {size_mb:.1f}MB），请压缩后再试")
    return target


def github_headers(credentials: GitHubCredentials) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {credentials.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AgnesStudio/0.1",
    }


async def upload_to_github(
    http: HttpClient,
    credentials: GitHubCredentials,
    path: str | os.PathLike[str],
    *,
    timestamp: float | None = None,
    timeout: float = 60,
) -> str:
    """上传到 GitHub 公开仓库，返回 raw 直链。"""
    target = _check_file(path)
    if not credentials.configured:
        raise ConfigError("GitHub 图床未配置。\n" + GH_GUIDE)

    headers = github_headers(credentials)
    branch = credentials.branch
    if not branch:
        # 未指定分支时取仓库默认分支，避免 main/master 猜错
        try:
            info = await http.get_json(
                f"{GH_API}/repos/{credentials.repo}", headers=headers, timeout=timeout
            )
        except AuthError as exc:
            raise AuthError(
                "GITHUB_TOKEN 无效或已过期，请重新生成。\n" + GH_GUIDE, status=exc.status
            ) from exc
        branch = str((info or {}).get("default_branch") or "main")

    stamp = int((time.time() if timestamp is None else timestamp) * 1000)
    repo_path = f"agnes-refs/{stamp}_{target.name}"
    quoted = urllib.parse.quote(repo_path)
    body = {
        "message": f"upload reference image {target.name}",
        "content": base64.b64encode(target.read_bytes()).decode("ascii"),
        "branch": branch,
    }
    data = await http.put_json(
        f"{GH_API}/repos/{credentials.repo}/contents/{quoted}",
        headers=headers,
        json=body,
        timeout=timeout,
    )
    content = (data or {}).get("content") if isinstance(data, dict) else None
    url = (content or {}).get("download_url") if isinstance(content, dict) else None
    if isinstance(url, str) and url.strip():
        return url.strip()
    return f"https://raw.githubusercontent.com/{credentials.repo}/{branch}/{quoted}"


async def upload_to_see(
    http: HttpClient,
    credentials: SeeCredentials,
    path: str | os.PathLike[str],
    *,
    timeout: float = 120,
) -> str:
    """上传到 S.E.E，返回直链。"""
    target = _check_file(path)
    if not credentials.configured:
        raise ConfigError("S.E.E 图床未配置。\n" + SEE_GUIDE)

    response = await http.request(
        "POST",
        SEE_UPLOAD_URL,
        headers={"Authorization": credentials.token},
        files={"file": (target.name, target.read_bytes(), "application/octet-stream")},
        timeout=timeout,
    )
    try:
        data: Any = response.json()
    except ValueError:
        raise ValidationError(f"图床上传失败：响应不是 JSON（HTTP {response.status_code}）")
    return extract_see_url(data)


def extract_see_url(data: Any) -> str:
    """兼容 S.E.E 的几种返回形态（data.url / images[] / 裸字符串）。"""
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict):
            url = inner.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
        images = data.get("images")
        if isinstance(images, list) and images:
            return str(images[0]).strip()
        if isinstance(images, str) and images.strip():
            return images.strip()
        message = data.get("message") or str(data)[:150]
        raise ValidationError(f"图床上传失败：{message}")
    raise ValidationError(f"图床上传失败：无法解析响应 {str(data)[:150]}")


async def upload_to_image_host(
    http: HttpClient,
    github: GitHubCredentials,
    see: SeeCredentials,
    path: str | os.PathLike[str],
    *,
    provider: str = "auto",
    timestamp: float | None = None,
) -> str:
    """本地图片 → 公网直链（自动 / 指定提供方）。"""
    choice = (provider or "auto").lower()
    if choice == "github":
        return await upload_to_github(http, github, path, timestamp=timestamp)
    if choice in ("see", "smms"):
        return await upload_to_see(http, see, path)
    if see.configured and not github.configured:
        return await upload_to_see(http, see, path)
    if github.configured:
        return await upload_to_github(http, github, path, timestamp=timestamp)
    if see.configured:
        return await upload_to_see(http, see, path)
    raise ConfigError(
        "尚未配置图床（二选一，推荐免费的 GitHub 方案）：\n\n" + GH_GUIDE + "\n" + SEE_GUIDE
    )
