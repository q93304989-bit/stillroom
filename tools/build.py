"""打包成绿色目录（onedir）。

    python tools/build.py                 # 默认：不含内置视频播放
    python tools/build.py --with-video    # 含 QtMultimedia（+约 15MB）
    python tools/build.py --clean         # 先清掉 build/dist 再打

产物：`dist/Stillroom/`，双击里面的 `Stillroom.exe` 即可运行；
把 `.env` 放在 exe 同级目录即可沿用旧版的配置习惯。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "Stillroom.spec"
DIST = ROOT / "dist" / "Stillroom"
BUILD_DIR = ROOT / "build" / "Stillroom"

README = """Stillroom — 使用说明
========================================

1. 运行
   双击 Stillroom.exe 即可。首次启动会在数据目录里建好历史库。

2. 配置密钥
   把 .env.example 复制成 .env（放在本文件同级目录，也就是 exe 旁边），
   至少填一项：
       AGNES_API_KEY=sk-...
       AGNES_BASE_URL=https://apihub.agnes-ai.com/v1
   国内版把 AGNES_BASE_URL 换成 https://api.agnes-ai.cn/v1
   （视频查询端点会由该地址自动推导，不用手填）

   也可以在软件内的「设置」页里填，保存后立刻生效、不需要重启。

3. 数据放在哪
   默认 F:\\AgnesGeneratorData：history.db（历史记录）+ media/（图片视频缓存）
   + thumbs/（缩略图）。设置页可以改目录，也可以一键打开。

4. 视频参考图
   视频接口只接受公网图片直链。本地图片请在视频页点「上传本地图」，
   先把图片传到你自己的图床（GitHub 公开仓库免费，或 S.E.E），拿到直链后再提交。

5. 自检
   命令行运行 Stillroom.exe --self-test 可以不开窗口验证程序是否正常，
   结果会写到同目录的 self-test.log。

6. 提示
   本目录里的其他文件都是程序运行需要的，请不要单独移动 exe。
"""


def dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return total / 1024 / 1024


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包 Stillroom")
    ap.add_argument("--with-video", action="store_true", help="打包内置视频播放（+约 15MB）")
    ap.add_argument("--clean", action="store_true", help="先删除 build/ 与 dist/")
    args = ap.parse_args(argv)

    if args.clean:
        for target in (ROOT / "build", ROOT / "dist"):
            if target.exists():
                shutil.rmtree(target)
                print(f"已删除 {target}")

    env = dict(os.environ)
    env["AGNES_WITH_VIDEO"] = "1" if args.with_video else "0"

    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    cost = time.perf_counter() - started

    if result.returncode != 0:
        print(result.stdout[-4000:])
        print(result.stderr[-4000:], file=sys.stderr)
        print(f"打包失败（{cost:.0f}s）")
        return result.returncode

    exe = DIST / "Stillroom.exe"

    # 中间产物 build/<name>/<name>.exe 和 dist 里的正式 exe 同名，但它旁边没有
    # _internal，双击会报「Failed to load Python DLL」。构建完就把它删掉，免得点错。
    stray = BUILD_DIR / "Stillroom.exe"
    if stray.exists():
        try:
            stray.unlink()
            print(f"已清理中间产物：{stray}")
        except OSError as exc:                       # pragma: no cover
            print(f"提示：未能删除中间产物 {stray}（{exc}），请忽略它、只运行 dist 里的 exe")

    if exe.exists():
        (DIST / "使用说明.txt").write_text(README, encoding="utf-8")
        # .env.example 也放一份在 exe 同级：用户要照着它建 .env，放在 _internal 里等于看不见
        example = ROOT / ".env.example"
        if example.exists():
            shutil.copy2(example, DIST / ".env.example")

    print(f"打包完成，用时 {cost:.0f}s")
    print(f"  目录：{DIST}")
    print(f"  体积：{dir_size_mb(DIST):.1f}MB" + ("（含视频播放）" if args.with_video else ""))
    if exe.exists():
        print(f"  入口：{exe}（{exe.stat().st_size / 1024 / 1024:.1f}MB）")
        # 这里不要用 ▶ 之类的符号：GBK 控制台编不出来，会把这行打印变成异常，
        # 于是打包明明成功、退出码却是 1（脚本化打包会被误判成失败）。
        print(f"  请运行这一个：dist\\Stillroom\\Stillroom.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
