import QtQuick
import QtQuick.Controls

/* 多行提示词输入框 */
TextArea {
    id: control

    implicitHeight: Math.max(96, contentHeight + 24)
    font.pixelSize: 13
    color: theme.textMain
    placeholderTextColor: theme.textDisabled
    selectionColor: theme.accent
    selectedTextColor: theme.onAccent
    wrapMode: TextArea.Wrap
    topPadding: 10
    bottomPadding: 10
    leftPadding: 12
    rightPadding: 12

    background: Rectangle {
        radius: theme.radiusControl
        color: theme.field
        border.width: 1
        border.color: control.activeFocus ? theme.accent : theme.fieldBorder
    }
}
