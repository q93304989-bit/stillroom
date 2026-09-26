"""Phase 2 冒烟：跑完整编排链路，并实时打印结构化事件。

默认只读（不发请求）：

    .venv\\Scripts\\python.exe tools/phase2_smoke.py

真实运行（会消耗额度）：

    .venv\\Scripts\\python.exe tools/phase2_smoke.py --image
    .venv\\Scripts\\python.exe tools/phase2_smoke.py --video
    .venv\\Scripts\\python.exe tools/phase2_smoke.py --video --cancel-after 12   # 演示中途取消

历史（落在数据目录的 SQLite 里）：

    .venv\\Scripts\\python.exe tools/phase2_smoke.py --list-history
    .venv\\Scripts\\python.exe tools/phase2_smoke.py --clear-history
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bootstrap import AppContext, build_context  # noqa: E402
from app.clients.image_client import ImageRequest  # noqa: E402
from app.clients.video_client import VideoRequest  # noqa: E402
from app.config import paths  # noqa: E402
from app.net.errors import AppError  # noqa: E402
from app.state.events import Event  # noqa: E402


def make_printer(start: float):
    """事件打印机：把结构化事件按时间线打出来（界面将来订阅的是同一份数据）。"""

    def _print(event: Event) -> None:
        ts = f"+{event.ts - start:6.1f}s"
        detail = ""
        if event.type == "tool.called":
            detail = f"tool={event.get('tool_name')}"
            throttled = event.get("throttled_seconds") or 0
            if throttled:
                detail += f" 限流等待={throttled:.0f}s"
        elif event.type == "tool.returned":
            detail = f"tool={event.get('tool_name')}"
        elif event.type == "job.progress":
            detail = str(event.get("message", ""))
        elif event.type == "job.retrying":
            detail = str(event.get("message", ""))
        elif event.type == "artifact.created":
            detail = f"{event.get('kind')} {event.get('path')} ({event.get('bytes')} B)"
        elif event.type in ("job.succeeded", "job.failed"):
            detail = str(event.get("message") or event.get("error") or "")
        elif event.type == "job.canceled":
            detail = str(event.get("message", ""))
        print(f"  {ts}  {event.type:<16} {detail}")

    return _print


def print_overview(ctx: AppContext) -> None:
    agnes = ctx.credentials.agnes
    print("== 运行环境 ==")
    print(f"  .env 候选 : {', '.join(str(p) for p in paths.env_candidates())}")
    print(f"  数据目录  : {ctx.data_dir}")
    print(f"  接口地址  : {agnes.base_url}（{agnes.site}）")
    print(f"  密钥      : {'已配置' if agnes.api_key else '未配置'}")
    print(f"  视频查询  : {agnes.query_endpoint}")
    print(f"  网络模式  : {ctx.http.network_mode.value}")
    print()
    print("== 能力与限额（限额来自能力元数据） ==")
    for spec in ctx.registry.specs():
        limit = ",".join(f"{k}={v}" for k, v in (spec.rate_limit or {}).items()) or "-"
        print(f"  {spec.name:<20} 副作用={'/'.join(sorted(spec.side_effects)) or '-':<28} 限额={limit}")
    print()
    print(f"== 历史（{ctx.history.path}）== 共 {ctx.history.count()} 条")


def _duration(job) -> str:
    return f"{job.duration:.1f}s" if job.duration else "-"


def print_history(ctx: AppContext, limit: int = 10) -> None:
    records = ctx.history.list(limit=limit)
    if not records:
        print("  （空）")
        return
    for record in records:
        stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(record.created_at))
        prompt = record.prompt if len(record.prompt) <= 24 else record.prompt[:24] + "…"
        extra = record.result_url or record.media_path or record.error or ""
        print(f"  {stamp}  {record.kind:<5} {record.status:<8} {prompt:<26} {extra[:60]}")


async def run_image(ctx: AppContext, printer) -> int:
    ctx.bus.subscribe(printer)
    print("\n== 文生图 ==")
    job = await ctx.generation.start_image(
        ImageRequest(prompt="清晨薄雾中的浮空城市，赛博朋克风格", size="1024x768")
    ).wait()
    print(f"  状态={job.status.value} 耗时={_duration(job)} 尝试={job.attempts}")
    if job.status.value != "succeeded":
        print(f"  失败：{job.error}")
        return 2
    print(f"  URL   : {job.result['url']}")
    print(f"  本地  : {job.result['media_path']}")
    return 0


async def run_video(ctx: AppContext, printer, prompt: str, cancel_after: float) -> int:
    ctx.bus.subscribe(printer)
    print(f"\n== 视频生成 ==（提示词：{prompt}）")
    handle = ctx.generation.start_video(VideoRequest(prompt=prompt, seconds="5"))

    if cancel_after > 0:
        await asyncio.sleep(cancel_after)
        print(f"  → {cancel_after:.0f}s 后请求取消（服务端任务可能仍在跑）")
        handle.cancel()

    job = await handle.wait()
    print(f"  状态={job.status.value} 尝试={job.attempts}")
    if job.status.value == "succeeded":
        print(f"  URL {job.result['url']}（video_id={job.result['video_id']}）")
        return 0
    if job.status.value == "canceled":
        return 0
    print(f"  失败：{job.error}")
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 2 冒烟（默认只读）")
    ap.add_argument("--image", action="store_true", help="真实跑一次文生图")
    ap.add_argument("--video", action="store_true", help="真实跑一次文生视频（含轮询）")
    ap.add_argument("--cancel-after", type=float, default=0.0, help="N 秒后取消（配合 --video）")
    ap.add_argument("--prompt", default="一只在窗台上打盹的橘猫，午后阳光")
    ap.add_argument("--list-history", action="store_true")
    ap.add_argument("--clear-history", action="store_true")
    ap.add_argument("--cache-videos", action="store_true", help="视频也下载到本地")
    args = ap.parse_args(argv)

    ctx = build_context(cache_videos=args.cache_videos)
    start = time.time()
    try:
        print_overview(ctx)
        if args.clear_history:
            removed = ctx.history.clear()
            print(f"\n已清空 {removed} 条历史")

        if args.list_history:
            print("\n== 最近历史 ==")
            print_history(ctx)
            return 0

        if not (args.image or args.video):
            print("\n== 最近历史 ==")
            print_history(ctx)
            print("\n（只读模式，未发起请求。加 --image / --video 才会真实调用。）")
            return 0

        printer = make_printer(start)
        if args.video:
            return asyncio.run(run_video(ctx, printer, args.prompt, args.cancel_after))
        return asyncio.run(run_image(ctx, printer))
    except AppError as exc:
        print(f"\n失败：{exc.user_message}")
        return 2
    finally:
        asyncio.run(ctx.aclose())


if __name__ == "__main__":
    sys.exit(main())
