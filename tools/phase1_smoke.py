"""Phase 1 冒烟：验证「地基层 + 客户端 + 能力注册表」在真实接口上能跑通。

默认**不发任何请求**，只打印配置与能力清单（可随时安全运行）：

    .venv\\Scripts\\python.exe tools/phase1_smoke.py

真实调用（会消耗额度，自行决定）：

    .venv\\Scripts\\python.exe tools/phase1_smoke.py --image           # 文生图一次
    .venv\\Scripts\\python.exe tools/phase1_smoke.py --video           # 提交视频任务（每分钟限 1 个）
    .venv\\Scripts\\python.exe tools/phase1_smoke.py --video --wait    # 提交并轮询到完成
    .venv\\Scripts\\python.exe tools/phase1_smoke.py --query <video_id>

轮询策略只在这里做最简实现——正式的退避/限流/取消属于 Phase 2 的编排层。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.capabilities.registry import build_registry  # noqa: E402
from app.clients.image_client import ImageRequest  # noqa: E402
from app.clients.video_client import (  # noqa: E402
    DONE_STATUSES,
    FAILED_STATUSES,
    VideoRequest,
    extract_video_id,
)
from app.config import paths, settings  # noqa: E402
from app.config.credentials import load_credentials  # noqa: E402
from app.net.errors import AppError  # noqa: E402
from app.net.http import HttpClient  # noqa: E402


def describe_environment() -> None:
    creds = load_credentials()
    print("== 环境 ==")
    print(f"  运行目录   : {paths.runtime_dir()}")
    print(f"  .env 候选  : {', '.join(str(p) for p in paths.env_candidates())}")
    print(f"  数据目录   : {settings.data_dir()}")
    print(f"  网络模式   : {settings.get('network_mode', 'auto')}")
    print()
    print("== 凭据 ==")
    agnes = creds.agnes
    print(f"  Agnes Key  : {'已配置' if agnes.api_key else '未配置'}")
    print(f"  接口地址   : {agnes.base_url}（{agnes.site}）")
    print(f"  图片端点   : {agnes.images_endpoint}")
    print(f"  视频提交   : {agnes.videos_endpoint}")
    print(f"  视频查询   : {agnes.query_endpoint}")
    print(f"  图床       : GitHub={'是' if creds.github.configured else '否'} / S.E.E={'是' if creds.see.configured else '否'}")
    llm = creds.llm
    print(f"  LLM        : {llm.provider or '未配置'}{f' · {llm.model}' if llm.model else ''}")
    print()


def describe_capabilities() -> None:
    creds = load_credentials()
    http = HttpClient(network_mode=settings.get("network_mode", "auto"))
    registry = build_registry(http=http, credentials=creds)

    print("== 能力注册表 ==")
    print(f"  {'名称':<20}{'副作用':<34}{'幂等':<6}{'限额':<18}{'超时'}")
    for spec in registry.specs():
        effects = ",".join(sorted(spec.side_effects)) or "-"
        limit = ",".join(f"{k}={v}" for k, v in (spec.rate_limit or {}).items()) or "-"
        print(
            f"  {spec.name:<20}{effects:<34}{'是' if spec.idempotent else '否':<6}"
            f"{limit:<18}{spec.timeout_s:g}s"
        )
    print()
    print("  提示：视频提交的 per_minute=1 是平台硬限制，写进元数据后调度器与界面都从这里取。")
    print()


async def run_image() -> int:
    creds = load_credentials()
    async with HttpClient(network_mode=settings.get("network_mode", "auto")) as http:
        registry = build_registry(http=http, credentials=creds)
        started = time.perf_counter()
        url = await registry.invoke("image.generate", {"prompt": "清晨薄雾中的浮空城市，赛博朋克风格"})
        cost = time.perf_counter() - started
    print(f"\n== 文生图 ==\n  完成，耗时 {cost:.1f}s\n  URL: {url}")
    return 0


async def run_video(prompt: str, wait: bool, max_polls: int) -> int:
    creds = load_credentials()
    async with HttpClient(network_mode=settings.get("network_mode", "auto")) as http:
        registry = build_registry(http=http, credentials=creds)
        data = await registry.invoke(
            "video.submit", {"prompt": prompt, "model": creds.agnes.video_model}
        )
        video_id = extract_video_id(data)
        print(f"\n== 视频提交 ==\n  响应: {data}")
        if not video_id:
            print("  没有拿到 video_id，无法继续")
            return 1
        print(f"  video_id: {video_id}")

        if not wait:
            print("  未加 --wait，跳过后台轮询。可稍后用 --query 查询。")
            return 0

        print("  开始轮询（平台限制：查询过密会被限流，这里 10 秒一次）...")
        for index in range(1, max_polls + 1):
            await asyncio.sleep(10)
            try:
                status = await registry.invoke(
                    "video.query",
                    {"video_id": video_id, "model_name": creds.agnes.video_model},
                )
            except AppError as exc:
                print(f"  [{index}] 查询失败：{exc.user_message}")
                continue
            state = str(status.get("status", "unknown"))
            progress = status.get("progress", "-")
            print(f"  [{index}] {state} · 进度 {progress}%")
            if state in DONE_STATUSES:
                if state in FAILED_STATUSES:
                    print(f"  任务失败：{status.get('error') or status}")
                    return 1
                print(f"  完成：{status.get('url')}")
                return 0
        print("  轮询次数用尽，任务可能仍在服务端运行")
        return 1


async def run_query(video_id: str) -> int:
    creds = load_credentials()
    async with HttpClient(network_mode=settings.get("network_mode", "auto")) as http:
        registry = build_registry(http=http, credentials=creds)
        status = await registry.invoke(
            "video.query", {"video_id": video_id, "model_name": creds.agnes.video_model}
        )
    print(f"\n== 查询 {video_id} ==\n  {status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 1 冒烟（默认不发请求）")
    ap.add_argument("--image", action="store_true", help="真实跑一次文生图")
    ap.add_argument("--video", action="store_true", help="真实提交一个视频任务")
    ap.add_argument("--wait", action="store_true", help="提交后轮询到完成")
    ap.add_argument("--query", default="", help="查询已有任务的 video_id")
    ap.add_argument("--prompt", default="一只在窗台上打盹的橘猫，午后阳光")
    ap.add_argument("--max-polls", type=int, default=90, help="最多轮询次数（默认 90 × 10s）")
    args = ap.parse_args(argv)

    describe_environment()
    describe_capabilities()

    if not (args.image or args.video or args.query):
        print("（当前为只读模式，未发起任何请求。加 --image / --video / --query 才会真实调用。）")
        return 0

    try:
        if args.query:
            return asyncio.run(run_query(args.query))
        if args.video:
            return asyncio.run(run_video(args.prompt, args.wait, args.max_polls))
        return asyncio.run(run_image())
    except AppError as exc:
        print(f"\n失败：{exc.user_message}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
