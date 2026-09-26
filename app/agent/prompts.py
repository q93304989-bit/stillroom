"""流水线里用到的判断问题与提示词模板。

集中放在这里，是为了将来做「自更新」时**只改这一个文件**：改完是版本化的提示词补丁，
不涉及流程结构（结构固定在 `phases.py`）。

写问题的两条经验（都来自 TypeSafe 官方文档）：

1. 问题描述的是「意思」，不要把参数名当问题（`"哪个尺寸？"` 这种没有可匹配的信息）；
2. 一次请求里并行问多个独立问题，比逐个问便宜得多。
"""

from __future__ import annotations

from typing import Any, Mapping

from app.clients.typesafe_client import choice, noul, score

#: 画幅选项（与界面下拉保持一致，闭集）
ASPECT_OPTIONS = {
    "16:9": "横版宽幅，适合风景与场景",
    "9:16": "竖版，适合手机上看、海报与人物",
    "1:1": "正方形，适合头像与封面",
    "4:3": "偏方，适合传统照片比例",
    "3:4": "竖版偏方，适合人物与插画",
    "21:9": "超宽，适合电影感画面",
}

#: 可用程度的分档（评估阶段用，必须有序且每档能独立看懂）
QUALITY_LEVELS = ["完全不可用", "需要大改", "小修即可", "直接可用"]

#: 改进方向（评估阶段用；none 表示不必改）
FIX_OPTIONS = {
    "none": "不需要改",
    "subject": "主体不对或太弱",
    "style": "风格不符",
    "composition": "构图问题",
    "lighting": "光线问题",
    "detail": "细节瑕疵太多（手部、文字、结构）",
}


def understand_questions(
    request: str, *, overrides: Mapping[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    """第一阶段：需求够不够开工、是图还是视频、要多大的画幅。

    `overrides` 是补丁对问题文案的覆盖（键形如 `understand.enough`），
    只换问法，不换选项与结构——判断口径变了会找不到原因，所以覆盖权在白名单里。
    """
    overrides = overrides or {}

    def ask(key: str, default: str) -> str:
        return str(overrides.get(f"understand.{key}") or default)

    return {
        # 第一步永远是「这是什么类型的活」。类型判错了，后面全歪：
        # 「先帮我想一个牛逼的剧本」被判成出视频，就会硬编一条画面提示词去生成，
        # 用户拿到的是个跑偏的产物，还得自己收拾。
        "task": choice(
            "这条需求属于哪一类？只看用户真正要的东西，不看话里的动词。",
            {
                "image": "要一张（或多张）静态图片：海报、插画、头像、产品图等",
                "video": "要一段动态视频",
                "text": "要文字产物：剧本、文案、标题、说明、翻译等",
                "other": "其他：答疑、查资料、写代码、算数、做表格等",
            },
        ),
        # 第二步才是可行性。类型是「出图/出视频」也可能做不了（精度、时长、素材等要求超出现有能力）。
        "feasible": noul(
            "按上面的类型和这条需求的具体要求，我现在（一个只会出图 / 出视频的工具）"
            "真的能做出它要的东西吗？",
            {
                "yes": "能做出它要的东西",
                "no": "做不了（类型就不是出图/出视频，或具体要求超出能力）",
            },
        ),
        "enough": noul(
            ask("enough",
                f"这条需求是否已经足够开工（有明确的主体或场景），不需要再追问？需求：{request}")
        ),
        "aspect": choice(ask("aspect", "最合适的画幅是哪个？"), ASPECT_OPTIONS),
        # 第三期加的两问：本地参考够不够、要不要联网。
        # 为什么用「几条」而不是「够不够」：阈值要和用户设置取 max（方案 4.1），
        # 所以模型给的必须是**数量**，不能只是一个是非。
        "reference_need": choice(
            ask("reference_need", "要做出这条需求要的效果，大概需要几条参考才够？"),
            {
                "0": "不需要参考：需求本身已经把画面说清楚了",
                "1": "一条就够：常见题材，看一眼就能对齐",
                "2": "两三条：需要一点风格或构图上的参照",
                "3+": "越多越好：冷门题材，或强调某种具体风格与细节",
            },
        ),
        "web_search": noul(
            ask("web_search",
                "用户是不是明确要求联网搜索（说了「搜一下」「上网查」「参考网上的」这类话）？"
                "只判断这个要求本身，不考虑你觉得该不该搜。"),
            {
                "yes": "用户明确要求联网搜索",
                "no": "用户没提联网，或明确说不要联网",
            },
        ),
    }


def evaluate_questions(
    requirement: str, *, overrides: Mapping[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    """第四阶段：符合需求吗、可用程度如何、最该改哪里。"""
    overrides = overrides or {}

    def ask(key: str, default: str) -> str:
        return str(overrides.get(f"evaluate.{key}") or default)

    return {
        "fits": noul(ask("fits", f"这张图是否符合用户需求？需求：{requirement}")),
        "quality": score(ask("quality", "这张图作为成品的可用程度"), QUALITY_LEVELS),
        "fix": choice(ask("fix", "最该改进的地方（若不需要改选 none）"), FIX_OPTIONS),
    }


def composer_messages(
    requirement: str,
    *,
    style_hints: list[str] | None = None,
    previous_prompt: str = "",
    evaluation: Mapping[str, Any] | None = None,
    description: Mapping[str, Any] | None = None,
    suffix: str = "",
    aspect_preference: str = "",
) -> list[dict[str, str]]:
    """给生成模型的消息：把需求（以及判断结果）写成一条可用的图像提示词。

    注意分工：**改提示词这件事必须用生成模型**（Jev 不做生成），而「改得好不好」仍然
    交给下一轮的视觉 + Jev 判断——两边各司其职。

    `suffix` / `aspect_preference` 来自当前生效的提示词补丁（自更新）：
    它们是「固定附加的指令」，追加在基础要求之后，不影响其余部分的拼装。
    """
    parts = [
        "你是图像提示词工程师。把下面的需求写成一条中文图像提示词，",
        "要求：具体（主体、风格、光线、构图）、不要解释、不要引号，只输出提示词本身。",
    ]
    if suffix:
        parts.append(f"另外，始终遵守：{suffix}")
    if aspect_preference:
        parts.append(f"除非需求另有所指，默认按 {aspect_preference} 画幅来写。")
    parts.append(f"\n需求：{requirement}")
    if style_hints:
        parts.append("\n可参考的风格线索：" + "；".join(style_hints[:3]))
    if previous_prompt and evaluation:
        parts.append(f"\n上一版提示词：{previous_prompt}")
        if description:
            parts.append(
                "\n上一版画面的实际内容："
                + "；".join(
                    str(description.get(key, ""))
                    for key in ("subject", "style", "composition", "lighting")
                    if description.get(key)
                )
            )
        fix = evaluation.get("fix")
        quality = evaluation.get("quality")
        parts.append(
            f"\n评审结论：可用程度 {quality}；最该改进：{fix}。"
            "请针对这一点改写提示词，其余部分保持原来的风格。"
        )
    return [{"role": "user", "content": "\n".join(parts)}]
