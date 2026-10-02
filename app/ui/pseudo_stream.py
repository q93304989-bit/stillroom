"""伪流式：把一整段回复切成小块，再按「像在打字」的节奏一段段交给界面。

为什么要前端自己做
------------------
协议层刻意不做真流式：它先把整段生成出来、走完 Quality Gate，再交给前端。
代价是首字延迟不会变好；换来的是「发送前可以完整校验」。所以「一段段显示」这件事
只能由**前端**做，而且要做得像真的——按语义边界切块，每块之间停 20–50 毫秒。

这个模块只有两个纯函数（不 import Qt、不碰 IO、不读时间），所以切分与节奏都能被直接测：

    split_into_chunks(text)   切块：`"".join(...)` 逐字等于原文，不丢字也不加字
    chunk_delay_ms(chunk)     节奏：20–50ms，标点越重停得越久

界面层拿到块之后的展示（气泡、光标、自动滚到底）在 QML 那边；这里是纯逻辑，
放 Python 是为了能被测试钉住——节奏这种东西最容易在一次次调参里悄悄跑偏。
"""

from __future__ import annotations

#: 句末标点：一句话的结束，断得最干脆
_SENTENCE_ENDS = "。！？!?…；;"
#: 子句标点：一句话内部的小停顿，句子太长时才在这里断
_CLAUSE_ENDS = "，,、：:"

#: 一块最多多少个字。太大就「一口气吐一大段」，看不出流式；
#: 太小则碎得不像话（标点会频繁落在块首），28 是个折中。
MAX_CHARS = 28

#: 一段的停顿时长区间（毫秒）。任务要求 20–50ms。
MIN_DELAY_MS = 20
MAX_DELAY_MS = 50


def _atoms(text: str) -> list[str]:
    """切成「带尾标点的最小单位」：句末标点、子句标点、换行都算一个断点。

    标点跟着前一节走，所以拼起来永远等于原文。
    """
    atoms: list[str] = []
    buffer: list[str] = []
    for char in text:
        buffer.append(char)
        if char in _SENTENCE_ENDS or char in _CLAUSE_ENDS or char == "\n":
            atoms.append("".join(buffer))
            buffer = []
    if buffer:
        atoms.append("".join(buffer))
    return atoms


def split_into_chunks(text: str, *, max_chars: int = MAX_CHARS) -> list[str]:
    """按语义边界把整段回复切成小块（伪流式的展示单位）。

    规则从粗到细：
      1. **句末标点一定断**——一句话一块。这是「看起来像在打字」最自然的最小单位，
         哪怕这句话很短也不并到下一句去（并了就会一大坨突然出现）
      2. 段落之间的空行跟着上一句走，于是那一段显示完之后会有一个更长的停顿
      3. 一句话内部还有逗号 / 顿号 / 冒号，攒到 `max_chars` 就在那里断
      4. 仍然超长（一长串没有标点的字）就硬切，宁可难看也不能卡住

    不变式：`"".join(split_into_chunks(t)) == t`。**一个字都不许丢**——伪流式只是
    「一段段显示」，不是摘要、不是改写，显示完必须与整段完全一致。空文本返回空列表。
    """
    if not text:
        return []
    if max_chars < 1:
        raise ValueError("max_chars 至少要 1")

    chunks: list[str] = []
    current = ""
    pending_space = ""          # 段落之间的空行：先记着，回头挂到前一块的尾巴上

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    def hard_split() -> None:
        """把 current 里超出的部分硬切出去：块不超长，字一个不丢。"""
        nonlocal current
        while len(current) > max_chars:
            chunks.append(current[:max_chars])
            current = current[max_chars:]

    for atom in _atoms(text):
        if atom.strip() == "":
            pending_space += atom
            continue
        if pending_space:
            # 空行属于「上一句的结尾」，不属于「下一句的开头」：优先挂回前一块（字符顺序不变）。
            # 前一块塞不下（空行特别长）就先放回待发块，交给下面的硬切处理。
            room = max_chars - len(chunks[-1]) if chunks and not current else 0
            if room >= len(pending_space):
                chunks[-1] += pending_space
            else:
                current += pending_space
            pending_space = ""
        if len(current) + len(atom) > max_chars:
            hard_split()
            flush()
        current += atom
        hard_split()
        tail = current.rstrip()
        if tail and tail[-1] in _SENTENCE_ENDS:
            flush()
    flush()
    if pending_space:
        # 文本以空行收尾：并到最后一块里（没有块就单独成块——反正一个字不能丢）
        if chunks:
            chunks[-1] += pending_space
        else:
            chunks.append(pending_space)
    return chunks


def chunk_delay_ms(chunk: str) -> int:
    """这一块之后停多久（毫秒），落在 [MIN_DELAY_MS, MAX_DELAY_MS]。

    依据（都是为了「像人在打字」）：
      · 段落结束停得最久，让人看出分段
      · 句末标点次之，子句标点再次之，没有标点就最快
      · 同一档里，块越长停得越久一点

    刻意不用随机数：随机的东西测不住，而且「自然」并不需要真的随机——
    按标点分档就已经比固定间隔自然得多。
    """
    stripped = chunk.rstrip()
    if stripped == "":
        base = MIN_DELAY_MS
    elif "\n" in chunk:
        base = 46
    elif stripped[-1] in _SENTENCE_ENDS:
        base = 36
    elif stripped[-1] in _CLAUSE_ENDS:
        base = 28
    else:
        base = MIN_DELAY_MS
    bonus = min(8, len(chunk) // 6)          # 长块多停一点
    return max(MIN_DELAY_MS, min(MAX_DELAY_MS, base + bonus))