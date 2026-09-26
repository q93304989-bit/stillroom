import QtQuick
import QtQuick.Controls

/* 下拉选择：高度与圆角跟输入框一致；弹出列表沿用全局 palette。 */
ComboBox {
    id: control

    implicitHeight: theme.controlHeight
    implicitWidth: Math.max(120, contentItem.implicitWidth + 48)
    font.pixelSize: 13

    background: Rectangle {
        radius: theme.radiusControl
        color: theme.field
        border.width: 1
        border.color: control.activeFocus ? theme.accent : theme.fieldBorder
    }

    contentItem: Text {
        leftPadding: 12
        rightPadding: 28
        text: control.displayText
        font: control.font
        color: theme.textMain
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
}
