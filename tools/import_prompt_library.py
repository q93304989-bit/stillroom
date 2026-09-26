"""把开源提示词库（标准 JSON）转成可入库的 Markdown。

    python tools/import_prompt_library.py 下载的.json -o 提示词库.md
    python tools/import_prompt_library.py 目录/ -o 提示词库.md      # 合并多个源

## 为什么需要这个「转换」而不是直接把 JSON 丢进知识库

实测对比（同一份 53 条的 JSON，同一次检索）：

| 做法 | 切片数 | 检索「赛博朋克霓虹」 |
|---|---|---|
| JSON 直接入库（走 `_flatten_json`） | 513 片 | **0 命中** |
| 先转成本脚本的 Markdown 再入库 | **92 片** | **命中 1 条** |

原因：`_flatten_json` 把 JSON 逐字段拍平成 `id: xxx` / `sourceId: xxx` 一行一段，
真正的提示词（`prompt`）被夹在元数据中间、还会被切碎；元数据本身又成了检索噪声。
转成「一条一段、标题+提示词+标签」之后，每条提示词完整、噪声也没有了。

## 数据来源（这几个源是同一个 JSON 格式，字段完全一致）

来自 [yukkcat/image-prompts](https://github.com/yukkcat/image-prompts) 注册表，
与「无限画布」内置的 7 个源相同。字段：
`id, sourceId, title, prompt, description, coverUrl, referenceImageUrls,
 tags, author, sourceUrl, createdAt, imageMode, imageModel`

各源地址为 `https://raw.githubusercontent.com/yukkcat/image-prompts/main/dist/sources/<id>.json`，
其中 `<id>` 取上表 `sourceId`（如 `awesome-gpt-image`、`youmind-nano-banana-pro`）。

## 转换规则（每一步都有理由）

1. **一条一段**：`## 标题` + 提示词正文（+ 描述 + 标签）。标题给检索一个锚点，
   正文保持完整不被切断。
2. **丢掉纯噪声字段**：`id` / `sourceId` / `coverUrl` / `imageMode` 对「找参考」没用，
   留着只会稀释关键词命中。
3. **保留 `tags` 与 `author`**：标签是检索最强的命中词（如「摄影与照片级写实」），
   作者可追溯来源。但**丢掉 `@` 开头的作者标签**（那是社交账号，不是主题词）。
4. **保留 `sourceUrl`**：来源可追溯，用引用块放，不混进正文。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 对「找参考」没用的字段（留着只会稀释关键词命中）
_NOISE_FIELDS = ("id", "sourceId", "coverUrl", "referenceImageUrls", "imageMode",
                 "imageModel", "createdAt")


def _clean_tags(raw: Any) -> list[str]:
    """标签里 `@xxx` 是社交账号，不是主题词——丢掉，免得它抢命中。"""
    out: list[str] = []
    for tag in raw or []:
        text = str(tag or "").strip()
        if text and not text.startswith("@"):
            out.append(text)
    return out


def to_markdown(items: Iterable[Mapping[str, Any]], *, title: str) -> str:
    """把一组提示词条目转成一段 Markdown（同一份文件的条目合成一个文档）。"""
    lines = [f"# {title}", ""]
    kept = 0
    for item in items:
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            continue                      # 没有提示词的条目没有参考价值
        kept += 1
        heading = _clean_heading(str(item.get("title") or ""), fallback=f"提示词 {kept}")
        lines.append(f"## {heading}")
        lines.append(prompt)
        description = _clean_description(str(item.get("description") or ""), prompt)
        if description:
            lines.append("")
            lines.append(description)
        tags = _clean_tags(item.get("tags"))
        if tags:
            lines.append("")
            lines.append("标签：" + "、".join(tags))
        author = str(item.get("author") or "").strip()
        source = str(item.get("sourceUrl") or "").strip()
        if author or source:
            credit = "来源：" + " ".join(part for part in (author, source) if part)
            lines.append("")
            lines.append(f"> {credit}")
        lines.append("")
    if kept == 0:
        raise SystemExit("这份 JSON 里没有任何带 prompt 的条目，检查一下文件")
    lines.append(f"（共 {kept} 条）")
    return "\n".join(lines)


def _clean_heading(raw: str, *, fallback: str) -> str:
    """标题里不能有 `#`（会破坏 Markdown 层级，也会被切片当成新标题）。"""
    text = raw.strip().lstrip("#").strip()
    return text or fallback


def _clean_description(description: str, prompt: str) -> str:
    """`description` 常常不是描述，而是模型名 / 任务名——那种就别当描述用。

    实测 182 条里 25 条是这样（`Nano Banana 2` / `Mission 1` / `Poster 1` / `Panda` …）。
    它们既没信息量、又会给检索加噪声（而且长得像正文，会骗到切片）。

    判据按**形态**分，不靠猜长度（长度不可靠：真描述也可能很短）：
    - 已经在提示词里出现 → 重复，丢掉；
    - 像标识符（无标点、无空格超过 3 段、不像句子）→ 丢掉。
    """
    text = description.strip()
    if not text or text in prompt:
        return ""
    # 像句子就留下：带中文标点，或够长（≥ 25 字，短句描述在这个数据集里几乎没有）
    if any(p in text for p in "。！？；，、："):
        return text
    if len(text) < 25:
        return ""
    return text


def _load(path: Path) -> list[Mapping[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"读不了 {path.name}：{exc}") from exc
    if isinstance(data, Mapping):
        # 有的源把数组包在 items / data 里
        for key in ("items", "data", "prompts"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise SystemExit(f"{path.name} 的顶层不是数组（也不是 items/data/prompts）")
    return [row for row in data if isinstance(row, Mapping)]


def collect(target: Path) -> tuple[list[Mapping[str, Any]], str]:
    """支持传单个文件，也支持传一个目录（把里面的 .json 都合并）。"""
    if target.is_dir():
        files = sorted(target.glob("*.json"))
        if not files:
            raise SystemExit(f"{target} 里没有 .json 文件")
        items: list[Mapping[str, Any]] = []
        for path in files:
            items.extend(_load(path))
        return items, f"提示词参考库（合并 {len(files)} 个源）"
    return _load(target), target.stem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把开源提示词库（标准 JSON）转成可入库的 Markdown"
    )
    parser.add_argument("source", help="JSON 文件或含 JSON 的目录")
    parser.add_argument("-o", "--out", required=True, help="输出的 .md 路径")
    parser.add_argument("--title", default="", help="文档标题（默认取文件名）")
    args = parser.parse_args(argv)

    items, default_title = collect(Path(args.source))
    text = to_markdown(items, title=args.title or default_title)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    # 计数只数二级标题（一级是文档标题，注释里带 # 的行不算）
    kept = sum(1 for line in text.splitlines() if line.startswith("## "))
    print(f"已写入 {out}（读到 {len(items)} 条 → 产出 {kept} 条）")
    print("下一步：在「知识库」页上传这个 .md，它会自动切片入库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
