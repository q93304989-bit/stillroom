"""助手聊天页的 UI/UX 回归测试。

这些用例只依赖 adapter 接口与 QML 暴露的真实操作入口，不依赖协议层实现细节。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from app.adapters.protocol_client import STATUS_COMPLETED, ProtocolClient, Reply  # noqa: E402
from test_assistant_page import ChatHarness, wait_until  # noqa: E402


WORKSPACE = Path(__file__).resolve().parents[1]
CHAT_PANEL_QML = WORKSPACE / "app" / "ui" / "qml" / "components" / "ChatPanel.qml"


class RecordingAdapter:
    """最小 adapter 替身：只实现 UI 需要的三个异步接口。"""

    mock = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send_message(self, request_id, text, *, conversation_id=""):
        self.calls.append(("send", request_id, text))
        return Reply(
            conversation_id=conversation_id or "conv-test",
            request_id=request_id,
            text=f"回复：{text}",
            status=STATUS_COMPLETED,
        )

    async def regenerate_reply(self, request_id, source_request_id, *, conversation_id=""):
        self.calls.append(("regenerate", request_id, source_request_id))
        return Reply(
            conversation_id=conversation_id or "conv-test",
            request_id=request_id,
            text="重新生成的回复",
            status=STATUS_COMPLETED,
        )

    async def abort_reply(self, request_id):
        self.calls.append(("abort", request_id, ""))

    async def aclose(self):
        self.calls.append(("close", "", ""))


def test_chat_list_keeps_delegates_cached_and_lightweight():
    source = CHAT_PANEL_QML.read_text(encoding="utf-8")

    assert re.search(r"ListView\s*\{", source)
    assert re.search(r"cacheBuffer\s*:", source)
    assert re.search(r"reuseItems\s*:\s*true", source)
    assert re.search(r"delegate\s*:", source)


def test_streaming_follows_bottom_until_the_user_scrolls_away(qt_app, tmp_path):
    full_text = "这是一段足够长的回复，需要多行显示以测试滚动跟随。" * 200
    harness = ChatHarness(
        tmp_path,
        client=ProtocolClient(reply_factory=lambda _text: full_text),
        pace_scale=0.25,
    )
    try:
        root = harness.root
        root.openPage(0)
        root.openAgentTab(0)
        root.sendChat("开始生成一段长回复")

        assert wait_until(qt_app, lambda: root.property("agentChatStreaming") is True)
        assert root.property("agentChatAtBottom") is True
        assert root.property("agentChatAutoFollow") is True

        assert wait_until(qt_app, lambda: root.property("agentChatScrollY") > 128)
        root.setChatScrollPosition(0)
        assert root.property("agentChatAtBottom") is False
        assert root.property("agentChatAutoFollow") is False
        assert root.property("agentChatBackToBottomVisible") is True

        paused_y = root.property("agentChatScrollY")
        assert wait_until(qt_app, lambda: root.property("agentChatChunks") >= 4)
        assert abs(root.property("agentChatScrollY") - paused_y) <= 1.0

        root.scrollChatToBottom()
        assert root.property("agentChatAtBottom") is True
        assert root.property("agentChatAutoFollow") is True
        assert root.property("agentChatBackToBottomVisible") is False
        assert harness.errors() == []
    finally:
        harness.close()


def test_composer_adapts_height_and_exposes_keyboard_contract(qt_app, tmp_path):
    harness = ChatHarness(tmp_path)
    try:
        root = harness.root
        root.openPage(0)
        root.openAgentTab(0)
        root.setChatDraft("第一行\n" + "很长的多行输入" * 80)
        root.focusChatComposer()

        assert wait_until(qt_app, lambda: root.property("agentChatComposerFocused") is True)
        assert root.property("agentChatFocusRingVisible") is True
        assert root.property("agentChatInputHeight") <= root.property("agentChatMaxInputHeight")
        assert root.property("agentChatMaxInputHeight") <= root.property("height") * 0.40 + 1.0
        assert root.property("agentChatKeyHint") == "Enter 换行 · Ctrl+Enter 发送"
        assert root.property("agentChatComposerAcceptsTabFocus") is True
        assert root.property("agentChatSendAcceptsTabFocus") is True
        assert root.property("agentChatJumpAcceptsTabFocus") is True

        draft = root.property("agentChatDraft")
        assert root.handleChatComposerKey(Qt.Key_Return, Qt.NoModifier) is False
        assert root.property("agentChatDraft") == draft

        assert root.handleChatComposerKey(Qt.Key_Return, Qt.ControlModifier) is True
        assert root.property("agentChatDraft") == ""
        assert harness.errors() == []
    finally:
        harness.close()


def test_hover_actions_copy_edit_and_regenerate_through_adapter(qt_app, tmp_path):
    adapter = RecordingAdapter()
    harness = ChatHarness(tmp_path, client=adapter, pace_scale=0.01)
    try:
        root = harness.root
        root.openPage(0)
        root.openAgentTab(0)
        root.sendChat("原始问题")

        assert wait_until(qt_app, lambda: not harness.assistant.streaming)
        rows = harness.assistant.property("messages")
        user_id = rows[0]["id"]
        assistant_id = rows[1]["id"]

        root.setChatMessageHovered(assistant_id, True)
        assert root.property("agentChatHoveredMessageId") == assistant_id
        assert root.property("agentChatActionsVisible") is True

        root.chatMessageAction("copy", assistant_id)
        assert QGuiApplication.clipboard().text() == rows[1]["text"]

        root.chatMessageAction("edit", user_id)
        assert root.property("agentChatDraft") == "原始问题"

        root.chatMessageAction("regenerate", assistant_id)
        assert wait_until(qt_app, lambda: root.property("agentChatMessages") == 3)
        assert wait_until(qt_app, lambda: not harness.assistant.streaming)
        assert root.property("agentChatLastReply") == "重新生成的回复"
        assert adapter.calls[-1][0] == "regenerate"
        assert harness.errors() == []
        snapshot = tmp_path / "chat-ux.png"
        assert root.grabWindow().save(str(snapshot))
        assert snapshot.stat().st_size > 0
    finally:
        harness.close()
