import QtQuick
import QtQuick.Controls

/* 单行输入框 */
TextField {
    id: control

    implicitHeight: theme.controlHeight
    font.pixelSize: 13
    color: theme.textMain
    placeholderTextColor: theme.textDisabled
    selectionColor: theme.accent
    selectedTextColor: theme.onAccent

    background: Rectangle {
        radius: theme.radiusControl
        color: theme.field
        border.width: 1
        border.color: control.activeFocus ? theme.accent : theme.fieldBorder
    }
}
