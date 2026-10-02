import QtQuick

/* 对话里的一条消息。形制抄 Codex：

   · 助手的话**直接平铺在页面上**（不套气泡）—— 一段长回复套个框，读起来像在填表格；
   · 用户的话装在一只浅色圆角块里，一眼分得清「谁说的」；
   · 正在流式的那条在末尾带一个光标块：它还在往外说，这件事必须看得见。

   两种角色都由 theme 取色，所以换主题不用动这里。 */
Item {
    id: message

    property string messageId: ""
    property string role: "assistant"
    property string text: ""
    property string status: "ok"          // ok / streaming / aborted / failed
    property bool externallyHovered: false

    signal copyRequested(string value)
    signal editRequested(string value)
    signal regenerateRequested(string messageId)
    signal hoverStateChanged(bool hovered)

    readonly property bool fromUser: role === "user"
    readonly property bool streaming: status === "streaming"
    readonly property bool waiting: streaming && text === ""      // 还没吐第一个字
    readonly property string cursor: streaming && text !== "" ? "▌" : ""
    readonly property string hint: status === "aborted" ? "已打断"
                                 : status === "failed" ? "这条没成"
                                 : ""
    readonly property bool showActions: hover.hovered || externallyHovered
                                        || copyButton.activeFocus
                                        || editButton.activeFocus
                                        || regenerateButton.activeFocus
    readonly property bool actionsVisible: actionRow.opacity > 0

    implicitHeight: column.height + actionRow.height + 4

    HoverHandler {
        id: hover
        onHoveredChanged: message.hoverStateChanged(hovered)
    }

    Column {
        id: column
        width: message.width
        spacing: 6

        /* 用户：圆角块。用 card 而不是 fill —— 这一页是平铺在页面底色上的，
           而 fill 在浅色主题里与页面底色是同一个值，块会整个消失。 */
        Rectangle {
            visible: message.fromUser
            width: parent.width
            height: userText.implicitHeight + 20
            radius: 12
            color: theme.card
            border.width: 1
            border.color: theme.cardBorder

            Text {
                id: userText
                anchors.fill: parent
                anchors.margins: 10
                text: message.text
                color: theme.textMain
                font.pixelSize: 13
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
            }
        }

        /* 助手：平铺，行距放宽一点（长段落靠这个才读得下去） */
        Text {
            visible: !message.fromUser
            width: parent.width
            text: message.waiting ? "正在生成…" : message.text + message.cursor
            color: message.waiting ? theme.textDisabled : theme.textMain
            font.pixelSize: 14
            lineHeight: 1.45
            lineHeightMode: Text.ProportionalHeight
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }

        Text {
            width: parent.width
            visible: message.hint !== ""
            text: message.hint
            color: theme.textDisabled
            font.pixelSize: 11
        }
    }

    Row {
        id: actionRow
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 30
        spacing: 2
        opacity: message.showActions ? 1 : 0
        visible: opacity > 0

        Behavior on opacity {
            NumberAnimation { duration: 100 }
        }

        MessageActionButton {
            id: editButton
            objectName: "edit-" + message.messageId
            visible: message.fromUser
            iconGlyph: "✎"
            actionName: "编辑"
            onClicked: message.editRequested(message.text)
        }

        MessageActionButton {
            id: copyButton
            objectName: "copy-" + message.messageId
            iconGlyph: "⧉"
            actionName: "复制"
            onClicked: message.copyRequested(message.text)
        }

        MessageActionButton {
            id: regenerateButton
            objectName: "regenerate-" + message.messageId
            visible: !message.fromUser && !message.streaming
            iconGlyph: "↻"
            actionName: "重新生成"
            onClicked: message.regenerateRequested(message.messageId)
        }
    }
}
