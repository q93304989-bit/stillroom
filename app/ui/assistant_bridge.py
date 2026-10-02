"""助手页「对话」的后端：把 adapter 拿回的整段回复，按语义边界一段段交给界面（伪流式）。

三件事的分工在这里写死，别混：

    取数据     只经过 `ProtocolClient`（adapter）。界面层不许有第二个数据源，
               更不许在 QML 里现造一段假回复——那样协议层接上时就要满 UI 找假数据。
    切块+节奏  本层（前端）负责。协议层不做真流式，也不管展示节奏。
    质量校验   **不在这里做**。那是协议层的 Quality Gate：前端拿到什么就显示什么，
               不自己判断「这段话能不能给用户看」。

线程：与其它桥一致——槽在 Qt 主线程被调用，取数据在 asyncio 线程里干，回来时跨线程
emit 信号（Qt 会排队回主线程）。**定时器只在 Qt 主线程里起**（QTimer 不能跨线程开），
所以推进流的 `_advance` 只会由主线程的 timeout 触发。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from app.adapters.protocol_client import (
    STATUS_ABORTED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    ProtocolClient,
    ProtocolError,
)
from app.ui.async_runner import AsyncRunner
from app.ui.pseudo_stream import chunk_delay_ms, split_into_chunks

#: 一条消息在界面上的状态。streaming 是中间态，其余三个是终态。
MSG_OK = "ok"
MSG_STREAMING = "streaming"
MSG_ABORTED = "aborted"
MSG_FAILED = "failed"

_MESSAGE_TEXT = {
    MSG_OK: "说完了",
    MSG_STREAMING: "正在回复…",
    MSG_ABORTED: "已打断",
    MSG_FAILED: "失败了",
}

#: 协议层的终态 → 界面上的消息状态（终态不止 COMPLETED 一种，都要如实显示）
_TERMINAL_TO_MESSAGE = {
    STATUS_COMPLETED: MSG_OK,
    STATUS_ABORTED: MSG_ABORTED,
    STATUS_FAILED: MSG_FAILED,
    STATUS_TIMEOUT: MSG_FAILED,
    STATUS_BUDGET_EXCEEDED: MSG_FAILED,
}

#: 协议层终态的人话（给提示条用）
_TERMINAL_TEXT = {
    STATUS_ABORTED: "协议层说这一轮已中止",
    STATUS_FAILED: "协议层报失败",
    STATUS_TIMEOUT: "协议层报超时",
    STATUS_BUDGET_EXCEEDED: "协议层报额度用尽",
}


@dataclass
class _Message:
    """一条消息的权威副本（界面上的 ListModel 是它的一份投影）。"""

    id: str
    role: str                       # "user" / "assistant"
    text: str
    status: str = MSG_OK

    def as_dict(self) -> dict:
        return {"id": self.id, "role": self.role, "text": self.text, "status": self.status}


class AssistantBridge(QObject):
    """QML 侧入口对象（注册为上下文属性 `assistantBridge`）。"""

    stateChanged = Signal()                     # 任何变化（只读探针都挂它）
    noticeRaised = Signal(str, str)             # level(info/warn/error), text
    messageAppended = Signal(str, str, str, str)  # message_id, role, text, status
    messageProgressed = Signal(str, str)        # message_id, 到此刻为止的累计文本
    messageSettled = Signal(str, str, str)      # message_id, status, 最终文本
    conversationCleared = Signal()

    #: 内部：从 asyncio 线程回到 Qt 主线程（跨线程 emit，Qt 自动排队）。
    _replyReady = Signal(str, str, str, str)    # message_id, 全文, 协议层终态, 会话把手
    _replyFailed = Signal(str, str)             # message_id, 一句人话

    def __init__(
        self,
        runner: AsyncRunner,
        client: ProtocolClient | None = None,
        *,
        pace_scale: float = 1.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runner = runner
        # adapter 可以注入（测试塞替身用）；不注入就用默认的那份 mock
        self._client: ProtocolClient = client if client is not None else ProtocolClient()
        #: 节奏缩放：1.0 是真实观感（20–50ms/段）。测试传一个很小的值，
        #: 就能在几十毫秒内把整段跑完，而走的还是同一条代码路径。
        self._pace = max(0.0, float(pace_scale))

        self._messages: list[_Message] = []
        self._seq = 0                      # 消息 id 的序号（clear 之后也继续往上走，不复用）
        self._conversation_id = ""
        self._stream_id = ""               # 正在流式输出的那条消息 id；空串=没在流
        self._full_text = ""
        self._chunks: list[str] = []
        self._cursor = 0
        self._partial = ""
        self._requests: dict[str, str] = {}       # message_id → 这一轮的 request_id（幂等键）
        self._abort_requested: set[str] = set()   # 已点过打断的消息
        self._abort_sent: set[str] = set()        # 已经把「中止」送到协议层的轮次
        self._last_terminal = ""
        self._closed = False

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._advance)

        self._replyReady.connect(self._on_reply_ready)
        self._replyFailed.connect(self._on_reply_failed)

    # ---------------------------------------------------------------- 只读状态

    ready = Property(bool, lambda self: self._client is not None, constant=True)
    mock = Property(bool, lambda self: bool(getattr(self._client, "mock", False)), constant=True)
    sourceLabel = Property(
        str,
        lambda self: "本地占位回复（协议层还没接上）" if self.mock else "来自协议层",
        constant=True,
    )
    messages = Property(
        "QVariantList", lambda self: [item.as_dict() for item in self._messages], notify=stateChanged
    )
    messageCount = Property(int, lambda self: len(self._messages), notify=stateChanged)
    streaming = Property(bool, lambda self: self._stream_id != "", notify=stateChanged)
    streamingText = Property(str, lambda self: self._partial, notify=stateChanged)
    streamedChunks = Property(int, lambda self: self._cursor, notify=stateChanged)
    totalChunks = Property(int, lambda self: len(self._chunks), notify=stateChanged)
    lastTerminalStatus = Property(str, lambda self: self._last_terminal, notify=stateChanged)
    conversationId = Property(str, lambda self: self._conversation_id, notify=stateChanged)
    lastReplyText = Property(str, lambda self: self._last_reply_text(), notify=stateChanged)

    @Property(str, notify=stateChanged)
    def statusText(self) -> str:
        """一句话说清现在到哪了（界面顶部的状态条）。"""
        if self._stream_id:
            return f"正在回复…（第 {self._cursor} / {len(self._chunks)} 段）"
        if not self._messages:
            return "还没有开始"
        return _MESSAGE_TEXT.get(self._messages[-1].status, "")

    # ---------------------------------------------------------------- 槽（QML 调用）

    @Slot(str)
    def send(self, text: str) -> None:
        """发一句。

        这里只挡「空的输入」——那是输入校验，不是质量校验。质量校验（这段话该不该
        给用户看、能不能发）是协议层的活，前端不抢。
        """
        cleaned = (text or "").strip()
        if cleaned == "":
            self._notify("warn", "先说一句话吧")
            return
        if self._closed:
            self._notify("warn", "窗口正在关闭，这句没发出去")
            return
        if self._stream_id:
            self._notify("warn", "上一条还在说，先等它说完或者点「打断」")
            return

        self._seq += 1
        user_id = f"m{self._seq}-u"
        self._seq += 1
        reply_id = f"m{self._seq}-a"

        # 用户那句立刻上屏：等回复是等回复，不该连自己说的话都看不见
        self._append(_Message(user_id, "user", cleaned))
        self._append(_Message(reply_id, "assistant", "", MSG_STREAMING))

        self._stream_id = reply_id
        self._full_text = ""
        self._chunks = []
        self._cursor = 0
        self._partial = ""
        self._last_terminal = ""
        self.stateChanged.emit()

        # 一轮一个 request_id：它既是幂等键，也是「打断这一轮」的把手
        request_id = uuid.uuid4().hex
        self._requests[reply_id] = request_id
        self._launch(request_id, cleaned, reply_id)

    @Slot(str)
    def regenerate(self, message_id: str) -> None:
        """重新生成一条助手回复。

        只追加一条新的助手消息，不重复用户气泡；真正的重跑语义通过 adapter
        交给协议层，UI 不在这里判断上一轮内容能否重跑。
        """
        if self._closed:
            self._notify("warn", "窗口正在关闭，这条没法重新生成")
            return
        if self._stream_id:
            self._notify("warn", "上一条还在说，先等它说完或者点「打断」")
            return

        source_index = -1
        for index, item in enumerate(self._messages):
            if item.id == message_id and item.role == "assistant":
                source_index = index
                break
        if source_index < 0:
            self._notify("warn", "找不到这条回复")
            return
        source_request_id = self._requests.get(message_id, "")
        source_text = ""
        for item in reversed(self._messages[:source_index]):
            if item.role == "user":
                source_text = item.text
                break
        if not source_request_id or not source_text:
            self._notify("warn", "这条回复没有可重新生成的来源")
            return

        self._seq += 1
        reply_id = f"m{self._seq}-a"
        self._append(_Message(reply_id, "assistant", "", MSG_STREAMING))
        self._stream_id = reply_id
        self._full_text = ""
        self._chunks = []
        self._cursor = 0
        self._partial = ""
        self._last_terminal = ""
        self.stateChanged.emit()

        request_id = uuid.uuid4().hex
        self._requests[reply_id] = request_id
        self._launch(request_id, source_text, reply_id, source_request_id=source_request_id)

    @Slot()
    def cancel(self) -> None:
        """打断正在说的这条。

        两边都要做：
          · 界面这边立刻停住——协议层的中止是两段式的（先 ABORT_PENDING，步末才 ABORTED），
            等它回来用户会觉得「点了没反应」；
          · 同时把中止请求送给协议层——只停前端不动后端，那边还在烧算力和额度。
        已经显示出来的部分原样留着：用户看到的就是他说停的地方，不补、不删。
        """
        message_id = self._stream_id
        if not message_id:
            self._notify("info", "现在没有正在回复的内容")
            return
        self._abort_requested.add(message_id)
        self._timer.stop()
        self._request_abort(self._requests.get(message_id, ""))
        self._settle(message_id, self._partial, MSG_ABORTED)

    @Slot()
    def clear(self) -> None:
        """清空界面上的这段对话。

        只清界面：协议层那边的执行记录不动（要能 Replay、也要能对账）。
        正在回复时按打断处理，否则定时器会继续往一条已经不在的消息里写字。
        """
        if self._stream_id:
            self.cancel()
        self._messages.clear()
        self.conversationCleared.emit()
        self.stateChanged.emit()

    # ---------------------------------------------------------------- 取数据（asyncio 线程）

    def _launch(
        self,
        request_id: str,
        text: str,
        reply_id: str,
        *,
        source_request_id: str = "",
    ) -> None:
        """把「等这一轮回复」丢进 asyncio 线程（一轮 = 一次执行）。

        错误在这里就地变成一句人话：界面不该因为协议层出错而卡在「正在回复…」。
        """
        conversation_id = self._conversation_id      # 在 Qt 线程里取，别跨线程读

        async def _run() -> None:
            try:
                if source_request_id:
                    reply = await self._client.regenerate_reply(
                        request_id,
                        source_request_id,
                        conversation_id=conversation_id,
                    )
                else:
                    reply = await self._client.send_message(
                        request_id, text, conversation_id=conversation_id
                    )
            except ProtocolError as exc:
                self._replyFailed.emit(reply_id, exc.user_message)
                return
            except Exception as exc:                      # pragma: no cover - 兜底
                self._replyFailed.emit(reply_id, f"{type(exc).__name__}：{exc}")
                return
            self._replyReady.emit(reply_id, reply.text, reply.status, reply.conversation_id)

        self._runner.submit(_run())

    def _request_abort(self, request_id: str) -> None:
        """把「中止这一轮」送到协议层（同一轮只送一次）。

        按 request_id（= 这一轮的幂等键）指认，而不是按会话：同一段对话里的下一轮
        不该被上一轮的中止误伤。
        """
        if not request_id or request_id in self._abort_sent:
            # 回复还没回来时拿不到 request_id 的情况不存在（发出去的瞬间就记下了），
            # 这个判断只是防止同一个请求被送两遍。
            return
        self._abort_sent.add(request_id)
        self._fire_and_forget(self._client.abort_reply(request_id))

    def _fire_and_forget(self, coro) -> None:
        """丢一个协程出去执行，失败只当没这回事（通知不到协议层不该让界面崩）。"""
        try:
            future = self._runner.submit(coro)
        except Exception:                                 # pragma: no cover - 循环已经停了
            return
        future.add_done_callback(self._swallow)

    @staticmethod
    def _swallow(future) -> None:
        try:
            future.exception()
        except Exception:                                 # pragma: no cover - 已取消等情形
            pass

    # ---------------------------------------------------------------- 回到主线程（信号槽）

    def _on_reply_ready(self, message_id: str, text: str, status: str, conversation_id: str) -> None:
        """整段回复到手（在 Qt 主线程执行）。从这里开始才是「一段段显示」。"""
        if conversation_id:
            self._conversation_id = conversation_id       # 下一轮带上这段对话的上下文
        if message_id in self._abort_requested:
            # 用户已经点过打断：协议层的中止是两段式的，不能等它，界面这边立刻停住；
            # 那一刀现在补给它（指认这一轮自己的 request_id，不碰别的轮次）。
            self._request_abort(self._requests.get(message_id, ""))
            if message_id == self._stream_id:
                self._settle(message_id, self._partial, MSG_ABORTED)
            return
        if message_id != self._stream_id:
            return                                        # 界面已经清空/换了一条，别往旧消息上写
        self._last_terminal = status
        if status != STATUS_COMPLETED:
            note = _TERMINAL_TEXT.get(status, f"协议层报了一个非完成终态：{status}")
            self._settle(message_id, text, _TERMINAL_TO_MESSAGE.get(status, MSG_FAILED))
            self._notify("error" if status != STATUS_ABORTED else "warn", note)
            return

        self._full_text = text or ""
        self._chunks = split_into_chunks(self._full_text)
        self._cursor = 0
        self._partial = ""
        if not self._chunks:
            self._settle(message_id, "", MSG_OK)
            return
        self.stateChanged.emit()
        # 第一段不等节奏：点完发送立刻要有字出来，否则「点了没反应」
        self._timer.start(0)

    def _on_reply_failed(self, message_id: str, message: str) -> None:
        if message_id != self._stream_id:
            return
        self._settle(message_id, self._partial, MSG_FAILED)
        self._notify("error", message)

    def _advance(self) -> None:
        """推下一段（只由主线程的 QTimer 触发）。"""
        message_id = self._stream_id
        if not message_id:
            return
        if self._cursor >= len(self._chunks):
            self._settle(message_id, self._full_text, MSG_OK)
            return
        chunk = self._chunks[self._cursor]
        self._cursor += 1
        self._partial += chunk
        self._replace_text(message_id, self._partial)
        self.messageProgressed.emit(message_id, self._partial)
        self.stateChanged.emit()
        self._timer.start(self._delay_for(chunk))

    def _delay_for(self, chunk: str) -> int:
        """这一段之后停多久。20–50ms 的自然节奏，测试用 `pace_scale` 整体压缩。"""
        return max(1, int(round(chunk_delay_ms(chunk) * self._pace)))

    # ---------------------------------------------------------------- 内部：消息表

    def _append(self, message: _Message) -> None:
        self._messages.append(message)
        self.messageAppended.emit(message.id, message.role, message.text, message.status)
        self.stateChanged.emit()

    def _replace_text(self, message_id: str, text: str) -> None:
        for item in self._messages:
            if item.id == message_id:
                item.text = text
                return

    def _settle(self, message_id: str, text: str, status: str) -> None:
        """收尾一条：定状态、停定时器、把「流式」那支旗子放下来。"""
        self._timer.stop()
        for item in self._messages:
            if item.id == message_id:
                item.text = text
                item.status = status
                break
        self._stream_id = ""
        # 刻意不在这里清空 _chunks / _cursor / _partial：状态条与「推了几段」的探针
        # 要能读到刚刚这一次的结果（下一次 send 时才重置）。
        self.messageSettled.emit(message_id, status, text)
        self.stateChanged.emit()

    def _last_reply_text(self) -> str:
        for item in reversed(self._messages):
            if item.role == "assistant":
                return item.text
        return ""

    def _notify(self, level: str, text: str) -> None:
        self.noticeRaised.emit(level, text)

    # ---------------------------------------------------------------- 收尾

    def detach(self) -> None:
        """停定时器、断开与协议层的连接（窗口关闭时调用）。"""
        self._timer.stop()
        self._closed = True
        self._fire_and_forget(self._client.aclose())
