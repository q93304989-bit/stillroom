"""生成 Phase 0 用的假画廊数据（真实 JPEG 文件 + 记录清单）。

产物（默认落在系统临时目录，不污染仓库）：
    <dir>/thumbs/<id>.jpg     500 张缩略图源文件
    <dir>/records.json        500 条记录，字段与未来 history 表一致的最小集

用法：
    python tools/phase0/make_fixtures.py --count 500 --size 1024x768
    python tools/phase0/make_fixtures.py --out D:\\bench --count 200
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

DEFAULT_DIR = os.path.join(tempfile.gettempdir(), "agnes_phase0")


def make_source(size: tuple[int, int], seed: int) -> Image.Image:
    """造一张有层次感的源图：纯色块 + 噪点，保证 JPEG 有真实体积"""
    rnd = random.Random(seed)
    base = (rnd.randint(20, 220), rnd.randint(20, 220), rnd.randint(20, 220))
    img = Image.new("RGB", size, base)
    draw = ImageDraw.Draw(img)
    for _ in range(24):
        x0 = rnd.randint(0, size[0] - 40)
        y0 = rnd.randint(0, size[1] - 40)
        x1 = min(size[0], x0 + rnd.randint(40, size[0] // 3))
        y1 = min(size[1], y0 + rnd.randint(40, size[1] // 3))
        color = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
        draw.rectangle([x0, y0, x1, y1], fill=color)
    return img


def build(out_dir: str, count: int, size: tuple[int, int]) -> str:
    root = Path(out_dir)
    thumbs = root / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    records = []
    for i in range(count):
        rec_id = f"rec{i:05d}"
        path = thumbs / f"{rec_id}.jpg"
        if not path.exists():
            img = make_source(size, seed=i)
            img.save(path, "JPEG", quality=82)
        records.append(
            {
                "id": rec_id,
                "kind": "image" if i % 3 else "video",
                "prompt": f"假记录 {i}：日落时分薄雾峡谷上方的发光浮空城市",
                "thumb": str(path),
                "bytes": path.stat().st_size,
            }
        )
    elapsed = time.perf_counter() - t0

    manifest = root / "records.json"
    manifest.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    total_mb = sum(r["bytes"] for r in records) / 1024 / 1024
    print(f"OK 生成 {count} 条记录 -> {root}")
    print(f"   图片合计 {total_mb:.1f}MB，耗时 {elapsed:.1f}s")
    print(f"   清单 {manifest}")
    return str(root)


def parse_size(text: str) -> tuple[int, int]:
    w, _, h = text.lower().partition("x")
    return int(w), int(h)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 Phase 0 假数据")
    ap.add_argument("--out", default=DEFAULT_DIR, help=f"输出目录，默认 {DEFAULT_DIR}")
    ap.add_argument("--count", type=int, default=500, help="记录条数，默认 500")
    ap.add_argument("--size", default="1024x768", help="源图尺寸，默认 1024x768")
    args = ap.parse_args(argv)
    build(args.out, args.count, parse_size(args.size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
