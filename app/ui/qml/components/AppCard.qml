import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

/* 带标题的卡片。子元素直接写在卡片里即可（进入 body）。 */
Rectangle {
    id: card

    property string title: ""
    default property alias content: body.data

    Layout.fillWidth: true          // 放在 ColumnLayout 里时自动铺满
    implicitHeight: layout.implicitHeight + theme.cardPadding * 2
    radius: theme.radiusCard
    color: theme.card
    border.width: 1
    border.color: theme.cardBorder

    Column {
        id: layout
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: theme.cardPadding
        spacing: 10

        Text {
            width: parent.width
            visible: card.title !== ""
            text: card.title
            color: theme.textMain
            font.pixelSize: 14
            font.bold: true
        }

        Column {
            id: body
            width: parent.width
            spacing: 8
        }
    }
}
