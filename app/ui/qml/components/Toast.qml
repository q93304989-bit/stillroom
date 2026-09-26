import QtQuick
import QtQuick.Controls

/* 轻提示：出现 3 秒自动消失。接收 backend.noticeRaised。 */
Rectangle {
    id: toast

    property string level: "info"
    property string text: ""

    function show(newLevel, newText) {
        level = newLevel;
        text = newText;
        visible = true;
        hideTimer.restart();
    }

    width: Math.min(parent ? parent.width - 48 : 400, label.implicitWidth + 36)
    height: Math.max(40, label.implicitHeight + 20)
    radius: theme.radiusControl
    color: level === "error" ? theme.dangerBg
         : level === "warn" ? theme.selected
         : theme.card
    border.width: 1
    border.color: level === "error" ? theme.danger
                : level === "warn" ? theme.warning
                : theme.cardBorder
    visible: false
    z: 100

    Text {
        id: label
        anchors.centerIn: parent
        width: parent.width - 32
        text: toast.text
        color: toast.level === "error" ? theme.dangerText
             : toast.level === "warn" ? theme.warning
             : theme.textMain
        font.pixelSize: 13
        wrapMode: Text.Wrap
        horizontalAlignment: Text.AlignHCenter
    }

    Timer {
        id: hideTimer
        interval: 3000
        onTriggered: toast.visible = false
    }
}
