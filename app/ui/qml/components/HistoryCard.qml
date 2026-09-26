import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

/* 历史列表的一行：缩略图 + 提示词 + 元信息 + 操作按钮。
   点整行进入大图查看，「载入参数」把当时的参数回填到生成页。 */
Rectangle {
    id: card

    property string recordId: ""
    property string kind: "image"
    property string status: "success"
    property string prompt: ""
    property string timeText: ""
    property string badge: ""
    property string durationText: ""
    property string thumb: ""
    property string paramsSummary: ""
    property bool selected: false
    property bool favorite: false
    property var tags: []

    signal activated()
    signal loadRequested()
    signal deleteRequested()
    signal favoriteToggled()

    implicitHeight: 76
    radius: theme.radiusControl
    color: selected ? theme.selected : (hover.hovered ? theme.fillHover : theme.fill)
    border.width: 1
    border.color: selected ? theme.accent : theme.cardBorder

    HoverHandler {
        id: hover
    }

    TapHandler {
        onTapped: card.activated()
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: 10
        spacing: 12

        // 缩略图（没有就是占位块；后台补好后自动出现）
        Rectangle {
            Layout.preferredWidth: 56
            Layout.preferredHeight: 56
            radius: 8
            color: theme.bg
            border.width: 1
            border.color: theme.cardBorder

            Image {
                anchors.fill: parent
                anchors.margins: 1
                source: card.thumb
                sourceSize.width: 112
                sourceSize.height: 112
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                cache: true
                visible: card.thumb !== ""
            }

            Text {
                anchors.centerIn: parent
                visible: card.thumb === ""
                text: card.kind === "video" ? "▶" : "…"
                color: theme.textDisabled
                font.pixelSize: 16
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 4

            Text {
                Layout.fillWidth: true
                text: card.prompt
                color: theme.textMain
                font.pixelSize: 13
                elide: Text.ElideRight
                maximumLineCount: 1
            }

            RowLayout {
                spacing: 8
                Rectangle {
                    height: 20
                    width: badgeText.implicitWidth + 16
                    radius: theme.radiusPill
                    color: card.kind === "video" ? theme.videoBg : theme.accentSoft
                    Text {
                        id: badgeText
                        anchors.centerIn: parent
                        text: card.badge
                        color: card.kind === "video" ? theme.videoText : theme.textMain
                        font.pixelSize: 11
                    }
                }
                Text {
                    text: card.status === "success" ? "成功" : (card.status === "failed" ? "失败" : card.status)
                    color: card.status === "success" ? theme.success : theme.dangerText
                    font.pixelSize: 11
                }
                Text {
                    text: card.timeText
                    color: theme.textSub
                    font.pixelSize: 11
                }
                Text {
                    visible: card.durationText !== ""
                    text: "耗时 " + card.durationText
                    color: theme.textSub
                    font.pixelSize: 11
                }
                Text {
                    Layout.fillWidth: true
                    text: card.paramsSummary
                    color: theme.textDisabled
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }
                // 标签：只在有标签时出现，避免空行占位
                Repeater {
                    model: card.tags
                    Rectangle {
                        height: 18
                        width: tagLabel.implicitWidth + 14
                        radius: theme.radiusPill
                        color: theme.accentSoft
                        Text {
                            id: tagLabel
                            anchors.centerIn: parent
                            text: String(modelData)
                            color: theme.textMain
                            font.pixelSize: 10
                        }
                    }
                }
            }
        }

        // 收藏：填实表示已收藏（这是评价信号里最强的「认可」）
        AppButton {
            text: card.favorite ? "★" : "☆"
            implicitWidth: 40
            onClicked: card.favoriteToggled()
        }
        AppButton {
            text: "载入参数"
            implicitWidth: 92
            onClicked: card.loadRequested()
        }
        AppButton {
            text: "删除"
            destructive: true
            implicitWidth: 64
            onClicked: card.deleteRequested()
        }
    }
}
