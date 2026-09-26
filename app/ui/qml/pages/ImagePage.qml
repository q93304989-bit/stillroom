import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../components"

/* 图片生成页：文生图 / 图生图（最多 5 张参考图，本地文件自动转 data URI）。 */
Item {
    id: page
    objectName: "imagePage"

    property var refs: []

    /* 给界面测试用：把「URL 输入框 + 添加」这条路径走一遍（与点按钮同一条代码）。
       直接改 refs 就测不到输入框是否真的接上了。 */
    function addReferenceUrl(text) {
        urlField.text = String(text || "");
        addUrlButton.clicked();
    }

    /* 只读探针：给界面测试读（理由同其它页面：findChild 会被包装缓存坑到）。
       这两条是「迁移遗漏审计」补回来的功能，必须能被测到。 */
    readonly property int promptCount: prompt.text.length
    readonly property int refCount: page.refs.length

    readonly property var sizes: [
        "1024x768", "512x512", "768x1024", "1024x1024",
        "1280x720", "720x1280", "1920x1080"
    ]
    readonly property var models: ["agnes-image-2.5-flash", "agnes-image-2.1-flash"]

    FileDialog {
        id: picker
        title: "选择参考图"
        fileMode: FileDialog.OpenFiles
        nameFilters: ["图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)", "所有文件 (*)"]
        onAccepted: {
            var list = page.refs.slice();
            for (var i = 0; i < selectedFiles.length; ++i)
                list.push(selectedFiles[i].toString());
            page.refs = list.slice(0, 5);
        }
    }

    /* 从历史记录「载入参数」：把当时的提示词、尺寸、模型与参考图填回来 */
    Connections {
        target: backend
        function onParamsLoaded(kind, params) {
            if (kind !== "image")
                return;
            prompt.text = params.prompt || "";
            if (params.size) {
                var sizeIndex = page.sizes.indexOf(String(params.size));
                if (sizeIndex >= 0)
                    sizeCombo.currentIndex = sizeIndex;
            }
            if (params.model) {
                var modelIndex = page.models.indexOf(String(params.model));
                if (modelIndex >= 0)
                    modelCombo.currentIndex = modelIndex;
            }
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
                /* 实时字数：旧版在提示词标题右侧一直显示「N 字」，迁移时漏了。
                   提示词长度直接影响出图质量与费用，输入时应当看得见。 */
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
                    placeholderText: "描述画面：主体 + 风格 + 光线 + 构图"
                }
            }

            AppCard {
                title: "参数"
                RowLayout {
                    width: parent.width
                    spacing: 16
                    ColumnLayout {
                        spacing: 6
                        Text { text: "尺寸"; color: theme.textSub; font.pixelSize: 12 }
                        AppCombo {
                            id: sizeCombo
                            Layout.preferredWidth: 150
                            model: page.sizes
                            currentIndex: 0
                        }
                    }
                    ColumnLayout {
                        spacing: 6
                        Text { text: "模型"; color: theme.textSub; font.pixelSize: 12 }
                        AppCombo {
                            id: modelCombo
                            Layout.preferredWidth: 220
                            model: page.models
                            currentIndex: 0
                        }
                    }
                    Item { Layout.fillWidth: true }
                }
            }

            AppCard {
                title: "参考图（可选，最多 5 张）"

                Text {
                    width: parent.width
                    text: "本地文件会自动转成 base64 一并提交；也可以直接填公网 URL。"
                    color: theme.textSub
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
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
                            color: theme.fill
                            border.width: 1
                            border.color: theme.cardBorder

                            Text {
                                id: chipText
                                anchors.left: parent.left
                                anchors.leftMargin: 12
                                anchors.verticalCenter: parent.verticalCenter
                                width: parent.width - 44
                                text: {
                                    var s = String(modelData);
                                    var i = s.lastIndexOf("/");
                                    return i >= 0 ? s.substring(i + 1) : s;
                                }
                                color: theme.textMain
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
                                    color: theme.textSub
                                    font.pixelSize: 12
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                }
                                background: Rectangle { color: "transparent" }
                            }
                        }
                    }
                }

                RowLayout {
                    spacing: 8
                    AppField {
                        id: urlField
                        Layout.fillWidth: true
                        placeholderText: "或直接填公网图片 URL（https://...）"
                    }
                    AppButton {
                        id: addUrlButton
                        text: "添加"
                        enabled: urlField.text.trim().length > 7 && page.refs.length < 5
                        onClicked: {
                            var list = page.refs.slice();
                            list.push(urlField.text.trim());
                            page.refs = list.slice(0, 5);
                            urlField.text = "";
                        }
                    }
                    AppButton {
                        text: "添加图片"
                        enabled: page.refs.length < 5
                        onClicked: picker.open()
                    }
                    AppButton {
                        text: "清空"
                        enabled: page.refs.length > 0
                        onClicked: page.refs = []
                    }
                    Text {
                        text: page.refs.length + " / 5"
                        color: theme.textDisabled
                        font.pixelSize: 12
                    }
                    Item { Layout.fillWidth: true }
                }
            }

            RowLayout {
                spacing: 8
                AppButton {
                    primary: true
                    text: backend.busy ? "取消" : "生成"
                    enabled: backend.busy || prompt.text.trim() !== ""
                    onClicked: {
                        if (backend.busy)
                            backend.cancel();
                        else
                            backend.generateImage(prompt.text, sizeCombo.currentText,
                                                  modelCombo.currentText, page.refs);
                    }
                }
                AppButton {
                    text: "清空结果"
                    enabled: !backend.busy && backend.hasResult
                    onClicked: backend.clearResult()
                }
                Item { Layout.fillWidth: true }
            }

                ResultPanel { kind: "image" }
            }
        }
    }
}
