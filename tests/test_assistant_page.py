"""助手页「对话 + 伪流式」的测试：adapter → 切块/节奏 → 桥 → QML。

三条边界都钉住：

  · 界面只经过 adapter —— 用例里把 adapter 换成替身，桥照样工作，不需要协议层在场
  · 切块与节奏在前端 —— 而且**一个字都不能丢**（切块拼回去必须逐字等于原文）
  · 协议层给什么就显示什么 —— 终态不是 COMPLETED 时如实标失败，前端不自己改判

全部离线：adapter 是 mock，QML 离屏加载，不发任何请求、不连数据库。
"""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from PySide6.QtCore import QObject, QUrl  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402

from app.adapters import protocol_client  # noqa: E402
from app.adapters.protocol_client import (  # noqa: E402
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_TIMEOUT,
    TERMINAL_STATUSES,
    ProtocolClient,
    ProtocolError,
    Reply,
)
from app.bootstrap import build_context  # noqa: E402
from app.ui.agent_bridge import AgentBridge  # noqa: E402
from app.ui.assistant_bridge import (  # noqa: E402
    MSG_ABORTED,
    MSG_FAILED,
    MSG_OK,
    MSG_STREAMING,
    AssistantBridge,
)
from app.ui.async_runner import AsyncRunner  # noqa: E402
from app.ui.bridge import UiBridge  # noqa: E402
from app.ui.knowledge_bridge import KnowledgeBridge  # noqa: E402
from app.ui.pseudo_stream import (  # noqa: E402
    MAX_CHARS,
    MAX_DELAY_MS,
    MIN_DELAY_MS,
    chunk_delay_ms,
    split_into_chunks,
)
from app.ui.selfupdate_bridge import SelfUpdateBridge  # noqa: E402
from app.ui.settings_bridge import SettingsBridge  # noqa: E402
from app.ui.theme import Theme  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1]
QML_DIR = WORKSPACE / "app" / "ui" / "qml"

#: 夹具创建的引擎故意留着不销毁（理由同 test_ui.py：shiboken 的包装缓存按地址找对象，
#: 上一个引擎的界面树一释放，地址就会被下一个引擎重新用到）。
_ENGINES_KEPT_ALIVE: list = []


