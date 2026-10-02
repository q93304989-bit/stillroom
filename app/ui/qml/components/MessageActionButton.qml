import QtQuick
import QtQuick.Controls

/* 消息悬停操作：图标优先、尺寸固定，避免按钮出现时把正文挤得跳动。 */
ToolButton {
    id: control

    property string iconGlyph: ""
    property string actionName: ""

    implicitWidth: 30
    implicitHeight: 30
    padding: 0
    hoverEnabled: true
    activeFocusOnTab: true

    Accessible.name: actionName
    Accessible.role: Accessible.Button
    ToolTip.visible: hovered || activeFocus
    ToolTip.text: actionName

    contentItem: Text {
        text: control.iconGlyph
        color: control.enabled ? theme.textSub : theme.textDisabled
        font.pixelSize: 16
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }

    background: Rectangle {
        radius: 8
        color: control.down ? theme.fillHover
             : control.hovered || control.activeFocus ? theme.card
             : "transparent"
        border.width: control.activeFocus ? 1 : 0
        border.color: theme.accent
    }
}
