import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

/* 助手页的「对话」面板：伪流式展示层。

   数据只从注入的 AssistantBridge 进来；QML 不造假数据，也不接触协议层。
   滚动遵循「贴底跟随、用户离开即停、点按钮回底」，输入条的 Enter/Ctrl+Enter
   语义、焦点反馈与消息操作都收口在这里。 */
Item {
    id: panel
    objectName: "chatPanel"

    readonly property int contentWidth: Math.min(width, 840)
    property var bridge: null

    readonly property bool ready: bridge !== null
    readonly property bool streaming: ready && bridge.streaming
    readonly property bool canSend: !streaming && input.text.trim() !== ""
    readonly property int messageCount: chatModel.count
    readonly property string draft: input.text

    readonly property bool atBottom: list.atBottom
    readonly property bool autoFollow: list.autoFollow
    readonly property real scrollY: list.contentY
    readonly property bool backToBottomVisible: ready && chatModel.count > 0 && !list.atBottom
    readonly property int cacheBuffer: list.cacheBuffer

    readonly property real maxInputHeight: Math.max(
        120,
        Math.round((Window.window ? Window.window.height : panel.height) * 0.40)
    )
    readonly property real inputHeight: inputScroll.height
    readonly property string keyHint: "Enter 换行 · Ctrl+Enter 发送"
    readonly property bool composerFocused: input.activeFocus
    readonly property bool focusRingVisible: composerFocusRing.visible
    readonly property bool composerAcceptsTabFocus: input.activeFocusOnTab
    readonly property bool sendAcceptsTabFocus: sendButton.activeFocusOnTab
    readonly property bool jumpAcceptsTabFocus: jumpButton.activeFocusOnTab
    readonly property string hoveredMessageId: {
        for (var i = 0; i < chatModel.count; ++i) {
            if (chatModel.get(i).msgHovered)
                return chatModel.get(i).msgId;
        }
        return "";
    }
    readonly property bool actionsVisible: hoveredMessageId !== ""

    function rowOf(messageId) {
        for (var i = 0; i < chatModel.count; ++i) {
            if (chatModel.get(i).msgId === messageId)
                return i;
        }
        return -1;
    }

    function send(text) {
        if (text !== undefined)
            input.text = text;
        if (!ready || streaming || input.text.trim() === "")
            return;
        bridge.send(input.text);
        input.text = "";
        input.forceActiveFocus();
        list.resumeFollowing(true);
    }

    function setDraft(text) {
        input.text = text === undefined ? "" : text;
    }

    function focusComposer() {
        input.forceActiveFocus();
    }

    function handleComposerKey(key, modifiers) {
        var enter = key === Qt.Key_Return || key === Qt.Key_Enter;
        if (enter && (modifiers & Qt.ControlModifier)) {
            panel.send();
            return true;
        }
        return false;
    }

    function setHovered(messageId, hovered) {
        var row = rowOf(messageId);
        if (row >= 0)
            chatModel.setProperty(row, "msgHovered", hovered === true);
    }

    function setScrollPosition(value) {
        list.setScrollPosition(value);
    }

    function scrollChatToBottom() {
        list.resumeFollowing(true);
    }

    function messageAction(kind, messageId) {
        var row = rowOf(messageId);
        if (row < 0)
            return;
        var item = chatModel.get(row);
        if (kind === "copy") {
            clipboardWriter.write(item.msgText);
        } else if (kind === "edit" && item.msgRole === "user") {
            input.text = item.msgText;
            input.forceActiveFocus();
        } else if (kind === "regenerate" && item.msgRole === "assistant" && ready) {
            bridge.regenerate(messageId);
        }
    }

    ListModel { id: chatModel }

    TextEdit {
        id: clipboardWriter
        visible: false
        width: 1
        height: 1
        textFormat: TextEdit.PlainText

        function write(value) {
            text = value;
            selectAll();
            copy();
            deselect();
        }
    }

    Connections {
        target: panel.bridge
        enabled: panel.ready

        function onMessageAppended(id, role, text, status) {
            chatModel.append({
                "msgId": id,
                "msgRole": role,
                "msgText": text,
                "msgStatus": status,
                "msgHovered": false
            });
            list.follow();
        }
        function onMessageProgressed(id, text) {
            var row = panel.rowOf(id);
            if (row < 0)
                return;
            chatModel.setProperty(row, "msgText", text);
            list.follow();
        }
        function onMessageSettled(id, status, text) {
            var row = panel.rowOf(id);
            if (row < 0)
                return;
            chatModel.setProperty(row, "msgText", text);
            chatModel.setProperty(row, "msgStatus", status);
            list.follow();
        }
        function onConversationCleared() {
            chatModel.clear();
            list.resumeFollowing(true);
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 6

        RowLayout {
            Layout.fillWidth: true
            Layout.maximumWidth: panel.contentWidth
            Layout.alignment: Qt.AlignHCenter
            spacing: 10

            Text {
                text: "对话"
                color: theme.textMain
                font.pixelSize: 14
                font.bold: true
            }
            Text {
                text: panel.ready ? panel.bridge.sourceLabel : ""
                color: theme.textDisabled
                font.pixelSize: 11
            }
            Item { Layout.fillWidth: true }
            AppButton {
                objectName: "chatClearButton"
                text: "清空"
                enabled: panel.ready && panel.bridge.messageCount > 0
                KeyNavigation.tab: jumpButton.visible ? jumpButton : input
                onClicked: panel.bridge.clear()
            }
        }

        Text {
            Layout.fillWidth: true
            Layout.maximumWidth: panel.contentWidth
            Layout.alignment: Qt.AlignHCenter
            text: "协议层不做真流式：整段生成、过完质量校验再交过来；"
                  + "「一段段显示」是前端按语义边界切好、每段停 20–50 毫秒播出来的。"
            color: theme.textDisabled
            font.pixelSize: 11
            wrapMode: Text.Wrap
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            ListView {
                id: list
                anchors.fill: parent
                anchors.topMargin: 6
                anchors.bottomMargin: 6
                clip: true
                spacing: 18
                boundsBehavior: Flickable.StopAtBounds
                cacheBuffer: Math.max(320, Math.round(height))
                reuseItems: true
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                model: chatModel
                property bool autoFollow: true
                property bool programmaticScroll: false
                readonly property real bottomDistance: Math.max(
                    0, contentHeight - height - contentY
                )
                readonly property bool atBottom: bottomDistance <= 72

                function settleScrollMode() {
                    if (!programmaticScroll)
                        autoFollow = atBottom;
                }

                function follow() {
                    if (!autoFollow)
                        return;
                    programmaticScroll = true;
                    positionViewAtEnd();
                    Qt.callLater(function() { list.programmaticScroll = false; });
                }

                function resumeFollowing(force) {
                    if (!force && !autoFollow)
                        return;
                    autoFollow = true;
                    programmaticScroll = true;
                    positionViewAtEnd();
                    Qt.callLater(function() { list.programmaticScroll = false; });
                }

                function setScrollPosition(value) {
                    programmaticScroll = true;
                    var maxY = originY + Math.max(0, contentHeight - height);
                    var targetY = Math.max(originY, Math.min(value, maxY));
                    contentY = targetY;
                    autoFollow = Math.max(0, contentHeight - height - targetY) <= 72;
                    Qt.callLater(function() { list.programmaticScroll = false; });
                }

                onMovementStarted: settleScrollMode()
                onMovementEnded: settleScrollMode()
                onFlickStarted: settleScrollMode()
                onContentYChanged: {
                    if (!programmaticScroll && (dragging || flicking || moving))
                        settleScrollMode();
                }
                onContentHeightChanged: {
                    if (autoFollow)
                        Qt.callLater(function() { list.follow(); });
                }

                delegate: Item {
                    required property string msgId
                    required property string msgRole
                    required property string msgText
                    required property string msgStatus
                    required property bool msgHovered
                    required property int index

                    width: list.width
                    implicitHeight: body.implicitHeight

                    ChatMessage {
                        id: body
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: panel.contentWidth
                        messageId: msgId
                        role: msgRole
                        text: msgText
                        status: msgStatus
                        externallyHovered: msgHovered

                        onCopyRequested: function(value) {
                            clipboardWriter.write(value);
                        }
                        onEditRequested: function(value) {
                            input.text = value;
                            input.forceActiveFocus();
                        }
                        onRegenerateRequested: function(id) {
                            if (panel.ready)
                                panel.bridge.regenerate(id);
                        }
                    }

                    Connections {
                        target: body
                        function onHoverStateChanged(hovered) {
                            panel.setHovered(msgId, hovered);
                        }
                    }
                }
            }

            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.top: parent.top
                anchors.topMargin: 60
                width: panel.contentWidth - 10
                visible: chatModel.count === 0
                horizontalAlignment: Text.AlignHCenter
                text: "还没有开始。说一句话，回复会按段落一段段出现在这里。"
                color: theme.textDisabled
                font.pixelSize: 13
                wrapMode: Text.Wrap
            }

            MessageActionButton {
                id: jumpButton
                objectName: "chatBackToBottom"
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.margins: 12
                visible: panel.backToBottomVisible
                iconGlyph: "↓"
                actionName: "回到底部"
                KeyNavigation.tab: input
                onClicked: list.resumeFollowing(true)
            }
        }

        Rectangle {
            id: composer
            objectName: "chatComposer"
            Layout.fillWidth: true
            Layout.maximumWidth: panel.contentWidth
            Layout.alignment: Qt.AlignHCenter
            implicitHeight: composerBody.implicitHeight + 12
            radius: 18
            color: theme.card
            border.width: 1
            border.color: input.activeFocus ? theme.accent : theme.cardBorder

            Rectangle {
                id: composerFocusRing
                anchors.fill: parent
                anchors.margins: -3
                radius: 21
                visible: input.activeFocus
                color: "transparent"
                opacity: 0.35
                border.width: 1
                border.color: theme.accent
            }

            ColumnLayout {
                id: composerBody
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 6
                spacing: 0

                ScrollView {
                    id: inputScroll
                    Layout.fillWidth: true
                    Layout.minimumHeight: 42
                    Layout.preferredHeight: Math.max(
                        42, Math.min(input.implicitHeight, panel.maxInputHeight)
                    )
                    Layout.maximumHeight: panel.maxInputHeight
                    contentWidth: availableWidth
                    clip: true
                    ScrollBar.vertical.policy: input.contentHeight > panel.maxInputHeight
                            ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff

                    TextArea {
                        id: input
                        objectName: "chatInput"
                        width: inputScroll.availableWidth
                        implicitHeight: contentHeight + topPadding + bottomPadding
                        placeholderText: "随心输入…"
                        placeholderTextColor: theme.textDisabled
                        color: theme.textMain
                        font.pixelSize: 13
                        wrapMode: TextArea.Wrap
                        selectByMouse: true
                        persistentSelection: true
                        background: null
                        topPadding: 10
                        bottomPadding: 2
                        leftPadding: 12
                        rightPadding: 12
                        activeFocusOnTab: true
                        KeyNavigation.tab: sendButton
                        Keys.onPressed: function(event) {
                            event.accepted = panel.handleComposerKey(
                                event.key, event.modifiers
                            );
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.leftMargin: 8
                    Layout.rightMargin: 2
                    Layout.bottomMargin: 2
                    spacing: 8

                    Text {
                        id: sendHint
                        text: panel.keyHint
                        color: theme.textSub
                        font.pixelSize: 11
                    }

                    Item { Layout.fillWidth: true }

                    Text {
                        text: panel.ready ? panel.bridge.statusText : ""
                        color: panel.streaming ? theme.accent : theme.textDisabled
                        font.pixelSize: 11
                    }

                    Button {
                        id: sendButton
                        objectName: "chatSendButton"
                        implicitWidth: 30
                        implicitHeight: 30
                        padding: 0
                        hoverEnabled: true
                        enabled: panel.ready && (panel.streaming || panel.canSend)
                        activeFocusOnTab: true
                        KeyNavigation.backtab: input
                        Accessible.name: panel.streaming ? "停止生成" : "发送"
                        Accessible.role: Accessible.Button

                        onClicked: panel.streaming ? panel.bridge.cancel() : panel.send()

                        background: Rectangle {
                            radius: 15
                            color: {
                                if (panel.streaming)
                                    return sendButton.hovered ? theme.dangerBg : theme.fillHover;
                                if (!panel.canSend)
                                    return theme.fillHover;
                                return sendButton.hovered ? theme.accentHover : theme.accent;
                            }
                            border.width: sendButton.activeFocus ? 1 : 0
                            border.color: theme.accent
                        }

                        contentItem: Text {
                            text: panel.streaming ? "■" : "↑"
                            font.pixelSize: panel.streaming ? 11 : 15
                            font.bold: true
                            color: panel.streaming ? theme.dangerText
                                 : panel.canSend ? theme.onAccent
                                 : theme.textDisabled
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                }
            }
        }
    }
}
