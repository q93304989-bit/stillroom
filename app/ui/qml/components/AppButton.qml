import QtQuick
import QtQuick.Controls

/* 通用按钮：主按钮用强调色，次按钮用中性填充。
   颜色一律来自 theme，主题切换时自动跟随。 */
Button {
    id: control

    property bool primary: false
    property bool destructive: false

    implicitHeight: theme.buttonHeight
    implicitWidth: Math.max(88, contentItem.implicitWidth + 34)
    padding: 0
    font.pixelSize: 13
    font.bold: primary
    hoverEnabled: true

    background: Rectangle {
        radius: theme.radiusControl
        color: {
            if (!control.enabled)
                return theme.fill;
            if (control.primary)
                return control.hovered ? theme.accentHover : theme.accent;
            if (control.destructive)
                return control.hovered ? theme.dangerBg : theme.fill;
            return control.hovered ? theme.fillHover : theme.fill;
        }
        border.width: control.primary ? 0 : 1
        border.color: control.primary ? "transparent" : theme.cardBorder
    }

    contentItem: Text {
        text: control.text
        font: control.font
        color: {
            if (!control.enabled)
                return theme.textDisabled;
            if (control.primary)
                return theme.onAccent;
            if (control.destructive)
                return theme.dangerText;
            return theme.textMain;
        }
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
}
