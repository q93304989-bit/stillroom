"""UI 与协议层之间**唯一**的接触面（助手页「对话」用）。

为什么要有这一层
----------------
协议层（`runtime/` `validator/` `schemas/` `contracts/`）现在仍在开发中，接口还会动。
界面如果直接调它，协议层一改就得满 UI 找调用点；反过来，界面如果自己造一份假数据，
协议层接上时又得把假数据一个个揪出来。所以规矩是：

    `app/ui/**` 只认这个类的方法，其它什么都不认。

协议层接口冻结之后，**只改这一个文件**：把每个 `TODO(协议层)` 换成真实调用，UI 一行不动。

现在这份实现是 mock
--------------------
不联网、不碰数据库、不调用任何模型、不读 .env：回复文本是本地拼出来的占位文字，
只为了让「整段回复 → 按语义边界切块 → 一段段显示」这条前端链路能跑通、能被测。
`ProtocolClient.mock` 为 True，界面据此如实告诉用户「这还不是模型说的话」
（见 `AssistantBridge.sourceLabel`），不允许把 mock 装成真结果。

方法怎么定的
------------
贴着 P1 的 11 个工具语义走，但不猜协议层的实现细节：

    send_message(request_id, text, conversation_id=...)  ↔  execute_workflow + 轮询到终态
    abort_reply(request_id)                              ↔  abort_execution（两段式）
    status_of(request_id)                                ↔  get_execution_status

**一轮回复 = 一次 execution**，所以幂等键是「这一轮的 `request_id`」，而不是会话 id：
这样「打断这一轮」能精确落到某一次执行上，不会误伤同一段对话里的下一轮。
会话（`conversation_id`）只是「同一串上下文」的把手，由 `Reply` 带回来、下一轮再传进去。

调用形态：都是协程。界面层用 `AsyncRunner.submit()` 丢进 asyncio 线程执行，
跟其它桥一样，Qt 主线程不阻塞。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable

#: 协议层执行状态机里的终态。UI 只需要认识这几个值（9 个状态里其余都是中间态）。
STATUS_COMPLETED = "COMPLETED"
STATUS_ABORTED = "ABORTED"
STATUS_FAILED = "FAILED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

TERMINAL_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_ABORTED,
        STATUS_FAILED,
        STATUS_TIMEOUT,
        STATUS_BUDGET_EXCEEDED,
    }
)

#: mock 阶段挂的工作流 id。真接上之后由协议层的 `match_workflow` 决定用哪份工作流。
#: 写死一个占位值并摆在明处，免得以后有人以为「对话已经真的接到工作流上了」。
MOCK_WORKFLOW_ID = "assistant_chat"


class ProtocolError(RuntimeError):
    """adapter 对外只暴露这一种错误。

    协议层自己的错误码体系不该漏到界面上，界面上只显示一句人话：`user_message` 给用户看，
    `detail` 给日志与排查用。翻译在这一层做，协议层换错误码时只改这里。

    TODO(协议层): 接上后在这里做 `ErrorCode` → `user_message` 的映射表。
    """

    def __init__(self, user_message: str, *, detail: str = "") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


@dataclass(frozen=True)
class Reply:
    """一轮回复的结果：**完整**文本 + 终态 + 这段对话的把手。

    协议层不做真流式（`stream: false`）——先把整段生成出来、走完 Quality Gate 再交给前端。
    所以这里一次把全文交回来；「一段段显示」是前端自己的事（见 `app/ui/pseudo_stream.py`），
    不在这层做，这层也不做任何质量校验。
    """

    conversation_id: str
    request_id: str
    text: str
    status: str = STATUS_COMPLETED

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED


def mock_reply_text(text: str) -> str:
    """mock 回复：本地拼的占位文字（不联网、不调用模型）。

    刻意写成多段、多标点的中文：前端要按语义边界切块，得有一段「像人写的」文字，
    切分与节奏才跑得出真实观感。内容本身如实说明它是占位文本，不装成模型输出。

    TODO(协议层): 整个函数删掉，回复文本改由协议层给（事件里存的是元数据 + hash + ref，
    正文按 ref 去内容寻址 store 取）。
    """
    subject = " ".join((text or "").split())
    if len(subject) > 24:
        subject = subject[:24] + "……"
    return (
        f"先把你这句话收下来：「{subject}」。\n\n"
        "这一版是本地占位回复——协议层还没接上，所以它不含任何真实的模型输出；"
        "它唯一的用处，是把「整段回复 → 按语义边界切块 → 每段停 20–50 毫秒」这条展示链路跑通。\n\n"
        "顺带说清一件容易搞混的事：切块与节奏是前端的活，质量校验不是。"
        "真接上之后，文本在离开协议层之前就已经过一次 Quality Gate，"
        "前端拿到的每一段都是过了闸的，前端不再自己判断「这段话能不能给用户看」。\n\n"
        "所以现在你看到的分段，就是接上协议层之后的分段，一个字都不会变；"
        "变的只有文本本身——那会是执行链路的真实结果：理解需求、找参考、生成、评估、精修、交付。"
    )


class ProtocolClient:
    """UI 与协议层之间唯一的接触面。"""

    def __init__(
        self,
        *,
        reply_factory: Callable[[str], str] = mock_reply_text,
        workflow_id: str = MOCK_WORKFLOW_ID,
    ) -> None:
        self._reply_factory = reply_factory
        self._workflow_id = workflow_id
        #: 幂等表：request_id → 这一轮的结算结果（同一 request_id 重复调用拿到同一个对象）
        self._replies: dict[str, Reply] = {}
        self._turn_inputs: dict[str, str] = {}
        #: 已中止的轮次（mock 里文本是瞬间生成的，中止只能影响状态）
        self._aborted: set[str] = set()
        self._conversations: set[str] = set()
        self._closed = False

    # ---------------------------------------------------------------- 身份

    @property
    def mock(self) -> bool:
        """现在是不是 mock 顶着的。界面用它给用户一句实话。"""
        return True

    @property
    def workflow_id(self) -> str:
        return self._workflow_id

    # ---------------------------------------------------------------- 一轮对话

    async def send_message(
        self,
        request_id: str,
        text: str,
        *,
        conversation_id: str = "",
    ) -> Reply:
        """发一轮，等协议层把这一轮的**完整**回复交回来。

        `conversation_id` 留空表示「这段对话的第一轮」；后续轮次把它传回来，
        让上下文接得上。返回值里的 `conversation_id` 就是下一轮要传的那个把手。

        **幂等**：同一个 `request_id` 重复调用，永远只产生一次执行、拿到同一份结果
        （这是对外承诺的语义，所以 mock 也照做——把这条先钉住，UI 与测试照着它写）。

        TODO(协议层): 换成「execute_workflow(request_id, workflow_id, input) →
        轮询 execution_events 到终态 → 按 ref 取回这一轮的文本」。轮询要能看到
        `ABORT_PENDING → ABORTED`（两段式中止），所以这里返回的 `status` 必须是终态。
        """
        if self._closed:
            raise ProtocolError("协议层连接已关闭", detail="client closed")
        if not request_id:
            raise ProtocolError("这一轮没有 request_id", detail="empty request_id")

        cached = self._replies.get(request_id)
        if cached is not None:
            return cached                                   # 幂等：只有第一次真的执行

        conversation = conversation_id or f"conv-{uuid.uuid4().hex[:12]}"
        self._conversations.add(conversation)
        self._turn_inputs[request_id] = text
        reply = Reply(
            conversation_id=conversation,
            request_id=request_id,
            text=self._reply_factory(text),
            status=STATUS_COMPLETED,
        )
        self._replies[request_id] = reply
        return reply

    async def regenerate_reply(
        self,
        request_id: str,
        source_request_id: str,
        *,
        conversation_id: str = "",
    ) -> Reply:
        """从某一轮的源请求重新生成一份回复。

        TODO: 等协议层接口冻结后替换为真实调用。真实实现应通过 source_request_id
        重新执行同一输入，并保持 request_id 幂等；当前 mock 只复刻这层语义。
        """
        if self._closed:
            raise ProtocolError("协议层连接已关闭", detail="client closed")
        if not request_id or not source_request_id:
            raise ProtocolError("重新生成缺少请求标识", detail="empty request id")
        source_text = self._turn_inputs.get(source_request_id)
        if source_text is None:
            raise ProtocolError("找不到要重新生成的那一轮", detail="unknown source request")
        return await self.send_message(
            request_id, source_text, conversation_id=conversation_id
        )

    async def abort_reply(self, request_id: str) -> None:
        """请求中止某一轮。

        注意语义：中止是**两段式**的——`RUNNING` 下先到 `ABORT_PENDING`，只在当前步结束后
        才收敛成 `ABORTED`。所以调用方不能假设「一调就停下来了」：前端本来也不等它，
        到手的文本立刻停住显示即可，这里只是把「停止」的请求如实送到协议层。

        TODO(协议层): 先用 `request_id` 找到这次 execution，再调 `abort_execution`。
        它只发一个事件、不直接写状态，也不接受任何覆盖输入。
        现在 mock：把这一轮记成 ABORTED（文本是瞬间生成的，所以 mock 里中止只影响状态）。
        """
        if self._closed:
            raise ProtocolError("协议层连接已关闭", detail="client closed")
        if request_id not in self._replies:
            raise ProtocolError(
                "这一轮已经不在了", detail=f"unknown request_id {request_id}"
            )
        self._aborted.add(request_id)

    def status_of(self, request_id: str) -> str:
        """某一轮现在的状态（终态用上面那几个常量；不认识的返回空串）。

        TODO(协议层): 换成 `get_execution_status`。它是**读**接口，不推进状态机。
        现在 mock：读本地记的那一份。
        """
        if request_id in self._aborted:
            return STATUS_ABORTED
        reply = self._replies.get(request_id)
        return reply.status if reply else ""

    async def aclose(self) -> None:
        """断开与协议层的连接（窗口关闭时调用）。

        TODO(协议层): 关闭连接 / 释放句柄。现在 mock：只落一个开关。
        """
        self._closed = True
