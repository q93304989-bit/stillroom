"""用户知识库：上传 → 解析 → 切片 → 入库 → 检索。

与 A-RAG 的区别在「查什么」：A-RAG 查的是自己生成过的作品（参考图 + 提示词），知识库
查的是用户自己上传的资料（品牌规范、风格说明、产品信息）。两者产出的形状是一样的
——**进提示词的文字线索 + 给人看的来源**，所以能直接并进同一份上下文里。

切片规则（方案 5.2 节）：

1. 按空行 / 标题 / 代码块分段；
2. 段落超过 800 字按句子边界切，相邻两片重叠 100 字（避免一句话被劈成两半丢语境）；
3. 短于 20 字的段落与相邻段合并；标题永远跟它下面那节走；
4. 每片记 `ordinal`——「重建」和「引用第几片」都靠它。

检索（方案 5.3 节）：关键词召回（分词打分排序）→ Jev 一次请求重排 → 取前 K。
判断模型不可用就按关键词分返回并标 `reranked=False`——检索失败不该让生成流程挂掉。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.clients.typesafe_client import TypeSafeClient, score
from app.config.logs import log_error
from app.net.errors import AppError
from app.services.history import KNOWLEDGE_SCHEMA

#: 支持的上传类型（PDF 要额外装 pypdf，没装就给明确理由，不假装成功）
DOC_KINDS = ("txt", "md", "csv", "json", "pdf")
#: 文档状态
STATES = ("pending", "ready", "failed")

CHUNK_MAX_CHARS = 800
CHUNK_OVERLAP = 100
CHUNK_MIN_CHARS = 20
DEFAULT_TOP_K = 4
#: 关键词阶段最多留多少候选给重排（与 A-RAG 同一考虑：装得下，也不用全量）
DEFAULT_CANDIDATE_LIMIT = 24
RERANK_LEVELS = ["不相关", "有点相关", "相关", "很相关"]

_SUPPORTED_HINT = "只支持 txt / md / csv / json / pdf 这几种文件"
_HEADING = re.compile(r"^#{1,6}\s")
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
#: 需求里的高频虚词单独出现时没信息量，去掉能少召回一堆噪声
_STOP_WORDS = frozenset(
    "的 了 和 与 或 一个 一张 一份 帮我 请 要 想要 需要 生成 画 做 出 张 个 这 那".split()
)


class KnowledgeError(Exception):
    """给用户看的失败理由（不吞掉、不猜）。"""


# --------------------------------------------------------------------------- 切片


def _is_heading(block: str) -> bool:
    first = (block or "").split("\n", 1)[0].strip()
    return bool(_HEADING.match(first))


def strip_heading(text: str) -> str:
    """剥掉开头的 markdown 标题行，只留正文。

    为什么要剥：切片时「标题跟它下面那节走」是对的（分段语义），但标题**不该进提示词**。
    「别人收集的提示词」这类资料天然是 `## 1. 雨夜霓虹人像` + 正文的形态，
    整片塞进提示词会变成「可参考的风格线索：## 1. 雨夜霓虹人像 一个穿复古风衣的侦探…」——
    模型会把这些符号当正文读。而「这一片出自哪」已经由 `origin`（《文件名》第 N 片）交代了。

    只剥**开头连续**的标题行：正文里偶尔出现的 `#` 不是标题，不该动。
    """
    lines = (text or "").split("\n")
    index = 0
    while index < len(lines) and _HEADING.match(lines[index].strip()):
        index += 1
    body = "\n".join(lines[index:]).strip()
    # 整片都是标题（只有标题没有正文）时，宁可原样返回，也不交出一片空白
    return body or (text or "").strip()


def _blocks(text: str) -> list[str]:
    """按空行分段；围栏代码块整块保留；标题行另起一段（随后与它下面那节合并）。"""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    current: list[str] = []
    fence: str | None = None

    def flush() -> None:
        joined = "\n".join(current).strip()
        if joined:
            blocks.append(joined)
        current.clear()

    for line in lines:
        stripped = line.strip()
        if fence is not None:
            current.append(line)
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            current.append(line)
            continue
        if not stripped:
            flush()
            continue
        if _HEADING.match(stripped) and current:
            flush()
        current.append(line)
    flush()
    return blocks


def _merge_short(blocks: list[str], *, min_chars: int = CHUNK_MIN_CHARS) -> list[str]:
    """短段与相邻段合并：标题永远跟它下面那节；开头的短段并进下一节。"""
    glued: list[str] = []
    open_heading = False            # 上一块是「光秃秃一个标题」——它等着吃下面那节
    for block in blocks:
        if glued and open_heading:
            glued[-1] = glued[-1] + "\n" + block
        else:
            glued.append(block)
        open_heading = _is_heading(block) and "\n" not in block.strip()

    out: list[str] = []
    for block in glued:
        if len(block) < min_chars and not _is_heading(block):
            if out:
                out[-1] = out[-1] + "\n" + block
            else:
                out.append(block)
            continue
        out.append(block)
    if len(out) >= 2 and len(out[0]) < min_chars:
        out = [out[0] + "\n" + out[1], *out[2:]]
    return out


def _split_long(
    block: str, *, max_chars: int = CHUNK_MAX_CHARS, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """超长段落按句子边界切：下一片的开头就是上一片的尾巴（重叠 `overlap` 字）。"""
    if len(block) <= max_chars:
        return [block]

    sentences = [part for part in _SENTENCE_SPLIT.split(block) if part.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > max_chars:
            chunks.append(current)
            current = current[-overlap:] if overlap else ""
        current += sentence
        # 一句本身就超长（比如整段没有标点）→ 硬切，别让一片无限大
        while len(current) > max_chars:
            chunks.append(current[:max_chars])
            current = current[max_chars - overlap :]
    if current.strip():
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def slice_text(text: str) -> list[str]:
    """把一份文字切成片（切片规则的唯一入口，重建也走它）。"""
    chunks: list[str] = []
    for block in _merge_short(_blocks(text)):
        chunks.extend(_split_long(block))
    # 交付前剥掉开头的 markdown 标题行：标题的价值是「分段 + 定位」（定位已由 origin
    # 的「《文件名》第 N 片」承担），但混进提示词会被模型当正文读。
    # 这一步放在切片的最后、不改变分段语义——`_blocks` / `_merge_short` 仍按标题分段。
    out: list[str] = []
    for chunk in chunks:
        body = strip_heading(chunk)
        if body:
            out.append(body)
    return out


# --------------------------------------------------------------------------- 解析


def _decode(payload: bytes) -> str:
    """UTF-8 优先，退 GBK（中文用户的老文件大多是这两种）。"""
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise KnowledgeError("这份文件读不出文字：既不是 UTF-8 也不是 GBK 编码")


def _csv_text(text: str) -> str:
    rows = list(csv.reader(io.StringIO(text)))
    return "\n".join(
        " | ".join(cell.strip() for cell in row)
        for row in rows
        if any(cell.strip() for cell in row)
    )


def _flatten_json(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                out.extend(_flatten_json(item, f"{prefix}{key}."))
            else:
                out.extend(_flatten_json(item, f"{prefix}{key}: "))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_flatten_json(item, prefix))
        return out
    return [f"{prefix}{value}"]


def _json_text(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise KnowledgeError(f"这份 JSON 解析失败：{exc}") from exc
    return "\n".join(_flatten_json(data))


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader            # 可选依赖：装了才支持 PDF
    except ImportError as exc:
        raise KnowledgeError(
            "这份 PDF 提不出文字：没装 PDF 解析组件（第一版不支持），先转成 txt / md 再上传"
        ) from exc
    try:
        pages = [(page.extract_text() or "") for page in PdfReader(str(path)).pages]
    except Exception as exc:                    # 坏文件、加密文件都走这里
        raise KnowledgeError(f"这份 PDF 解析失败：{exc}") from exc
    text = "\n\n".join(page.strip() for page in pages if page.strip())
    if not text:
        raise KnowledgeError("这份 PDF 提不出文字（多半是扫描件，第一版不做 OCR）")
    return text


def read_document(path: Path, kind: str) -> str:
    """把文件读成纯文字；读不出来就抛 `KnowledgeError`（理由直接给用户看）。"""
    if kind == "pdf":
        return _read_pdf(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise KnowledgeError(f"读不到文件：{exc.strerror or exc}") from exc
    text = _decode(raw)
    if kind == "csv":
        return _csv_text(text)
    if kind == "json":
        return _json_text(text)
    return text


# --------------------------------------------------------------------------- 数据形状


@dataclass
class KnowledgeDocument:
    id: str
    name: str
    path: str
    kind: str
    size: int
    sha256: str
    state: str = "pending"
    error: str = ""
    chunk_count: int = 0
    created_at: float = 0.0
    built_at: float | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "KnowledgeDocument":
        return cls(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            kind=row["kind"],
            size=row["size"],
            sha256=row["sha256"],
            state=row["state"],
            error=row["error"] or "",
            chunk_count=row["chunk_count"],
            created_at=row["created_at"],
            built_at=row["built_at"],
        )


@dataclass
class KnowledgeChunk:
    doc_id: str
    ordinal: int
    text: str


@dataclass
class KnowledgeHit:
    """检索命中：片段 + 它在哪个文件的第几片（引用与展示都靠这个）。"""

    doc_id: str
    doc_name: str
    ordinal: int
    text: str
    score: float = 0.0


@dataclass
class KnowledgeResult:
    """与 `rag.search` 对齐的产出：fragments 进提示词，sources 给人看。"""

    hits: list[KnowledgeHit] = field(default_factory=list)
    reranked: bool = False
    reason: str = ""

    def fragments(self) -> list[str]:
        return [hit.text for hit in self.hits]

    def sources(self) -> list[dict[str, Any]]:
        return [
            {"doc_id": hit.doc_id, "name": hit.doc_name, "ordinal": hit.ordinal}
            for hit in self.hits
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.hits),
            "reranked": self.reranked,
            "reason": self.reason,
            "fragments": self.fragments(),
            "sources": self.sources(),
        }


# --------------------------------------------------------------------------- 关键词召回


def tokens_of(text: str) -> list[str]:
    """中文按二元组、西文按词切——不引分词库：够第一版用，而且每一步都能解释。"""
    parts = re.sub(r"[\W_]+", " ", (text or "").lower()).split()
    tokens: list[str] = []
    for part in parts:
        if re.fullmatch(r"[a-z0-9]+", part):
            if len(part) >= 2 and part not in _STOP_WORDS:
                tokens.append(part)
            continue
        if len(part) == 1:
            tokens.append(part)
            continue
        for index in range(len(part) - 1):
            gram = part[index : index + 2]
            if gram not in _STOP_WORDS:
                tokens.append(gram)
    return list(dict.fromkeys(tokens))


def _keyword_score(text: str, tokens: list[str]) -> float:
    """命中次数按词长加权：长词比二元组更有信息量。"""
    if not tokens:
        return 0.0
    lowered = text.lower()
    total = 0.0
    for token in tokens:
        count = lowered.count(token)
        if count:
            total += min(count, 4) * (len(token) + 1)
    return total


# --------------------------------------------------------------------------- 存储


class KnowledgeStore:
    """`kb_documents` / `kb_chunks` 两张表的唯一入口（与 `HistoryStore` 共用一个库文件）。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        root: str | Path,
        judge: TypeSafeClient | None = None,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> None:
        self.path = Path(db_path)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._judge = judge
        self.candidate_limit = candidate_limit
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(KNOWLEDGE_SCHEMA)      # 防御：测试里可能只建本 store
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()

    # ---------------------------------------------------------------- 生命周期

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def use_judge(self, judge: TypeSafeClient | None) -> "KnowledgeStore":
        """换一个判断模型（注册表重建时跟着换，界面不需要知道）。"""
        self._judge = judge
        return self

    def __enter__(self) -> "KnowledgeStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---------------------------------------------------------------- 入库

    def add(self, source: str | Path, *, name: str | None = None) -> KnowledgeDocument:
        """收下一份文件：校验类型 → 算 sha256 → 复制进知识库目录 → 记一行 pending。"""
        origin = Path(source)
        display = (name or origin.name).strip() or origin.name
        kind = Path(display).suffix.lower().lstrip(".")
        if kind not in DOC_KINDS:
            raise KnowledgeError(f"{_SUPPORTED_HINT}（这份是 {kind or '无后缀'}）")
        try:
            payload = origin.read_bytes()
        except OSError as exc:
            raise KnowledgeError(f"读不到文件：{exc.strerror or exc}") from exc

        digest = hashlib.sha256(payload).hexdigest()
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM kb_documents WHERE sha256 = ?", (digest,)
            ).fetchone()
            if existing is not None:
                return KnowledgeDocument.from_row(existing)     # 同一份内容不重复入库

            doc_id = uuid.uuid4().hex[:12]
            target = self.root / f"{doc_id}__{display}"
            target.write_bytes(payload)
            self._conn.execute(
                "INSERT INTO kb_documents (id, name, path, kind, size, sha256, state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (doc_id, display, str(target), kind, len(payload), digest, time.time()),
            )
            self._conn.commit()
        doc = self.get(doc_id)
        assert doc is not None
        return doc

    def documents(self) -> list[KnowledgeDocument]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM kb_documents ORDER BY created_at DESC"
            ).fetchall()
        return [KnowledgeDocument.from_row(row) for row in rows]

    def get(self, doc_id: str) -> KnowledgeDocument | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM kb_documents WHERE id = ?", (doc_id,)
            ).fetchone()
        return KnowledgeDocument.from_row(row) if row else None

    def chunks(self, doc_id: str) -> list[KnowledgeChunk]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc_id, ordinal, text FROM kb_chunks WHERE doc_id = ? ORDER BY ordinal",
                (doc_id,),
            ).fetchall()
        return [KnowledgeChunk(row["doc_id"], row["ordinal"], row["text"]) for row in rows]

    # ---------------------------------------------------------------- 构建

    def build(self, doc_id: str) -> KnowledgeDocument | None:
        """解析 → 切片 → 写库。失败也留痕（`state=failed` + 理由），不把异常甩给界面。"""
        doc = self.get(doc_id)
        if doc is None:
            return None
        try:
            text = read_document(Path(doc.path), doc.kind)
            chunks = slice_text(text)
            if not chunks:
                raise KnowledgeError("这份文件里没有可用的文字")
        except KnowledgeError as exc:
            return self._mark_failed(doc_id, str(exc))
        except Exception as exc:                    # 意外错误也要有据可查
            log_error("knowledge.build", exc)
            return self._mark_failed(doc_id, f"构建出错（{type(exc).__name__}）：{exc}")

        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
            self._conn.executemany(
                "INSERT INTO kb_chunks (doc_id, ordinal, text, created_at) VALUES (?, ?, ?, ?)",
                [(doc_id, ordinal, chunk, now) for ordinal, chunk in enumerate(chunks)],
            )
            self._conn.execute(
                "UPDATE kb_documents SET state = 'ready', error = '', chunk_count = ?,"
                " built_at = ? WHERE id = ?",
                (len(chunks), now, doc_id),
            )
            self._conn.commit()
        return self.get(doc_id)

    def rebuild(self, doc_id: str) -> KnowledgeDocument | None:
        """按当前切片规则重来一遍（原文件留着就是为了这个）。"""
        return self.build(doc_id)

    def remove(self, doc_id: str, *, delete_file: bool = True) -> bool:
        """删文档（默认连原文件一起删；界面会先问一句）。

        返回值只说「库里的记录删掉了没有」。**原文件删不掉的原因请用 `delete_file()`**——
        界面会把它转成一句提示，不然磁盘上会留下一个没人知道的孤儿文件。
        """
        doc = self.get(doc_id)
        if doc is None:
            return False
        with self._lock:
            self._conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
            self._conn.commit()
        if delete_file:
            self.delete_file(doc.path)
        return True

    @staticmethod
    def delete_file(path: str | Path, *, attempts: int = 5) -> str:
        """删一个原文件；返回空串表示删掉了，否则返回能直接给用户看的原因。

        Windows 上「刚读完就删」偶尔会被索引 / 杀毒短暂占用，所以退一步重试几次；
        真过不去也**必须把原因交出去**——库里那条已经删了，磁盘上留的孤儿文件
        得让用户知道在哪、为什么。
        """
        target = Path(path)
        last: OSError | None = None
        for index in range(max(1, attempts)):
            try:
                target.unlink(missing_ok=True)
                return ""
            except OSError as exc:
                last = exc
                if index < attempts - 1:
                    time.sleep(0.05 * (index + 1))
        if last is not None:                        # pragma: no cover - 文件持续被占用
            log_error("knowledge.remove", last)
            return f"{type(last).__name__}: {last}"
        return ""

    def ready_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM kb_documents WHERE state = 'ready'"
            ).fetchone()
        return int(row["n"])

    def _mark_failed(self, doc_id: str, message: str) -> KnowledgeDocument | None:
        with self._lock:
            self._conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
            self._conn.execute(
                "UPDATE kb_documents SET state = 'failed', error = ?, chunk_count = 0 WHERE id = ?",
                (message, doc_id),
            )
            self._conn.commit()
        return self.get(doc_id)

    # ---------------------------------------------------------------- 检索

    def candidates(self, requirement: str) -> list[KnowledgeHit]:
        """关键词召回：读 ready 文档的所有片子，打分排序。

        第一版不建倒排索引——知识库通常只有几十份文档，一次遍历比维护索引简单可靠得多；
        真到读不动的时候再换 FTS5，对外接口不用动。
        """
        tokens = tokens_of(requirement)
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.doc_id, c.ordinal, c.text, d.name AS doc_name"
                " FROM kb_chunks c JOIN kb_documents d ON d.id = c.doc_id"
                " WHERE d.state = 'ready'"
            ).fetchall()
        hits: list[KnowledgeHit] = []
        for row in rows:
            value = _keyword_score(row["text"], tokens) if tokens else 0.5
            if value <= 0:
                continue
            hits.append(
                KnowledgeHit(row["doc_id"], row["doc_name"], row["ordinal"], row["text"], value)
            )
        hits.sort(key=lambda hit: (-hit.score, hit.doc_name, hit.ordinal))
        return hits[: self.candidate_limit]

    async def search(
        self,
        requirement: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        timeout: float | None = None,
    ) -> KnowledgeResult:
        """完整检索：关键词召回 → Jev 重排 → 取前 K 片。"""
        pool = self.candidates(requirement)
        if not pool:
            if not self.documents():
                return KnowledgeResult(reason="知识库还是空的：先去「知识库」页上传资料")
            if self.ready_count() == 0:
                return KnowledgeResult(reason="知识库里的文件还没「构建」，先构建再来检索")
            return KnowledgeResult(reason="知识库里没有和这次需求对得上的片段")

        ordered, reranked, reason = await self._rerank(requirement, pool, timeout=timeout)
        return KnowledgeResult(hits=ordered[: max(1, top_k)], reranked=reranked, reason=reason)

    async def _rerank(
        self, requirement: str, pool: list[KnowledgeHit], *, timeout: float | None
    ) -> tuple[list[KnowledgeHit], bool, str]:
        """一次 Jev 请求给所有候选打分；失败就退化成关键词顺序。"""
        if self._judge is None:
            return pool, False, "未接入判断模型，按关键词分返回"

        questions = {
            f"chunk_{index}": score(
                f"这段资料对完成这个需求有没有帮助？（需求：{requirement}）", RERANK_LEVELS
            )
            for index, _ in enumerate(pool)
        }
        state = {
            "requirement": requirement,
            "candidates": [
                {"id": f"{hit.doc_name}#{hit.ordinal}", "text": hit.text[:600]} for hit in pool
            ],
        }
        try:
            result = await self._judge.ask(state, questions, timeout=timeout or 30)
        except AppError as exc:
            return pool, False, f"判断不可用（{exc.kind}），按关键词分返回"

        scored: list[tuple[float, float, KnowledgeHit]] = []
        for index, hit in enumerate(pool):
            value = result.score(f"chunk_{index}")
            confidence = result.confidence(f"chunk_{index}") or 0.0
            scored.append((value if value is not None else -1.0, confidence, hit))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

        ordered: list[KnowledgeHit] = []
        for value, _, hit in scored:
            hit.score = value
            ordered.append(hit)
        return ordered, True, ""
