import QtQuick 2.15
import QtQuick.Window 2.15

/* Phase 0 原型：500 项画廊，唯一目的是量帧时间。
 *
 * 与旧版对应的关键差异只有一条：
 *   缩略图交给 Qt 的异步图片管线（asynchronous: true + sourceSize），
 *   不阻塞 GUI 线程；旧版是在渲染循环里同步 PIL.open + LANCZOS。
 *
 * 关闭本文件的 asynchronous 即为「同步解码」对照组（--sync）。
 */
Window {
    id: root
    width: 1280
    height: 840
    visible: true
    color: "#1A1A1C"
    title: "Agnes Qt Quick 基准"

    // 由 Python 侧驱动（只翻开关，滚动/跳转都在 QML 内执行，
    // 测量循环里不夹 Python→QML 调用）
    property real scrollSpeed: 0      // px/秒；0 = 静止
    property bool jumping: false
    property int thumbW: 200
    property int thumbH: 200
    // 由 Python 在 load() 之前以上下文属性注入，保证委托创建时就是正确设置
    readonly property bool asyncLoad: typeof benchAsync !== "undefined" ? benchAsync : true
    property int frames: 0            // 供 Python 侧读取的渲染帧计数
    property var jumpStalls: []       // 每次随机跳转到出帧的毫秒数
    property real jumpPendingAt: 0

    onFrameSwapped: {
        frames++
        if (root.jumpPendingAt > 0) {
            root.jumpStalls = root.jumpStalls.concat([Date.now() - root.jumpPendingAt])
            root.jumpPendingAt = 0
        }
    }

    GridView {
        id: view
        anchors.fill: parent
        anchors.margins: 8
        cellWidth: root.thumbW + 8
        cellHeight: root.thumbH + 8
        // 屏幕外预取的像素范围：滚动时委托才不会被反复销毁重建
        cacheBuffer: root.thumbH * 6
        model: benchModel
        clip: true

        delegate: Item {
            width: view.cellWidth
            height: view.cellHeight

            Rectangle {
                anchors.fill: parent
                anchors.margins: 4
                radius: 8
                color: "#232326"

                Image {
                    anchors.fill: parent
                    anchors.margins: 1
                    asynchronous: root.asyncLoad
                    cache: true
                    fillMode: Image.PreserveAspectCrop
                    source: modelData.thumb
                    // 让解码发生在显示尺寸，而不是原始 1024x768
                    sourceSize.width: root.thumbW
                    sourceSize.height: root.thumbH
                }
            }
        }

        // 匀速滚动
        Timer {
            interval: 16
            repeat: true
            running: root.scrollSpeed !== 0
            onTriggered: {
                var next = view.contentY + root.scrollSpeed * 0.016;
                var maxY = Math.max(0, view.contentHeight - view.height);
                if (next > maxY) {
                    next = 0;            // 到底后回到顶部，继续跑
                }
                view.contentY = next;
            }
        }

        // 随机跳转：整表位移，强迫创建新委托并解码新图片
        // —— 同步解码与异步解码的差距主要在这里体现
        Timer {
            interval: 120
            repeat: true
            running: root.jumping
            onTriggered: {
                var maxY = Math.max(0, view.contentHeight - view.height);
                root.jumpPendingAt = Date.now();
                view.contentY = Math.random() * maxY;
            }
        }
    }
}
