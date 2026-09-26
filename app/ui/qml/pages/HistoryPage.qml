import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

/* 历史记录页：列表 / 画廊两种视图，共用同一个模型（筛选与搜索只做一次）。 */
Item {
    id: page
    objectName: "historyPage"

    /* 只读探针：灯箱里「另存为」是否可用（给界面测试读）。 */
    readonly property bool lightboxSaveAsAvailable: lightbox.saveAsAvailable
    /* 只读探针：灯箱详情弹层（给界面测试读）。 */
    readonly property bool lightboxDetailOpen: lightbox.detailOpen
    readonly property int lightboxDetailLength: lightbox.detailLength

    /* 给界面测试用：打开灯箱里的详情（与点「详情」按钮同一条代码）。 */
    function openLightboxDetail() {
        lightbox.openDetail();
    }

    readonly property var historyModel: backend.historyModel
    property string mode: "list"          // list / gallery
    property var currentRecord: ({})
    property int currentIndex: -1

    readonly property var filterLabels: ["全部", "图片", "视频"]
    readonly property var filterValues: ["all", "image", "video"]

    function reload() {
        historyModel.reload(filterValues[filterCombo.currentIndex], search.text, 500);
    }

    function openRecord(recordId) {
        var rows = [];
        for (var i = 0; i < historyModel.count; ++i)
            rows.push(historyModel.recordIdAt(i));
        currentIndex = rows.indexOf(recordId);
        currentRecord = historyModel.recordAt(recordId);
        if (currentRecord)
            currentRecord.paramsSummary = summarize(currentRecord.params);
        lightbox.index = currentIndex;
        lightbox.records = rows;
        lightbox.record = currentRecord;
        lightbox.open();
    }

    function summarize(params) {
        if (!params)
            return "";
        var parts = [];
        if (params.model) parts.push(String(params.model));
        if (params.size) parts.push(String(params.size));
        if (params.seconds) parts.push(String(params.seconds) + "s");
        if (params.aspect_ratio) parts.push(String(params.aspect_ratio));
        if (params.images && params.images.length) parts.push("参考图 " + params.images.length + " 张");
        return parts.join(" · ");
    }

    function step(delta) {
        if (lightbox.records.length === 0)
            return;
        var next = lightbox.index + delta;
        if (next < 0)
            next = lightbox.records.length - 1;
        if (next >= lightbox.records.length)
            next = 0;
        openRecord(lightbox.records[next]);
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 20
        spacing: 12

        // ---------------- 工具行 ----------------
        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            AppButton {
                text: "列表"
                primary: page.mode === "list"
                implicitWidth: 62
                onClicked: page.mode = "list"
            }
            AppButton {
                text: "画廊"
                primary: page.mode === "gallery"
                implicitWidth: 62
                onClicked: page.mode = "gallery"
            }
            AppCombo {
                id: filterCombo
                Layout.preferredWidth: 110
                model: page.filterLabels
                currentIndex: 0
                onActivated: page.reload()
            }
            AppField {
                id: search
                Layout.fillWidth: true
                placeholderText: "搜索提示词或参数…"
                onTextChanged: searchTimer.restart()
            }
            Text {
                text: historyModel.count + " 条"
                color: theme.textSub
                font.pixelSize: 12
            }
            AppButton {
                text: "刷新"
                onClicked: page.reload()
            }
            AppButton {
                text: "清空"
                destructive: true
                onClicked: clearDialog.open()
            }
        }

        // ---------------- 内容 ----------------
        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: page.mode === "list" ? 0 : 1

            ListView {
                id: listView
                clip: true
                spacing: 8
                model: historyModel
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                delegate: HistoryCard {
                    width: listView.width - 12
                    recordId: model.recordId
                    kind: model.kind
                    status: model.status
                    prompt: model.prompt
                    timeText: model.timeText
                    badge: model.badge
                    durationText: model.durationText
                    thumb: model.thumb
                    paramsSummary: model.paramsSummary
                    selected: page.currentRecord && page.currentRecord.id === model.recordId
                    favorite: model.favorite
                    tags: model.tags
                    onActivated: page.openRecord(model.recordId)
                    onLoadRequested: backend.loadParams(model.recordId)
                    onDeleteRequested: page.removeRecord(model.recordId)
                    onFavoriteToggled: historyModel.setFavorite(model.recordId, !model.favorite)
                }
            }

            GridView {
                id: gridView
                objectName: "galleryView"
                clip: true
                cellWidth: Math.max(190, Math.min(260, (width - 24) / Math.max(1, Math.floor(width / 230))))
                cellHeight: cellWidth * 0.75 + 78
                // 多留几屏缓存：滚动时避免反复销毁/重建委托（重建意味着重新上传纹理）
                cacheBuffer: cellHeight * 3
                model: historyModel
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                delegate: GalleryCell {
                    width: gridView.cellWidth - 10
                    height: gridView.cellHeight - 10
                    ratio: 4 / 3
                    recordId: model.recordId
                    kind: model.kind
                    status: model.status
                    prompt: model.prompt
                    timeText: model.timeText
                    badge: model.badge
                    thumb: model.thumb
                    selected: page.currentRecord && page.currentRecord.id === model.recordId
                    onActivated: page.openRecord(model.recordId)
                }
            }
        }

        // ---------------- 空态 ----------------
        Text {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: historyModel.count === 0
            text: historyModel.loading ? "正在加载…" : "还没有历史记录\n生成图片或视频后会自动记录在这里"
            color: theme.textDisabled
            font.pixelSize: 13
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            lineHeight: 1.6
        }
    }

    Timer {
        id: searchTimer
        interval: 300
        onTriggered: page.reload()
    }

    Dialog {
        id: clearDialog
        title: "清空历史"
        modal: true
        anchors.centerIn: parent
        width: 380
        standardButtons: Dialog.Ok | Dialog.Cancel
        Text {
            width: parent.width
            text: "将删除全部 " + historyModel.count + " 条记录及其本地缓存，且不可恢复。"
            color: theme.textMain
            font.pixelSize: 13
            wrapMode: Text.Wrap
        }
        onAccepted: {
            historyModel.clearAll();
            page.currentRecord = ({});
        }
    }

    Lightbox {
        id: lightbox
        onLoadRequested: function (recordId) { backend.loadParams(recordId); }
        onStepRequested: function (delta) { page.step(delta); }
        onDeleteRequested: function (recordId) {
            page.removeRecord(recordId);
            lightbox.close();
        }
        // 评价信号：改完立刻把这条记录重新读回来，弹层里的星级与标签跟着刷新
        onFavoriteToggled: function (value) {
            var id = lightbox.record.id;
            historyModel.setFavorite(id, value);
            lightbox.record = historyModel.recordAt(id);
        }
        onTagAdded: function (tag) {
            var id = lightbox.record.id;
            historyModel.addTag(id, tag);
            lightbox.record = historyModel.recordAt(id);
        }
        onTagsReplaced: function (tags) {
            var id = lightbox.record.id;
            historyModel.setTags(id, tags);
            lightbox.record = historyModel.recordAt(id);
        }
        onRetryRequested: function () {
            var id = lightbox.record.id;
            historyModel.markAction(id, "retry");     // 负反馈：用户要重做
            backend.loadParams(id);                   // 参数回填到生成页（会自动切页）
            lightbox.close();
        }
    }

    function removeRecord(recordId) {
        if (historyModel.remove(recordId)) {
            page.currentRecord = ({});
            lightbox.close();
        }
    }

    Component.onCompleted: reload()
}