def wait_until(app, predicate, timeout: float = 10.0) -> bool:
    """抽事件循环直到条件成立（与 test_ui.py 同一套做法）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


# =============================================================== 一、adapter

async def test_mock_reply_is_offline_and_marked_as_mock():
    """adapter 现在是 mock：不联网也要给得出「一整段回复」，而且自己承认是 mock。"""
    client = ProtocolClient()
    assert client.mock is True

    reply = await client.send_message("req-1", "把中秋海报的提示词改得更国潮")

    assert reply.status == STATUS_COMPLETED
    assert len(reply.text) > 40, "整段回复太短，前端切块就没意义了"
    assert "国潮" in reply.text, "回复里没提到用户说的话——那就不像一轮真对话"
    assert reply.conversation_id, "没给会话把手，下一轮就接不上上下文"
    assert reply.request_id == "req-1"


async def test_same_request_id_never_runs_twice():
    """幂等：同一个 request_id 重复调用只产生一次执行、拿到同一份结果。"""
    client = ProtocolClient()

    first = await client.send_message("req-same", "第一句")
    second = await client.send_message("req-same", "第一句")
    other = await client.send_message("req-other", "第一句", conversation_id=first.conversation_id)

    assert first is second, "重复的 request_id 又跑了一次"
    assert other.request_id == "req-other"
    assert other.conversation_id == first.conversation_id, "同一段对话的把手没传下去"


async def test_request_id_must_not_be_empty():
    client = ProtocolClient()
    with pytest.raises(ProtocolError):
        await client.send_message("", "你好")


async def test_unknown_turn_is_a_protocol_error():
    """会话不在了要给一句人话，不是抛一个裸的 KeyError。"""
    client = ProtocolClient()
    with pytest.raises(ProtocolError) as excinfo:
        await client.abort_reply("从没发过的请求")
    assert excinfo.value.user_message


async def test_reply_status_is_one_of_the_terminal_states():
    """UI 只认终态：拿到的 status 必须在那几个值里，不能是中间态。"""
    client = ProtocolClient()
    reply = await client.send_message("req-terminal", "随便说一句")
    assert reply.status in TERMINAL_STATUSES


async def test_abort_shows_up_in_the_turn_status():
    """中止：这一轮的状态变成 ABORTED（前端据此显示「已打断」）。"""
    client = ProtocolClient()
    await client.send_message("req-abort", "会被打断的一轮")

    await client.abort_reply("req-abort")

    assert client.status_of("req-abort") == STATUS_ABORTED


async def test_closed_client_refuses_new_turns():
    client = ProtocolClient()
    await client.aclose()
    with pytest.raises(ProtocolError):
        await client.send_message("req-after-close", "还想再说一句")


def test_adapter_never_imports_the_protocol_layer():
    """adapter 是唯一接触面，所以它自己也不许伸手进协议层内部。

    静态查源码（AST）而不是查 `sys.modules`：同一个进程里别的用例可能早把 `runtime`
    导进来了，那样的断言会假绿。
    """
    tree = ast.parse(Path(protocol_client.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    forbidden = {"runtime", "validator", "schemas", "contracts", "mcp_server"}
    assert not (imported & forbidden), f"adapter 伸手进了协议层：{sorted(imported & forbidden)}"


def test_ui_layer_reaches_the_protocol_layer_only_through_the_adapter():
    """界面层（app/ui/**）里不许出现协议层的 import —— 一律经过 adapter。"""
    forbidden = {"runtime", "validator", "schemas", "contracts", "mcp_server"}
    offenders: list[str] = []
    for path in (WORKSPACE / "app" / "ui").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = {node.module.split(".")[0]}
            else:
                continue
            if names & forbidden:
                offenders.append(f"{path.name}: {sorted(names & forbidden)}")
    assert offenders == [], f"界面绕过了 adapter：{offenders}"


# =============================================================== 二、切块与节奏

def test_chunks_rejoin_to_the_original_text():
    """伪流式只负责「一段段显示」，不是摘要：拼回去必须逐字等于原文。"""
    samples = [
        "第一句。第二句！第三句？",
        "一段没有标点的长文字就这样一直写下去看看它会不会被硬切开而且一个字都不许丢",
        "段落一。\n\n段落二，里面有逗号、顿号，还有：冒号。\n\n最后一段",
        "单句",
        "英文 sentence one. And a second one! Then a third?",
        "结尾没有标点",
        "。",
        "多行\n换行\n到底",
        "以空行收尾。\n\n",
        "\n\n\n",
        "   ",
    ]
    for sample in samples:
        chunks = split_into_chunks(sample)
        assert "".join(chunks) == sample, f"切块丢了字：{sample!r} → {chunks!r}"


def test_chunks_are_never_empty_and_never_over_the_limit():
    text = "标点很多。逗号也有，顿号、冒号：都在。还有一段没有标点的超长句子" * 3
    chunks = split_into_chunks(text)
    assert all(chunk for chunk in chunks), "出现了空块（推给界面就是白闪一下）"
    assert all(len(chunk) <= MAX_CHARS for chunk in chunks)
    assert len(chunks) > 3, "整段只切出一两块，看不出流式"


def test_empty_text_gives_no_chunks():
    assert split_into_chunks("") == []


def test_sentences_become_separate_chunks():
    """短句按句切：一句话一段（并成一大坨再由定时器吐出来就不像打字了）。"""
    assert split_into_chunks("第一句。第二句！") == ["第一句。", "第二句！"]


def test_paragraph_breaks_stay_with_the_previous_chunk():
    """段落空行跟着上一句走：换行也是「内容」，不许挪到下一块前面去。"""
    chunks = split_into_chunks("第一段。\n\n第二段。")
    assert chunks[0].endswith("\n\n"), f"段落空行没跟上一块走：{chunks!r}"
    assert "第二段。" in chunks[1]
    assert "".join(chunks) == "第一段。\n\n第二段。"


def test_delay_always_stays_in_the_natural_window():
    """节奏是任务写死的 20–50ms：任何一块（含空白块、硬切块）都不许越界。"""
    for chunk in ["。", "\n\n", "字" * 200, "普通一句，", "句末。", "   \n", "没有标点的一段"]:
        delay = chunk_delay_ms(chunk)
        assert MIN_DELAY_MS <= delay <= MAX_DELAY_MS, f"{chunk!r} 的停顿跑出了窗口：{delay}"


def test_heavier_punctuation_waits_longer():
    """标点越重停得越久：段落 > 句末 > 逗号 > 没有标点。这是「像在打字」的全部秘密。"""
    paragraph = chunk_delay_ms("这一段落结束了。\n")
    sentence = chunk_delay_ms("这一句结束了。")
    clause = chunk_delay_ms("这里只是逗号，")
    plain = chunk_delay_ms("这里没有标点")
    assert paragraph > sentence > clause > plain


# =============================================================== 三、桥（不需要 QML）

class FakeClient:
    """adapter 的替身：只实现界面真正用到的那几个方法。

    用例只认 adapter 的方法名与返回值，不碰协议层实现——协议层怎么改，这些用例都不用动。
    这就是「界面只经过 adapter」在测试上的回报。
    """

    def __init__(
        self,
        *,
        text: str = "第一句。第二句！第三句？",
        status: str = STATUS_COMPLETED,
        fail: str = "",
        mock: bool = False,
    ) -> None:
        self.mock = mock
        self.sent: list[tuple[str, str, str]] = []      # (request_id, conversation_id, text)
        self.aborted: list[str] = []                    # request_id
        self.closed = False
        self._text = text
        self._status = status
        self._fail = fail

    async def send_message(self, request_id, text, *, conversation_id=""):
        if self._fail == "send":
            raise ProtocolError("这次没能跑起来")
        self.sent.append((request_id, conversation_id, text))
        if self._status != STATUS_COMPLETED:
            return Reply(conversation_id or "conv-fake", request_id, "", self._status)
        return Reply(conversation_id or "conv-fake", request_id, self._text, self._status)

    async def abort_reply(self, request_id):
        self.aborted.append(request_id)

    def status_of(self, request_id):
        return self._status

    async def aclose(self):
        self.closed = True


def make_bridge(client=None, *, pace_scale: float = 0.02) -> tuple[AssistantBridge, AsyncRunner]:
    """建一个桥（不需要 QML，也不需要协议层）。

    `pace_scale` 把 20–50ms 的节奏整体压小，好让用例跑得快——走的还是同一条代码路径，
    节奏本身的窗口由上面那两个纯函数用例负责钉住。
    """
    runner = AsyncRunner()
    return AssistantBridge(runner, client, pace_scale=pace_scale), runner


def close_bridge(bridge: AssistantBridge, runner: AsyncRunner) -> None:
    bridge.detach()
    assert runner.close(), "asyncio 线程没能在超时内退出"


def test_send_appends_both_messages_right_away(qt_app):
    """发出去之后，用户那句与「正在回复」的空助手那条都要立刻在列表里。"""
    client = FakeClient()
    bridge, runner = make_bridge(client)
    try:
        bridge.send("帮我想一句中秋海报的文案")

        rows = bridge.property("messages")
        assert [(row["role"], row["status"]) for row in rows] == [
            ("user", MSG_OK),
            ("assistant", MSG_STREAMING),
        ]
        assert rows[0]["text"] == "帮我想一句中秋海报的文案"
        assert bridge.streaming is True
        # 取数据在 asyncio 线程里，等它真的走到 adapter 上
        assert wait_until(qt_app, lambda: len(client.sent) == 1)
        assert client.sent[0][2] == "帮我想一句中秋海报的文案"
    finally:
        close_bridge(bridge, runner)


def test_reply_is_streamed_in_several_chunks(qt_app):
    """整段回复要一段段推出来，最后逐字等于 adapter 给的全文。"""
    client = FakeClient(text="第一句。第二句！第三句？没有标点的收尾")
    bridge, runner = make_bridge(client)
    seen: list[str] = []
    bridge.messageProgressed.connect(lambda _id, text: seen.append(text))
    try:
        bridge.send("说点什么")

        assert wait_until(qt_app, lambda: not bridge.streaming, timeout=10)
        assert bridge.property("streamedChunks") == len(split_into_chunks(client._text))
        assert bridge.property("streamedChunks") >= 3
        assert bridge.property("lastReplyText") == client._text
        assert bridge.property("lastTerminalStatus") == STATUS_COMPLETED
        # 每推一段都是「只增不减」的：界面上的字不会往回缩
        assert seen == sorted(seen, key=len)
        assert seen[-1] == client._text
        assert bridge.property("messages")[1]["status"] == MSG_OK
    finally:
        close_bridge(bridge, runner)


def test_streaming_stops_at_the_last_chunk(qt_app):
    """推完最后一段就收工：定时器不再空转（否则状态条会一直显示「正在回复」）。"""
    bridge, runner = make_bridge(FakeClient(text="第一句。第二句！第三句？"))
    progressed: list[str] = []
    bridge.messageProgressed.connect(lambda _id, text: progressed.append(text))
    try:
        bridge.send("说点什么")
        assert wait_until(qt_app, lambda: not bridge.streaming, timeout=10)

        settled = len(progressed)
        assert wait_until(qt_app, lambda: bridge.property("messageCount") == 2)
        time.sleep(0.05)
        qt_app.processEvents()
        assert len(progressed) == settled, "收工之后还在继续推字"
        assert bridge.property("statusText") == "说完了"
    finally:
        close_bridge(bridge, runner)


def test_empty_input_is_refused_before_anything_is_sent(qt_app):
    """空输入挡在门口：不建消息、不打扰 adapter（这是输入校验，不是质量校验）。"""
    client = FakeClient()
    bridge, runner = make_bridge(client)
    notices: list[tuple[str, str]] = []
    bridge.noticeRaised.connect(lambda level, text: notices.append((level, text)))
    try:
        bridge.send("   ")

        assert bridge.property("messageCount") == 0
        assert client.sent == []
        assert notices and notices[0][0] == "warn"
    finally:
        close_bridge(bridge, runner)


def test_second_message_is_refused_while_streaming(qt_app):
    """一次只跑一轮：还在回复时再发一句要被挡住（否则界面上两条回复会打架）。"""
    client = FakeClient(text="一段稍微长一点的回复，用来占住流式的时间。再来一句收尾。")
    bridge, runner = make_bridge(client)
    notices: list[tuple[str, str]] = []
    bridge.noticeRaised.connect(lambda level, text: notices.append((level, text)))
    try:
        bridge.send("第一句")
        assert bridge.streaming is True
        assert wait_until(qt_app, lambda: len(client.sent) == 1)

        bridge.send("第二句")

        assert bridge.property("messageCount") == 2
        assert len(client.sent) == 1
        assert any(level == "warn" for level, _ in notices)
    finally:
        close_bridge(bridge, runner)


def test_cancel_stops_the_stream_and_keeps_what_was_shown(qt_app):
    """打断：立刻停在半句话上，并且真的通知了协议层去中止这一轮。"""
    client = FakeClient(text="第一句，慢一点说。第二句，还要再说。第三句，最后一句。第四句收尾。")
    bridge, runner = make_bridge(client)
    try:
        bridge.send("说个长的")
        assert wait_until(qt_app, lambda: bridge.property("streamedChunks") >= 1, timeout=10)
        bridge.cancel()

        assert bridge.streaming is False
        rows = bridge.property("messages")
        assert rows[1]["status"] == MSG_ABORTED
        assert 0 < len(rows[1]["text"]) < len(client._text), "打断没有停在用户说停的地方"
        request_id = client.sent[0][0]
        assert wait_until(qt_app, lambda: client.aborted == [request_id]), \
            "只停了界面、没通知协议层，那边还在烧算力"
    finally:
        close_bridge(bridge, runner)


def test_cancel_without_a_running_reply_is_a_no_op(qt_app):
    bridge, runner = make_bridge(FakeClient())
    notices: list[tuple[str, str]] = []
    bridge.noticeRaised.connect(lambda level, text: notices.append((level, text)))
    try:
        bridge.cancel()
        assert bridge.property("messageCount") == 0
        assert notices and notices[0][0] == "info"
    finally:
        close_bridge(bridge, runner)


def test_cancel_is_not_confused_by_the_next_turn(qt_app):
    """中止按 request_id 指认：打断第一轮不该把第二轮一起中止。"""
    client = FakeClient(text="一句话。")
    bridge, runner = make_bridge(client)
    try:
        bridge.send("第一轮")
        assert wait_until(qt_app, lambda: not bridge.streaming, timeout=10)
        bridge.send("第二轮")
        assert wait_until(qt_app, lambda: not bridge.streaming, timeout=10)

        request_ids = [item[0] for item in client.sent]
        assert len(set(request_ids)) == 2, "两轮用了同一个 request_id：幂等键会互相顶掉"
        assert client.aborted == [], "没点打断却去中止了"
    finally:
        close_bridge(bridge, runner)


def test_conversation_handle_is_reused_across_turns(qt_app):
    """会话把手：第一轮拿到的 conversation_id，第二轮要带上（上下文才接得上）。"""
    client = FakeClient(text="一句话。")
    bridge, runner = make_bridge(client)
    try:
        bridge.send("第一轮")
        assert wait_until(qt_app, lambda: not bridge.streaming, timeout=10)
        bridge.send("第二轮")
        assert wait_until(qt_app, lambda: not bridge.streaming, timeout=10)

        first, second = client.sent[0][1], client.sent[1][1]
        assert first == "", "第一轮不该自带会话把手"
        assert second == "conv-fake", "第二轮没把上一轮的会话把手传回去"
        assert bridge.property("conversationId") == "conv-fake"
    finally:
        close_bridge(bridge, runner)


def test_protocol_error_becomes_a_human_sentence(qt_app):
    """协议层出错：消息标失败、提示是人话，界面不会一直卡在「正在回复」。"""
    bridge, runner = make_bridge(FakeClient(fail="send"))
    notices: list[tuple[str, str]] = []
    bridge.noticeRaised.connect(lambda level, text: notices.append((level, text)))
    try:
        bridge.send("随便说点")

        assert wait_until(qt_app, lambda: not bridge.streaming, timeout=10)
        assert bridge.property("messages")[1]["status"] == MSG_FAILED
        assert any(level == "error" and "没能跑起来" in text for level, text in notices)
    finally:
        close_bridge(bridge, runner)


def test_terminal_state_other_than_completed_is_shown_as_failure(qt_app):
    """协议层给的终态不是 COMPLETED（比如超时）：如实标失败，前端不自己改判成败。"""
    bridge, runner = make_bridge(FakeClient(status=STATUS_TIMEOUT))
    notices: list[tuple[str, str]] = []
    bridge.noticeRaised.connect(lambda level, text: notices.append((level, text)))
    try:
        bridge.send("会超时的一句")

        assert wait_until(qt_app, lambda: not bridge.streaming, timeout=10)
        assert bridge.property("lastTerminalStatus") == STATUS_TIMEOUT
        assert bridge.property("messages")[1]["status"] == MSG_FAILED
        assert any("超时" in text for _, text in notices)
    finally:
        close_bridge(bridge, runner)


def test_status_text_reports_the_chunk_progress(qt_app):
    """状态条要能说出「第几段 / 一共几段」——伪流式最直观的进度就在这儿。"""
    bridge, runner = make_bridge(FakeClient(text="第一句。第二句！第三句？"))
    try:
        bridge.send("问一句")
        assert wait_until(qt_app, lambda: bridge.property("streamedChunks") >= 1, timeout=10)
        assert "段" in bridge.property("statusText")
        assert wait_until(qt_app, lambda: not bridge.streaming, timeout=10)
        assert bridge.property("statusText") == "说完了"
    finally:
        close_bridge(bridge, runner)


def test_clear_empties_the_conversation(qt_app):
    bridge, runner = make_bridge(FakeClient(text="一句话。"))
    cleared: list[bool] = []
    bridge.conversationCleared.connect(lambda: cleared.append(True))
    try:
        bridge.send("说一句")
        assert wait_until(qt_app, lambda: not bridge.streaming, timeout=10)

        bridge.clear()

        assert bridge.property("messageCount") == 0
        assert bridge.property("messages") == []
        assert cleared == [True]
    finally:
        close_bridge(bridge, runner)


def test_clear_while_streaming_stops_the_clock(qt_app):
    """清空时正在回复：先停下计时器，否则它会继续往一条已经不存在的消息里写字。"""
    bridge, runner = make_bridge(FakeClient(text="很长的一段，用来占住时间。再加一句收尾。"))
    progressed: list[str] = []
    bridge.messageProgressed.connect(lambda _id, text: progressed.append(text))
    try:
        bridge.send("说个长的")
        assert wait_until(qt_app, lambda: len(progressed) >= 1, timeout=10)

        bridge.clear()
        assert bridge.property("messageCount") == 0
        assert bridge.streaming is False

        settled = len(progressed)
        time.sleep(0.05)
        qt_app.processEvents()
        assert len(progressed) == settled, "清空之后还在继续推字"
        assert bridge.property("messages") == []
    finally:
        close_bridge(bridge, runner)


def test_mock_source_label_is_honest(qt_app):
    """mock 要写在明处：不能让用户以为这是模型说的话。"""
    mock_bridge, runner = make_bridge(ProtocolClient())
    try:
        assert mock_bridge.property("mock") is True
        assert "占位" in mock_bridge.property("sourceLabel")
    finally:
        close_bridge(mock_bridge, runner)

    real_bridge, runner2 = make_bridge(FakeClient())
    try:
        assert real_bridge.property("mock") is False
        assert real_bridge.property("sourceLabel") == "来自协议层"
    finally:
        close_bridge(real_bridge, runner2)


def test_the_whole_reply_is_in_hand_before_anything_is_shown(qt_app):
    """伪流式的前提：adapter 一次把**整段**交回来，前端才有得切。

    这是刻意的取舍（协议层 stream: false，先整段过质量校验）。所以这里明确钉一条：
    显示之前桥手里就已经有全文了——切块发生在「收到全文」之后，而不是边收边切。
    """
    client = FakeClient(text="整段文本。前端负责切。每段停 20 到 50 毫秒。")
    bridge, runner = make_bridge(client, pace_scale=0.0)
    appended: list[str] = []

    def remember(_id, role, _text, _status):
        if role == "assistant":
            appended.append(role)

    bridge.messageAppended.connect(remember)
    try:
        bridge.send("问一句")
        assert appended == ["assistant"]
        assert wait_until(qt_app, lambda: not bridge.streaming, timeout=10)
        chunks = split_into_chunks(client._text)
        assert bridge.property("streamedChunks") == len(chunks)
        assert "".join(chunks) == client._text
    finally:
        close_bridge(bridge, runner)


def test_detach_closes_the_connection(qt_app):
    """收尾：断开与协议层的连接（窗口关掉后不该再留着它）。"""
    client = FakeClient()
    bridge, runner = make_bridge(client)
    try:
        bridge.detach()
        assert wait_until(qt_app, lambda: client.closed)
        assert bridge.streaming is False
    finally:
        assert runner.close()


def test_send_after_detach_is_refused(qt_app):
    """已收尾的桥不再往外发东西（否则退出时会冒出一个没人管的请求）。"""
    client = FakeClient()
    bridge, runner = make_bridge(client)
    notices: list[tuple[str, str]] = []
    bridge.noticeRaised.connect(lambda level, text: notices.append((level, text)))
    try:
        bridge.detach()
        bridge.send("退出前最后一句")

        assert client.sent == []
        assert bridge.property("messageCount") == 0
        assert notices
    finally:
        assert runner.close()


# =============================================================== 四、QML 集成

class ChatHarness:
    """把装配、引擎、桥拼起来（只有界面用例需要它；纯逻辑用例不建 QML）。

    `with_chat_bridge=False` 复现的是老夹具 / 截图工具的形态：**不注入** `assistantBridge`，
    助手页必须干净地降级（不报错、落在「跑流程」页签）——这也正是保护既有 463 项测试的那条路。
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        with_chat_bridge: bool = True,
        client=None,
        pace_scale: float = 0.02,
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "AGNES_API_KEY=sk-test\n"
            "AGNES_BASE_URL=https://api.test/v1\n",
            encoding="utf-8",
        )
        self.context = build_context(env_file=env_file, data_dir=tmp_path / "data")
        self.runner = AsyncRunner()
        self.theme = Theme(dark=True)
        self.bridge = UiBridge(self.context, self.runner, self.theme)
        self.settings_bridge = SettingsBridge(self.context, self.runner, self.theme)
        self.agent_bridge = AgentBridge(self.context, self.runner, self.bridge)
        self.selfupdate_bridge = SelfUpdateBridge(self.context, self.runner)
        self.knowledge_bridge = KnowledgeBridge(self.context, self.runner)
        self.assistant = (
            AssistantBridge(self.runner, client, pace_scale=pace_scale)
            if with_chat_bridge
            else None
        )
        self.warnings: list[str] = []

        self.engine = QQmlApplicationEngine()
        self.engine.warnings.connect(lambda items: self.warnings.extend(str(item) for item in items))
        self.engine.rootContext().setContextProperty("backend", self.bridge)
        self.engine.rootContext().setContextProperty("theme", self.theme)
        self.engine.rootContext().setContextProperty("settingsBridge", self.settings_bridge)
        self.engine.rootContext().setContextProperty("agentBridge", self.agent_bridge)
        self.engine.rootContext().setContextProperty("selfUpdateBridge", self.selfupdate_bridge)
        self.engine.rootContext().setContextProperty("knowledgeBridge", self.knowledge_bridge)
        if self.assistant is not None:
            self.engine.rootContext().setContextProperty("assistantBridge", self.assistant)
        self.engine.addImportPath(str(QML_DIR))
        self.engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))

    @property
    def root(self):
        roots = self.engine.rootObjects()
        return roots[0] if roots else None

    def errors(self) -> list[str]:
        return [
            message
            for message in self.warnings
            if "error" in message.lower() or "is not a type" in message
        ]

    def close(self) -> None:
        self.bridge.detach()
        self.agent_bridge.detach()
        if self.assistant is not None:
            self.assistant.detach()
        try:
            self.runner.run_blocking(self.context.http.aclose(), timeout=5.0)
        except Exception:                    # pragma: no cover - 已经断开时不必纠缠
            pass
        assert self.runner.close(), "asyncio 线程没能在超时内退出"
        self.context.history.close()
        self.context.prompts.close()
        _ENGINES_KEPT_ALIVE.append(self.engine)


def test_chat_page_loads_and_starts_on_the_chat_tab(qt_app, tmp_path):
    """注入桥之后：QML 干净加载、助手页有「对话」与「跑流程」两个页签、页数仍是 6。"""
    harness = ChatHarness(tmp_path)
    try:
        root = harness.root
        assert root is not None
        assert harness.errors() == [], f"QML 报错：{harness.errors()}"
        assert root.property("pageCount") == 6, "新页签不该挤进导航（导航还是 6 页）"

        root.openPage(0)
        root.openAgentTab(0)
        assert root.property("agentTabIndex") == 0
        assert root.property("agentChatMessages") == 0
        assert root.property("agentChatRows") == 0
        assert root.findChild(QObject, "chatPanel") is not None
        assert root.findChild(QObject, "chatComposer") is not None, "底部那条输入条没了"

        root.openAgentTab(1)
        assert root.property("agentTabIndex") == 1
        assert root.findChild(QObject, "agentScroll") is not None
    finally:
        harness.close()


def test_chat_page_streams_the_reply_all_the_way_into_qml(qt_app, tmp_path):
    """从桥到界面：用户那句、逐段推的回复、最后全文，都要在 QML 里看得见。"""
    harness = ChatHarness(tmp_path)
    try:
        root = harness.root
        assert root is not None
        root.openAgentTab(0)
        # 走界面的真实入口（等同人打完字点「发送」），不是直接调桥
        root.sendChat("把中秋海报的提示词改得更国潮一点")

        # 自己说的那句立刻上屏（不等回复），输入框同时被清空
        assert root.property("agentChatMessages") == 2
        assert root.property("agentChatRows") == 2, "QML 没收到 messageAppended"
        assert root.property("agentChatStreaming") is True
        assert root.property("agentChatDraft") == "", "发出去之后输入框没清空"

        assert wait_until(qt_app, lambda: not harness.assistant.streaming, timeout=20)

        assert root.property("agentChatStreaming") is False
        assert root.property("agentChatChunks") >= 3, "只推了一两段，看不出是流式"
        full = root.property("agentChatLastReply")
        assert full
        rows = harness.assistant.property("messages")
        assert rows[1]["status"] == MSG_OK
        assert rows[1]["text"] == full
        assert harness.errors() == []
    finally:
        harness.close()


def test_chat_page_stops_at_the_partial_text_when_aborted(qt_app, tmp_path):
    """界面上点「打断」的效果：最后那条助手消息停在半句话上，状态是已打断、不再往下吐。"""
    full_text = "第一句，慢慢说。第二句，还要继续。第三句，别急。第四句，这才收尾。"
    # 这条用真实节奏（20–50ms 一段）跑：要的正是「说到一半被打断」那个瞬间
    harness = ChatHarness(
        tmp_path, client=ProtocolClient(reply_factory=lambda _text: full_text), pace_scale=1.0
    )
    try:
        root = harness.root
        assert root is not None
        root.openAgentTab(0)
        harness.assistant.send("说一段长的")

        assert wait_until(qt_app, lambda: harness.assistant.streamedChunks >= 1, timeout=20)
        harness.assistant.cancel()

        assert root.property("agentChatStreaming") is False
        rows = harness.assistant.property("messages")
        assert rows[1]["status"] == MSG_ABORTED
        assert len(rows[1]["text"]) < len(full_text), "打断没有停在用户说停的地方"
        assert root.property("agentChatLastReply") == rows[1]["text"]
    finally:
        harness.close()


def test_chat_page_falls_back_to_the_pipeline_without_a_bridge(qt_app, tmp_path):
    """没有注入桥时（老夹具、截图工具）：不报错，助手页落在「跑流程」。

    这条是给既有 463 项测试兜底的：新页面不许让它们加载出一屏报错或一张空板。
    """
    harness = ChatHarness(tmp_path, with_chat_bridge=False)
    try:
        root = harness.root
        assert root is not None
        assert harness.errors() == [], f"没注入桥就报错了：{harness.errors()}"
        assert root.property("pageCount") == 6
        assert root.property("agentTabIndex") == 1
        assert root.property("agentChatMessages") == 0
        assert root.property("agentChatRows") == 0
        # 跑流程那一块还在（截图工具靠 agentScroll 滚到长期档案）
        assert root.findChild(QObject, "agentScroll") is not None
    finally:
        harness.close()