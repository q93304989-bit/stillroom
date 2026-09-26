import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

/* 画廊单元格：一张图 + 底部两条信息。

   这个委托在滚动时会被反复创建/销毁，所以刻意压低了图元数量：
   没有 hover 动效、没有多余的描边、状态与时间合并成一行文字。
   （Phase 4 基准显示：委托越重，滚动帧时间越差，而这不是视觉上的必要开销。） */
Rectangle {
    id: cell

    property string recordId: ""
    property string kind: "image"
    property string status: "success"
    property string prompt: ""
    property string timeText: ""
    property string badge: ""
    property string thumb: ""
    property real ratio: 1.0          // 宽高比，用于让瀑布流高低错落
    property bool selected: false

    signal activated()

    radius: theme.radiusCard
    color: selected ? theme.selected : theme.card
    border.width: 1
    border.color: selected ? theme.accent : theme.cardBorder

    TapHandler {
        onTapped: cell.activated()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 6

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: Math.max(90, (cell.width - 16) / Math.max(0.5, cell.ratio))
            radius: 8
            color: theme.bg
            clip: true

            Image {
                anchors.fill: parent
                source: cell.thumb
                sourceSize.width: 260
                sourceSize.height: 260
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                cache: true
                visible: cell.thumb !== ""
            }

            Text {
                anchors.centerIn: parent
                visible: cell.thumb === ""
                text: cell.kind === "video" ? "▶ 视频" : "生成中…"
                color: theme.textDisabled
                font.pixelSize: 12
            }

            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.margins: 6
                height: 20
                width: badgeText.implicitWidth + 14
                radius: theme.radiusPill
                color: cell.kind === "video" ? theme.videoBg : theme.accentSoft
                opacity: 0.94
                Text {
                    id: badgeText
                    anchors.centerIn: parent
                    text: cell.badge
                    color: cell.kind === "video" ? theme.videoText : theme.textMain
                    font.pixelSize: 11
                }
            }
        }

        Text {
            Layout.fillWidth: true
            text: cell.prompt
            color: theme.textMain
            font.pixelSize: 12
            elide: Text.ElideRight
            maximumLineCount: 1
        }

        Item {
            Layout.fillWidth: true
            implicitHeight: 16
            Text {
                anchors.left: parent.left
                text: cell.timeText
                color: theme.textSub
                font.pixelSize: 11
            }
            Text {
                anchors.right: parent.right
                text: cell.status === "success" ? "成功" : "失败"
                color: cell.status === "success" ? theme.success : theme.dangerText
                font.pixelSize: 11
            }
        }
    }
}
