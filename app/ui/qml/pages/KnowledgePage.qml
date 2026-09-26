import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../components"

/* 知识库：上传资料 → 自动切片入库 → 检索试跑。
   助手「找参考」时查的就是这里的东西（来源开关里的「知识库」）。 */
Item {
    id: page
    objectName: "knowledgePage"

    readonly property var bridge: knowledgeBridge

    /* 只读探针：给界面测试读（理由同其它页面：findChild 会被包装缓存坑到）。 */
    readonly property int docCount: bridge.documents.length
    readonly property int hitCount: bridge.results.length

    /* 删除确认：先问一句，再决定要不要连原文件一起删（方案 5.1 节）。 */
    property string pendingId: ""
    property string pendingName: ""

    function askDelete(id, name) {
        pendingId = id;
        pendingName = name;
        deleteFileBox.checked = true;
    }

    function confirmDelete() {
        if (pendingId !== "")
            bridge.remove(pendingId, deleteFileBox.checked);
        pendingId = "";
    }

    FileDialog {
        id: filePicker
        title: "选择要放进知识库的文件"
        fileMode: FileDialog.OpenFiles
        nameFilters: ["支持的文件 (*.txt *.md *.csv *.json *.pdf)", "所有文件 (*)"]
        onAccepted: {
            var paths = [];
            for (var i = 0; i < selectedFiles.length; i++)
                paths.push(bridge.localPath(selectedFiles[i].toString()));
            bridge.addFiles(paths);
        }
    }

    ScrollView {
        id: scroll
        objectName: "knowledgeScroll"
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

                // ---------------- 上传 ----------------
                AppCard {
                    title: "上传资料"
                    Text {
                        width: parent.width
                        text: "支持 txt / md / csv / json / pdf。上传后立刻切片入库，助手找参考时就能翻到；"
                              + "原文件留在数据目录的 knowledge 里，改了切片规则可以重建。"
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        width: parent.width
                        spacing: 12
                        AppButton {
                            text: "选择文件…"
                            primary: true
                            enabled: !bridge.busy
                            onClicked: filePicker.open()
                        }
                        Text {
                            Layout.fillWidth: true
                            text: bridge.statusText
                            color: theme.textSub
                            font.pixelSize: 12
                            elide: Text.ElideRight
                        }
                        Item { Layout.fillWidth: true }
                    }
                    Text {
                        width: parent.width
                        text: "扫描件（图片型 PDF）提不出文字，第一版不做 OCR；这种情况会直接告诉你，不会假装入库成功。"
                        color: theme.textDisabled
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }
                }

                // ---------------- 文件列表 ----------------
                AppCard {
                    title: "库里的资料（" + page.docCount + "）"
                    Text {
                        width: parent.width
                        visible: page.docCount === 0
                        text: "还没有资料。上传一份风格说明或产品资料，助手找参考时就会翻它。"
                        color: theme.textDisabled
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    Column {
                        width: parent.width
                        spacing: 8
                        Repeater {
                            model: bridge.documents
                            delegate: Rectangle {
                                width: parent.width
                                implicitHeight: rowBody.implicitHeight + 20
                                radius: theme.radiusControl
                                color: theme.fill
                                border.width: 1
                                border.color: modelData.failed ? theme.danger : theme.cardBorder
                                ColumnLayout {
                                    id: rowBody
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.top: parent.top
                                    anchors.margins: 10
                                    spacing: 4
                                    RowLayout {
                                        width: parent.width
                                        spacing: 12
                                        Text {
                                            Layout.fillWidth: true
                                            text: modelData.name
                                            color: theme.textMain
                                            font.pixelSize: 13
                                            elide: Text.ElideMiddle
                                        }
                                        Text {
                                            text: modelData.kindLabel
                                            color: theme.textSub
                                            font.pixelSize: 12
                                        }
                                        Text {
                                            text: modelData.sizeText
                                            color: theme.textSub
                                            font.pixelSize: 12
                                        }
                                        Text {
                                            text: modelData.chunkCount + " 片"
                                            color: theme.textSub
                                            font.pixelSize: 12
                                        }
                                        Text {
                                            text: modelData.stateLabel
                                            color: modelData.failed ? theme.dangerText : theme.textSub
                                            font.pixelSize: 12
                                        }
                                        Text {
                                            text: modelData.builtText
                                            color: theme.textDisabled
                                            font.pixelSize: 11
                                        }
                                        AppButton {
                                            text: "重建"
                                            enabled: !bridge.busy
                                            onClicked: bridge.build(modelData.id)
                                        }
                                        AppButton {
                                            text: "删除"
                                            destructive: true
                                            enabled: !bridge.busy
                                            onClicked: page.askDelete(modelData.id, modelData.name)
                                        }
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        visible: modelData.failed
                                        text: modelData.error
                                        color: theme.dangerText
                                        font.pixelSize: 11
                                        wrapMode: Text.Wrap
                                    }
                                }
                            }
                        }
                    }
                }

                // ---------------- 检索试跑 ----------------
                AppCard {
                    title: "检索试跑"
                    Text {
                        width: parent.width
                        text: "写一句和你要生成的东西差不多的话，看看能命中哪些片段——"
                              + "这里看到的，就是助手在「找参考」时会拿到的。"
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        width: parent.width
                        spacing: 12
                        AppField {
                            id: queryField
                            Layout.fillWidth: true
                            placeholderText: "例如：中秋国潮海报，红金配色"
                        }
                        AppButton {
                            text: "试检索"
                            primary: true
                            enabled: !bridge.busy
                            onClicked: bridge.testSearch(queryField.text)
                        }
                    }
                    Text {
                        width: parent.width
                        visible: bridge.resultReason !== ""
                        text: bridge.resultReason
                        color: theme.textSub
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    Column {
                        width: parent.width
                        spacing: 8
                        Repeater {
                            model: bridge.results
                            delegate: Rectangle {
                                width: parent.width
                                implicitHeight: hitBody.implicitHeight + 20
                                radius: theme.radiusControl
                                color: theme.fill
                                border.width: 1
                                border.color: theme.cardBorder
                                ColumnLayout {
                                    id: hitBody
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.top: parent.top
                                    anchors.margins: 10
                                    spacing: 4
                                    RowLayout {
                                        width: parent.width
                                        spacing: 10
                                        Text {
                                            Layout.fillWidth: true
                                            text: modelData.sourceLabel
                                            color: theme.textMain
                                            font.pixelSize: 12
                                            font.bold: true
                                            elide: Text.ElideMiddle
                                        }
                                        Text {
                                            text: "分 " + modelData.score
                                            color: theme.textDisabled
                                            font.pixelSize: 11
                                        }
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        text: modelData.snippet
                                        color: theme.textSub
                                        font.pixelSize: 12
                                        wrapMode: Text.Wrap
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    /* 删除确认浮层：连原文件一起删是可选项，默认勾上（用户上传的多半是副本）。 */
    Rectangle {
        id: confirmLayer
        visible: page.pendingId !== ""
        anchors.fill: parent
        color: "#88000000"
        z: 60

        MouseArea { anchors.fill: parent }

        Rectangle {
            width: Math.min(440, parent.width - 60)
            implicitHeight: box.implicitHeight + 40
            height: implicitHeight
            anchors.centerIn: parent
            radius: theme.radiusCard
            color: theme.card
            border.width: 1
            border.color: theme.cardBorder
            ColumnLayout {
                id: box
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: theme.cardPadding
                spacing: 10
                Text {
                    Layout.fillWidth: true
                    text: "删除《" + page.pendingName + "》？"
                    color: theme.textMain
                    font.pixelSize: 14
                    font.bold: true
                    wrapMode: Text.Wrap
                }
                Text {
                    Layout.fillWidth: true
                    text: "删掉之后就检索不到了。"
                    color: theme.textSub
                    font.pixelSize: 12
                    wrapMode: Text.Wrap
                }
                CheckBox {
                    id: deleteFileBox
                    text: "连原文件一起删（在知识库目录里的那份）"
                    font.pixelSize: 12
                    checked: true
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    AppButton {
                        text: "取消"
                        onClicked: page.pendingId = ""
                    }
                    AppButton {
                        text: "删除"
                        destructive: true
                        onClicked: page.confirmDelete()
                    }
                    Item { Layout.fillWidth: true }
                }
            }
        }
    }
}
