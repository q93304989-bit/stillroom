"""Phase 0：旧版缩略图机制的单次成本（对照基线）。

旧版 gallery_ui._thumb_for / history_ui._thumb_for 的做法是：
在渲染循环（UI 线程）里 PIL.open 原图 → LANCZOS 缩放 → 建 CTkImage。
这里只量其中的 PIL 部分：一次同步解码 + 缩放的耗时。

CTkImage 的构造成本无法脱离 customtkinter 测量，故只报 PIL 部分并说明。

用法：
    python tools/phase0/bench_legacy_thumb.py
    python tools/phase0/bench_legacy_thumb.py --data <目录> --samples 200
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

DEFAULT_DIR = os.path.join(tempfile.gettempdir(), "agnes_phase0")

# 旧版两处缩略图尺寸：画廊卡片（按列宽重算）与列表 56x56
TARGETS = {"gallery_card_200": (200, 200), "history_list_56": (56, 56)}


def bench(paths: list[str], target: tuple[int, int]) -> dict:
    durations = []
    for path in paths:
        t0 = time.perf_counter()
        with Image.open(path) as im:
            img = im.copy()          # 旧版就是 copy 之后交给 CTkImage
        img = img.resize(target, Image.LANCZOS)
        durations.append((time.perf_counter() - t0) * 1000)
    return {
        "samples": len(durations),
        "median_ms": round(statistics.median(durations), 2),
        "mean_ms": round(statistics.fmean(durations), 2),
        "p95_ms": round(sorted(durations)[int(len(durations) * 0.95) - 1], 2),
        "max_ms": round(max(durations), 2),
        "total_ms": round(sum(durations), 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="旧版同步缩略图成本")
    ap.add_argument("--data", default=DEFAULT_DIR, help="假数据目录")
    ap.add_argument("--samples", type=int, default=200, help="采样张数")
    ap.add_argument("--json-out", default="", help="结果写 JSON")
    args = ap.parse_args(argv)

    manifest = Path(args.data) / "records.json"
    if not manifest.exists():
        print(f"缺数据：{manifest}\n先跑 python tools/phase0/make_fixtures.py", file=sys.stderr)
        return 2
    records = json.loads(manifest.read_text(encoding="utf-8"))[: args.samples]
    paths = [r["thumb"] for r in records]

    with Image.open(paths[0]) as im:
        src_size = im.size
    print(f"源图 {src_size[0]}x{src_size[1]} · 采样 {len(paths)} 张 · 同步解码 + LANCZOS")
    print()

    result = {"source_size": list(src_size), "samples": len(paths), "targets": {}}
    header = f"{'目标尺寸':<20}{'中位ms':>9}{'均值ms':>9}{'p95ms':>8}{'最大ms':>9}{'200张合计s':>12}"
    print(header)
    for name, target in TARGETS.items():
        stats = bench(paths, target)
        result["targets"][name] = stats
        total_s = stats["total_ms"] / 1000
        print(
            f"{name:<20}{stats['median_ms']:>9}{stats['mean_ms']:>9}"
            f"{stats['p95_ms']:>8}{stats['max_ms']:>9}{total_s:>12.2f}"
        )

    print()
    print("对照：旧版把这些耗时全部算在 UI 线程的一帧里（渲染循环内逐张解码）。")
    print("      Qt Quick 原型把同样的解码放到异步图片线程，Python 侧只做委托创建。")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"结果已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
