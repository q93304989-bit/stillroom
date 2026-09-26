import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

/* 大图 / 视频查看器：图片可滚轮缩放、拖动平移，视频走系统播放器或内置播放。
   用 Popup 实现，因此无论从哪个页面打开，都覆盖整个窗口。 */
Popup {
    id: box

    property var record: ({})
    property var records: []
    property int index: -1
    property bool isVideo: record && record.kind === "video"

    /* 只读探针：给界面测试读（理由同其它页面：findChild 会被包装缓存坑到）。 */
    readonly property bool saveAsAvailable: !!(record && (record.media_path || record.result_url))
    /* 详情弹层是否开着、里面有多少内容（测试验证「任务ID/失败原因」真的呈现了） */
    readonly property bool detailOpen: detailDialog.opened
    readonly property int detailLength: detailArea.text.length

    /* 打开详情（按钮与测试共用同一条代码路径，测试才不会绕过真实逻辑）。 */
    function openDetail() {
        if (!record || !record.id)
            return;
        detailArea.text = backend.recordDetail(record.id);
        detailDialog.open();
    }

    /* 另存为：把这条历史记录存到用户选的位置（本地缓存优先，没有才下载）。
       旧版历史区一直有「保存」，迁移时与结果区一起漏了。 */
    FileDialog {
        id: savePicker
        title: "另存历史记录"
        fileMode: FileDialog.SaveFile
        defaultSuffix: box.isVideo ? "mp4" : "png"
        nameFilters: box.isVideo
                     ? ["MP4 视频 (*.mp4)", "所有文件 (*)"]
                     : ["PNG 图片 (*.png)", "JPEG 图片 (*.jpg *.jpeg)", "所有文件 (*)"]
        onAccepted: backend.saveAs(selectedFile.toString(),
                                   box.record.result_url, box.record.media_path)
    }

    property string mediaSource: {
        if (!record)
            return "";
        if (isVideo)
            return record.result_url || record.media_path || "";
        return record.media_path ? "file:///" + String(record.media_path).replace(/\\/g, "/")
                                 : (record.result_url || "");
    }

    signal loadRequested(string recordId)
    signal stepRequested(int delta)
    signal deleteRequested(string recordId)
    signal favoriteToggled(bool value)
    signal tagAdded(string tag)
    signal tagsReplaced(var tags)
    signal retryRequested()

    modal: true
    padding: 0
    closePolicy: Popup.CloseOnEscape
    width: parent ? parent.width : 900
    height: parent ? parent.height : 640

    background: Rectangle {
        color: "#0E0E10"          // 查看器始终用深色 HUD，与旧版一致
    }

    // ---------------- 顶部信息条 ----------------
    Rectangle {
        id: topBar
        anchors.top: parent.top
        width: parent.width
        height: 48
        color: "#1D1D1F"

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            spacing: 10

            Text {
                text: box.isVideo ? "视频" : "图片"
                color: "#FFFFFF"
                font.pixelSize: 13
                font.bold: true
            }
            Text {
                Layout.fillWidth: true
                text: box.record ? (box.record.prompt || "") : ""
                color: "#D8D8DC"
                font.pixelSize: 12
                elide: Text.ElideRight
            }
            Text {
                visible: box.index >= 0
                text: (box.index + 1) + " / " + box.records.length
                color: "#8E8E93"
                font.pixelSize: 12
            }
            AppButton {
                text: "‹"
                implicitWidth: 36
                onClicked: box.stepRequested(-1)
            }
            AppButton {
                text: "›"
                implicitWidth: 36
                onClicked: box.stepRequested(1)
            }
            AppButton {
                text: "关闭"
                onClicked: box.close()
            }
        }
    }

    // ---------------- 内容区 ----------------
    Item {
        id: stage
        anchors.top: topBar.bottom
        anchors.bottom: bottomBar.top
        anchors.left: parent.left
        anchors.right: parent.right
        clip: true

        // 图片：滚轮缩放 + 拖动平移
        Flickable {
            id: viewer
            anchors.fill: parent
            visible: !box.isVideo
            contentWidth: Math.max(width, picture.width * picture.scale)
            contentHeight: Math.max(height, picture.height * picture.scale)
            boundsBehavior: Flickable.StopAtBounds

            Image {
                id: picture
                width: viewer.width
                height: viewer.height
                source: box.isVideo ? "" : box.mediaSource
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                cache: false
                scale: 1.0

                onStatusChanged: {
                    if (status === Image.Ready) {
                        picture.scale = 1.0;
                        viewer.contentX = 0;
                        viewer.contentY = 0;
                    }
                }
            }

            PinchHandler {
                id: pinch
                target: null
                onActiveChanged: if (active) picture.scale = Math.max(0.2, Math.min(8, picture.scale * pinch.scale))
            }

            WheelHandler {
                onWheel: function (event) {
                    var factor = event.angleDelta.y > 0 ? 1.12 : 1 / 1.12;
                    picture.scale = Math.max(0.2, Math.min(8, picture.scale * factor));
                }
            }
        }

        // 视频：内置播放（缺 QtMultimedia 时只有提示）
        Loader {
            id: videoLoader
            anchors.fill: parent
            visible: box.isVideo
            active: box.isVideo
            source: box.isVideo ? "VideoPlayer.qml" : ""
            onLoaded: item.source = box.mediaSource
        }

        Text {
            anchors.centerIn: parent
            visible: box.isVideo && !videoLoader.item
            text: "内置播放不可用，请用下方「用播放器打开」"
            color: "#8E8E93"
            font.pixelSize: 13
        }
    }

    // ---------------- 底部信息条 ----------------
    Rectangle {
        id: bottomBar
        anchors.bottom: parent.bottom
        width: parent.width
        height: 126
        color: "#1D1D1F"

        ColumnLayout {
            anchors.fill: parent
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            anchors.topMargin: 8
            anchors.bottomMargin: 8
            spacing: 6

            Text {
                Layout.fillWidth: true
                text: box.record ? box.record.paramsSummary || "" : ""
                color: "#8E8E93"
                font.pixelSize: 12
                elide: Text.ElideRight
            }

            // 标签行：输入 + 已有标签（点 ✕ 删除）
            RowLayout {
                Layout.fillWidth: true
                spacing: 6

                Text {
                    text: "标签"
                    color: "#8E8E93"
                    font.pixelSize: 12
                }
                AppField {
                    id: tagField
                    Layout.preferredWidth: 170
                    placeholderText: "输入标签后回车"
                    onAccepted: {
                        if (text.trim() !== "") {
                            box.tagAdded(text.trim());
                            text = "";
                        }
                    }
                }
                AppButton {
                    text: "添加"
                    implicitWidth: 60
                    enabled: tagField.text.trim() !== ""
                    onClicked: {
                        box.tagAdded(tagField.text.trim());
                        tagField.text = "";
                    }
                }
                Repeater {
                    model: box.record && box.record.tags ? box.record.tags : []
                    Rectangle {
                        height: 22
                        width: tagChip.implicitWidth + 34
                        radius: theme.radiusPill
                        color: theme.accentSoft
                        Text {
                            id: tagChip
                            anchors.left: parent.left
                            anchors.leftMargin: 10
                            anchors.verticalCenter: parent.verticalCenter
                            text: String(modelData)
                            color: theme.textMain
                            font.pixelSize: 11
                        }
                        Text {
                            anchors.right: parent.right
                            anchors.rightMargin: 8
                            anchors.verticalCenter: parent.verticalCenter
                            text: "✕"
                            color: theme.textSub
                            font.pixelSize: 11
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    var kept = [];
                                    var all = box.record.tags || [];
                                    for (var i = 0; i < all.length; ++i)
                                        if (String(all[i]) !== String(modelData))
                                            kept.push(all[i]);
                                    box.tagsReplaced(kept);
                                }
                            }
                        }
                    }
                }
                Item { Layout.fillWidth: true }
            }

            RowLayout {
                spacing: 8
                AppButton {
                    text: box.record && box.record.favorite ? "★ 已收藏" : "☆ 收藏"
                    onClicked: box.favoriteToggled(!(box.record && box.record.favorite))
                }
                AppButton {
                    text: "重试"
                    onClicked: box.retryRequested()
                }
                AppButton {
                    text: "载入参数"
                    onClicked: box.loadRequested(box.record.id)
                }
                AppButton {
                    /* 详情：主界面刻意不铺全文，这里是排查问题的出口
                       （任务ID / 失败原因 / 参考图完整清单都在里面） */
                    text: "详情"
                    enabled: !!(box.record && box.record.id)
                    onClicked: box.openDetail()
                }
                AppButton {
                    text: "复制地址"
                    enabled: !!(box.record && box.record.result_url)
                    onClicked: backend.copyText(box.record.result_url)
                }
                AppButton {
                    text: "在浏览器打开"
                    enabled: !!(box.record && box.record.result_url)
                    onClicked: backend.openUrl(box.record.result_url)
                }
                AppButton {
                    /* 与结果区同一个动作：本地缓存优先，没有才下载 */
                    text: "另存为"
                    enabled: !!(box.record && (box.record.media_path || box.record.result_url))
                    onClicked: {
                        savePicker.currentFile = "stillroom_"
                            + String(box.record.id || "result") + (box.isVideo ? ".mp4" : ".png");
                        savePicker.open();
                    }
                }
                AppButton {
                    text: "用播放器打开"
                    visible: box.isVideo
                    enabled: !!(box.record && (box.record.media_path || box.record.result_url))
                    onClicked: backend.openRecordFile(box.record.id)
                }
                AppButton {
                    text: "打开本地文件"
                    visible: !box.isVideo
                    enabled: !!(box.record && box.record.media_path)
                    onClicked: backend.openRecordFile(box.record.id)
                }
                Item { Layout.fillWidth: true }
                AppButton {
                    text: "删除"
                    destructive: true
                    onClicked: box.deleteRequested(box.record.id)
                }
            }
        }
    }

    Keys.onPressed: function (event) {
        if (event.key === Qt.Key_Left)
            box.stepRequested(-1);
        else if (event.key === Qt.Key_Right)
            box.stepRequested(1);
        else if (event.key === Qt.Key_Escape)
            box.close();
    }

    /* 详情弹层：等宽展示，可整段复制（旧版那个「详情」弹层，含任务ID与失败原因）。 */
    Dialog {
        id: detailDialog
        title: "记录详情"
        modal: true
        anchors.centerIn: parent
        width: 640
        height: 460
        standardButtons: Dialog.Close

        ScrollView {
            anchors.fill: parent
            contentWidth: availableWidth

            TextEdit {
                id: detailArea
                width: parent.width
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                color: theme.textMain
                font.family: "Consolas"
                font.pixelSize: 12
                textFormat: TextEdit.PlainText
            }
        }

        footer: RowLayout {
            spacing: 8
            Item { Layout.fillWidth: true }
            AppButton {
                text: "复制全文"
                onClicked: backend.copyText(detailArea.text)
            }
            AppButton {
                text: "关闭"
                onClicked: detailDialog.close()
            }
        }
    }
}
