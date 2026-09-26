import QtQuick
import QtQuick.Controls

/* 左侧导航项：选中态用强调浅底 + 强调文字 */
Button {
    id: control

    property bool current: false

    implicitHeight: 40
    padding: 0
    font.pixelSize: 13
    hoverEnabled: true
    flat: true

    background: Rectangle {
        radius: theme.radiusControl
        color: control.current ? theme.accentSoft
             : control.hovered ? theme.fillHover
             : "transparent"
    }

    contentItem: Text {
        leftPadding: 12
        text: control.text
        font: control.font
        color: !control.enabled ? theme.textDisabled
             : control.current ? theme.textMain
             : theme.textSub
        verticalAlignment: Text.AlignVCenter
    }
}
