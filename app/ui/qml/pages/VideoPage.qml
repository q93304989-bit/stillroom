import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../components"

/* 视频生成页：文生视频 / 图参考视频。
   视频接口只接受公网 http(s) URL，所以本地图要么先传图床，要么直接填 URL。 */
Item {
    id: page

    property var refs: []
    property string pendingUpload: ""

    /* 只读探针：给界面测试读（理由同其它页面：findChild 会被包装缓存坑到）。 */
    readonly property bool uploadConfirmOpen: uploadConfirmDialog.opened
    readonly property int refCount: page.refs.length

    readonly property var models: [
        "agnes-video-2.5-flash", "agnes-video-2.5", "agnes-video-v2.0"
    ]
    readonly property var aspectRatios: ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
    readonly property var secondsOptions: ["4", "5", "8", "10", "12"]

    FileDialog {
        id: uploadPicker
        title: "选择要上传到图床的本地图片"
        fileMode: FileDialog.OpenFile
        nameFilters: ["图片 (*.png *.jpg *.jpeg *.webp *.bmp)", "所有文件 (*)"]
        onAccepted: {
            page.pendingUpload = selectedFile.toString();
            backend.uploadReference(page.pendingUpload);
        }
    }

    /* 上传到公网图床不可回滚，审批闸门会拦一道——这里给出确认出口。
       以前没有这个出口，用户点了「上传本地图」只闪过一句提示，看起来就是「点了没反应」。 */
    Dialog {
        id: uploadConfirmDialog
        title: "上传本地图到公网图床"
        modal: true
        anchors.centerIn: parent
        width: 440
        standardButtons: Dialog.Ok | Dialog.Cancel
        Text {
            width: parent.width
            text: "这张图会被上传到公网图床（GitHub 或 S.E.E），拿到直链后再用于视频参考图。\n\n"
                  + "上传后内容就在公网上了，无法撤回。是否继续？"
            color: theme.textMain
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }
        onAccepted: backend.confirmUpload()
        onRejected: backend.cancelUpload()
    }

    Connections {
        target: backend
        /* 框的开合完全跟着桥的 `pendingApproval` 走，界面自己不记这份状态。
           为什么不只在「点上传」时弹一次：状态可能从别处被清掉（用户取消、
           或上传完成），只单向打开会留下一个关不掉的框。 */
        function onStateChanged() {
            var waiting = backend.pendingApproval === "image_host.upload";
            if (waiting && !uploadConfirmDialog.opened)
                uploadConfirmDialog.open();
            else if (!waiting && uploadConfirmDialog.opened)
                uploadConfirmDialog.close();
        }
        function onReferenceUploaded(localPath, url) {
            if (!url)
                return;
            var list = page.refs.slice();
            list.push(url);
            page.refs = list;
        }
        function onParamsLoaded(kind, params) {
            if (kind !== "video")
                return;
            prompt.text = params.prompt || "";
            var modelIndex = page.models.indexOf(String(params.model || ""));
            if (modelIndex >= 0)
                modelCombo.currentIndex = modelIndex;
            var secondsIndex = page.secondsOptions.indexOf(String(params.seconds || ""));
            if (secondsIndex >= 0)
                secondsCombo.currentIndex = secondsIndex;
            var aspectIndex = page.aspectRatios.indexOf(String(params.aspect_ratio || ""));
            if (aspectIndex >= 0)
                aspectCombo.currentIndex = aspectIndex;
            page.refs = params.images ? params.images.slice() : [];
        }
    }

    ScrollView {
        id: scroll
        anchors.fill: parent
        contentWidth: availableWidth
        ScrollBar.vertical.policy: ScrollBar.AsNeeded

        Item {
            width: scroll.availableWidth - 40
            implicitHeight: content.implicitHeight + 40

            ColumnLayout {
                id: content
                x: 20
                y: 20
                width: parent.width
                spacing: 16

            AppCard {
                title: "提示词"
                /* 实时字数（与图片页同一处理）：旧版一直有，迁移时漏了 */
                RowLayout {
                    width: parent.width
                    spacing: 8
                    Item { Layout.fillWidth: true }
                    Text {
                        text: prompt.text.length + " 字"
                        color: theme.textDisabled
                        font.pixelSize: 12
                    }
                }
                AppTextArea {
                    id: prompt
                    width: parent.width
                    placeholderText: "描述镜头与动作：主体 + 场景 + 运镜 + 光线"
                }
            }

            AppCard {
                title: "参数"
                RowLayout {
                    width: parent.width
                    spacing: 16
                    ColumnLayout {
                        spacing: 6
                        Text { text: "模型"; color: theme.textSub; font.pixelSize: 12 }
                        AppCombo {
                            id: modelCombo
                            Layout.preferredWidth: 200
                            model: page.models
                            currentIndex: 0
                        }
                    }
                    ColumnLayout {
                        spacing: 6
                        Text { text: "时长"; color: theme.textSub; font.pixelSize: 12 }
                        AppCombo {
                            id: secondsCombo
                            Layout.preferredWidth: 110
                            model: page.secondsOptions
                            currentIndex: 1
                        }
                    }
                    ColumnLayout {
                        spacing: 6
                        Text { text: "画幅"; color: theme.textSub; font.pixelSize: 12 }
                        AppCombo {
                            id: aspectCombo
                            Layout.preferredWidth: 130
                            model: page.aspectRatios
                            currentIndex: 0
                        }
                    }
                    Item { Layout.fillWidth: true }
                }
                Text {
                    width: parent.width
                    text: "平台限制：视频任务每分钟只能提交 1 个，超出的会被自动排队等待。"
                    color: theme.warning
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                }
            }

            AppCard {
                title: "参考图（可选，最多 5 张，仅支持公网 URL）"

                RowLayout {
                    width: parent.width
                    spacing: 8
                    AppField {
                        id: urlField
                        Layout.fillWidth: true
                        placeholderText: "https://... 图片直链"
                    }
                    AppButton {
                        text: "添加"
                        enabled: urlField.text.trim().length > 7 && page.refs.length < 5
                        onClicked: {
                            var list = page.refs.slice();
                            list.push(urlField.text.trim());
                            page.refs = list;
                            urlField.text = "";
                        }
                    }
                    AppButton {
                        text: "上传本地图"
                        onClicked: uploadPicker.open()
                    }
                }

                Flow {
                    width: parent.width
                    spacing: 8

                    Repeater {
                        model: page.refs
                        Rectangle {
                            height: 28
                            width: Math.min(chipText.implicitWidth + 44, 420)
                            radius: theme.radiusPill
                            color: theme.videoBg

                            Text {
                                id: chipText
                                anchors.left: parent.left
                                anchors.leftMargin: 12
                                anchors.verticalCenter: parent.verticalCenter
                                width: parent.width - 44
                                text: String(modelData)
                                color: theme.videoText
                                font.pixelSize: 12
                                elide: Text.ElideMiddle
                            }

                            Button {
                                anchors.right: parent.right
                                anchors.rightMargin: 4
                                anchors.verticalCenter: parent.verticalCenter
                                width: 24
                                height: 24
                                flat: true
                                text: "✕"
                                onClicked: {
                                    var list = page.refs.slice();
                                    list.splice(index, 1);
                                    page.refs = list;
                                }
                                contentItem: Text {
                                    text: parent.text
                                    color: theme.videoText
                                    font.pixelSize: 12
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                                background: Rectangle { color: "transparent" }
                            }
                        }
                    }
                }

                Text {
                    width: parent.width
                    text: "本地图片请先「上传本地图」到图床（GitHub 或 S.E.E），拿到直链后再提交。"
                    color: theme.textDisabled
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                }
            }

            RowLayout {
                spacing: 8
                AppButton {
                    primary: true
                    text: backend.busy ? "取消" : "生成视频"
                    enabled: backend.busy || prompt.text.trim() !== ""
                    onClicked: {
                        if (backend.busy)
                            backend.cancel();
                        else
                            backend.generateVideo(prompt.text, modelCombo.currentText,
                                                  secondsCombo.currentText, aspectCombo.currentText,
                                                  page.refs);
                    }
                }
                AppButton {
                    text: "清空结果"
                    enabled: !backend.busy && backend.hasResult
                    onClicked: backend.clearResult()
                }
                Item { Layout.fillWidth: true }
            }

                ResultPanel { kind: "video" }
            }
        }
    }
}
