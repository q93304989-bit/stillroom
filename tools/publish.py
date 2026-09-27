"""把本地代码同步到公开仓库，并推送。

    python tools/publish.py                  # 同步 + 提交 + 推送
    python tools/publish.py --dry-run        # 只看会改什么，不写不推
    python tools/publish.py --no-push        # 同步并提交到本地发布区，但不推远端
    python tools/publish.py --branch main    # 推 main（默认 develop）

## 为什么要这个脚本

方案 B：**本地开发，发版时同步到公开仓库**。手工挑文件同步正是最容易出错的地方
（本项目已经撞过几次「改了一处、漏了另一处」），所以把规则写死成脚本：

1. **同步什么**：本地 git 已跟踪的全部文件，**排除 `docs/` 与 `.idea/`**
   （公开仓库只放可运行代码——决定见 CHANGELOG 的 v1.0.0）。
   用 `git archive` 导出，所以未跟踪的临时文件不会被带过去。
2. **发布区在哪**：`.publish/`（项目内、已 gitignore）。如果不存在就 clone 一份，
    然后**切到目标分支**（`--branch`，默认 develop）。切之前会丢掉发布区里的脏改动——
    那里的内容由本脚本接管，手工改动不该留在那儿。
    放在项目内而不是系统临时目录——临时目录会被清理，发布区丢了很麻烦。
3. **推送前一定做密钥检查**：`.env` 绝不能被带进去；扫一遍 `sk-*` / `ghp_*` / `apikey_*`。
   有发现就**中止**，不推。
4. **有改动才提交**；没有改动就只报告，不制造空提交。
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

#: Windows 控制台默认是 GBK，中文/符号直接 print 会抛 UnicodeEncodeError。
#: 把标准输出强制成 UTF-8（改不了就退化成替换，绝不因为「打不出字」而崩）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):        # pragma: no cover - 老环境
        pass

ROOT = Path(__file__).resolve().parents[1]
PUBLISH_DIR = ROOT / ".publish"

#: 不公开的顶层目录（与 CHANGELOG 的 v1.0.0 决定一致）
EXCLUDED = ("docs", ".idea")

#: 公开仓库「额外忽略」块的锚点，见 apply_public_ignores()
PUBLIC_IGNORE_MARKER = "# 公开仓库额外忽略（由 tools/publish.py 依据 EXCLUDED 补齐）"

#: 密钥形态（推送前必须扫到 0 处）
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"apikey_[a-z0-9]{20,}"),
)

DEFAULT_REMOTE = "https://github.com/q93304989-bit/stillroom.git"


def run(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        args, cwd=str(cwd or ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SystemExit(f"命令失败：{' '.join(args)}\n{detail[:800]}")
    return proc


# --------------------------------------------------------------------------- 导出

def export_to(target: Path) -> int:
    """把「已跟踪 + 排除的目录」导出到 target（清空后重建）。返回文件数。"""
    # git archive 出的是二进制流，subprocess 必须用 capture_output 收字节
    # （用 text=True 会把内容按编码解坏，tar 就解不开了）
    proc = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD", "--", ".",
         *[f":(exclude){d}" for d in EXCLUDED]],
        cwd=str(ROOT), capture_output=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git archive 失败：{proc.stderr.decode('utf-8', 'replace')[:400]}")

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tf:
        for member in tf.getmembers():
            # 只接受普通文件与目录；拒绝软链与绝对路径（防意外写到别处）
            if member.isdir():
                continue
            if not member.isfile():
                continue
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise SystemExit(f"tar 里有异常路径：{member.name}")
            tf.extract(member, path=target, filter="data")
            count += 1
    return count


def secret_scan(root: Path) -> list[str]:
    """扫密钥。返回命中的「文件:行号」列表（空表示干净）。"""
    hits: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in (
            ".py", ".qml", ".md", ".toml", ".txt", ".json", ".example",
            ".spec", ".lock", ".gitignore", ".yml", ".yaml", "", ".cfg", ".ini",
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    hits.append(f"{path.relative_to(root)}:{number}")
                    break
    return hits


# --------------------------------------------------------------------------- 发布区

def ensure_repo(dry_run: bool) -> Path:
    """确保 `.publish/` 是一个指向远端的 git 仓库。"""
    if (PUBLISH_DIR / ".git").exists():
        return PUBLISH_DIR
    if dry_run:
        return PUBLISH_DIR          # dry-run 不需要真的建仓库，只导出检查
    print(f"发布区不存在，先 clone 一份到 {PUBLISH_DIR}")
    PUBLISH_DIR.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", DEFAULT_REMOTE, str(PUBLISH_DIR)], cwd=ROOT.parent)
    return PUBLISH_DIR


def switch_branch(repo: Path, branch: str) -> None:
    """把发布区切到 branch。分支不存在就按 origin/<branch> 建，再不行就新建。

    这是之前漏掉的一步：脚本把提交落在「发布区当前所在的分支」上，日志里却写
    args.branch，于是改动提交到了错的分支（--branch 只作用在 push 上）。
    实测：在 .publish 的 main 上留下了提交，却打印「已提交到发布区（develop）」。
    """
    current = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip()
    if current == branch:
        return

    # 发布区的内容由本脚本接管，脏改动留着只会挡住 checkout
    run(["git", "reset", "--hard", "HEAD"], cwd=repo)
    run(["git", "clean", "-fd"], cwd=repo)

    def has(ref: str) -> bool:
        return run(["git", "rev-parse", "--verify", "--quiet", ref],
                   cwd=repo, check=False).returncode == 0

    if has(f"refs/heads/{branch}"):
        run(["git", "checkout", branch], cwd=repo)
    elif has(f"refs/remotes/origin/{branch}"):
        run(["git", "checkout", "-b", branch, f"origin/{branch}"], cwd=repo)
    else:
        run(["git", "checkout", "-b", branch], cwd=repo)

    # 只做快进：发布区若已有未推送的提交，ff-only 会失败——那些提交必须保住
    run(["git", "fetch", "origin", branch], cwd=repo, check=False)
    run(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=repo, check=False)
    print(f"发布区已切到分支 {branch}")


def apply_public_ignores(repo: Path) -> None:
    """给发布区的 .gitignore 补上「只在公开仓库成立」的忽略规则，幂等。

    本地 .gitignore **不能**忽略 `docs/`（本地要跟踪它，忽略了新写的文档会被
    静默吞掉），公开仓库却只放可运行代码。不补这一块的话，每次同步都会把公开
    仓库那份带 `docs/` 的 .gitignore 覆盖掉——同一份规则在两边来回翻烙饼，
    正是这个脚本要消灭的那类「改了一处、漏了另一处」。
    """
    path = repo / ".gitignore"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    # 去掉上一次写的块，重新按当前的 EXCLUDED 生成——这样 EXCLUDED 改了也能收敛
    head = text.split(PUBLIC_IGNORE_MARKER)[0].rstrip("\n")
    # 必须按「整行」比：本地 .gitignore 里的 `docs/design/.shotdata/` 也含有 `docs/`，
    # 用子串判断会误以为已经忽略过 docs/（第一版就踩了这个坑）。
    ignored = {line.strip() for line in head.splitlines()}
    extra = [name for name in EXCLUDED if f"{name}/" not in ignored]
    block = ""
    if extra:
        block = "\n" + PUBLIC_IGNORE_MARKER + "\n" + "\n".join(f"{name}/" for name in extra) + "\n"
    updated = head + "\n" + block
    if updated == text:
        return          # 内容没变就什么都不做：不重写、不重复打印（连跑两次应当安静）
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    print(f"  公开仓库 .gitignore 额外忽略：{'、'.join(extra) if extra else '无（清理了过期条目）'}")


def sync_files(repo: Path) -> tuple[int, list[str]]:
    """把代码同步进发布区（不动 .git）。返回 (文件数, 相对发布区的改动列表)。"""
    staging = repo / ".sync-staging"
    count = export_to(staging)

    # 先清掉仓库里除 .git 与暂存区之外的东西，再搬进来——
    # 这样「本地删了文件」也能同步过去（否则远端会留着一个已删的旧文件）。
    for entry in repo.iterdir():
        if entry.name in (".git", ".sync-staging"):
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()

    for entry in staging.iterdir():
        shutil.move(str(entry), str(repo / entry.name))
    staging.rmdir()

    apply_public_ignores(repo)

    status = run(["git", "status", "--porcelain"], cwd=repo).stdout
    changed = [line for line in status.splitlines() if line.strip()]
    return count, changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把本地代码同步到公开仓库并推送")
    parser.add_argument("--dry-run", action="store_true", help="只看会改什么，不写不推")
    parser.add_argument("--no-push", action="store_true", help="提交到发布区但不推远端")
    parser.add_argument("--branch", default="develop", help="推哪个分支（默认 develop）")
    parser.add_argument("--message", default="", help="提交信息（留空自动生成）")
    args = parser.parse_args(argv)

    repo = ensure_repo(args.dry_run)

    if not args.dry_run:
        switch_branch(repo, args.branch)

    # 1) 同步前先看本地有没有未提交改动——有就提醒（导出的是 HEAD，不含工作区改动）
    dirty = run(["git", "status", "--porcelain"]).stdout.strip()
    if dirty:
        print("[注意] 本地有未提交的改动，它们【不会】被同步（导出的是 HEAD）：")
        for line in dirty.splitlines()[:10]:
            print(f"    {line}")
        print("  想同步就先 commit。")
        print()

    # 2) 导出
    if args.dry_run:
        probe = ROOT / ".publish-dryrun"
        count = export_to(probe)
        hits = secret_scan(probe)
        shutil.rmtree(probe)
        print(f"（演练）会写入 {count} 个文件；密钥扫描：{'发现 ' + str(len(hits)) if hits else '干净'}")
        if hits:
            for h in hits[:10]:
                print(f"    {h}")
        return 0

    count, changed = sync_files(repo)
    print(f"已从本地导出 {count} 个文件（排除 {', '.join(EXCLUDED)}）")

    if not changed:
        print("发布区与本地一致，没有需要提交的改动。")
        if not args.no_push:
            ahead = run(["git", "log", "--oneline", f"origin/{args.branch}..{args.branch}"],
                        cwd=repo, check=False).stdout.strip()
            if ahead:
                print("但本地发布区有未推送的提交，正在推送…")
                run(["git", "push", "origin", args.branch], cwd=repo)
                print("已推送。")
        return 0

    print(f"检测到 {len(changed)} 处改动：")
    for line in changed[:20]:
        print(f"    {line}")
    if len(changed) > 20:
        print(f"    …还有 {len(changed) - 20} 处")

    # 3) 密钥检查（发现就中止，绝不推）
    hits = secret_scan(repo)
    if hits:
        print()
        print("[中止] 发现疑似密钥，不会提交、不会推送：")
        for h in hits[:10]:
            print(f"    {h}")
        print("  请先从本地删掉/忽略这些内容，再重跑。")
        return 1
    print("密钥扫描：干净")

    # 4) 提交 + 推送
    message = args.message or "sync: 从本地同步代码"
    run(["git", "add", "-A"], cwd=repo)
    run(["git", "commit", "-m", message], cwd=repo)
    print(f"已提交到发布区（{args.branch}）")

    if args.no_push:
        print("（--no-push：未推送。想推就重跑不带这个参数）")
        return 0

    run(["git", "push", "origin", args.branch], cwd=repo)
    print(f"已推送到 origin/{args.branch}")
    head = run(["git", "log", "--oneline", "-1"], cwd=repo).stdout.strip()
    print(f"  最新：{head}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
